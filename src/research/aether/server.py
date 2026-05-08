from __future__ import annotations

import hashlib
import json
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import DEFAULT_WORKERS, LEGACY_ROOT, db_path, static_dir
from .campaigns import create_campaign, get_active_campaign_id, list_campaigns
from src.research.active import deploy_record

from .db import add_event, connect, init_db, loads
from .legacy_import import import_legacy
from .llm_hypothesis import create_llm_hypothesis_batch
from .llm_runtime import llm_status
from .llm_traces import list_llm_traces
from .manifest import generate_manifest
from .orchestrator import (
    bootstrap,
    cycle_mode,
    is_cycle_running,
    is_stop_requested,
    queue_scout_tasks,
    start_cycle_background,
    stop_cycle,
)
from .ranking import build_strategy_ranking, ranking_summary
from .repository import ensure_agents, list_state, mark_agents_without_work_standby, seed_base_tree
from .web_research import create_web_hypothesis_batch


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    bootstrap()
    with connect() as conn:
        add_event(
            conn,
            type="server_started",
            agent_role="chief_orchestrator",
            message=f"Aether online. DB={db_path()} workers_default={DEFAULT_WORKERS}",
        )
    yield


app = FastAPI(title="Aether", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.mount("/static", StaticFiles(directory=static_dir()), name="static")


def _active_id(node_id: str, engine: str, params: dict, symbol: str, timeframe: str) -> str:
    payload = json.dumps(
        {
            "node_id": node_id,
            "engine": engine,
            "params": params,
            "symbol": symbol,
            "timeframe": timeframe,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return "aether-" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:14]


def _active_record_from_node(conn, ranking_row: dict) -> tuple[dict | None, str | None]:
    node_id = ranking_row["node_id"]
    row = conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    if not row:
        return None, f"node nao encontrado: {node_id}"
    metrics = loads(row["metrics_json"], {})
    best = metrics.get("best_probe") if isinstance(metrics.get("best_probe"), dict) else {}
    engine = str(best.get("engine") or "")
    params = best.get("params") if isinstance(best.get("params"), dict) else {}
    probe_specs = metrics.get("probe_specs") if isinstance(metrics.get("probe_specs"), list) else []
    clean_specs = [
        {"engine": str(spec.get("engine")), "params": dict(spec.get("params") or {})}
        for spec in probe_specs
        if isinstance(spec, dict) and spec.get("engine")
    ]
    if len(clean_specs) > 1 and engine != "combo":
        return None, "hipotese multi-engine sem backtest combo aprovado"
    if len(clean_specs) > 1 and engine == "combo":
        params = {**params, "mode": params.get("mode") or "consensus", "specs": params.get("specs") or clean_specs}
    symbol = str(best.get("symbol") or "").upper()
    timeframe = str(best.get("timeframe") or row["timeframe"] or "")
    if not engine or not params:
        return None, "sem engine/params no best_probe"
    if not symbol:
        return None, "sem simbolo no best_probe"
    record = {
        "id": _active_id(node_id, engine, params, symbol, timeframe),
        "source": "aether",
        "node_id": node_id,
        "campaign_id": row["campaign_id"],
        "engine": engine,
        "params": params,
        "probe_specs": clean_specs,
        "asset": symbol,
        "symbol": symbol,
        "timeframe": timeframe,
        "score": float(row["score"] or ranking_row.get("score") or 0.0),
        "readiness": ranking_row.get("readiness"),
        "summary": {
            "name": row["name"],
            "pnl_pct": ranking_row.get("pnl_pct"),
            "sharpe": ranking_row.get("sharpe"),
            "mdd_pct": ranking_row.get("mdd_pct"),
            "trade_count": ranking_row.get("trade_count"),
            "validation_passed": bool(metrics.get("validation_passed")),
            "validation_stage": metrics.get("validation_stage"),
            "critic_status": metrics.get("critic_status"),
            "critic_reason": metrics.get("critic_reason"),
            "best_probe": best,
        },
        "risk": {
            "take_profit_pct": None,
            "stop_loss_pct": None,
            "max_hold_candles": None,
        },
    }
    return record, None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(static_dir() / "index.html")


@app.get("/api/state")
def api_state() -> dict:
    with connect() as conn:
        ensure_agents(conn)
        if not is_cycle_running():
            mark_agents_without_work_standby(conn)
        state = list_state(conn)
    state["runtime"] = {
        "db_path": str(db_path()),
        "legacy_root": str(LEGACY_ROOT),
        "default_workers": DEFAULT_WORKERS,
        "cycle_running": is_cycle_running(),
        "cycle_mode": cycle_mode(),
        "stop_requested": is_stop_requested(),
    }
    with connect() as conn:
        state["campaigns"] = list_campaigns(conn)
        from .db import get_setting as _get_setting
        state["hyper_controls"] = _get_setting(conn, "hyper_controls", default=None)
    return state


@app.get("/api/ranking")
def api_ranking(
    capital: float = Query(default=1000.0, gt=0, le=10_000_000),
    limit: int = Query(default=80, ge=1, le=250),
    campaign_id: str = Query(default="active"),
    dedupe: bool = Query(default=True),
    include_deployed: bool = Query(default=True),
    hide_blocked: bool = Query(default=True),
) -> dict:
    with connect() as conn:
        campaign_v = get_active_campaign_id(conn) if campaign_id == "active" else campaign_id
        rows = build_strategy_ranking(
            conn,
            starting_capital=capital,
            limit=limit,
            campaign_id=campaign_v,
            dedupe=dedupe,
            include_deployed=include_deployed,
            hide_blocked=hide_blocked,
        )
        campaigns = list_campaigns(conn)
    return {
        "ok": True,
        "capital": capital,
        "campaign_id": campaign_v,
        "dedupe": dedupe,
        "include_deployed": include_deployed,
        "hide_blocked": hide_blocked,
        "count": len(rows),
        "summary": ranking_summary(rows),
        "campaigns": campaigns,
        "strategies": rows,
    }


@app.post("/api/ranking/deploy")
def api_deploy_ranking(
    capital: float = Query(default=1000.0, gt=0, le=10_000_000),
    limit: int = Query(default=120, ge=1, le=250),
    campaign_id: str = Query(default="active"),
    max_deploy: int = Query(default=8, ge=1, le=50),
    include_needs_validation: bool = Query(default=False),
    dedupe: bool = Query(default=True),
) -> dict:
    with connect() as conn:
        campaign_v = get_active_campaign_id(conn) if campaign_id == "active" else campaign_id
        rows = build_strategy_ranking(conn, starting_capital=capital, limit=limit, campaign_id=campaign_v, dedupe=dedupe)
        allowed = {"paper_ready"}
        eligible = [row for row in rows if row.get("readiness") in allowed][:max_deploy]
        deployed = []
        skipped = []
        for ranking_row in eligible:
            record, reason = _active_record_from_node(conn, ranking_row)
            if reason or record is None:
                skipped.append({"node_id": ranking_row.get("node_id"), "reason": reason or "registro invalido"})
                continue
            deployed.append(deploy_record(record))
        add_event(
            conn,
            type="ranking_deployed",
            agent_role="manifest_agent",
            message=f"Ranking enviado ao bot: {len(deployed)} estrategias ativas.",
            payload={
                "campaign_id": campaign_v,
                "deployed": len(deployed),
                "skipped": skipped,
                "include_needs_validation": include_needs_validation,
            },
        )
    return {
        "ok": True,
        "campaign_id": campaign_v,
        "deployed_count": len(deployed),
        "skipped_count": len(skipped),
        "deployed": deployed,
        "skipped": skipped,
    }


@app.get("/api/mindmap")
def api_mindmap() -> dict:
    from collections import defaultdict
    with connect() as conn:
        campaign_id = get_active_campaign_id(conn)
        rows = conn.execute(
            "SELECT id, parent_id, name, status, score, depth, metrics_json, tags_json "
            "FROM nodes WHERE campaign_id=? AND id != 'root' AND id NOT LIKE 'tf:%'", 
            (campaign_id,)
        ).fetchall()
        
        parents, children = {}, defaultdict(list)
        for r in rows:
            if r['depth'] == 1:
                parents[r['id']] = r
            elif r['depth'] == 2:
                children[r['parent_id']].append(r)
        
        def get_engine(row):
            metrics = loads(row['metrics_json'], {})
            tags = loads(row['tags_json'], [])
            engine = str(metrics.get("best_probe", {}).get("engine") or "")
            if not engine:
                specs = metrics.get("probe_specs") or []
                if isinstance(specs, list) and specs and isinstance(specs[0], dict):
                    engine = str(specs[0].get("engine") or "")
            if not engine:
                for tag in tags:
                    if isinstance(tag, str) and tag.startswith("engine:"):
                        return tag.split(":", 1)[1].lower()
            return (engine or "unknown").lower()

        by_engine = defaultdict(list)
        for pid, pnode in parents.items():
            by_engine[get_engine(pnode)].append(pnode)

        lines = ["mindmap", "  root((Aether))"]
        for eng, pnodes in sorted(by_engine.items()):
            active_pnodes = [p for p in pnodes if p['status'] not in ('pruned', 'paused', 'archive')]
            if active_pnodes:
                lines.append(f"    {eng.capitalize()}")
            for pnode in sorted(active_pnodes, key=lambda x: x['score'] or 0.0, reverse=True):
                p_name = str(pnode['name'] or pnode['id']).replace('(', '').replace(')', '').replace('"', '')[:40]
                icon = "✅ " if pnode['status'] == "validated" else "⏳ "
                lines.append(f"      {icon}{p_name}")
                
                active_tf_nodes = [t for t in children.get(pnode['id'], []) if t['status'] not in ('pruned', 'paused', 'archive')]
                for tf_node in sorted(active_tf_nodes, key=lambda x: x['score'] or 0.0, reverse=True):
                    tf_name = str(tf_node['name'] or tf_node['id']).split('/')[-1].strip()
                    icon_tf = "✅ " if tf_node['status'] == "validated" else ""
                    lines.append(f"        {icon_tf}{tf_name}\n          ::icon(fas fa-info-circle)\n          ({tf_node['status']})")

        return {"ok": True, "mermaid": "\n".join(lines)}


def _build_agents_action_graph(limit_per_agent: int) -> dict:
    """Build the live action map used by /api/agents_graph."""
    from collections import Counter
    import html

    agent_colors = {
        "chief_orchestrator": "#fbbf24",
        "hypothesis_generator": "#f59e0b",
        "scorekeeper": "#22c55e",
        "validation_agent": "#10b981",
        "auto_validator": "#10b981",
        "critic_agent": "#a78bfa",
        "hyper_agent": "#06b6d4",
        "auto_deploy": "#3b82f6",
        "drift_agent": "#ef4444",
        "market_regime_agent": "#84cc16",
        "policy_agent": "#ec4899",
        "archivist_agent": "#94a3b8",
        "manifest_agent": "#fbbf24",
        "infra_data_agent": "#64748b",
        "portfolio_agent": "#14b8a6",
        "agent_1m_micro": "#22d3ee",
        "agent_5m_scalp": "#34d399",
        "agent_15m": "#60a5fa",
        "agent_1h": "#fbbf24",
        "agent_4h": "#f472b6",
        "agent_1d": "#c084fc",
    }
    display_names = {
        "chief_orchestrator": "Chief",
        "hypothesis_generator": "Hypothesis",
        "scorekeeper": "Score",
        "validation_agent": "Validator",
        "auto_validator": "Validator",
        "critic_agent": "Critic v4",
        "hyper_agent": "Hyper",
        "auto_deploy": "Deploy",
        "drift_agent": "Drift",
        "market_regime_agent": "Regime",
        "policy_agent": "Policy",
        "archivist_agent": "Archivist",
        "manifest_agent": "Manifest",
        "infra_data_agent": "InfraData",
        "portfolio_agent": "Portfolio",
        "agent_1m_micro": "1m Micro",
        "agent_5m_scalp": "5m Scalp",
        "agent_15m": "15m",
        "agent_1h": "1h",
        "agent_4h": "4h",
        "agent_1d": "1d",
    }
    phases = {
        "control": ("Controle", "#fbbf24"),
        "research": ("Pesquisa", "#f59e0b"),
        "testing": ("Testes", "#5dd39e"),
        "validation": ("Validacao", "#10b981"),
        "critic": ("Critic", "#a78bfa"),
        "deploy": ("Deploy", "#3b82f6"),
        "portfolio": ("Portfolio", "#14b8a6"),
        "risk": ("Risco", "#ef4444"),
        "infra": ("Infra", "#64748b"),
        "archive": ("Arquivo", "#94a3b8"),
    }

    nodes: list[dict] = []
    edges: list[dict] = []
    node_by_id: dict[str, dict] = {}
    edge_seen: set[tuple[str, str, str, bool]] = set()

    def add_node(node_id: str, label: str, **kwargs):
        item = {"id": node_id, "label": label, **kwargs}
        current = node_by_id.get(node_id)
        if current:
            current.update({k: v for k, v in item.items() if v is not None})
            return current
        node_by_id[node_id] = item
        nodes.append(item)
        return item

    def add_edge(source: str, target: str, **kwargs) -> None:
        if not source or not target or source == target:
            return
        key = (source, target, str(kwargs.get("label") or ""), bool(kwargs.get("dashes")))
        if key in edge_seen:
            return
        edge_seen.add(key)
        edges.append({"from": source, "to": target, **kwargs})

    def row_dict(row) -> dict:
        return {key: row[key] for key in row.keys()}

    def short(value, max_len: int = 36) -> str:
        text = str(value or "").replace("\n", " ").strip()
        return text if len(text) <= max_len else text[: max_len - 1] + "..."

    def title(*lines: object) -> str:
        return "<br>".join(html.escape(str(line)) for line in lines if line not in (None, ""))

    def color_obj(background: str, border: str, highlight: str | None = None) -> dict:
        return {
            "background": background,
            "border": border,
            "highlight": {"background": highlight or background, "border": "#f8fafc"},
        }

    def phase_for(role: str, kind: str | None = None) -> str:
        if role == "chief_orchestrator":
            return "control"
        if role.startswith("agent_"):
            return "testing"
        if role in {"hypothesis_generator", "scorekeeper"}:
            return "research"
        if role in {"validation_agent", "auto_validator"}:
            return "validation"
        if role == "critic_agent":
            return "critic"
        if role in {"auto_deploy", "manifest_agent"}:
            return "deploy"
        if role == "portfolio_agent":
            return "portfolio"
        if role in {"drift_agent", "market_regime_agent", "policy_agent"}:
            return "risk"
        if role == "archivist_agent":
            return "archive"
        if role == "infra_data_agent":
            return "infra"
        return (kind or "infra").replace("timeframe_worker", "testing")

    def event_color(event_type: str) -> tuple[str, str]:
        text = (event_type or "").lower()
        if any(token in text for token in ("failed", "error", "rejected", "pruned", "paused")):
            return "#311612", "#ef4444"
        if any(token in text for token in ("started", "running", "queued")):
            return "#10251b", "#5dd39e"
        if any(token in text for token in ("validated", "approved", "deploy", "completed")):
            return "#102033", "#3b82f6"
        if "research" in text or "hypothesis" in text:
            return "#2a2110", "#f59e0b"
        return "#111827", "#64748b"

    def agent_id(role: str) -> str:
        return f"agent::{role}"

    def phase_id(phase: str) -> str:
        return f"phase::{phase}"

    def load_node_map(conn, ids: set[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        pending = {str(i) for i in ids if i}
        while pending and len(out) < 700:
            batch = list(pending)[:240]
            pending.difference_update(batch)
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(f"SELECT * FROM nodes WHERE id IN ({placeholders})", batch).fetchall()
            for row in rows:
                item = row_dict(row)
                item["metrics"] = loads(item.pop("metrics_json"), {})
                item["tags"] = loads(item.pop("tags_json"), [])
                out[item["id"]] = item
                parent = item.get("parent_id")
                if parent and parent not in out:
                    pending.add(parent)
        return out

    def add_strategy_node(node: dict, *, active: bool = False, queued: bool = False) -> str:
        raw_id = str(node.get("id") or "")
        graph_id = f"node::{raw_id}"
        status = str(node.get("status") or "candidate")
        score = float(node.get("score") or 0.0)
        border = "#5dd39e" if active else "#d6a84d" if queued else "#334155"
        background = "#10251b" if active else "#2a2110" if queued else "#0f172a"
        line2 = " / ".join(part for part in [node.get("timeframe"), status, f"{score:.2f}"] if part)
        add_node(
            graph_id,
            f"{short(node.get('name') or node.get('code') or raw_id, 30)}\n{short(line2, 30)}",
            color=color_obj(background, border),
            shape="box" if (active or queued) else "ellipse",
            borderWidth=5 if active else 4 if queued else 2,
            size=18 + min(10, int(score * 10)),
            font={"size": 12 if active else 10, "color": "#e2e8f0", "face": "system-ui"},
            shadow={"enabled": bool(active or queued), "color": border, "size": 24 if active else 16, "x": 0, "y": 0},
            rawNodeId=raw_id,
            kind="strategy",
            active=active,
            title=title(
                node.get("name") or raw_id,
                f"status={status} score={score:.3f}",
                f"timeframe={node.get('timeframe') or '-'}",
                node.get("hypothesis") or "",
            ),
        )
        return graph_id

    def add_strategy_path(node_id: str, *, active: bool = False, queued: bool = False) -> str | None:
        if node_id not in node_map:
            return None
        chain = []
        current = node_map.get(node_id)
        safety = 0
        while current and safety < 10:
            chain.append(current)
            parent_id = current.get("parent_id")
            current = node_map.get(parent_id) if parent_id else None
            safety += 1
        chain.reverse()

        prev = "__root__"
        target_graph_id = None
        for item in chain:
            if item.get("id") == "root":
                continue
            is_target = item.get("id") == node_id
            graph_id = add_strategy_node(item, active=active and is_target, queued=queued and is_target)
            target_graph_id = graph_id
            add_edge(
                prev,
                graph_id,
                color={"color": "#5dd39e" if active else "#475569", "opacity": 0.62 if active else 0.28},
                width=3 if active and is_target else 1,
                dashes=not active,
            )
            prev = graph_id
        return target_graph_id

    add_node(
        "__root__",
        "Aether\nMapa de acao",
        color=color_obj("#fbbf24", "#f8fafc", "#fde68a"),
        size=44,
        font={"size": 21, "color": "#111827", "face": "system-ui"},
        shape="star",
        borderWidth=3,
    )

    focus: list[dict] = []
    active_graph_ids: set[str] = set()

    with connect() as conn:
        campaign_id = get_active_campaign_id(conn)
        ensure_agents(conn)
        agent_rows = [row_dict(r) for r in conn.execute("SELECT * FROM agents ORDER BY kind, role").fetchall()]
        for agent in agent_rows:
            agent["stats"] = loads(agent.pop("stats_json"), {})

        task_rows = []
        for row in conn.execute(
            """
            SELECT * FROM tasks
            WHERE campaign_id=? AND status IN ('running','queued')
            ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, priority DESC, updated_at DESC
            LIMIT 160
            """,
            (campaign_id,),
        ).fetchall():
            item = row_dict(row)
            item["payload"] = loads(item.pop("payload_json"), {})
            task_rows.append(item)

        run_rows = [row_dict(r) for r in conn.execute(
            """
            SELECT * FROM runs
            WHERE campaign_id=? AND status='running'
            ORDER BY started_at DESC, id DESC
            LIMIT 80
            """,
            (campaign_id,),
        ).fetchall()]

        ev_rows = [row_dict(r) for r in conn.execute(
            """
            SELECT id, ts, type, message, agent_role, node_id, payload_json
            FROM events
            WHERE campaign_id=? OR campaign_id IS NULL
            ORDER BY id DESC
            LIMIT 1000
            """,
            (campaign_id,),
        ).fetchall()]
        for event in ev_rows:
            event["payload"] = loads(event.pop("payload_json"), {})

        by_agent: dict[str, list[dict]] = {}
        agent_event_counts: Counter = Counter()
        for event in ev_rows:
            role = event.get("agent_role") or "unknown"
            by_agent.setdefault(role, []).append(event)
            agent_event_counts[role] += 1

        known_roles = {a["role"] for a in agent_rows}
        for role in sorted(by_agent):
            if role not in known_roles:
                agent_rows.append(
                    {
                        "role": role,
                        "label": display_names.get(role, role.replace("_", " ").title()),
                        "kind": "sidecar",
                        "timeframe": None,
                        "status": "standby",
                        "current_task_id": None,
                        "stats": {},
                        "updated_at": "",
                    }
                )

        display_events: list[dict] = []
        for events_for_role in by_agent.values():
            display_events.extend(events_for_role[:limit_per_agent])

        node_ids = {
            *(str(t.get("node_id")) for t in task_rows if t.get("node_id")),
            *(str(r.get("node_id")) for r in run_rows if r.get("node_id")),
            *(str(e.get("node_id")) for e in display_events if e.get("node_id")),
        }
        node_map = load_node_map(conn, node_ids)

    tasks_by_id = {t["id"]: t for t in task_rows}
    agent_rows.sort(key=lambda a: (phase_for(a.get("role", ""), a.get("kind")), a.get("role", "")))
    phases_in_use = {phase_for(a.get("role", ""), a.get("kind")) for a in agent_rows}

    for phase in sorted(phases_in_use):
        label, color = phases.get(phase, (phase.title(), "#64748b"))
        add_node(
            phase_id(phase),
            label,
            color=color_obj("#111827", color),
            shape="box",
            borderWidth=2,
            font={"size": 13, "color": "#e5e7eb", "face": "system-ui"},
            size=20,
        )
        add_edge("__root__", phase_id(phase), color={"color": color, "opacity": 0.32}, width=2)

    def add_agent_node(agent: dict) -> None:
        role = agent.get("role") or "unknown"
        status = str(agent.get("status") or "standby")
        color = agent_colors.get(role, "#64748b")
        active = status == "running"
        queued = status == "queued"
        label = display_names.get(role, agent.get("label") or short(role, 16))
        event_count = agent_event_counts.get(role, 0)
        phase = phase_for(role, agent.get("kind"))
        add_node(
            agent_id(role),
            f"{short(label, 16)}\n{status.upper()}\n{event_count} eventos",
            color=color_obj("#0f172a", "#f8fafc" if active else color),
            shape="dot",
            size=32 if active else 27 if queued else min(25, 14 + event_count // 5),
            borderWidth=5 if active else 4 if queued else 2,
            font={"size": 12, "color": "#f8fafc", "face": "system-ui"},
            shadow={"enabled": bool(active or queued), "color": color, "size": 22 if active else 14, "x": 0, "y": 0},
            kind="agent",
            title=title(
                agent.get("label") or role,
                f"role={role}",
                f"status={status}",
                f"task={agent.get('current_task_id') or '-'}",
                (agent.get("stats") or {}).get("bubble") or "",
            ),
        )
        add_edge(phase_id(phase), agent_id(role), color={"color": color, "opacity": 0.45}, width=2 if active else 1)

    for agent in agent_rows:
        add_agent_node(agent)

    handled_tasks: set[str] = set()
    for agent in agent_rows:
        role = agent.get("role") or "unknown"
        status = str(agent.get("status") or "standby")
        if status not in {"running", "queued"}:
            continue
        current_task_id = agent.get("current_task_id")
        task = tasks_by_id.get(current_task_id) if current_task_id else None
        if task:
            handled_tasks.add(task["id"])
        node_id = str(task.get("node_id")) if task and task.get("node_id") else None
        stats = agent.get("stats") or {}
        active = status == "running"
        queued = status == "queued"
        edge_color = "#5dd39e" if active else "#d6a84d"
        action = task.get("task_type") if task else stats.get("phase") or current_task_id or "work"

        if node_id and node_id in node_map:
            graph_id = add_strategy_path(node_id, active=active, queued=queued)
            if graph_id:
                active_graph_ids.add(graph_id)
                add_edge(
                    agent_id(role),
                    graph_id,
                    color={"color": edge_color, "opacity": 0.92},
                    width=5 if active else 3,
                    label="testando" if task else "atuando",
                    font={"size": 10, "color": edge_color, "face": "system-ui"},
                )
                target_node = node_map[node_id]
                focus.append(
                    {
                        "agent": agent.get("label") or role,
                        "role": role,
                        "status": status,
                        "action": action,
                        "node": target_node.get("name") or target_node.get("code") or node_id,
                        "node_id": node_id,
                        "graph_node_id": graph_id,
                        "timeframe": target_node.get("timeframe"),
                        "detail": stats.get("bubble") or (f"{task.get('status')} {task.get('task_type')}" if task else ""),
                    }
                )
        else:
            work_id = f"work::{role}::{current_task_id or action or status}"
            label = "Pesquisando" if "research" in str(action) else "Trabalhando"
            add_node(
                work_id,
                f"{label}\n{short(action, 26)}",
                color=color_obj("#10251b" if active else "#2a2110", edge_color),
                shape="diamond",
                size=24 if active else 18,
                borderWidth=5 if active else 3,
                font={"size": 12, "color": "#e2e8f0", "face": "system-ui"},
                shadow={"enabled": bool(active or queued), "color": edge_color, "size": 22 if active else 14, "x": 0, "y": 0},
                title=title(agent.get("label") or role, action, stats.get("bubble") or ""),
            )
            add_edge(agent_id(role), work_id, color={"color": edge_color, "opacity": 0.9}, width=5 if active else 3)
            active_graph_ids.add(work_id)
            focus.append(
                {
                    "agent": agent.get("label") or role,
                    "role": role,
                    "status": status,
                    "action": action,
                    "node": label,
                    "node_id": None,
                    "graph_node_id": work_id,
                    "timeframe": agent.get("timeframe"),
                    "detail": stats.get("bubble") or "",
                }
            )

    for task in task_rows:
        if task["id"] in handled_tasks:
            continue
        role = task.get("agent_role") or "unknown"
        node_id = str(task.get("node_id")) if task.get("node_id") else None
        active = task.get("status") == "running"
        queued = task.get("status") == "queued"
        edge_color = "#5dd39e" if active else "#d6a84d"
        if node_id and node_id in node_map:
            graph_id = add_strategy_path(node_id, active=active, queued=queued)
            if graph_id:
                add_edge(agent_id(role), graph_id, color={"color": edge_color, "opacity": 0.78}, width=4 if active else 2, dashes=queued)

    for role, events_for_role in by_agent.items():
        color = agent_colors.get(role, "#64748b")
        type_counts = Counter(str(event.get("type") or "?") for event in events_for_role)
        for type_name, qty in type_counts.most_common(3):
            summary_id = f"summary::{role}::{type_name}"
            bg, border = event_color(type_name)
            add_node(
                summary_id,
                f"{short(type_name.replace('_', ' '), 22)}\nx {qty}",
                color=color_obj(bg, border),
                shape="dot",
                size=9 + min(10, qty // 4),
                font={"size": 9, "color": "#cbd5e1", "face": "system-ui"},
                title=title(f"{qty} eventos", type_name),
            )
            add_edge(agent_id(role), summary_id, color={"color": color, "opacity": 0.24}, width=1, dashes=True)

        for event in events_for_role[:limit_per_agent]:
            event_type = str(event.get("type") or "?")
            bg, border = event_color(event_type)
            event_id = f"event::{event.get('id')}"
            add_node(
                event_id,
                f"{short(event_type.replace('_', ' '), 20)}\n{short(event.get('ts'), 19)}",
                color=color_obj(bg, border),
                shape="dot",
                size=10,
                borderWidth=2,
                font={"size": 9, "color": "#cbd5e1", "face": "system-ui"},
                title=title(event.get("ts"), event_type, event.get("message") or ""),
            )
            event_node_id = str(event.get("node_id")) if event.get("node_id") else None
            if event_node_id and event_node_id in node_map:
                target_id = add_strategy_node(node_map[event_node_id], active=False)
                add_edge(agent_id(role), target_id, color={"color": border, "opacity": 0.25}, width=1)
                add_edge(target_id, event_id, color={"color": border, "opacity": 0.45}, width=1, dashes=True)
            else:
                add_edge(agent_id(role), event_id, color={"color": border, "opacity": 0.45}, width=1, dashes=True)

    try:
        active_file = Path("src/logs/research/active_strategies.json")
        if active_file.exists():
            active_payload = json.loads(active_file.read_text(encoding="utf-8-sig"))
            strategies = active_payload.get("strategies") or []
            if strategies:
                if agent_id("auto_deploy") not in node_by_id:
                    add_agent_node(
                        {
                            "role": "auto_deploy",
                            "label": "Auto Deploy",
                            "kind": "deploy",
                            "timeframe": None,
                            "status": "standby",
                            "current_task_id": None,
                            "stats": {},
                        }
                    )
                for strategy in strategies[:12]:
                    sid = f"paper::{str(strategy.get('id') or '?')[:32]}"
                    asset = strategy.get("asset") or strategy.get("symbol") or "?"
                    engine = strategy.get("engine") or "?"
                    timeframe = strategy.get("timeframe") or "?"
                    score = float(strategy.get("score") or 0.0)
                    add_node(
                        sid,
                        f"{short(asset, 12)} {short(engine, 12)}\n{timeframe} / {score:.2f}",
                        color=color_obj("#102033", "#3b82f6"),
                        shape="box",
                        size=14,
                        borderWidth=2,
                        font={"size": 10, "color": "#dbeafe", "face": "system-ui"},
                        rawNodeId=strategy.get("source_node_id") or strategy.get("node_id"),
                        title=title("Ativa no bot paper", strategy.get("source_node_id") or strategy.get("node_id") or ""),
                    )
                    add_edge(agent_id("auto_deploy"), sid, color={"color": "#3b82f6", "opacity": 0.5}, width=1, dashes=True)
    except Exception:
        pass

    stats = {
        "nodes": len(nodes),
        "edges": len(edges),
        "agents": len(agent_rows),
        "running_agents": sum(1 for a in agent_rows if a.get("status") == "running"),
        "queued_agents": sum(1 for a in agent_rows if a.get("status") == "queued"),
        "active_targets": len(active_graph_ids),
        "recent_events": sum(agent_event_counts.values()),
    }
    return {"ok": True, "nodes": nodes, "edges": edges, "focus": focus, "stats": stats}


@app.get("/api/agents_graph")
def api_agents_graph(limit_per_agent: int = Query(default=8, ge=1, le=25)) -> dict:
    """Grafo focado nos AGENTES e suas atividades recentes.

    Retorna {nodes:[...], edges:[...]} no formato vis-network. Diferente do
    /api/mindmap (arvore de 1000+ hipoteses), este foca no que cada agente
    fez recentemente -- legivel mesmo com muito historico.

    Niveis:
      L0: root (Atlas)
      L1: agentes (hypothesis_generator, auto_validator, critic_agent, hyper_agent,
                   auto_deploy, drift_agent, market_regime_agent, policy_agent)
      L2: atividade recente de cada agente (truncado em limit_per_agent)
    """
    from collections import Counter
    return _build_agents_action_graph(limit_per_agent)

    AGENT_COLORS = {
        "hypothesis_generator": "#f59e0b",  # amber
        "auto_validator":       "#10b981",  # green
        "critic_agent":         "#a78bfa",  # violet
        "hyper_agent":          "#06b6d4",  # cyan
        "auto_deploy":          "#3b82f6",  # blue
        "drift_agent":          "#ef4444",  # red
        "market_regime_agent":  "#84cc16",  # lime
        "policy_agent":         "#ec4899",  # pink
        "archivist_agent":      "#94a3b8",  # slate
        "manifest_agent":       "#fbbf24",  # yellow
        "infra_data_agent":     "#64748b",  # slate-500
    }
    DISPLAY_NAMES = {
        "hypothesis_generator": "Hypothesis",
        "auto_validator":       "Validator",
        "critic_agent":         "Critic v4",
        "hyper_agent":          "Hyper",
        "auto_deploy":          "Deploy",
        "drift_agent":          "Drift",
        "market_regime_agent":  "Regime",
        "policy_agent":         "Policy",
        "archivist_agent":      "Archivist",
        "manifest_agent":       "Manifest",
        "infra_data_agent":     "InfraData",
    }

    nodes: list[dict] = []
    edges: list[dict] = []
    seen = set()

    def add_node(node_id: str, label: str, **kwargs):
        if node_id in seen:
            return
        seen.add(node_id)
        nodes.append({"id": node_id, "label": label, **kwargs})

    def add_edge(source: str, target: str, **kwargs):
        edges.append({"from": source, "to": target, **kwargs})

    # L0: root
    add_node("__root__", "Atlas", color="#fbbf24", size=42, font={"size": 22, "color": "#111", "face": "system-ui"}, shape="dot", borderWidth=3)

    with connect() as conn:
        # Eventos recentes (24h) agrupados por agent_role
        ev_rows = conn.execute(
            """
            SELECT id, ts, type, message, agent_role, node_id, payload_json
            FROM events
            WHERE ts > datetime('now','-1 day')
            ORDER BY ts DESC
            LIMIT 800
            """
        ).fetchall()

        by_agent: dict[str, list] = {}
        agent_event_counts: Counter = Counter()
        for r in ev_rows:
            role = r["agent_role"] or "unknown"
            by_agent.setdefault(role, []).append(r)
            agent_event_counts[role] += 1

        # L1: agente
        for role in sorted(by_agent.keys()):
            evs = by_agent[role]
            label = DISPLAY_NAMES.get(role, role[:14])
            color = AGENT_COLORS.get(role, "#64748b")
            count = len(evs)
            size = min(36, 14 + count // 4)
            add_node(
                role, f"{label}\n{count} eventos",
                color=color, size=size,
                font={"size": 14, "color": "#fff", "face": "system-ui"},
                shape="dot", borderWidth=2,
            )
            add_edge("__root__", role, color={"color": color, "opacity": 0.4}, width=2)

            # L2: top atividades distintas (dedup por type, top N)
            type_counts: Counter = Counter()
            type_msgs: dict[str, str] = {}
            for ev in evs:
                t = (ev["type"] or "?")[:60]
                type_counts[t] += 1
                if t not in type_msgs and ev["message"]:
                    type_msgs[t] = ev["message"][:90]
            for type_name, qty in type_counts.most_common(limit_per_agent):
                act_id = f"{role}::{type_name}"
                short = type_name.replace("_", " ").replace(role.split("_")[0] + " ", "")
                lbl = f"{short}\n× {qty}"
                add_node(
                    act_id, lbl,
                    color={"background": color, "border": color, "highlight": {"background": "#fff", "border": color}},
                    size=8 + min(14, qty // 2),
                    font={"size": 11, "color": "#cbd5e1", "face": "system-ui"},
                    shape="dot",
                    title=type_msgs.get(type_name, "")[:200],
                )
                add_edge(role, act_id, color={"color": color, "opacity": 0.3}, width=1)

        # Estrategias ativas (deploy -> active_strategies vinculadas)
        # Fonte: arquivo active_strategies.json se existir
        try:
            from pathlib import Path as _P
            asf = _P("src/logs/research/active_strategies.json")
            if asf.exists():
                import json as _j
                act = _j.loads(asf.read_text(encoding="utf-8-sig"))
                strats = act.get("strategies") or []
                if strats and "auto_deploy" in by_agent:
                    for s in strats[:12]:
                        sid = f"strat::{s.get('id','?')[:18]}"
                        asset = (s.get("asset") or "?")
                        engine = (s.get("engine") or "?")
                        tf = (s.get("timeframe") or "?")
                        score = s.get("score") or 0.0
                        lbl = f"{asset} {engine[:10]}\n{tf} • {score:.2f}"
                        add_node(
                            sid, lbl,
                            color={"background": "#1e293b", "border": "#3b82f6"},
                            size=11, font={"size": 11, "color": "#dbeafe", "face": "system-ui"},
                            shape="box",
                            title=f"{s.get('source_node_id','')}",
                        )
                        add_edge("auto_deploy", sid, color={"color": "#3b82f6", "opacity": 0.5}, width=1, dashes=True)
        except Exception:
            pass

    return {"ok": True, "nodes": nodes, "edges": edges, "stats": {"nodes": len(nodes), "edges": len(edges)}}


@app.get("/api/campaigns")
def api_campaigns() -> dict:
    with connect() as conn:
        return {"ok": True, **list_campaigns(conn)}


@app.post("/api/campaigns/new")
def api_new_campaign(label: str | None = Query(default=None, max_length=80)) -> dict:
    with connect() as conn:
        result = create_campaign(conn, label=label)
    return {"ok": True, "campaign": result}


@app.get("/api/agents/llm")
def api_llm_agents() -> dict:
    return {"ok": True, **llm_status()}


@app.get("/api/agents/traces")
def api_llm_traces(limit: int = Query(default=80, ge=1, le=300)) -> dict:
    with connect() as conn:
        return {"ok": True, "traces": list_llm_traces(conn, limit=limit)}


@app.get("/api/agents/broker")
def api_broker_status() -> dict:
    """Status do Context Broker (sessao unica + snapshot da arvore)."""
    from .context_broker import status as _broker_status
    return {"ok": True, **_broker_status()}


@app.get("/api/tree/focus")
def api_tree_focus() -> dict:
    """Estado do tree_focus (open_count, completion_ratio, alvos prioritarios)."""
    from .tree_focus import assess, should_open_new_branch, priority_targets
    with connect() as conn:
        campaign_id = get_active_campaign_id(conn)
        a = assess(conn, campaign_id)
        allow, reason, _ = should_open_new_branch(conn, campaign_id)
        targets = priority_targets(conn, campaign_id, limit=12)
    return {
        "ok": True,
        "campaign_id": campaign_id,
        "assessment": a,
        "allow_new_root": allow,
        "decision_reason": reason,
        "priority_targets": targets,
    }


@app.post("/api/import/legacy")
def api_import_legacy() -> dict:
    with connect() as conn:
        seed_base_tree(conn)
        counts = import_legacy(conn, LEGACY_ROOT)
    return {"ok": True, "counts": counts}


@app.post("/api/tree/seed")
def api_seed_tree() -> dict:
    with connect() as conn:
        ensure_agents(conn)
        created = seed_base_tree(conn)
        mark_agents_without_work_standby(conn)
    return {"ok": True, "created": created}


@app.post("/api/tasks/queue-scouts")
def api_queue_scouts(
    limit_nodes: int = Query(default=5, ge=1, le=25),
    probes_per_node: int = Query(default=60, ge=1, le=500),
    target_probe_count: int = Query(default=0, ge=0, le=5000),
) -> dict:
    created = queue_scout_tasks(
        limit_nodes=limit_nodes,
        probes_per_node=probes_per_node,
        target_probe_count=target_probe_count,
    )
    return {"ok": True, "queued": created}


def _researcher_inflight(conn, source_type: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM nodes
        WHERE source_type=?
          AND id != 'root'
          AND id NOT LIKE 'tf:%'
          AND status IN ('candidate','promising','expand','queued','running')
        """,
        (source_type,),
    ).fetchone()
    try:
        return int(row[0])
    except (TypeError, ValueError, IndexError):
        return 0


@app.post("/api/research/web")
def api_web_research(
    max_new: int = Query(default=1, ge=1, le=5),
) -> dict:
    with connect() as conn:
        ensure_agents(conn)
        inflight = _researcher_inflight(conn, "web_research")
        if inflight > 0:
            mark_agents_without_work_standby(conn)
            return {
                "ok": False,
                "skipped": True,
                "reason": "researcher_busy",
                "inflight": inflight,
                "message": f"Pesquisa web aguardando validacao ({inflight} em teste).",
            }
        result = create_web_hypothesis_batch(conn, max_new=max_new)
        mark_agents_without_work_standby(conn)
    return {"ok": True, **result}


@app.post("/api/research/llm")
def api_llm_research(
    max_new: int = Query(default=3, ge=1, le=5),
) -> dict:
    with connect() as conn:
        ensure_agents(conn)
        inflight = _researcher_inflight(conn, "llm_agent")
        if inflight > 0:
            mark_agents_without_work_standby(conn)
            return {
                "ok": False,
                "skipped": True,
                "reason": "researcher_busy",
                "inflight": inflight,
                "message": f"Agente LLM aguardando validacao ({inflight} em teste).",
            }
        result = create_llm_hypothesis_batch(conn, max_new=max_new)
        mark_agents_without_work_standby(conn)
    return {"ok": True, **result}


@app.post("/api/cycle/start")
def api_start_cycle(
    workers: int = Query(default=DEFAULT_WORKERS, ge=1, le=max(DEFAULT_WORKERS * 2, 1)),
    limit_nodes: int = Query(default=5, ge=1, le=25),
    probes_per_node: int = Query(default=60, ge=1, le=500),
    max_tasks: int = Query(default=5, ge=1, le=25),
    continuous: bool = Query(default=False),
) -> dict:
    return start_cycle_background(
        workers=workers,
        limit_nodes=limit_nodes,
        probes_per_node=probes_per_node,
        max_tasks=max_tasks,
        continuous=continuous,
    )


@app.post("/api/cycle/stop")
def api_stop_cycle() -> dict:
    return stop_cycle()


@app.post("/api/manifest/{node_id:path}")
def api_manifest(node_id: str, force: bool = False) -> dict:
    try:
        return {"ok": True, **generate_manifest(node_id, force=force)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"node not found: {node_id}") from exc


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "sim", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _select_port(start_port: int = 8765) -> int:
    if _port_available(start_port):
        return start_port
    if not _env_bool("AETHER_ALLOW_PORT_FALLBACK", False):
        raise RuntimeError(
            f"porta {start_port} ja esta em uso; pare a instancia atual com scripts\\stop_aether.ps1 "
            "ou defina AETHER_ALLOW_PORT_FALLBACK=1 para subir em outra porta"
        )
    port = start_port + 1
    while not _port_available(port):
        port += 1
    return port


def main() -> None:
    try:
        port = _select_port(_env_int("AETHER_PORT", 8765))
    except RuntimeError as exc:
        print(f"Aether nao iniciado: {exc}")
        raise SystemExit(1) from exc
    print(f"Aether running at http://127.0.0.1:{port}")
    uvicorn.run("src.research.aether.server:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    main()
