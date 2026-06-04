"""Builder do grafo de trabalho dos agentes para /api/agents_graph."""
from __future__ import annotations

from collections import Counter, defaultdict
import html
from typing import Any

from .campaigns import get_active_campaign_id
from .db import connect, loads
from .ranking import build_strategy_ranking
from .repository import AGENT_DEFINITIONS


AGENT_COLORS = {
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

DISPLAY_NAMES = {
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

STATUS_LABELS = {
    "running": "RODANDO",
    "queued": "FILA",
    "standby": "STANDBY",
    "failed": "FALHA",
}

STATUS_COLORS = {
    "running": ("#10251b", "#5dd39e"),
    "queued": ("#2a2110", "#d6a84d"),
    "standby": ("#0f172a", "#64748b"),
    "failed": ("#311612", "#ef4444"),
}


def _build_agents_action_graph(limit_per_agent: int) -> dict[str, Any]:
    """Build a graph that shows strategy nodes plus agent work."""

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_by_id: dict[str, dict[str, Any]] = {}
    edge_seen: set[tuple[str, str, str, bool]] = set()

    def add_node(node_id: str, label: str, **kwargs: Any) -> dict[str, Any]:
        item = {"id": node_id, "label": label, **kwargs}
        current = node_by_id.get(node_id)
        if current:
            current.update({k: v for k, v in item.items() if v is not None})
            return current
        node_by_id[node_id] = item
        nodes.append(item)
        return item

    def add_edge(source: str, target: str, **kwargs: Any) -> None:
        if not source or not target or source == target:
            return
        key = (source, target, str(kwargs.get("label") or ""), bool(kwargs.get("dashes")))
        if key in edge_seen:
            return
        edge_seen.add(key)
        edges.append({"from": source, "to": target, **kwargs})

    def row_dict(row: Any) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    def short(value: object, max_len: int = 34) -> str:
        text = str(value or "").replace("\n", " ").strip()
        return text if len(text) <= max_len else text[: max_len - 1] + "..."

    def title(*lines: object) -> str:
        return "<br>".join(html.escape(str(line)) for line in lines if line not in (None, ""))

    def color_obj(background: str, border: str, highlight: str | None = None) -> dict[str, Any]:
        return {
            "background": background,
            "border": border,
            "highlight": {"background": highlight or background, "border": "#f8fafc"},
        }

    def agent_id(role: str) -> str:
        return f"agent::{role}"

    def task_graph_id(task_id: str) -> str:
        return f"work::{task_id}"

    def event_graph_id(role: str, event_id: object) -> str:
        return f"last::{role}::{event_id}"

    def stage_graph_id(node_id: str, stage: str) -> str:
        return f"stage::{stage}::{node_id}"

    def passing_graph_id(node_id: str) -> str:
        return f"passing::{node_id}"

    def status_colors(status: str) -> tuple[str, str]:
        return STATUS_COLORS.get(status, STATUS_COLORS["standby"])

    def node_summary(raw_node_id: str | None, node_map: dict[str, dict[str, Any]]) -> str:
        if not raw_node_id:
            return ""
        node = node_map.get(raw_node_id)
        if not node:
            return raw_node_id
        parts = [
            node.get("name") or node.get("code") or raw_node_id,
            node.get("timeframe"),
            node.get("status"),
        ]
        return " / ".join(str(part) for part in parts if part)

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

    def load_node_summaries(conn: Any, ids: set[str]) -> dict[str, dict[str, Any]]:
        clean_ids = [str(node_id) for node_id in ids if node_id]
        if not clean_ids:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(clean_ids), 240):
            batch = clean_ids[start:start + 240]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT id, code, name, hypothesis, causal_mechanism,
                       mutation_from_parent, status, score, timeframe,
                       agent_role, prune_reason, result_summary,
                       metrics_json, source_type, source_path, created_at, updated_at
                FROM nodes
                WHERE id IN ({placeholders})
                """,
                batch,
            ).fetchall()
            for row in rows:
                item = row_dict(row)
                item["metrics"] = loads(item.pop("metrics_json"), {})
                out[row["id"]] = item
        return out

    focus: list[dict[str, Any]] = []
    passing_strategies: list[dict[str, Any]] = []

    with connect() as conn:
        campaign_id = get_active_campaign_id(conn)

        agent_rows = [row_dict(row) for row in conn.execute("SELECT * FROM agents ORDER BY kind, role").fetchall()]
        for agent in agent_rows:
            agent["stats"] = loads(agent.pop("stats_json"), {})
        known_agent_roles = {str(agent.get("role")) for agent in agent_rows}
        for role, label, kind, timeframe in AGENT_DEFINITIONS:
            if role not in known_agent_roles:
                agent_rows.append(
                    {
                        "role": role,
                        "label": label,
                        "kind": kind,
                        "timeframe": timeframe,
                        "status": "standby",
                        "current_task_id": None,
                        "stats": {},
                        "updated_at": "",
                    }
                )

        task_rows: list[dict[str, Any]] = []
        for row in conn.execute(
            """
            SELECT *
            FROM tasks
            WHERE campaign_id=? AND status IN ('running','queued')
            ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, priority DESC, updated_at DESC
            LIMIT 180
            """,
            (campaign_id,),
        ).fetchall():
            item = row_dict(row)
            item["payload"] = loads(item.pop("payload_json"), {})
            task_rows.append(item)

        event_rows: list[dict[str, Any]] = []
        for row in conn.execute(
            """
            SELECT id, ts, type, message, agent_role, node_id, payload_json
            FROM events
            WHERE campaign_id=? OR campaign_id IS NULL
            ORDER BY id DESC
            LIMIT 500
            """,
            (campaign_id,),
        ).fetchall():
            item = row_dict(row)
            item["payload"] = loads(item.pop("payload_json"), {})
            event_rows.append(item)

        node_ids = {
            *(str(task.get("node_id")) for task in task_rows if task.get("node_id")),
            *(str(event.get("node_id")) for event in event_rows if event.get("node_id")),
        }
        try:
            passing_strategies = [
                row
                for row in build_strategy_ranking(
                    conn,
                    starting_capital=1000.0,
                    limit=80,
                    campaign_id=campaign_id,
                    dedupe=True,
                    include_deployed=True,
                    hide_blocked=False,
                )
                if row.get("readiness") == "paper_ready"
            ]
            node_ids.update(str(row.get("node_id")) for row in passing_strategies if row.get("node_id"))
        except Exception:
            passing_strategies = []
        node_map = load_node_summaries(conn, node_ids)

    def compact_probe(probe: dict[str, Any]) -> dict[str, Any]:
        params = probe.get("params") if isinstance(probe.get("params"), dict) else {}
        return {
            "engine": probe.get("engine"),
            "symbol": probe.get("symbol"),
            "timeframe": probe.get("timeframe"),
            "params": params,
        }

    def metrics_summary(metrics: dict[str, Any]) -> dict[str, Any]:
        best = metrics.get("best_probe") if isinstance(metrics.get("best_probe"), dict) else {}
        return {
            "score": metrics.get("score"),
            "pnl_pct": metrics.get("pnl_pct", best.get("pnl_pct")),
            "trade_count": metrics.get("trade_count", best.get("trade_count")),
            "sharpe": metrics.get("sharpe", best.get("sharpe")),
            "mdd_pct": metrics.get("mdd_pct", best.get("mdd_pct")),
            "scout_count": metrics.get("scout_count"),
            "pass_count": metrics.get("pass_count"),
            "critic_status": metrics.get("critic_status"),
            "validation_stage": metrics.get("validation_stage"),
            "best_engine": best.get("engine"),
            "best_symbol": best.get("symbol"),
        }

    def best_probe(node: dict[str, Any]) -> dict[str, Any]:
        metrics = node.get("metrics") if isinstance(node.get("metrics"), dict) else {}
        best = metrics.get("best_probe") if isinstance(metrics.get("best_probe"), dict) else {}
        if best:
            return best
        specs = metrics.get("probe_specs") if isinstance(metrics.get("probe_specs"), list) else []
        for spec in specs:
            if isinstance(spec, dict):
                return spec
        return {}

    def params_text(params: Any, max_items: int = 3) -> str:
        if not isinstance(params, dict) or not params:
            return ""
        parts = []
        for key, value in list(params.items())[:max_items]:
            if key == "specs" and isinstance(value, list):
                engines = []
                for spec in value:
                    if isinstance(spec, dict) and spec.get("engine"):
                        engines.append(str(spec.get("engine")))
                if engines:
                    parts.append("+".join(dict.fromkeys(engines[:4])))
                else:
                    parts.append(f"{len(value)} specs")
                continue
            if isinstance(value, (dict, list, tuple)):
                parts.append(f"{key}={len(value)} itens")
                continue
            parts.append(f"{key}={value}")
        return ", ".join(parts)

    def strategy_payload(row: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
        probe = best_probe(node)
        metrics = node.get("metrics") if isinstance(node.get("metrics"), dict) else {}
        return {
            "node_id": row.get("node_id"),
            "name": row.get("name"),
            "status": row.get("status"),
            "readiness": row.get("readiness"),
            "score": row.get("score"),
            "timeframe": row.get("timeframe"),
            "agent_role": row.get("agent_role"),
            "pnl_pct": row.get("pnl_pct"),
            "sharpe": row.get("sharpe"),
            "mdd_pct": row.get("mdd_pct"),
            "trade_count": row.get("trade_count"),
            "deployed": row.get("deployed"),
            "deploy_blocker": row.get("deploy_blocker"),
            "summary": row.get("summary"),
            "hypothesis": node.get("hypothesis"),
            "causal_mechanism": node.get("causal_mechanism"),
            "mutation_from_parent": node.get("mutation_from_parent"),
            "engine": probe.get("engine") or metrics.get("best_engine"),
            "asset": probe.get("symbol") or metrics.get("best_symbol"),
            "params": probe.get("params") if isinstance(probe.get("params"), dict) else {},
            "critic_status": metrics.get("critic_status"),
            "validation_stage": metrics.get("validation_stage"),
        }

    def fmt_num(value: Any, digits: int = 2, suffix: str = "") -> str:
        try:
            return f"{float(value):.{digits}f}{suffix}"
        except (TypeError, ValueError):
            return "-"

    def task_detail(task: dict[str, Any], *, role: str | None = None) -> dict[str, Any]:
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        probes = payload.get("probes") if isinstance(payload.get("probes"), list) else []
        node_id = str(task.get("node_id") or "") or None
        node = node_map.get(node_id or "", {})
        metrics = node.get("metrics") if isinstance(node.get("metrics"), dict) else {}
        probe_specs = metrics.get("probe_specs") if isinstance(metrics.get("probe_specs"), list) else []
        engines = sorted(
            {
                str(item.get("engine"))
                for item in [*probes[:20], *probe_specs[:20]]
                if isinstance(item, dict) and item.get("engine")
            }
        )
        return {
            "id": task.get("id"),
            "role": role or task.get("agent_role"),
            "status": task.get("status"),
            "type": task.get("task_type"),
            "priority": task.get("priority"),
            "updated_at": task.get("updated_at"),
            "node_id": node_id,
            "node_name": node.get("name") or node.get("code") or node_id,
            "node_status": node.get("status"),
            "node_score": node.get("score"),
            "timeframe": payload.get("timeframe") or node.get("timeframe"),
            "hypothesis": node.get("hypothesis"),
            "causal_mechanism": node.get("causal_mechanism"),
            "source_type": node.get("source_type"),
            "source_path": node.get("source_path"),
            "summary": node.get("result_summary") or node.get("prune_reason"),
            "engines": engines,
            "probes_count": len(probes),
            "probe_specs": [compact_probe(spec) for spec in probe_specs[:6] if isinstance(spec, dict)],
            "sample_probes": [compact_probe(probe) for probe in probes[:6] if isinstance(probe, dict)],
            "metrics": metrics_summary(metrics),
        }

    tasks_by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in task_rows:
        tasks_by_agent[str(task.get("agent_role") or "unknown")].append(task)

    events_by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    events_by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    event_counts: Counter[str] = Counter()
    event_type_counts: Counter[str] = Counter()
    for event in event_rows:
        role = str(event.get("agent_role") or "system")
        event_type = str(event.get("type") or "evento")
        events_by_agent[role].append(event)
        event_counts[role] += 1
        event_type_counts[event_type] += 1
        if event.get("node_id"):
            events_by_node[str(event.get("node_id"))].append(event)

    known_roles = {str(agent.get("role")) for agent in agent_rows}
    for role in sorted(set(tasks_by_agent) | set(events_by_agent)):
        if role not in known_roles:
            agent_rows.append(
                {
                    "role": role,
                    "label": DISPLAY_NAMES.get(role, role.replace("_", " ").title()),
                    "kind": "sidecar",
                    "timeframe": None,
                    "status": "standby",
                    "current_task_id": None,
                    "stats": {},
                    "updated_at": "",
                }
            )

    def sort_agent(agent: dict[str, Any]) -> tuple[int, str]:
        status = str(agent.get("status") or "standby")
        order = {"running": 0, "queued": 1, "failed": 2, "standby": 3}
        return order.get(status, 4), str(agent.get("role") or "")

    agent_rows.sort(key=sort_agent)

    running_tasks = sum(1 for task in task_rows if task.get("status") == "running")
    queued_tasks = sum(1 for task in task_rows if task.get("status") == "queued")
    work_items = 0
    deployed_passing = sum(1 for row in passing_strategies if row.get("deployed"))
    blocked_passing = sum(1 for row in passing_strategies if row.get("deploy_blocker"))
    stage_counts = {
        "hypothesis": sum(event_type_counts.get(key, 0) for key in ("hypothesis_created", "research_added", "llm_hypothesis")),
        "scout": running_tasks + queued_tasks + event_type_counts.get("tasks_queued", 0),
        "validation": sum(1 for row in passing_strategies if row.get("readiness") == "paper_ready"),
        "critic": sum(
            1
            for row in passing_strategies
            if (node_map.get(str(row.get("node_id") or ""), {}).get("metrics") or {}).get("critic_status") == "approved"
        ),
        "deploy": deployed_passing,
        "blocked": blocked_passing,
    }

    def latest_event_matching(*tokens: str) -> dict[str, Any] | None:
        lowered = tuple(token.lower() for token in tokens)
        for event in event_rows:
            text = " ".join(
                str(event.get(key) or "")
                for key in ("type", "message", "agent_role")
            ).lower()
            if any(token in text for token in lowered):
                return event
        return None

    def latest_event_where(predicate: Any) -> dict[str, Any] | None:
        for event in event_rows:
            if predicate(event):
                return event
        return None

    def is_error_event(event: dict[str, Any]) -> bool:
        event_type = str(event.get("type") or "").lower()
        if any(token in event_type for token in ("error", "failed", "exception")):
            return True
        message = str(event.get("message") or "").lower()
        if "0 falha" in message or "0 failure" in message:
            return False
        return any(token in message for token in ("erro", "error", "exception", "falha tecnica", "failed"))

    oldest_running_task = None
    running_task_rows = [task for task in task_rows if task.get("status") == "running"]
    if running_task_rows:
        oldest_running_task = sorted(running_task_rows, key=lambda task: str(task.get("updated_at") or ""))[0]
    latest_error = latest_event_where(is_error_event)
    latest_prune = latest_event_matching("pruned", "hard_deleted", "poda", "paused")
    latest_deploy = latest_event_matching("deploy", "manifest", "ranking_deployed")
    latest_critic = latest_event_matching("critic")

    signals: list[dict[str, Any]] = []
    if oldest_running_task:
        signals.append(
            {
                "level": "running",
                "label": "Mais antiga",
                "value": short(oldest_running_task.get("task_type"), 24),
                "detail": f"{oldest_running_task.get('agent_role') or '-'} desde {oldest_running_task.get('updated_at') or '-'}",
                "node_id": oldest_running_task.get("node_id"),
            }
        )
    if latest_error:
        signals.append(
            {
                "level": "danger",
                "label": "Ultimo erro",
                "value": short(latest_error.get("type"), 24),
                "detail": short(latest_error.get("message"), 92),
                "node_id": latest_error.get("node_id"),
            }
        )
    elif latest_prune:
        signals.append(
            {
                "level": "warning",
                "label": "Ultima poda",
                "value": short(latest_prune.get("type"), 24),
                "detail": short(latest_prune.get("message"), 92),
                "node_id": latest_prune.get("node_id"),
            }
        )
    if latest_critic:
        signals.append(
            {
                "level": "quality",
                "label": "Critic",
                "value": short(latest_critic.get("type"), 24),
                "detail": short(latest_critic.get("message"), 92),
                "node_id": latest_critic.get("node_id"),
            }
        )
    if latest_deploy:
        signals.append(
            {
                "level": "deploy",
                "label": "Deploy",
                "value": short(latest_deploy.get("type"), 24),
                "detail": short(latest_deploy.get("message"), 92),
                "node_id": latest_deploy.get("node_id"),
            }
        )

    selected_strategy = passing_strategies[0] if passing_strategies else None
    root_kwargs: dict[str, Any] = {}
    if selected_strategy:
        root_kwargs = {"x": 650, "y": 150, "fixed": {"x": True, "y": True}, "physics": False}
    add_node(
        "__root__",
        "AETHER\nAGENTES",
        color=color_obj("#0f172a", "#67e8f9", "#1e293b"),
        shape="box",
        size=28,
        widthConstraint={"minimum": 150, "maximum": 190},
        margin={"top": 14, "right": 16, "bottom": 14, "left": 16},
        font={"size": 14, "color": "#e0f2fe", "face": "system-ui"},
        borderWidth=3,
        shadow={"enabled": True, "color": "#22d3ee", "size": 18, "x": 0, "y": 4},
        **root_kwargs,
    )
    if selected_strategy:
        node_id = str(selected_strategy.get("node_id") or "")
        node = node_map.get(node_id, {})
        strategy = strategy_payload(selected_strategy, node)
        family = short(str(strategy.get("name") or "estrategia").split("/")[0].strip(), 26)
        engine = strategy.get("engine") or selected_strategy.get("code") or "engine"
        asset = strategy.get("asset") or "-"
        params = params_text(strategy.get("params"))
        agent_role = str(strategy.get("agent_role") or node.get("agent_role") or "")
        agent_label = DISPLAY_NAMES.get(agent_role, agent_role.replace("_", " ").title() if agent_role else "-")
        passing_detail = {"kind": "strategy", "strategy": strategy}
        stage_style = {
            "shape": "box",
            "widthConstraint": {"minimum": 170, "maximum": 220},
            "margin": {"top": 14, "right": 15, "bottom": 14, "left": 15},
            "font": {"size": 12, "color": "#dbeafe", "face": "system-ui"},
            "borderWidth": 2,
            "fixed": {"x": True, "y": True},
            "physics": False,
            "researchPassing": True,
            "rawNodeId": node_id,
            "detail": passing_detail,
        }
        stage_nodes = [
            (
                "family",
                -560,
                -90,
                f"Familia\n{family}\norigem {short(selected_strategy.get('source_bucket') or 'aether', 16)}",
                "#10172a",
                "#8b5cf6",
            ),
            (
                "engine",
                -330,
                -105,
                f"Engine\n{short(engine, 28)}\nagente {short(agent_label, 18)}",
                "#082033",
                "#38bdf8",
            ),
            (
                "indicators",
                -100,
                -40,
                f"Indicadores\n{short(engine, 24)}\n{short(params or asset, 28)}",
                "#071726",
                "#38bdf8",
            ),
            (
                "entry",
                140,
                -95,
                f"Entrada\nscore {fmt_num(strategy.get('score'), 2)} - trades {strategy.get('trade_count') or '-'}\nPnL {fmt_num(strategy.get('pnl_pct'), 1, '%')}",
                "#071426",
                "#60a5fa",
            ),
            (
                "exit",
                140,
                80,
                f"Saida\ncritic {short(strategy.get('critic_status') or 'approved', 14)}\nSharpe {fmt_num(strategy.get('sharpe'), 2)}",
                "#160d1f",
                "#ec4899",
            ),
            (
                "risk",
                -90,
                115,
                f"Gestao\nMDD {fmt_num(strategy.get('mdd_pct'), 1, '%')}\n{short(strategy.get('deploy_blocker') or 'sem bloqueio', 30)}",
                "#10132a",
                "#8b5cf6",
            ),
            (
                "generated",
                390,
                -10,
                f"Estrategia gerada\n{short(node_id, 30)}\nScore {fmt_num(strategy.get('score'), 2)}",
                "#0b2942",
                "#22d3ee",
            ),
        ]
        for stage, x, y, label, bg, border in stage_nodes:
            add_node(
                stage_graph_id(node_id, stage),
                label,
                x=x,
                y=y,
                color=color_obj(bg, border),
                shadow={"enabled": stage == "generated", "color": border, "size": 20, "x": 0, "y": 4},
                **stage_style,
            )
        flow_edges = [
            ("family", "engine"),
            ("engine", "indicators"),
            ("indicators", "entry"),
            ("entry", "generated"),
            ("family", "risk"),
            ("risk", "exit"),
            ("exit", "generated"),
        ]
        for source, target in flow_edges:
            add_edge(
                stage_graph_id(node_id, source),
                stage_graph_id(node_id, target),
                color={"color": "#60a5fa", "opacity": 0.78},
                width=3,
                smooth={"enabled": True, "type": "cubicBezier", "roundness": 0.45},
                arrows={"to": {"enabled": True, "scaleFactor": 0.6}},
            )

        agent_tasks = tasks_by_agent.get(agent_role, [])
        recent_node_events = events_by_node.get(node_id, [])[:3] or events_by_agent.get(agent_role, [])[:3]
        add_node(
            stage_graph_id(node_id, "agent_work"),
            f"Trabalho do agente\n{short(agent_label, 24)}\n{len(agent_tasks)} tarefas ativas",
            x=-340,
            y=130,
            color=color_obj("#071b2b", AGENT_COLORS.get(agent_role, "#38bdf8")),
            shape="box",
            widthConstraint={"minimum": 190, "maximum": 230},
            margin={"top": 12, "right": 14, "bottom": 12, "left": 14},
            font={"size": 12, "color": "#dbeafe", "face": "system-ui"},
            borderWidth=2,
            fixed={"x": True, "y": True},
            physics=False,
            rawNodeId=node_id,
            researchPassing=True,
            detail={
                "kind": "agent",
                "role": agent_role,
                "label": agent_label,
                "agent_kind": "research_worker",
                "timeframe": strategy.get("timeframe"),
                "status": "running" if agent_tasks else "standby",
                "current_task_id": agent_tasks[0].get("id") if agent_tasks else None,
                "updated_at": node.get("updated_at"),
                "stats": {"bubble": strategy.get("summary") or "Estrategia passando no pesquisador."},
                "tasks": [task_detail(task, role=agent_role) for task in agent_tasks[:4]],
                "recent_events": [
                    {"ts": event.get("ts"), "type": event.get("type"), "message": event.get("message")}
                    for event in recent_node_events
                ],
            },
        )
        add_edge(
            stage_graph_id(node_id, "engine"),
            stage_graph_id(node_id, "agent_work"),
            color={"color": AGENT_COLORS.get(agent_role, "#38bdf8"), "opacity": 0.72},
            width=2,
            label="trabalho",
            dashes=True,
            font={"size": 10, "color": "#93c5fd", "face": "system-ui", "strokeWidth": 3, "strokeColor": "#06111f"},
        )
        for index, event in enumerate(recent_node_events[:2]):
            event_type = str(event.get("type") or "evento")
            e_bg, e_border = event_color(event_type)
            ev_id = f"strategy_event::{node_id}::{event.get('id') or index}"
            add_node(
                ev_id,
                f"Ultima acao\n{short(event_type.replace('_', ' '), 24)}\n{short(event.get('ts'), 19)}",
                x=-560 + index * 210,
                y=245,
                color=color_obj(e_bg, e_border),
                shape="box",
                widthConstraint={"minimum": 170, "maximum": 210},
                margin={"top": 10, "right": 12, "bottom": 10, "left": 12},
                font={"size": 10, "color": "#cbd5e1", "face": "system-ui"},
                borderWidth=2,
                fixed={"x": True, "y": True},
                physics=False,
                rawNodeId=node_id,
                researchPassing=True,
                kind="last_action",
                detail={
                    "kind": "event",
                    "agent": agent_label,
                    "role": agent_role,
                    "event": {
                        "ts": event.get("ts"),
                        "type": event_type,
                        "message": event.get("message"),
                        "target": node_summary(node_id, node_map),
                    },
                },
            )
            add_edge(stage_graph_id(node_id, "agent_work"), ev_id, color={"color": e_border, "opacity": 0.45}, width=1, dashes=True)

        passing_y = 380
        for index, row in enumerate(passing_strategies[:16]):
            pid = str(row.get("node_id") or "")
            pnode = node_map.get(pid, {})
            pstrategy = strategy_payload(row, pnode)
            col = index % 8
            line = index // 8
            add_node(
                passing_graph_id(pid),
                f"PASSANDO\n{short(row.get('name'), 24)}\nPnL {fmt_num(row.get('pnl_pct'), 0, '%')} - Sh {fmt_num(row.get('sharpe'), 2)}",
                x=-650 + col * 185,
                y=passing_y + line * 105,
                color=color_obj("#0b1f2f", "#22c55e", "#123923"),
                shape="box",
                size=15,
                widthConstraint={"minimum": 155, "maximum": 180},
                margin={"top": 10, "right": 11, "bottom": 10, "left": 11},
                font={"size": 10, "color": "#dcfce7", "face": "system-ui"},
                borderWidth=2,
                fixed={"x": True, "y": True},
                physics=False,
                rawNodeId=pid,
                researchPassing=True,
                kind="strategy",
                detail={"kind": "strategy", "strategy": pstrategy},
            )
            add_edge(
                stage_graph_id(node_id, "generated"),
                passing_graph_id(pid),
                color={"color": "#22c55e", "opacity": 0.24},
                width=1,
                dashes=True,
            )

    for agent in agent_rows:
        role = str(agent.get("role") or "unknown")
        status = str(agent.get("status") or "standby")
        label = DISPLAY_NAMES.get(role, agent.get("label") or short(role, 16))
        color = AGENT_COLORS.get(role, "#64748b")
        bg, border = status_colors(status)
        active = status == "running"
        queued = status == "queued"
        agent_tasks = tasks_by_agent.get(role, [])
        current_task_id = agent.get("current_task_id")
        stats = agent.get("stats") or {}
        current_first = sorted(
            agent_tasks,
            key=lambda task: (
                0 if task.get("id") == current_task_id else 1,
                0 if task.get("status") == "running" else 1,
                -float(task.get("priority") or 0.0),
                str(task.get("updated_at") or ""),
            ),
        )
        recent_events = events_by_agent.get(role, [])[:4]

        add_node(
            agent_id(role),
            f"{short(label, 16)}\n{STATUS_LABELS.get(status, status.upper())} · {len(agent_tasks)} tarefas",
            color=color_obj(bg, "#67e8f9" if active else border),
            shape="box",
            size=28 if active else 24 if queued else 20,
            widthConstraint={"minimum": 150, "maximum": 190},
            margin={"top": 12, "right": 14, "bottom": 12, "left": 14},
            borderWidth=4 if active else 3 if queued else 2,
            font={"size": 13, "color": "#f8fafc", "face": "system-ui", "bold": {"color": "#ffffff"}},
            shadow={"enabled": True, "color": color if (active or queued) else "rgba(34,211,238,.25)", "size": 26 if active else 18 if queued else 10, "x": 0, "y": 4},
            kind="agent",
            detail={
                "kind": "agent",
                "role": role,
                "label": agent.get("label") or label,
                "agent_kind": agent.get("kind"),
                "timeframe": agent.get("timeframe"),
                "status": status,
                "current_task_id": current_task_id,
                "updated_at": agent.get("updated_at"),
                "stats": stats,
                "tasks": [task_detail(task, role=role) for task in current_first[:6]],
                "recent_events": [
                    {
                        "ts": event.get("ts"),
                        "type": event.get("type"),
                        "message": event.get("message"),
                    }
                    for event in recent_events
                ],
            },
            title=title(
                agent.get("label") or role,
                f"role={role}",
                f"status={status}",
                f"current_task={current_task_id or '-'}",
                stats.get("bubble") or "",
            ),
        )
        add_edge("__root__", agent_id(role), color={"color": color, "opacity": 0.44}, width=3 if active else 1)

        for task in current_first[:limit_per_agent]:
            task_status = str(task.get("status") or "queued")
            t_bg, t_border = status_colors(task_status)
            task_id = str(task.get("id") or "")
            node_id = str(task.get("node_id") or "") or None
            target = node_summary(node_id, node_map)
            task_label = STATUS_LABELS.get(task_status, task_status.upper())
            graph_id = task_graph_id(task_id)
            work_items += 1
            add_node(
                graph_id,
                f"{task_label}\n{short(task.get('task_type'), 24)}\n{short(target or task_id, 34)}",
                color=color_obj(t_bg, t_border),
                shape="box",
                size=24 if task_status == "running" else 19,
                widthConstraint={"minimum": 175, "maximum": 230},
                margin={"top": 11, "right": 14, "bottom": 11, "left": 14},
                borderWidth=4 if task_status == "running" else 2,
                font={"size": 11, "color": "#e2e8f0", "face": "system-ui"},
                shadow={"enabled": True, "color": t_border, "size": 22 if task_status == "running" else 9, "x": 0, "y": 4},
                rawNodeId=node_id,
                kind="work",
                detail={"kind": "work", "agent": agent.get("label") or role, "task": task_detail(task, role=role)},
                title=title(
                    f"task={task_id}",
                    f"type={task.get('task_type')}",
                    f"status={task_status}",
                    f"target={target or '-'}",
                    stats.get("bubble") or "",
                    task.get("error") or "",
                ),
            )
            add_edge(
                agent_id(role),
                graph_id,
                color={"color": t_border, "opacity": 0.9 if task_status == "running" else 0.62},
                width=5 if task_status == "running" else 3,
                label="fluxo vivo" if task_status == "running" else "fila",
                font={"size": 10, "color": t_border, "face": "system-ui", "strokeWidth": 3, "strokeColor": "#06111f"},
                smooth={"enabled": True, "type": "cubicBezier", "roundness": 0.42},
                dashes=task_status != "running",
            )
            focus.append(
                {
                    "agent": agent.get("label") or role,
                    "role": role,
                    "status": task_status,
                    "action": task.get("task_type"),
                    "node": target or task_id,
                    "node_id": node_id,
                    "graph_node_id": graph_id,
                    "timeframe": agent.get("timeframe") or (node_map.get(node_id or "", {}).get("timeframe") if node_id else None),
                    "detail": stats.get("bubble") or f"{task_status} desde {task.get('updated_at') or '-'}",
                    "task": task_detail(task, role=role),
                }
            )

        if not agent_tasks and events_by_agent.get(role):
            event = events_by_agent[role][0]
            event_type = str(event.get("type") or "evento")
            e_bg, e_border = event_color(event_type)
            target = node_summary(str(event.get("node_id") or "") or None, node_map)
            graph_id = event_graph_id(role, event.get("id"))
            work_items += 1
            add_node(
                graph_id,
                f"ULTIMA ACAO\n{short(event_type.replace('_', ' '), 24)}\n{short(event.get('ts'), 19)}",
                color=color_obj(e_bg, e_border),
                shape="box",
                size=15,
                borderWidth=2,
                font={"size": 10, "color": "#cbd5e1", "face": "system-ui"},
                rawNodeId=str(event.get("node_id") or "") or None,
                kind="last_action",
                detail={
                    "kind": "event",
                    "agent": agent.get("label") or role,
                    "role": role,
                    "event": {
                        "ts": event.get("ts"),
                        "type": event_type,
                        "message": event.get("message"),
                        "target": target,
                    },
                },
                title=title(event.get("ts"), event_type, event.get("message") or "", target),
            )
            add_edge(agent_id(role), graph_id, color={"color": e_border, "opacity": 0.35}, width=1, dashes=True)

    focus.sort(key=lambda item: 0 if item.get("status") == "running" else 1)
    stats = {
        "nodes": len(nodes),
        "edges": len(edges),
        "agents": len(agent_rows),
        "running_agents": sum(1 for agent in agent_rows if agent.get("status") == "running"),
        "queued_agents": sum(1 for agent in agent_rows if agent.get("status") == "queued"),
        "running_tasks": running_tasks,
        "queued_tasks": queued_tasks,
        "work_items": work_items,
        "recent_events": sum(event_counts.values()),
        "passing_strategies": len(passing_strategies),
        "deployed_passing": deployed_passing,
        "blocked_passing": blocked_passing,
        "stage_counts": stage_counts,
        "signals": signals[:4],
        "selected_node_id": selected_strategy.get("node_id") if selected_strategy else None,
        "selected_graph_id": stage_graph_id(str(selected_strategy.get("node_id") or ""), "generated") if selected_strategy else None,
    }
    return {
        "ok": True,
        "layout": "strategy_flow" if selected_strategy else "agent_work",
        "nodes": nodes,
        "edges": edges,
        "focus": focus[:20],
        "strategies": [
            strategy_payload(row, node_map.get(str(row.get("node_id") or ""), {}))
            for row in passing_strategies[:80]
        ],
        "stats": stats,
    }
