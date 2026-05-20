"""Diagnostico rapido do Aether: quem gera, distribuicao de timeframes, pendentes."""
import os, sqlite3, sys, json
db = os.path.expandvars(r"%LOCALAPPDATA%\Aether\runtime\aether.sqlite3")
con = sqlite3.connect(db, timeout=5.0)
con.row_factory = sqlite3.Row

print("=== Latest 12 events ===")
for r in con.execute("SELECT ts, type, agent_role, substr(message,1,140) as msg FROM events ORDER BY ts DESC LIMIT 12"):
    role = r["agent_role"] or ""
    print(f"{r['ts']} | {r['type'][:30]:30} | {role[:18]:18} | {r['msg'] or ''}")

print()
print("=== Childen of latest LLM hypothesis (atr-breakout-com-obv-trend-fluxo-4h) ===")
for r in con.execute("SELECT id, status, score, created_at FROM nodes WHERE id LIKE 'llmhyp:atr-breakout-com-obv-trend-fluxo-4h%' ORDER BY id"):
    print(f"{r['created_at']} | {r['status']:12} | sc={r['score']:.3f} | {r['id']}")

print()
print("=== Status of all candidate/expanded nodes (last created) ===")
for r in con.execute("SELECT id, status, score, timeframe, created_at FROM nodes WHERE status IN ('candidate','expanded','expand') ORDER BY created_at DESC LIMIT 20"):
    print(f"{r['created_at']} | {r['status']:10} | tf={(r['timeframe'] or '?'):4} | sc={(r['score'] or 0):.3f} | {r['id'][:80]}")

print()
print("=== llm_research_completed events (last 8) ===")
for r in con.execute("SELECT ts, message, payload_json FROM events WHERE type='llm_research_completed' ORDER BY ts DESC LIMIT 8"):
    print(f"{r['ts']} | {r['message']}")

print()
print("=== Distribuicao por timeframe nos active (nao archive/paused) ===")
for r in con.execute("SELECT timeframe, status, COUNT(*) c FROM nodes WHERE status NOT IN ('archive','paused','pruned') GROUP BY timeframe, status ORDER BY timeframe, status"):
    print(f"  tf={(r['timeframe'] or 'NULL'):6} status={r['status']:12} count={r['c']}")

print()
print("=== Quantos candidates por timeframe ===")
for r in con.execute("SELECT timeframe, COUNT(*) c FROM nodes WHERE status='candidate' GROUP BY timeframe ORDER BY c DESC"):
    print(f"  tf={(r['timeframe'] or 'NULL'):6} candidates={r['c']}")

print()
print("=== Settings: hyper_controls + llm_status if any ===")
for r in con.execute("SELECT key, value_json, updated_at FROM settings"):
    print(f"{r['updated_at']} | {r['key']}")
    try:
        v = json.loads(r['value_json'])
        for k, val in v.items():
            print(f"   {k} = {val}")
    except Exception:
        print(f"   {r['value_json'][:200]}")

print()
print("=== Settings: agent statuses (recent) ===")
for r in con.execute("SELECT role, status, current_task_id, substr(stats_json,1,200) s, updated_at FROM agents ORDER BY updated_at DESC LIMIT 10"):
    print(f"{r['updated_at']} | {r['role']:24} | {r['status']:10} | task={r['current_task_id']} | {r['s']}")
