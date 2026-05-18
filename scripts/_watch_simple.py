"""Watchdog simplificado pos-recovery: so emite eventos REAIS.

Diff vs _watchdog_stack.py:
- NAO checa deploy.log staleness (deploy esta intencionalmente off por memory pressure)
- NAO checa trading_bot.log staleness (esta dentro de src/logs)
- So PIDs mortos (CRIT) e DB stale >15min (WARN)
- STATE snapshot a cada 15min com producao real
"""
from __future__ import annotations
import os, sqlite3, sys, time, ctypes
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
DB = Path(os.path.expandvars(r"%LOCALAPPDATA%\Aether\runtime\aether.sqlite3"))

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


def read_pids(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
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


def db_age_seconds() -> float | None:
    try:
        con = sqlite3.connect(str(DB), timeout=2.0)
        ts = con.execute("SELECT MAX(ts) FROM events").fetchone()[0]
        con.close()
        if not ts:
            return None
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


def production_last_hour() -> dict[str, int]:
    try:
        con = sqlite3.connect(str(DB), timeout=2.0)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: dict[str, int] = {}
        for evt, key in [
            ("llm_research_completed", "llm"),
            ("validated", "val"),
            ("critic_approved", "app"),
            ("critic_rejected", "rej"),
        ]:
            n = con.execute("SELECT COUNT(*) FROM events WHERE type=? AND ts >= ?", (evt, cutoff)).fetchone()[0]
            out[key] = int(n or 0)
        con.close()
        return out
    except Exception:
        return {}


def emit(level: str, src: str, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {level} {src}: {msg}", flush=True)


def main() -> int:
    cycle = 0
    pids_file = LOGS / "aether.pids"
    happy_file = LOGS / "happynation.pids"
    while True:
        cycle += 1
        ap = read_pids(pids_file)
        hp = read_pids(happy_file)
        for n, p in ap.items():
            if not is_pid_alive(p):
                emit("CRIT", f"aether/{n}", f"PID {p} morto")
        for n, p in hp.items():
            if not is_pid_alive(p):
                emit("CRIT", f"happy/{n}", f"PID {p} morto")
        db_age = db_age_seconds()
        # 30min: probes com swing engines (lookback 200+) levam ate 20min por task.
        if db_age is not None and db_age > 1800:
            emit("WARN", "aether/db", f"sem eventos ha {db_age:.0f}s")
        # STATE a cada 15min
        if cycle % 15 == 1:
            prod = production_last_hour()
            ap_alive = sum(1 for p in ap.values() if is_pid_alive(p))
            hp_alive = sum(1 for p in hp.values() if is_pid_alive(p))
            emit("STATE", "watchdog",
                 f"c={cycle} aether={ap_alive}/{len(ap)} happy={hp_alive}/{len(hp)} "
                 f"llm/h={prod.get('llm',0)} val/h={prod.get('val',0)} "
                 f"app/h={prod.get('app',0)} rej/h={prod.get('rej',0)} "
                 f"db_evt={db_age if db_age else -1:.0f}s")
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
