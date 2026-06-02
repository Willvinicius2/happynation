from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import DEFAULT_WORKERS, LEGACY_ROOT, db_path, static_dir
from .campaigns import create_campaign, get_active_campaign_id, list_campaigns
from src.research.active import deploy_record

from .db import add_event, connect, get_setting, init_db, loads, set_setting
from .legacy_import import import_legacy
from .llm_hypothesis import create_llm_hypothesis_batch
from .llm_runtime import llm_status
from .llm_traces import list_llm_traces
from .manifest import generate_manifest
from .auto_deploy import DEFAULT_RISK
from .orchestrator import (
    bootstrap,
    cycle_mode,
    is_cycle_running,
    is_stop_requested,
    prune_weak_branches,
    queue_scout_tasks,
    start_cycle_background,
    stop_cycle,
)
from .ranking import active_manifest_summary, build_strategy_ranking, ranking_summary
from .repository import ensure_agents, list_state, mark_agents_without_work_standby, seed_base_tree
from .web_research import create_web_hypothesis_batch


RANKING_RESET_PREFIX = "ranking_hidden_node_ids"


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


@app.middleware("http")
async def no_cache_ui(request, call_next):
    response = await call_next(request)
    if request.url.path in {"/", "/pet"} or request.url.path.startswith(("/api/", "/static/")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


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
        "source": "aether_manual_ranking_deploy",
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
        "risk": dict(DEFAULT_RISK),
    }
    return record, None


def _deploy_record_uncapped(record: dict) -> dict:
    try:
        return deploy_record(record, max_per_family=0)
    except TypeError as exc:
        if "max_per_family" not in str(exc):
            raise
        return deploy_record(record)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(static_dir() / "index.html")


@app.get("/pet")
def pet() -> FileResponse:
    return FileResponse(static_dir() / "pet.html")


@app.get("/api/state")
def api_state() -> dict:
    with connect() as conn:
        try:
            ensure_agents(conn)
            if not is_cycle_running():
                mark_agents_without_work_standby(conn)
        except sqlite3.OperationalError:
            # `database is locked` em GET nao deve quebrar a UI; cycle writer
            # vai eventualmente fazer ensure_agents no proprio fluxo. Skip silencioso.
            pass
        try:
            max_nodes = int(os.environ.get("AETHER_UI_STATE_MAX_NODES", "850"))
        except (TypeError, ValueError):
            max_nodes = 850
        state = list_state(conn, max_nodes=max_nodes, compact_metrics=True)
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


def _recent_discoveries(
    conn,
    campaign_id: str,
    *,
    limit: int = 24,
    reset_after: str | None = None,
) -> list[dict]:
    """Recent pipeline activity for the ranking screen.

    The paper ranking only shows fully validated strategies. This companion
    feed keeps brand-new LLM/local discoveries visible while they move through
    scout, validation, critic and deploy gates.
    """
    deployed_ids = set()
    try:
        from src.research.active import list_active

        for rec in list_active():
            for key in ("source_node_id", "node_id"):
                node_id = rec.get(key)
                if node_id:
                    deployed_ids.add(str(node_id))
    except Exception:
        deployed_ids = set()

    params: list[object] = []
    campaign_where = ""
    if campaign_id != "all":
        campaign_where = "AND campaign_id=?"
        params.append(campaign_id)
    reset_where = ""
    if reset_after:
        reset_where = "AND created_at > ?"
        params.append(reset_after)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT id, campaign_id, name, hypothesis, causal_mechanism,
               mutation_from_parent, status, score, timeframe, agent_role,
               result_summary, prune_reason, metrics_json, source_type,
               source_path, created_at, updated_at
        FROM nodes
        WHERE id != 'root'
          AND id NOT LIKE 'tf:%'
          AND status NOT IN ('pruned', 'paused', 'archive')
          {campaign_where}
          {reset_where}
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    out: list[dict] = []
    for row in rows:
        metrics = loads(row["metrics_json"], {})
        best = metrics.get("best_probe") if isinstance(metrics.get("best_probe"), dict) else {}
        pnl_pct = metrics.get("pnl_pct")
        if pnl_pct is None:
            pnl_pct = best.get("pnl_pct")
        trade_count = metrics.get("trade_count")
        if trade_count is None:
            trade_count = best.get("trade_count")
        mdd_pct = metrics.get("mdd_pct")
        if mdd_pct is None:
            mdd_pct = best.get("mdd_pct")
        stage = "explorando"
        if row["status"] == "validated" and metrics.get("critic_status") == "approved":
            stage = "critic aprovado"
        elif row["status"] == "validated":
            stage = "validada"
        elif best:
            stage = "backtest"
        elif row["status"] in {"candidate", "visited"}:
            stage = "scout"
        out.append(
            {
                "node_id": row["id"],
                "campaign_id": row["campaign_id"],
                "name": row["name"],
                "hypothesis": row["hypothesis"],
                "causal_mechanism": row["causal_mechanism"],
                "mutation_from_parent": row["mutation_from_parent"],
                "status": row["status"],
                "stage": stage,
                "score": round(float(row["score"] or 0.0), 4),
                "asset": str(best.get("symbol") or "").upper() or None,
                "engine": best.get("engine"),
                "timeframe": best.get("timeframe") or row["timeframe"],
                "pnl_pct": round(float(pnl_pct), 4) if pnl_pct is not None else None,
                "trade_count": int(float(trade_count)) if trade_count is not None else None,
                "mdd_pct": round(float(mdd_pct), 4) if mdd_pct is not None else None,
                "critic_status": metrics.get("critic_status"),
                "source_type": row["source_type"],
                "source_path": row["source_path"],
                "deployed": row["id"] in deployed_ids,
                "summary": row["result_summary"] or row["prune_reason"] or "",
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    return out


def _discovery_summary(rows: list[dict], *, new_only_since: str | None = None) -> dict:
    return {
        "recent": len(rows),
        "openai": sum(1 for row in rows if str(row.get("source_path") or "").lower() == "openai"),
        "with_backtest": sum(1 for row in rows if row.get("pnl_pct") is not None),
        "validated": sum(1 for row in rows if row.get("status") == "validated"),
        "deployed": sum(1 for row in rows if row.get("deployed")),
        "new_only_since": new_only_since,
    }


def _ranking_reset_key(campaign_id: str) -> str:
    return f"{RANKING_RESET_PREFIX}:{campaign_id}"


def _ranking_reset_state(conn, campaign_id: str) -> dict:
    state = get_setting(conn, _ranking_reset_key(campaign_id), default={})
    return state if isinstance(state, dict) else {}


def _apply_ranking_reset(conn, campaign_id: str, rows: list[dict]) -> tuple[list[dict], dict]:
    reset = _ranking_reset_state(conn, campaign_id)
    hidden_ids = {str(node_id) for node_id in reset.get("hidden_node_ids", []) if node_id}
    reset_at = reset.get("reset_at")
    if not hidden_ids and not reset_at:
        return rows, {
            "active": False,
            "hidden_count": 0,
            "reset_at": reset_at,
            "filtered_count": 0,
        }
    filtered = [
        row for row in rows
        if str(row.get("node_id") or "") not in hidden_ids
        and (not reset_at or str(row.get("created_at") or "") > str(reset_at))
    ]
    return filtered, {
        "active": True,
        "hidden_count": len(hidden_ids),
        "reset_at": reset_at,
        "filtered_count": len(rows) - len(filtered),
    }


def _ranking_created_after(reset: dict) -> str | None:
    if not reset:
        return None
    reset_at = reset.get("reset_at")
    return str(reset_at) if reset_at else None


@app.get("/api/ranking")
def api_ranking(
    capital: float = Query(default=1000.0, gt=0, le=10_000_000),
    limit: int = Query(default=250, ge=1, le=250),
    campaign_id: str = Query(default="active"),
    dedupe: bool = Query(default=True),
    include_deployed: bool = Query(default=True),
    hide_blocked: bool = Query(default=True),
    discoveries_limit: int = Query(default=24, ge=0, le=80),
) -> dict:
    with connect() as conn:
        campaign_v = get_active_campaign_id(conn) if campaign_id == "active" else campaign_id
        reset_before = _ranking_reset_state(conn, campaign_v)
        created_after = _ranking_created_after(reset_before)
        rows = build_strategy_ranking(
            conn,
            starting_capital=capital,
            limit=250,
            campaign_id=campaign_v,
            created_after=created_after,
            dedupe=dedupe,
            include_deployed=include_deployed,
            hide_blocked=hide_blocked,
        )
        rows, reset = _apply_ranking_reset(conn, campaign_v, rows)
        rows = rows[:limit]
        reset_after = _ranking_created_after(reset)
        discoveries = _recent_discoveries(
            conn,
            campaign_v,
            limit=discoveries_limit,
            reset_after=reset_after,
        ) if discoveries_limit else []
        campaigns = list_campaigns(conn)
        summary = ranking_summary(rows)
        summary.update(active_manifest_summary())
        summary["deployed_in_ranking"] = summary.get("deployed", 0)
        summary["active_manifest_outside_ranking"] = max(
            0,
            int(summary.get("active_manifest_total") or 0) - int(summary.get("deployed_in_ranking") or 0),
        )
        latest_ranked_at = max((str(row.get("updated_at") or "") for row in rows), default="")
        summary["latest_ranked_at"] = latest_ranked_at or None
    return {
        "ok": True,
        "capital": capital,
        "campaign_id": campaign_v,
        "dedupe": dedupe,
        "include_deployed": include_deployed,
        "hide_blocked": hide_blocked,
        "count": len(rows),
        "summary": summary,
        "ranking_reset": reset,
        "discovery_summary": _discovery_summary(discoveries, new_only_since=reset_after),
        "discoveries": discoveries,
        "campaigns": campaigns,
        "strategies": rows,
    }


@app.post("/api/ranking/reset")
def api_reset_ranking(
    capital: float = Query(default=1000.0, gt=0, le=10_000_000),
    campaign_id: str = Query(default="active"),
    dedupe: bool = Query(default=True),
) -> dict:
    with connect() as conn:
        campaign_v = get_active_campaign_id(conn) if campaign_id == "active" else campaign_id
        rows = build_strategy_ranking(
            conn,
            starting_capital=capital,
            limit=250,
            campaign_id=campaign_v,
            dedupe=dedupe,
            include_deployed=True,
            hide_blocked=False,
        )
        hidden_ids = sorted({str(row.get("node_id")) for row in rows if row.get("node_id")})
        payload = {
            "reset_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hidden_node_ids": hidden_ids,
            "hidden_count": len(hidden_ids),
        }
        set_setting(conn, _ranking_reset_key(campaign_v), payload)
        add_event(
            conn,
            type="ranking_reset",
            agent_role="manifest_agent",
            message=f"Ranking visual zerado: {len(hidden_ids)} estrategias antigas ocultas.",
            payload={"campaign_id": campaign_v, "hidden_count": len(hidden_ids)},
        )
    return {"ok": True, "campaign_id": campaign_v, **payload}


@app.delete("/api/ranking/reset")
def api_clear_ranking_reset(campaign_id: str = Query(default="active")) -> dict:
    with connect() as conn:
        campaign_v = get_active_campaign_id(conn) if campaign_id == "active" else campaign_id
        set_setting(conn, _ranking_reset_key(campaign_v), {})
        add_event(
            conn,
            type="ranking_reset_cleared",
            agent_role="manifest_agent",
            message="Filtro de reset do ranking removido.",
            payload={"campaign_id": campaign_v},
        )
    return {"ok": True, "campaign_id": campaign_v}


@app.post("/api/ranking/deploy")
def api_deploy_ranking(
    capital: float = Query(default=1000.0, gt=0, le=10_000_000),
    limit: int = Query(default=250, ge=1, le=250),
    campaign_id: str = Query(default="active"),
    max_deploy: int = Query(default=50, ge=1, le=150),
    include_needs_validation: bool = Query(default=False),
    dedupe: bool = Query(default=True),
    force: bool = Query(default=False),
) -> dict:
    with connect() as conn:
        campaign_v = get_active_campaign_id(conn) if campaign_id == "active" else campaign_id
        reset_before = _ranking_reset_state(conn, campaign_v)
        rows = build_strategy_ranking(
            conn,
            starting_capital=capital,
            limit=limit,
            campaign_id=campaign_v,
            created_after=_ranking_created_after(reset_before),
            dedupe=dedupe,
        )
        rows, reset = _apply_ranking_reset(conn, campaign_v, rows)
        allowed = {"paper_ready"}
        eligible = [row for row in rows if row.get("readiness") in allowed][:max_deploy]
        deployed = []
        skipped = []
        for ranking_row in eligible:
            deploy_blocker = str(ranking_row.get("deploy_blocker") or "")
            if (
                deploy_blocker
                and not deploy_blocker.startswith("Aguardando")
                and not deploy_blocker.startswith("Cap rotacionavel")
                and not force
            ):
                skipped.append({"node_id": ranking_row.get("node_id"), "reason": deploy_blocker})
                continue
            record, reason = _active_record_from_node(conn, ranking_row)
            if reason or record is None:
                skipped.append({"node_id": ranking_row.get("node_id"), "reason": reason or "registro invalido"})
                continue
            deployed.append(_deploy_record_uncapped(record))
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
                "force": force,
                "ranking_reset": reset,
            },
        )
    return {
        "ok": True,
        "campaign_id": campaign_v,
        "ranking_reset": reset,
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


from .server_agents_graph import _build_agents_action_graph


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
    window_min = max(1, _env_int("AETHER_INFLIGHT_WINDOW_MINUTES", 30))
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM nodes
        WHERE source_type=?
          AND id != 'root'
          AND id NOT LIKE 'tf:%'
          AND status IN ('candidate','promising','expand','queued','running')
          AND COALESCE(updated_at, created_at) >= ?
        """,
        (source_type, cutoff),
    ).fetchone()
    try:
        return int(row["c"] if hasattr(row, "keys") else row[0])
    except (TypeError, ValueError, IndexError):
        return 0


@app.post("/api/research/web")
def api_web_research(
    max_new: int = Query(default=1, ge=1, le=5),
) -> dict:
    try:
        with connect() as conn:
            ensure_agents(conn)
            inflight = _researcher_inflight(conn, "web_research")
            inflight_tolerance = max(0, _env_int("AETHER_WEB_INFLIGHT_TOLERANCE", 24))
            if inflight > inflight_tolerance:
                mark_agents_without_work_standby(conn)
                return {
                    "ok": False,
                    "skipped": True,
                    "reason": "researcher_busy",
                    "inflight": inflight,
                    "tolerance": inflight_tolerance,
                    "message": f"Pesquisa web aguardando validacao ({inflight} em teste, tol={inflight_tolerance}).",
                }
            result = create_web_hypothesis_batch(conn, max_new=max_new)
            mark_agents_without_work_standby(conn)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            return {
                "ok": False,
                "skipped": True,
                "reason": "database_locked",
                "message": "SQLite ocupado por outro agente; tente novamente em alguns segundos.",
            }
        raise
    return {"ok": True, **result}


@app.post("/api/research/llm")
def api_llm_research(
    max_new: int = Query(default=3, ge=1, le=5),
) -> dict:
    try:
        with connect() as conn:
            ensure_agents(conn)
            inflight = _researcher_inflight(conn, "llm_agent")
            inflight_tolerance = max(0, _env_int("AETHER_LLM_INFLIGHT_TOLERANCE", 16))
            if inflight > inflight_tolerance:
                mark_agents_without_work_standby(conn)
                return {
                    "ok": False,
                    "skipped": True,
                    "reason": "researcher_busy",
                    "inflight": inflight,
                    "tolerance": inflight_tolerance,
                    "message": f"Agente LLM aguardando validacao ({inflight} em teste, tol={inflight_tolerance}).",
                }
            result = create_llm_hypothesis_batch(conn, max_new=max_new)
            mark_agents_without_work_standby(conn)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            return {
                "ok": False,
                "skipped": True,
                "reason": "database_locked",
                "message": "SQLite ocupado por outro agente; tente novamente em alguns segundos.",
            }
        raise
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


@app.post("/api/prune/weak")
def api_prune_weak(limit: int = Query(default=12, ge=1, le=50)) -> dict:
    with connect() as conn:
        result = prune_weak_branches(conn, limit=limit)
        mark_agents_without_work_standby(conn)
    return {"ok": True, **result}


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
