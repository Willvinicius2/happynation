"""Loop infinito que executa agent_runner a cada AGENTS_INTERVAL_SEC.

Mais robusto que PowerShell wrappers:
  - Falha em uma execucao nao mata o loop (try/except)
  - Logging estruturado em src/logs/agents/runner_loop.log
  - Honra SIGINT pra parada limpa
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "src" / "logs" / "agents" / "runner_loop.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def _setup_log() -> logging.Logger:
    log = logging.getLogger("agent_runner_loop")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s",
                             datefmt="%Y-%m-%dT%H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


_running = True


def _shutdown(*_args):
    global _running
    _running = False


def main() -> int:
    log = _setup_log()
    interval = max(30, int(os.environ.get("AETHER_AGENTS_INTERVAL_SEC", "300")))
    log.info(f"agent_runner_loop START interval={interval}s root={ROOT}")
    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (AttributeError, ValueError):
        pass  # Windows nao tem SIGTERM padrao

    # Lazy import: o loop continua mesmo se a importacao falhar (ex: db indisponivel
    # momentaneamente). Tenta de novo no proximo tick.
    while _running:
        tick_started = datetime.now(timezone.utc)
        try:
            from src.agents.runner import run_default_agents
            report = run_default_agents()
            d = report.to_dict()
            counts = d.get("counts", {})
            log.info(
                f"tick OK info={counts.get('info',0)} warning={counts.get('warning',0)} "
                f"critical={counts.get('critical',0)} findings={len(d.get('findings',[]))}"
            )
        except Exception as exc:
            log.error(f"tick FAILED: {exc.__class__.__name__}: {exc}")
            log.debug(traceback.format_exc())
        # Sleep com check de shutdown a cada segundo
        elapsed = (datetime.now(timezone.utc) - tick_started).total_seconds()
        remaining = max(1.0, interval - elapsed)
        slept = 0.0
        while _running and slept < remaining:
            time.sleep(min(1.0, remaining - slept))
            slept += 1.0
    log.info("agent_runner_loop STOP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
