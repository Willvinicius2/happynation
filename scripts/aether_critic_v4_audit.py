"""Audit do critic v4: quantos passaram, em que gate falham."""
import sqlite3, sys, json
from collections import Counter
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from research.aether.config import db_path  # type: ignore

conn = sqlite3.connect(str(db_path()), timeout=10.0)
conn.row_factory = sqlite3.Row

# Critic v4 = nodes com critic_report.gpu_batch=true (foi processado pela versao nova)
print("=== Nodes processados pelo critic v4 (gpu_batch=true) ===")
rows = conn.execute("""
    SELECT id, status, json_extract(metrics_json,'$.critic_status') AS cs,
           json_extract(metrics_json,'$.critic_reason') AS reason,
           json_extract(metrics_json,'$.critic_run_at') AS ran_at
    FROM nodes
    WHERE json_extract(metrics_json,'$.critic_report.gpu_batch')=1
    ORDER BY ran_at DESC
""").fetchall()

if not rows:
    print("  (zero) -- critic v4 ainda nao rodou em nenhum node, OU nenhuma chegou na aprovacao final")
else:
    approved = sum(1 for r in rows if r['cs'] == 'approved')
    rejected = sum(1 for r in rows if r['cs'] == 'rejected')
    print(f"  total={len(rows)} approved={approved} rejected={rejected}")
    # Razoes de rejeicao agrupadas
    reasons = Counter()
    for r in rows:
        if r['cs'] != 'rejected': continue
        reason = (r['reason'] or '').split('(')[0].strip()
        reasons[reason] += 1
    print("\n  rejeicoes por gate:")
    for k, v in reasons.most_common():
        print(f"    {v}x -> {k}")

# Comparar v1 vs v4
print("\n=== Aprovacoes critic v1 (sem gpu_batch) ===")
v1 = conn.execute("""
    SELECT COUNT(*) AS n FROM nodes
    WHERE json_extract(metrics_json,'$.critic_status')='approved'
    AND json_extract(metrics_json,'$.critic_report.gpu_batch') IS NULL
""").fetchone()
print(f"  total approved historico (v1): {v1['n']}")

# Top 10 razoes de rejeicao globais (todos os criticos)
print("\n=== TOP gates que mais rejeitam (24h, todos os critics) ===")
rows = conn.execute("""
    SELECT json_extract(payload_json,'$.reason') AS reason
    FROM events WHERE type='critic_rejected' AND ts > datetime('now','-1 day')
""").fetchall()
groups = Counter()
for r in rows:
    if not r['reason']: continue
    key = r['reason'].split('(')[0].strip()
    groups[key] += 1
for k, v in groups.most_common(10):
    print(f"  {v}x -> {k}")

# Pipeline: validated waiting for critic
waiting = conn.execute("""
    SELECT COUNT(*) AS n FROM nodes
    WHERE status='validated'
      AND id != 'root' AND id NOT LIKE 'tf:%'
      AND (json_extract(metrics_json,'$.critic_status') IS NULL
           OR json_extract(metrics_json,'$.critic_status') NOT IN ('approved','rejected'))
""").fetchone()
print(f"\n=== Validateds aguardando critic: {waiting['n']} ===")

# Tempo desde ultimo approved
last_appr = conn.execute("""
    SELECT MAX(ts) AS ts FROM events WHERE type='critic_approved'
""").fetchone()
print(f"\nUltimo critic_approved: {last_appr['ts']}")

conn.close()
