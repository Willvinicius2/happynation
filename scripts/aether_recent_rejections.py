"""Lista rejeicoes recentes do critic com razao."""
import sqlite3, sys, json
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from research.aether.config import db_path

conn = sqlite3.connect(str(db_path()), timeout=10.0)
conn.row_factory = sqlite3.Row

cutoff = sys.argv[1] if len(sys.argv) > 1 else "2026-05-05T23:50:00Z"
print(f"=== rejeicoes desde {cutoff} ===")
rows = conn.execute(
    "SELECT ts, node_id, payload_json FROM events WHERE type='critic_rejected' AND ts > ? ORDER BY ts DESC",
    (cutoff,),
).fetchall()
print(f"total: {len(rows)}")
for r in rows[:15]:
    try:
        p = json.loads(r["payload_json"] or "{}")
        rep = p.get("report") or {}
        gpu = rep.get("gpu_batch")
        reason = (p.get("reason") or "")[:120]
        print(f"  {r['ts']} gpu={gpu} | {r['node_id'][:60]} | {reason}")
    except Exception as e:
        print(f"  {r['ts']} ERR {e}")

print("\n=== aprovacoes desde {cutoff} ===")
rows = conn.execute(
    "SELECT ts, node_id, payload_json FROM events WHERE type='critic_approved' AND ts > ? ORDER BY ts DESC",
    (cutoff,),
).fetchall()
print(f"total: {len(rows)}")
for r in rows[:15]:
    try:
        p = json.loads(r["payload_json"] or "{}")
        rep = p.get("report") or {}
        gpu = rep.get("gpu_batch")
        wf = rep.get("walk_forward", {}).get("passed")
        boot = rep.get("bootstrap", {}).get("sharpe_p5") if isinstance(rep.get("bootstrap"), dict) else None
        print(f"  {r['ts']} gpu={gpu} wf={wf} boot_p5={boot} | {r['node_id'][:60]}")
    except Exception as e:
        print(f"  {r['ts']} ERR {e}")

conn.close()
