"""Reset paper trading: zera trades.csv e PnL no state.json.

MANTEM:
  - paused_strategies (operacional)
  - manifest do Aether
  - DB do Aether

ZERA:
  - trades.csv (mantem so header)
  - _meta.realized_pnl_usd, market_value, allocated_usd, equity, cash_balance
  - _meta.strategy_stats, last_votes, strategy_activity, timeframe_mismatches
  - asset states (BTCUSDT, ETHUSDT, etc)

Backup automatico em src/logs/reset_backups/paper-YYYYMMDD-HHMMSS/

Uso:
  python -u scripts/reset_paper.py
  python -u scripts/reset_paper.py --keep-paused-clean   # tambem limpa orfas Aether
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
PAPER_DIR = ROOT / "src" / "logs" / "paper_balance"
STATE = PAPER_DIR / "state.json"
TRADES = PAPER_DIR / "trades.csv"
SUMMARY = PAPER_DIR / "summary.txt"
BACKUP_ROOT = ROOT / "src" / "logs" / "reset_backups"
MANIFEST = ROOT / "src" / "logs" / "research" / "active_strategies.json"


def _aether_name(record: dict) -> str:
    sid = str(record.get("id") or "")
    suffix = sid[-8:]
    engine = record.get("engine") or ""
    probe_specs = record.get("probe_specs")
    if engine == "combo" or (isinstance(probe_specs, list) and len(probe_specs) > 1):
        return f"Aether:combo#{suffix}"
    if engine:
        return f"Aether:{engine}#{suffix}"
    return f"unknown#{sid[:8]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-paused-clean", action="store_true",
                    help="remove tambem orfas Aether (mantidas so legacy)")
    args = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = BACKUP_ROOT / f"paper-{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    print(f"[reset] backup em {backup_dir}", flush=True)

    # Backup
    for src in (STATE, TRADES, SUMMARY):
        if src.exists():
            shutil.copy2(src, backup_dir / src.name)
            print(f"  backup: {src.name}", flush=True)

    # Carrega state atual
    state = json.loads(STATE.read_text(encoding="utf-8"))
    bankroll = float(state.get("_meta", {}).get("total_bankroll", 1000.0))
    paused_old = state.get("_meta", {}).get("paused_strategies", {})

    if args.keep_paused_clean:
        # Cruza com manifest: mantem so paused cujos names estao em entries do manifest
        if MANIFEST.exists():
            m = json.loads(MANIFEST.read_text(encoding="utf-8"))
            valid_names = {_aether_name(r) for r in m.get("strategies", [])}
            cleaned = {k: v for k, v in paused_old.items()
                       if not k.startswith("Aether:") or k in valid_names}
            print(f"  paused_strategies: {len(paused_old)} -> {len(cleaned)} (limpou orfas)", flush=True)
            paused_old = cleaned

    # Build state limpo
    new_state = {
        "_meta": {
            "total_bankroll": bankroll,
            "cash_balance": bankroll,
            "allocated_usd": 0.0,
            "market_value": 0.0,
            "equity": bankroll,
            "realized_pnl_usd": 0.0,
            "strategy_stats": {},
            "last_votes": {},
            "paused_strategies": paused_old,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "strategy_activity": {},
            "timeframe_mismatches": {},
        }
    }

    STATE.write_text(json.dumps(new_state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[reset] state.json zerado (bankroll mantido: ${bankroll:.2f})", flush=True)

    # trades.csv: mantem so header
    header = "timestamp,asset,strategy,side,price,qty,pnl_usd,pnl_pct,balance_after\n"
    TRADES.write_text(header, encoding="utf-8")
    print(f"[reset] trades.csv zerado (header preservado)", flush=True)

    # summary.txt
    if SUMMARY.exists():
        SUMMARY.unlink()
        print(f"[reset] summary.txt removido", flush=True)

    # state.json.bak / .lock removidos pra forcar primeiro write limpo
    for extra in ("state.json.bak", "state.json.lock"):
        p = PAPER_DIR / extra
        if p.exists():
            p.unlink()

    print(f"\n[reset] OK. Backup em {backup_dir}", flush=True)
    print(f"[reset] paused_strategies mantidas: {len(paused_old)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
