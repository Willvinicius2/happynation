"""Audit do funil do Aether: geracao LLM, rejeicoes, critic, arvore."""
from __future__ import annotations
import os, sys, sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from research.aether.config import db_path  # type: ignore


def main():
    conn = sqlite3.connect(str(db_path()), timeout=30.0)
    conn.row_factory = sqlite3.Row

    print("=== GERACAO LLM (calls/dia) ===")
    cur = conn.execute("""
        SELECT date(ts) AS d, COUNT(*) AS n, COALESCE(SUM(cost_usd),0.0) AS cost
        FROM llm_traces
        WHERE agent_role='hypothesis_generator' AND date(ts) > date('now','-7 days')
        GROUP BY date(ts) ORDER BY d DESC
    """)
    for r in cur.fetchall():
        print(f"  {r['d']}: calls={r['n']} cost=${r['cost']:.4f}")

    print("\n=== HIPOTESES POR CICLO ===")
    cur = conn.execute("""
        SELECT date(ts) AS d, COUNT(*) AS cycles,
            COALESCE(SUM(json_extract(payload_json,'$.created')),0) AS created,
            COALESCE(SUM(json_extract(payload_json,'$.rejected_diversity')),0) AS rej_div,
            COALESCE(SUM(json_extract(payload_json,'$.rejected_family_cap')),0) AS rej_fam,
            COALESCE(SUM(json_extract(payload_json,'$.rejected_duplicate')),0) AS rej_dup
        FROM events WHERE type='llm_research_completed' AND date(ts) > date('now','-7 days')
        GROUP BY date(ts) ORDER BY d DESC
    """)
    for r in cur.fetchall():
        print(f"  {r['d']}: cycles={r['cycles']} created={r['created']} rej_div={r['rej_div']} rej_fam={r['rej_fam']} rej_dup={r['rej_dup']}")

    print("\n=== CRITIC verdicts (24h) ===")
    cur = conn.execute("""
        SELECT type, COUNT(*) AS n FROM events
        WHERE type IN ('critic_approved','critic_rejected') AND ts > datetime('now','-1 day')
        GROUP BY type
    """)
    for r in cur.fetchall():
        print(f"  {r['type']}: {r['n']}")

    print("\n=== RAZOES rejeicao critic (24h) ===")
    cur = conn.execute("""
        SELECT json_extract(payload_json,'$.reason') AS reason, COUNT(*) AS n
        FROM events WHERE type='critic_rejected' AND ts > datetime('now','-1 day')
        GROUP BY substr(json_extract(payload_json,'$.reason'),1,40)
        ORDER BY n DESC LIMIT 12
    """)
    for r in cur.fetchall():
        rs = (r['reason'] or '?')[:90]
        print(f"  {r['n']}x: {rs}")

    print("\n=== ARVORE ===")
    cur = conn.execute("SELECT status, COUNT(*) AS n FROM nodes WHERE id != 'root' GROUP BY status ORDER BY n DESC")
    for r in cur.fetchall():
        print(f"  {r['status']}: {r['n']}")

    print("\n=== TOP FAMILIAS (ativas) ===")
    # Espelha a logica do family_cap em llm_hypothesis._collect_exploration_context:
    # best_probe.engine -> probe_specs[0].engine -> tag engine:*. Sem isso, parents
    # 'visited' (raiz da hipotese, antes do scout rodar) caem em '?' e poluem a auditoria.
    cur = conn.execute("""
        SELECT COALESCE(
            json_extract(metrics_json, '$.best_probe.engine'),
            json_extract(metrics_json, '$.probe_specs[0].engine'),
            json_extract(metrics_json, '$.engines[0]'),
            'unknown'
        ) AS eng, COUNT(*) AS n
        FROM nodes WHERE status IN ('candidate','visited','running','validated') AND id != 'root'
        GROUP BY eng ORDER BY n DESC LIMIT 15
    """)
    for r in cur.fetchall():
        print(f"  {r['eng']}: {r['n']}")

    print("\n=== TEMPO MEDIO entre ciclos do hypothesis_generator (ult 24h) ===")
    cur = conn.execute("""
        SELECT ts FROM events WHERE type='llm_research_completed'
        AND ts > datetime('now','-1 day') ORDER BY ts ASC
    """)
    rows = cur.fetchall()
    if len(rows) >= 2:
        from datetime import datetime
        ts = []
        for r in rows:
            try:
                ts.append(datetime.fromisoformat(r['ts'].replace('Z','+00:00')))
            except Exception:
                continue
        if len(ts) >= 2:
            deltas = [(ts[i+1]-ts[i]).total_seconds() for i in range(len(ts)-1)]
            avg = sum(deltas)/len(deltas)
            print(f"  ciclos={len(ts)} media={avg:.0f}s ({avg/60:.1f} min)")

    print("\n=== ENV LLM ===")
    print(f"  AETHER_LLM_MAX_NEW={os.environ.get('AETHER_LLM_MAX_NEW','3 (default)')}")
    print(f"  AETHER_HYPOTHESIS_MAX_TOKENS={os.environ.get('AETHER_HYPOTHESIS_MAX_TOKENS','1200 (default)')}")
    print(f"  AETHER_SEMANTIC_K={os.environ.get('AETHER_SEMANTIC_K','6 (default)')}")

    conn.close()


if __name__ == "__main__":
    main()
