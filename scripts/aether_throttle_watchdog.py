"""Watchdog da esteira industrial Aether.

Monitora CPU/RAM/GPU/lock-do-bot e ajusta dinamicamente knobs de
throughput pra nao exceder consumo seguro. Escreve em settings do DB
(chave `hyper_controls`) que o orchestrator ja le a cada ciclo.

Regras (default):
  - CPU>85% por 60s sustentado            -> tasks_per_worker -1 (min 1)
  - CPU<40% por 120s e ram_free>2GB       -> tasks_per_worker +1 (max TASKS_MAX)
  - RAM_free<1.2GB                        -> tasks_per_worker=1, llm_pause=true
  - RAM_free<800MB                        -> mata 1 instancia duplicada de critic/validator
  - GPU_mem_pct>90%                       -> critic_bt_workers=2
  - bot.lock ausente                      -> tasks_per_worker=1 (bot caiu)

Subir com:
  .venv\\Scripts\\python.exe -u scripts\\aether_throttle_watchdog.py

Variaveis de ambiente:
  AETHER_WD_POLL_SECONDS=10
  AETHER_WD_TASKS_MAX=6
  AETHER_WD_CPU_HIGH=85
  AETHER_WD_CPU_LOW=40
  AETHER_WD_RAM_TIGHT_GB=1.2
  AETHER_WD_RAM_CRIT_GB=0.8
  AETHER_WD_DRY_RUN=0   # 1 = nao escreve no DB, so loga
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.aether.config import db_path  # noqa: E402
from src.research.aether.db import get_setting, set_setting  # noqa: E402

LOG_PATH = ROOT / "src" / "logs" / "aether" / "throttle_watchdog.log"
BOT_LOCK = ROOT / "src" / "logs" / ".bot.lock"

POLL = int(os.getenv("AETHER_WD_POLL_SECONDS", "10"))
TASKS_MAX = int(os.getenv("AETHER_WD_TASKS_MAX", "6"))
TASKS_MIN = 1
CPU_HIGH = float(os.getenv("AETHER_WD_CPU_HIGH", "85"))
CPU_LOW = float(os.getenv("AETHER_WD_CPU_LOW", "40"))
RAM_TIGHT_GB = float(os.getenv("AETHER_WD_RAM_TIGHT_GB", "1.2"))
RAM_CRIT_GB = float(os.getenv("AETHER_WD_RAM_CRIT_GB", "0.8"))
DRY_RUN = os.getenv("AETHER_WD_DRY_RUN", "0").strip() in {"1", "true", "yes"}

CPU_HIGH_PERSIST_S = 60
CPU_LOW_PERSIST_S = 120


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _setup_logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("aether.throttle_watchdog")
    log.setLevel(logging.INFO)
    if log.handlers:
        return log
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _gpu_mem_pct() -> float | None:
    try:
        import subprocess
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=3,
        ).decode().strip().splitlines()
        if not out:
            return None
        used, total = out[0].split(",")
        return 100.0 * float(used.strip()) / max(1.0, float(total.strip()))
    except Exception:
        return None


def _read_state(log: logging.Logger) -> dict:
    cpu = psutil.cpu_percent(interval=1.0)
    vm = psutil.virtual_memory()
    ram_free_gb = vm.available / (1024 ** 3)
    gpu_pct = _gpu_mem_pct()
    bot_alive = BOT_LOCK.exists()
    return {
        "cpu_pct": cpu,
        "ram_free_gb": round(ram_free_gb, 2),
        "ram_used_pct": vm.percent,
        "gpu_mem_pct": round(gpu_pct, 1) if gpu_pct is not None else None,
        "bot_alive": bot_alive,
    }


def _current_controls(conn: sqlite3.Connection) -> dict:
    ctrl = get_setting(conn, "hyper_controls", default=None)
    if not isinstance(ctrl, dict):
        ctrl = {}
    return ctrl


def _write_controls(conn: sqlite3.Connection, ctrl: dict, log: logging.Logger) -> None:
    if DRY_RUN:
        log.info(f"[DRY] would write hyper_controls={ctrl}")
        return
    set_setting(conn, "hyper_controls", ctrl)


def _decide(state: dict, ctrl: dict, hist: dict, log: logging.Logger) -> tuple[dict, list[str]]:
    """Retorna (novos_controls, razoes). Mantem ctrl original se nada mudar."""
    actions: list[str] = []
    new = dict(ctrl)

    cur_tpw = int(new.get("tasks_per_worker") or int(os.getenv("AETHER_TASKS_PER_WORKER", "2")))
    llm_pause = bool(new.get("llm_pause") or False)

    cpu_high = state["cpu_pct"] >= CPU_HIGH
    cpu_low = state["cpu_pct"] <= CPU_LOW
    ram_tight = state["ram_free_gb"] < RAM_TIGHT_GB
    ram_crit = state["ram_free_gb"] < RAM_CRIT_GB
    gpu_high = state["gpu_mem_pct"] is not None and state["gpu_mem_pct"] >= 90
    bot_dead = not state["bot_alive"]

    # Persistencia temporal
    now = time.time()
    hist["cpu_high_since"] = now if cpu_high else (hist.get("cpu_high_since") if cpu_high else None)
    hist["cpu_low_since"] = now if cpu_low else (hist.get("cpu_low_since") if cpu_low else None)
    if not cpu_high:
        hist["cpu_high_since"] = None
    if not cpu_low:
        hist["cpu_low_since"] = None

    cpu_high_persist = bool(hist.get("cpu_high_since")) and (now - hist["cpu_high_since"]) >= CPU_HIGH_PERSIST_S
    cpu_low_persist = bool(hist.get("cpu_low_since")) and (now - hist["cpu_low_since"]) >= CPU_LOW_PERSIST_S

    # Regras
    if bot_dead:
        if cur_tpw > 1:
            new["tasks_per_worker"] = 1
            actions.append(f"bot_lock ausente -> tpw=1 (era {cur_tpw})")
    elif ram_crit:
        if cur_tpw > 1 or not llm_pause:
            new["tasks_per_worker"] = 1
            new["llm_pause"] = True
            actions.append(f"RAM critica {state['ram_free_gb']}GB -> tpw=1 + llm_pause")
    elif ram_tight:
        if cur_tpw > 2:
            new["tasks_per_worker"] = 2
            actions.append(f"RAM apertada {state['ram_free_gb']}GB -> tpw=2 (era {cur_tpw})")
        if not llm_pause:
            new["llm_pause"] = True
            actions.append("RAM apertada -> llm_pause=true")
    elif cpu_high_persist:
        if cur_tpw > 1:
            new["tasks_per_worker"] = max(1, cur_tpw - 1)
            actions.append(f"CPU>{CPU_HIGH}% por {CPU_HIGH_PERSIST_S}s -> tpw={new['tasks_per_worker']}")
    elif cpu_low_persist and state["ram_free_gb"] > 2.0:
        if cur_tpw < TASKS_MAX:
            new["tasks_per_worker"] = min(TASKS_MAX, cur_tpw + 1)
            actions.append(
                f"CPU<{CPU_LOW}% por {CPU_LOW_PERSIST_S}s e RAM_free={state['ram_free_gb']}GB -> tpw={new['tasks_per_worker']}"
            )
        if llm_pause:
            new["llm_pause"] = False
            actions.append("recursos OK -> llm_pause=false")

    if gpu_high and (new.get("critic_bt_workers") or 6) > 2:
        new["critic_bt_workers"] = 2
        actions.append(f"GPU mem {state['gpu_mem_pct']}% -> critic_bt=2")

    return new, actions


def _kill_duplicate_aether(log: logging.Logger) -> None:
    """Em RAM critica: mata 1 instancia duplicada de critic/validator se houver duas."""
    targets = ("src.research.aether.critic_agent", "src.research.aether.auto_validator")
    found: dict[str, list[psutil.Process]] = {t: [] for t in targets}
    for p in psutil.process_iter(["pid", "cmdline", "create_time"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for t in targets:
            if t in cmd:
                found[t].append(p)
                break
    for t, procs in found.items():
        if len(procs) > 1:
            procs.sort(key=lambda x: x.info["create_time"], reverse=True)
            victim = procs[0]
            try:
                victim.terminate()
                log.warning(f"RAM critica: matei duplicata {t} pid={victim.pid}")
            except psutil.NoSuchProcess:
                pass


def main() -> int:
    log = _setup_logger()
    log.info(
        f"watchdog iniciado | poll={POLL}s | TPW max={TASKS_MAX} | "
        f"CPU_HIGH={CPU_HIGH}% CPU_LOW={CPU_LOW}% | "
        f"RAM_TIGHT={RAM_TIGHT_GB}GB RAM_CRIT={RAM_CRIT_GB}GB | DRY_RUN={DRY_RUN}"
    )
    psutil.cpu_percent(None)  # warmup
    hist: dict = {}
    last_state: dict = {}
    while True:
        try:
            state = _read_state(log)
            with _connect() as conn:
                ctrl = _current_controls(conn)
                new_ctrl, actions = _decide(state, ctrl, hist, log)
                if actions:
                    _write_controls(conn, new_ctrl, log)
                    for a in actions:
                        log.info(f"action: {a} | state={state}")
                if state["ram_free_gb"] < RAM_CRIT_GB and last_state.get("ram_free_gb", 99) >= RAM_CRIT_GB:
                    _kill_duplicate_aether(log)
            if not actions:
                # heartbeat a cada ~5min
                if int(time.time()) % 300 < POLL:
                    log.info(f"hb state={state} ctrl={ctrl}")
            last_state = state
        except Exception as e:
            log.exception(f"erro no ciclo: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
