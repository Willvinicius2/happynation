"""Roda critic_agent em modo SINGLE-PASS (uma chamada de run_once),
processando todo o backlog de status='validated' sem critic_status.

Diferente de iniciar `python -m src.research.aether.critic_agent` que
roda em loop infinito.

Saida:
  src/logs/agents/critic_run_once_summary.json

Uso:
  python -u scripts/run_critic_once.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# carrega thresholds atuais do critic_agent (usam env vars)
from src.research.aether.critic_agent import (  # noqa: E402
    _connect,
    _setup_logger,
    run_once,
    CROSS_SYMBOL_MIN_PASS,
    CROSS_SYMBOL_MIN_PNL_PCT,
    CROSS_SYMBOL_POOL,
    WF_FOLDS,
    WF_MIN_PASS,
    WF_MIN_PNL,
    GRID_MIN_P25_PNL,
    GRID_MIN_POSITIVE_FRAC,
    MTF_DISABLED,
    MTF_MIN_PASS,
    BOOTSTRAP_MIN_SHARPE_P5,
)


def main() -> int:
    log = _setup_logger()
    print("=== thresholds carregados ===", flush=True)
    print(f"  cross: pool={CROSS_SYMBOL_POOL} min_pass={CROSS_SYMBOL_MIN_PASS} min_pnl={CROSS_SYMBOL_MIN_PNL_PCT}%", flush=True)
    print(f"  wf: folds={WF_FOLDS} min_pass={WF_MIN_PASS} min_pnl={WF_MIN_PNL}%", flush=True)
    print(f"  grid: min_pos_frac={GRID_MIN_POSITIVE_FRAC} min_p25_pnl={GRID_MIN_P25_PNL}%", flush=True)
    print(f"  mtf: disabled={MTF_DISABLED} min_pass={MTF_MIN_PASS}", flush=True)
    print(f"  bootstrap: min_sharpe_p5={BOOTSTRAP_MIN_SHARPE_P5}", flush=True)
    print("", flush=True)

    t0 = time.time()
    with _connect() as conn:
        # conta backlog
        backlog = conn.execute(
            """
            SELECT COUNT(*) FROM nodes
            WHERE status='validated' AND id != 'root' AND id NOT LIKE 'tf:%'
              AND (metrics_json IS NULL OR metrics_json NOT LIKE '%critic_status%')
            """
        ).fetchone()[0]
        print(f"[run-critic] backlog para criticar: {backlog}", flush=True)
        approved, rejected = run_once(conn, log)

    elapsed = time.time() - t0
    summary = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "backlog_at_start": backlog,
        "approved": approved,
        "rejected": rejected,
        "elapsed_seconds": round(elapsed, 1),
        "thresholds": {
            "cross_pool": CROSS_SYMBOL_POOL,
            "cross_min_pass": CROSS_SYMBOL_MIN_PASS,
            "cross_min_pnl": CROSS_SYMBOL_MIN_PNL_PCT,
            "wf_folds": WF_FOLDS,
            "wf_min_pass": WF_MIN_PASS,
            "wf_min_pnl": WF_MIN_PNL,
            "grid_min_pos_frac": GRID_MIN_POSITIVE_FRAC,
            "grid_min_p25_pnl": GRID_MIN_P25_PNL,
            "mtf_disabled": MTF_DISABLED,
            "mtf_min_pass": MTF_MIN_PASS,
            "bootstrap_min_sharpe_p5": BOOTSTRAP_MIN_SHARPE_P5,
        },
    }
    out = ROOT / "src/logs/agents/critic_run_once_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60, flush=True)
    print(f"CRITIC SINGLE-PASS COMPLETO em {elapsed:.1f}s", flush=True)
    print(f"  approved: {approved}", flush=True)
    print(f"  rejected: {rejected}", flush=True)
    print(f"  total processado: {approved + rejected}", flush=True)
    print(f"  taxa de aprovacao: {100*approved/(approved+rejected) if (approved+rejected) else 0:.1f}%", flush=True)
    print(f"\nrelatorio: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
