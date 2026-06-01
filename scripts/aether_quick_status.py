"""Quick status do funil + agents."""
import sqlite3, sys
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from research.aether.config import db_path  # type: ignore

conn = sqlite3.connect(str(db_path()), timeout=10.0)
conn.row_factory = sqlite3.Row

print("=== ULTIMOS 5 ciclos de pesquisa ===")
for r in conn.execute("""
    SELECT ts,
           json_extract(payload_json,'$.llm.created_hypotheses') AS llm_h,
           json_extract(payload_json,'$.llm.created_nodes') AS llm_n,
           json_extract(payload_json,'$.web.created_hypotheses') AS web_h,
           json_extract(payload_json,'$.web.created_nodes') AS web_n,
           json_extract(payload_json,'$.llm.error') AS err
    FROM events WHERE type='research_completed'
    ORDER BY ts DESC LIMIT 5
""").fetchall():
    err = (r["err"] or "")[:100]
    print(f"  {r['ts']}: llm_h={r['llm_h']} llm_nodes={r['llm_n']} web_h={r['web_h']} web_nodes={r['web_n']} err={err}")

print("\n=== ULTIMOS 5 ciclos LLM aceitos ===")
for r in conn.execute("""
    SELECT ts, json_extract(payload_json,'$.created') AS c,
           json_extract(payload_json,'$.rejected_family_cap') AS rf,
           json_extract(payload_json,'$.rejected_duplicate') AS rd,
           json_extract(payload_json,'$.model') AS model
    FROM events WHERE type='llm_research_completed'
    ORDER BY ts DESC LIMIT 5
""").fetchall():
    print(f"  {r['ts']}: model={r['model']} created={r['c']} rej_fam={r['rf']} rej_dup={r['rd']}")

print("\n=== ULTIMOS 8 skips/blocks hypothesis_generator ===")
for r in conn.execute("""
    SELECT ts, type, message FROM events
    WHERE type IN ('focus_block_new_root','llm_research_unavailable','llm_research_parse_error','exploration_forced_round')
    ORDER BY ts DESC LIMIT 8
""").fetchall():
    print(f"  {r['ts']}: {r['type']} | {(r['message'] or '')[:100]}")

print("\n=== Critic ultimos 60 min ===")
for r in conn.execute("""
    SELECT type, COUNT(*) AS n FROM events
    WHERE type IN ('critic_approved','critic_rejected') AND ts > datetime('now','-1 hour')
    GROUP BY type
""").fetchall():
    print(f"  {r['type']}: {r['n']}")

print("\n=== Hyper agent ultimos 5 eventos ===")
for r in conn.execute("""
    SELECT ts, type, message FROM events
    WHERE agent_role='hyper_agent' ORDER BY ts DESC LIMIT 5
""").fetchall():
    print(f"  {r['ts']}: {r['type']} | {(r['message'] or '')[:90]}")

print("\n=== Distancia ate proximo ciclo de generation ===")
r = conn.execute("SELECT MAX(ts) AS last FROM events WHERE type='research_completed'").fetchone()
print(f"  ultimo: {r['last']}")

conn.close()
