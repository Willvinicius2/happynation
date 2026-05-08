"""
Script para visualizar a arvore de hipoteses do Aether como um Mapa Mental / Arvore.
Agrupa por Familia (Engine) -> Hipotese -> Timeframes -> Status.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from research.aether.config import db_path

def get_engine(row):
    metrics = json.loads(row['metrics_json'] or '{}')
    tags = json.loads(row['tags_json'] or '[]')
    
    engine = ""
    best = metrics.get("best_probe") or {}
    if isinstance(best, dict):
        engine = str(best.get("engine") or "")
    if not engine:
        specs = metrics.get("probe_specs") or []
        if isinstance(specs, list) and specs and isinstance(specs[0], dict):
            engine = str(specs[0].get("engine") or "")
    if not engine:
        for tag in tags:
            if isinstance(tag, str) and tag.startswith("engine:"):
                engine = tag.split(":", 1)[1]
                break
    return (engine or "unknown").lower()

def print_tree(by_engine, children, campaign_id):
    print(f"🌲 MAPA MENTAL AETHER - Campanha: {campaign_id} 🌲")
    print("=" * 80)
    
    for eng, pnodes in sorted(by_engine.items()):
        eng_active = sum(1 for p in pnodes if p['status'] not in ('pruned', 'paused', 'archive'))
        print(f"\n⚙️  Engine: {eng.upper()} ({len(pnodes)} hipoteses, {eng_active} ativas)")
        
        for pnode in sorted(pnodes, key=lambda x: x['score'], reverse=True):
            p_status = pnode['status']
            p_score = pnode['score']
            p_name = str(pnode['name'] or pnode['id'])[:50]
            
            status_icon = "✅" if p_status == "validated" else "⏳" if p_status in ("candidate", "queued", "running") else "✂️ " if p_status == "pruned" else "⏸️"
            
            print(f"  ├─ {status_icon} {p_name} (Score: {p_score:.2f} | Status: {p_status})")
            
            tf_nodes = children.get(pnode['id'], [])
            for i, tf_node in enumerate(sorted(tf_nodes, key=lambda x: x['score'], reverse=True)):
                is_last = (i == len(tf_nodes) - 1)
                prefix = "  │  └─" if is_last else "  │  ├─"
                
                tf_status = tf_node['status']
                tf_score = tf_node['score']
                tf_name = str(tf_node['name'] or tf_node['id']).split('/')[-1].strip()
                
                m = json.loads(tf_node['metrics_json'] or '{}')
                critic = m.get('critic_status', '')
                critic_str = f" | Critic: {critic}" if critic else ""
                
                tf_icon = "✅" if tf_status == "validated" else "⏳" if tf_status in ("candidate", "queued", "running") else "✂️ " if tf_status == "pruned" else "⏸️"
                
                print(f"{prefix} {tf_icon} {tf_name} (Score: {tf_score:.2f} | Status: {tf_status}{critic_str})")

def print_mermaid(by_engine, children):
    print("mindmap")
    print("  root((Aether))")
    
    for eng, pnodes in sorted(by_engine.items()):
        print(f"    {eng.capitalize()}")
        active_pnodes = [p for p in pnodes if p['status'] not in ('pruned', 'paused', 'archive')]
        if not active_pnodes:
            continue
        for pnode in sorted(active_pnodes, key=lambda x: x['score'], reverse=True):
            p_name = str(pnode['name'] or pnode['id']).replace('(', '').replace(')', '').replace('"', '')[:40]
            icon = "✅ " if pnode['status'] == "validated" else "⏳ "
            print(f"      {icon}{p_name}")
            tf_nodes = children.get(pnode['id'], [])
            active_tf_nodes = [t for t in tf_nodes if t['status'] not in ('pruned', 'paused', 'archive')]
            for tf_node in sorted(active_tf_nodes, key=lambda x: x['score'], reverse=True):
                tf_name = str(tf_node['name'] or tf_node['id']).split('/')[-1].strip()
                icon_tf = "✅ " if tf_node['status'] == "validated" else ""
                print(f"        {icon_tf}{tf_name}\n          ::icon(fas fa-info-circle)\n          ({tf_node['status']})")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mermaid", action="store_true", help="Gera saida no formato Mermaid JS para copiar e colar em visualizadores.")
    args = parser.parse_args()
    conn = sqlite3.connect(str(db_path()), timeout=10.0)
    conn.row_factory = sqlite3.Row
    campaign_id = conn.execute("SELECT id FROM campaigns WHERE status='active' ORDER BY created_at DESC LIMIT 1").fetchone()['id']
    rows = conn.execute("SELECT id, parent_id, name, status, score, depth, metrics_json, tags_json FROM nodes WHERE campaign_id=? AND id != 'root' AND id NOT LIKE 'tf:%'", (campaign_id,)).fetchall()
    parents, children = {}, defaultdict(list)
    for r in rows:
        parents[r['id']] = r if r['depth'] == 1 else parents.get(r['id'])
        if r['depth'] == 2: children[r['parent_id']].append(r)
    by_engine = defaultdict(list)
    for pid, pnode in parents.items():
        by_engine[get_engine(pnode)].append(pnode)
    if args.mermaid: print_mermaid(by_engine, children)
    else: print_tree(by_engine, children, campaign_id)
    conn.close()

if __name__ == "__main__":
    main()