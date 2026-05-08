"""Lista TP/SL/MaxHold configurado em cada estrategia ativa."""
from collections import Counter
from src.research.active import list_active

recs = list_active()
print(f"=== TAKE PROFIT POR ESTRATEGIA ATIVA ({len(recs)}) ===\n")
print(f"{'ID':<18} {'Asset':<10} {'TF':<5} {'Engine':<8} {'TP%':>6} {'SL%':>6} {'MaxHold':>8} {'Score':>6} {'pnl%':>7}")
print("-" * 90)
for r in sorted(recs, key=lambda x: -x.get("score", 0)):
    rid = r.get("id", "")[-15:]
    a = r.get("asset", "?")
    tf = r.get("timeframe", "?")
    eng = r.get("engine", "?")
    risk = r.get("risk", {}) or {}
    tp = risk.get("take_profit_pct", "-")
    sl = risk.get("stop_loss_pct", "-")
    mh = risk.get("max_hold_candles", "-")
    sc = r.get("score", 0)
    pnl = (r.get("summary") or {}).get("pnl_pct", 0)
    print(f"{rid:<18} {a:<10} {tf:<5} {eng:<8} {str(tp):>6} {str(sl):>6} {str(mh):>8} {sc:>6.2f} {pnl:>6.1f}%")

print("\n=== Distribuicao ===")
tp_dist = Counter(r.get("risk", {}).get("take_profit_pct") for r in recs)
sl_dist = Counter(r.get("risk", {}).get("stop_loss_pct") for r in recs)
mh_dist = Counter(r.get("risk", {}).get("max_hold_candles") for r in recs)
print(f"TP%      : {dict(tp_dist)}")
print(f"SL%      : {dict(sl_dist)}")
print(f"MaxHold  : {dict(mh_dist)}")
