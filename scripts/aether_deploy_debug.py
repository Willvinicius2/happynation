"""Debug: por que auto_deploy nao pega as 14 aprovadas pelo v4?"""
import sqlite3, sys, json
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from research.aether.config import db_path

conn = sqlite3.connect(str(db_path()), timeout=10.0)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT id, name, status, score, metrics_json
    FROM nodes
    WHERE json_extract(metrics_json,'$.critic_status')='approved'
      AND json_extract(metrics_json,'$.critic_report.gpu_batch')=1
    ORDER BY score DESC
""").fetchall()

print(f"=== {len(rows)} nodes aprovados pelo critic v4 ===\n")

# limites do auto_deploy
MIN_SCORE=0.42; MIN_PNL_PCT=0.0; MIN_TRADES=8; MAX_MDD_PCT=80.0
print(f"Gates do auto_deploy: MIN_SCORE={MIN_SCORE} MIN_PNL_PCT={MIN_PNL_PCT} MIN_TRADES={MIN_TRADES} MAX_MDD_PCT={MAX_MDD_PCT}\n")

passou = 0
for r in rows:
    m = json.loads(r['metrics_json'] or '{}')
    bp = m.get('best_probe', {}) or {}
    score = float(r['score'] or 0)
    pnl = float(m.get('pnl_pct') or bp.get('pnl_pct') or 0)
    trades = int(m.get('trade_count') or bp.get('trade_count') or 0)
    mdd = float(m.get('mdd_pct') or bp.get('mdd_pct') or 0)
    vp = m.get('validation_passed')
    fails = []
    if vp is not True: fails.append(f"validation_passed={vp}")
    if score < MIN_SCORE: fails.append(f"score={score:.3f}<{MIN_SCORE}")
    if pnl < MIN_PNL_PCT: fails.append(f"pnl={pnl:.1f}%<{MIN_PNL_PCT}")
    if trades < MIN_TRADES: fails.append(f"trades={trades}<{MIN_TRADES}")
    if mdd > MAX_MDD_PCT: fails.append(f"mdd={mdd:.1f}%>{MAX_MDD_PCT}")
    status = "OK" if not fails else "BLOQ"
    if not fails: passou += 1
    print(f"  [{status}] score={score:.2f} pnl={pnl:.1f}% trades={trades} mdd={mdd:.1f}% vp={vp}")
    if fails:
        print(f"          motivo: {' | '.join(fails)}")
    print(f"          {r['id'][:75]}")

print(f"\n>>> {passou}/{len(rows)} passariam pelos gates do auto_deploy")
conn.close()
