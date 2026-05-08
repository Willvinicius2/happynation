"""Audit completo: critic v4 approvals + pipeline ate o bot."""
import sqlite3, sys, json
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from research.aether.config import db_path

conn = sqlite3.connect(str(db_path()), timeout=10.0)
conn.row_factory = sqlite3.Row

# =====================================================================
# 1. Critic v4 approvals (gpu_batch=true)
# =====================================================================
print("=" * 70)
print("APROVADAS PELO CRITIC v4 (gpu_batch=true)")
print("=" * 70)
rows = conn.execute("""
    SELECT id, name, status, score, metrics_json,
           json_extract(metrics_json,'$.critic_run_at') AS ran
    FROM nodes
    WHERE json_extract(metrics_json,'$.critic_status')='approved'
      AND json_extract(metrics_json,'$.critic_report.gpu_batch')=1
    ORDER BY ran DESC
""").fetchall()
print(f"total: {len(rows)}")
for r in rows[:10]:
    m = json.loads(r['metrics_json'] or '{}')
    rep = m.get('critic_report', {})
    bp = m.get('best_probe', {}) or {}
    wf = rep.get('walk_forward', {})
    grid = rep.get('grid_search', {})
    boot = rep.get('bootstrap', {})
    print(f"\n  {r['ran']} | status={r['status']}")
    print(f"    {r['id'][:80]}")
    print(f"    nome: {r['name']}")
    print(f"    engine: {bp.get('engine')} | tf: {bp.get('timeframe')} | symbol: {bp.get('symbol')}")
    print(f"    PnL: {bp.get('pnl_pct')}% | Sharpe: {bp.get('sharpe')} | trades: {bp.get('trade_count')} | hit: {bp.get('hit_rate')}")
    print(f"    cross: {[(c.get('symbol'), c.get('pnl_pct')) for c in (rep.get('cross_symbol') or [])]}")
    print(f"    WF: {wf.get('passed')}/{wf.get('valid')} folds (pnls={wf.get('fold_pnls')})")
    print(f"    grid: combos={grid.get('total_combos')} pos_frac={grid.get('pnl_positive_frac')} p25={grid.get('pnl_p25_pct')}%")
    print(f"    boot: sharpe p5/p50/p95={boot.get('sharpe_p5')}/{boot.get('sharpe_p50')}/{boot.get('sharpe_p95')} pos_frac={boot.get('sharpe_positive_frac')}")

# =====================================================================
# 2. Pipeline status: validated -> approved -> deploy -> bot
# =====================================================================
print("\n" + "=" * 70)
print("PIPELINE: COUNTS POR ESTADO")
print("=" * 70)
print("\n  --- nodes na DB ---")
for r in conn.execute("""
    SELECT status, COUNT(*) AS n FROM nodes WHERE id != 'root' GROUP BY status ORDER BY n DESC
""").fetchall():
    print(f"    {r['status']:15s}: {r['n']}")

print("\n  --- approvals do critic (todas versoes) ---")
for r in conn.execute("""
    SELECT
        CASE WHEN json_extract(metrics_json,'$.critic_report.gpu_batch')=1 THEN 'critic_v4_gpu' ELSE 'critic_legado' END AS ver,
        json_extract(metrics_json,'$.critic_status') AS cs,
        COUNT(*) AS n
    FROM nodes
    WHERE json_extract(metrics_json,'$.critic_status') IS NOT NULL
    GROUP BY ver, cs
""").fetchall():
    print(f"    {r['ver']:15s} {r['cs']:10s}: {r['n']}")

# =====================================================================
# 3. Auto deploy: o que foi enviado pro bot
# =====================================================================
print("\n" + "=" * 70)
print("AUTO_DEPLOY: ESTRATEGIAS ENVIADAS PRO BOT")
print("=" * 70)
rows = conn.execute("""
    SELECT ts, type, message, payload_json FROM events
    WHERE agent_role='auto_deploy' AND ts > datetime('now','-7 days')
    ORDER BY ts DESC LIMIT 10
""").fetchall()
print(f"  ultimos 10 eventos auto_deploy:")
for r in rows:
    print(f"    {r['ts']} | {r['type']}: {(r['message'] or '')[:90]}")

# Quais nodes ja foram deployados?
deployed = conn.execute("""
    SELECT id, name, json_extract(metrics_json,'$.deploy_status') AS ds,
           json_extract(metrics_json,'$.deploy_run_at') AS dt
    FROM nodes
    WHERE json_extract(metrics_json,'$.deploy_status') IS NOT NULL
    ORDER BY dt DESC
""").fetchall()
print(f"\n  nodes com deploy_status: {len(deployed)}")
for r in deployed[:5]:
    print(f"    {r['dt']} {r['ds']} | {r['id'][:60]}")

conn.close()
