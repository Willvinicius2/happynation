"""Detalhe das 14 estrategias aprovadas pelo critic v4: trades, periodo, validacao."""
import sqlite3, sys, json
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from research.aether.config import db_path

# duracao por candle, em horas
TF_HOURS = {"5m": 5/60, "10m": 10/60, "15m": 15/60, "30m": 30/60, "1h": 1, "4h": 4, "1d": 24}

conn = sqlite3.connect(str(db_path()), timeout=10.0)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT id, name, score, metrics_json,
           json_extract(metrics_json,'$.critic_run_at') AS ran
    FROM nodes
    WHERE json_extract(metrics_json,'$.critic_status')='approved'
      AND json_extract(metrics_json,'$.critic_report.gpu_batch')=1
    ORDER BY score DESC
""").fetchall()

print(f"{'#':>2} {'engine':>14} {'par':>8} {'tf':>4} {'trades':>6} {'bars':>5} {'periodo':>14} {'pnl':>8} {'sharpe':>6} {'val_pass':>8} {'val_stage':>14}")
print("-" * 130)

for i, r in enumerate(rows, 1):
    m = json.loads(r['metrics_json'] or '{}')
    bp = m.get('best_probe', {}) or {}
    engine = bp.get('engine', '?')
    par = bp.get('symbol', '?')
    tf = bp.get('timeframe', '?')
    trades = bp.get('trade_count') or m.get('trade_count') or 0
    bars = bp.get('bars') or m.get('bars') or 0
    pnl = bp.get('pnl_pct') or m.get('pnl_pct') or 0
    sharpe = bp.get('sharpe') or m.get('sharpe') or 0
    val_pass = m.get('validation_passed')
    val_stage = m.get('validation_stage', '?')
    # periodo em dias
    hours_per_bar = TF_HOURS.get(tf, 1)
    days = (bars * hours_per_bar) / 24
    if days > 365:
        periodo = f"{days/365:.1f} anos"
    elif days > 30:
        periodo = f"{days/30:.1f} meses"
    else:
        periodo = f"{days:.1f} dias"
    print(f"{i:>2} {engine:>14} {par:>8} {tf:>4} {trades:>6} {bars:>5} {periodo:>14} {pnl:>7.1f}% {sharpe:>6.2f} {str(val_pass):>8} {val_stage:>14}")

print()
print("=== Distribuicao de validation_stage ===")
from collections import Counter
stages = Counter()
for r in rows:
    m = json.loads(r['metrics_json'] or '{}')
    stages[m.get('validation_stage') or '?'] += 1
for stage, n in stages.most_common():
    print(f"  {stage}: {n}")

print()
print("=== Resumo por timeframe ===")
tfs = Counter()
for r in rows:
    m = json.loads(r['metrics_json'] or '{}')
    bp = m.get('best_probe', {}) or {}
    tfs[bp.get('timeframe', '?')] += 1
for tf, n in tfs.most_common():
    print(f"  {tf}: {n}")

conn.close()
