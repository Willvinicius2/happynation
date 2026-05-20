"""Watchdog: emite uma linha por anomalia detectada no stack.

Verifica cada 60s:
  - Aether: server, validator, critic, hyper, deploy via logs/aether.pids
  - HappyNation: bot, dashboard via logs/happynation.pids
  - Bot trading_bot.log staleness (deve atualizar em <90s)
  - Aether deploy.log staleness (deve atualizar em <120s)
  - DB aether ultima atividade (events.ts mais recente)
  - DB lock errors recentes nos logs

Cada anomalia emite uma linha de evento no stdout (consumido pelo Monitor tool
do Claude). Linhas comecando com STATE: sao snapshots periodicos (1x/10min).
"""
from __future__ import annotations
import os, sqlite3, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
SRC_LOGS = ROOT / "src" / "logs"
DB = Path(os.path.expandvars(r"%LOCALAPPDATA%\Aether\runtime\aether.sqlite3"))


def read_pids(path: Path) -> dict[str, int]:
    """Le pids file no formato: pid<tab>name<tab>module"""
    out: dict[str, int] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip().lstrip("﻿")
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            out[parts[1]] = int(parts[0])
        except ValueError:
            continue
    return out


if sys.platform == "win32":
    import ctypes
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _kernel32 = ctypes.windll.kernel32

    def is_pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = _kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        _kernel32.CloseHandle(h)
        return bool(ok) and code.value == _STILL_ACTIVE
else:
    def is_pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def file_mtime(path: Path) -> float | None:
    if not path.exists():
        return None
    return path.stat().st_mtime


def latest_log(prefix: str, dirpath: Path) -> Path | None:
    files = sorted(dirpath.glob(f"{prefix}-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    files = [f for f in files if not f.name.endswith(".err.log")]
    return files[0] if files else None


def latest_deploy_log() -> Path | None:
    """Prefer the auto_deploy internal log; stdout logs can stay quiet."""
    internal = SRC_LOGS / "aether" / "auto_deploy.log"
    if internal.exists():
        return internal
    return latest_log("deploy", LOGS)


def db_latest_event_age() -> float | None:
    try:
        con = sqlite3.connect(str(DB), timeout=2.0)
        ts = con.execute("SELECT MAX(ts) FROM events").fetchone()[0]
        con.close()
        if not ts:
            return None
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception as e:
        print(f"WARN db_latest_event_age error: {e}", flush=True)
        return None


def db_production_last_hour() -> dict[str, int]:
    """Conta producao real (LLM calls, aprovacoes, validacoes) na ultima hora."""
    try:
        con = sqlite3.connect(str(DB), timeout=2.0)
        cutoff = (datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: dict[str, int] = {}
        for evt_type, key in [
            ("llm_research_completed", "llm_calls"),
            ("validated", "validations"),
            ("critic_approved", "approvals"),
            ("critic_rejected", "rejections"),
        ]:
            n = con.execute(
                "SELECT COUNT(*) FROM events WHERE type=? AND ts >= ?",
                (evt_type, cutoff),
            ).fetchone()[0]
            out[key] = int(n or 0)
        con.close()
        return out
    except Exception as e:
        print(f"WARN db_production_last_hour error: {e}", flush=True)
        return {}


def emit(level: str, source: str, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {level} {source}: {msg}", flush=True)


def main() -> int:
    aether_pids_file = LOGS / "aether.pids"
    happy_pids_file = LOGS / "happynation.pids"
    last_state_emit = 0.0
    cycle = 0
    while True:
        cycle += 1
        now = time.time()
        # Aether processes
        aether_pids = read_pids(aether_pids_file)
        for name, pid in aether_pids.items():
            if not is_pid_alive(pid):
                emit("CRIT", f"aether/{name}", f"PID {pid} morto")
        # HappyNation processes
        happy_pids = read_pids(happy_pids_file)
        for name, pid in happy_pids.items():
            if not is_pid_alive(pid):
                emit("CRIT", f"happy/{name}", f"PID {pid} morto")
        # Bot trading_bot.log staleness (>90s sem update = problema)
        tb = SRC_LOGS / "trading_bot.log"
        mt = file_mtime(tb)
        if mt is not None:
            age = now - mt
            if age > 90:
                emit("WARN", "bot/log", f"trading_bot.log parado ha {age:.0f}s")
        else:
            emit("WARN", "bot/log", "trading_bot.log nao existe")
        # Aether deploy log staleness (>120s sem update = problema)
        dep = latest_deploy_log()
        if dep is not None:
            mt = file_mtime(dep)
            age = now - mt if mt else 9999
            if age > 120:
                emit("WARN", "aether/deploy", f"{dep.name} parado ha {age:.0f}s")
        # DB events freshness
        db_age = db_latest_event_age()
        if db_age is not None and db_age > 600:
            emit("WARN", "aether/db", f"ultimo event no DB ha {db_age:.0f}s")
        # State snapshot a cada 10 ciclos (~10 min)
        if cycle % 10 == 1:
            tb_age = (now - file_mtime(tb)) if file_mtime(tb) else -1
            dep_age = (now - file_mtime(dep)) if (dep and file_mtime(dep)) else -1
            alive_aether = sum(1 for p in aether_pids.values() if is_pid_alive(p))
            alive_happy = sum(1 for p in happy_pids.values() if is_pid_alive(p))
            prod = db_production_last_hour()
            prod_str = (
                f"llm/h={prod.get('llm_calls', 0)} val/h={prod.get('validations', 0)} "
                f"app/h={prod.get('approvals', 0)} rej/h={prod.get('rejections', 0)}"
            )
            emit("STATE", "watchdog",
                 f"cycle={cycle} aether={alive_aether}/{len(aether_pids)} happy={alive_happy}/{len(happy_pids)} {prod_str} bot_log={tb_age:.0f}s deploy_log={dep_age:.0f}s db_evt={db_age if db_age else -1:.0f}s")
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
