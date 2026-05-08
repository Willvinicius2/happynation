"""Inspeção do aproveitamento familia/arvore Aether."""
import os
import sqlite3
import json
from collections import Counter, defaultdict

DB = os.path.join(os.environ["LOCALAPPDATA"], "Aether", "runtime", "aether.sqlite3")


def main() -> None:
    con = sqlite3.connect(DB)
    cur = con.cursor()

    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    print("=" * 70)
    print("TABLES:", tables)
    print("=" * 70)

    print("\n--- nodes table schema ---")
    for r in cur.execute("PRAGMA table_info(nodes)").fetchall():
        print(" ", r)

    print("\n--- counts by status ---")
    rows = cur.execute(
        "SELECT status, COUNT(*) FROM nodes GROUP BY status ORDER BY 2 DESC"
    ).fetchall()
    for status, c in rows:
        print(f"  {status:25s} {c}")

    print("\n--- counts by family (top 30) ---")
    rows = cur.execute(
        "SELECT family, COUNT(*) FROM nodes WHERE family IS NOT NULL GROUP BY family ORDER BY 2 DESC LIMIT 30"
    ).fetchall()
    for family, c in rows:
        print(f"  {family:35s} {c}")

    print("\n--- counts by family x status (top families) ---")
    top_fams = [r[0] for r in cur.execute(
        "SELECT family, COUNT(*) FROM nodes WHERE family IS NOT NULL GROUP BY family ORDER BY 2 DESC LIMIT 12"
    ).fetchall()]
    for fam in top_fams:
        rows = cur.execute(
            "SELECT status, COUNT(*) FROM nodes WHERE family=? GROUP BY status ORDER BY 2 DESC",
            (fam,),
        ).fetchall()
        breakdown = ", ".join(f"{s}={c}" for s, c in rows)
        print(f"  {fam:35s} {breakdown}")

    print("\n--- parent/child structure ---")
    total = cur.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    roots = cur.execute("SELECT COUNT(*) FROM nodes WHERE parent_id IS NULL").fetchone()[0]
    children = total - roots
    print(f"  total nodes : {total}")
    print(f"  roots       : {roots}")
    print(f"  children    : {children}")

    rows = cur.execute(
        "SELECT depth, COUNT(*) FROM nodes GROUP BY depth ORDER BY depth"
    ).fetchall()
    print("  by depth:")
    for depth, c in rows:
        print(f"    depth {depth} -> {c}")

    print("\n--- top parents by # of children ---")
    rows = cur.execute(
        """
        SELECT n.id, n.family, n.status, COUNT(c.id) AS nc
        FROM nodes n
        LEFT JOIN nodes c ON c.parent_id = n.id
        GROUP BY n.id
        HAVING nc > 0
        ORDER BY nc DESC
        LIMIT 15
        """
    ).fetchall()
    for nid, fam, status, nc in rows:
        print(f"  parent={nid[:8]} family={fam:25s} status={status:18s} children={nc}")

    print("\n--- pruning patterns ---")
    cur.execute("PRAGMA table_info(nodes)")
    cols = [c[1] for c in cur.fetchall()]
    has_reason = "rejection_reason" in cols
    print("  columns has rejection_reason?", has_reason)
    if has_reason:
        rows = cur.execute(
            """
            SELECT rejection_reason, COUNT(*)
            FROM nodes
            WHERE rejection_reason IS NOT NULL AND rejection_reason != ''
            GROUP BY rejection_reason
            ORDER BY 2 DESC
            LIMIT 20
            """
        ).fetchall()
        for r, c in rows:
            print(f"  {c:5d}  {r[:120]}")

    print("\n--- approval funnel (last 7 days) ---")
    rows = cur.execute(
        """
        SELECT status, COUNT(*)
        FROM nodes
        WHERE created_at > datetime('now', '-7 days')
        GROUP BY status
        ORDER BY 2 DESC
        """
    ).fetchall()
    for status, c in rows:
        print(f"  {status:25s} {c}")

    print("\n--- by agent (last 24h) ---")
    rows = cur.execute(
        """
        SELECT agent, COUNT(*)
        FROM nodes
        WHERE created_at > datetime('now', '-24 hours')
        GROUP BY agent
        ORDER BY 2 DESC
        """
    ).fetchall()
    for agent, c in rows:
        print(f"  {agent or '(none)':30s} {c}")

    con.close()


if __name__ == "__main__":
    main()
