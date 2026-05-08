"""Resumo de PnL realizado + unrealized por estrategia (hoje)."""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "src" / "logs" / "paper_balance" / "state.json"
TRADES = ROOT / "src" / "logs" / "paper_balance" / "trades.csv"

with open(STATE, encoding="utf-8") as f:
    state = json.load(f)

meta = state["_meta"]
print("=" * 70)
print(f"BANCA | Equity: ${meta['equity']:.2f} | Cash: ${meta['cash_balance']:.2f}")
print(f"Alocado: ${meta['allocated_usd']:.2f} | PnL realizado total: ${meta['realized_pnl_usd']:+.2f}")
print("=" * 70)

stats = meta.get("strategy_stats", {}) or {}
print("\n=== TAKE REALIZADO POR ESTRATEGIA ===")
if stats:
    rows = sorted(stats.items(), key=lambda x: float(x[1].get("pnl_usd") or 0), reverse=True)
    print(f"{'Estrategia':<55} {'Trades':>7} {'PnL USD':>10}")
    for name, info in rows:
        trades = int(info.get("trades") or 0)
        pnl = float(info.get("pnl_usd") or 0)
        print(f"{name[:55]:<55} {trades:>7} ${pnl:>+9.2f}")
else:
    print("  (vazio - nenhum trade fechado ainda hoje)")

print("\n=== POSICOES ABERTAS (PnL nao realizado) ===")
opens = [(asset, d) for asset, d in state.items() if asset != "_meta" and isinstance(d, dict) and d.get("position")]
if opens:
    print(f"{'Asset':<10} {'Strategy':<45} {'Entry':>10} {'uPnL USD':>10} {'uPnL %':>8}")
    total_upnl = 0.0
    for asset, d in opens:
        pos = d["position"]
        upnl = d.get("unrealized_pnl_usd", 0)
        upct = d.get("unrealized_pnl_pct", 0)
        strat = pos.get("strategy", "?")
        entry = pos.get("entry_price", 0)
        total_upnl += upnl
        print(f"{asset:<10} {strat[:45]:<45} {entry:>10.4f} ${upnl:>+9.2f} {upct:>+7.2f}%")
    print(f"\nTotal unrealized: ${total_upnl:+.2f}")
else:
    print("  (nenhuma posicao aberta)")

print("\n=== TRADES DE HOJE (2026-05-08) ===")
trades_today = []
try:
    with open(TRADES, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = row.get("timestamp", "")
            if ts.startswith("2026-05-08"):
                trades_today.append(row)
except FileNotFoundError:
    pass
if trades_today:
    print(f"{'Time':<10} {'Asset':<8} {'Side':<5} {'Strategy':<40} {'Price':>12} {'PnL USD':>10}")
    for t in trades_today:
        time_s = t["timestamp"].split("T")[1][:8]
        pnl = t.get("pnl_usd") or ""
        pnl_str = f"${float(pnl):+.2f}" if pnl else ""
        print(f"{time_s:<10} {t['asset']:<8} {t['side']:<5} {t['strategy'][:40]:<40} {float(t['price']):>12.4f} {pnl_str:>10}")
else:
    print("  (sem trades em 2026-05-08)")
