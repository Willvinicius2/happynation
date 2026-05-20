from __future__ import annotations

import os
import sqlite3
import uuid
from typing import Any

from .campaign_constants import CURRENT_CAMPAIGN_ID
from .campaigns import get_active_campaign_id, node_campaign_id
from .config import TIMEFRAMES
from .db import add_event, dumps, get_setting, loads, row_to_dict
from .llm_traces import list_llm_traces
from .timeutils import now_iso


AGENT_DEFINITIONS = [
    ("chief_orchestrator", "Chief Orchestrator", "control", None),
    ("hypothesis_generator", "Hypothesis Generator", "reasoning", None),
    ("scorekeeper", "Scorekeeper", "reasoning", None),
    ("agent_1m_micro", "Agent 1m Micro", "timeframe_worker", "1m"),
    ("agent_5m_scalp", "Agent 5m Scalp", "timeframe_worker", "5m"),
    ("agent_15m", "Agent 15m", "timeframe_worker", "15m"),
    ("agent_1h", "Agent 1h", "timeframe_worker", "1h"),
    ("agent_4h", "Agent 4h", "timeframe_worker", "4h"),
    ("agent_1d", "Agent 1d", "timeframe_worker", "1d"),
    ("validation_agent", "Validation Agent", "quality", None),
    ("portfolio_agent", "Portfolio Agent", "portfolio", None),
    ("manifest_agent", "Manifest Agent", "export", None),
    ("archivist_agent", "Archivist Agent", "archive", None),
    ("infra_data_agent", "Infra/Data Agent", "infra", None),
    ("critic_agent", "Critic Agent", "quality", None),
    ("hyper_agent", "Hyper Agent", "meta", None),
]


STANDBY_BUBBLES = {
    "chief_orchestrator": "Aguardando o proximo ciclo.",
    "hypothesis_generator": "Sem pesquisa pendente.",
    "scorekeeper": "Sem score para recalcular.",
    "agent_1m_micro": "Standby no 1m (micro-estrutura).",
    "agent_5m_scalp": "Standby no 5m.",
    "agent_15m": "Standby no 15m.",
    "agent_1h": "Standby no 1h.",
    "agent_4h": "Standby no 4h.",
    "agent_1d": "Standby no diario.",
    "validation_agent": "Aguardando sobreviventes.",
    "portfolio_agent": "Aguardando candidatos.",
    "manifest_agent": "Aguardando aprovacao.",
    "archivist_agent": "Memoria pronta.",
    "infra_data_agent": "Dados monitorados.",
    "critic_agent": "Sem validadas para criticar.",
    "hyper_agent": "Aguardando proxima avaliacao da arvore.",
}


HYPOTHESIS_LIBRARY = [
    {
        "slug": "rsi-extreme-reversion",
        "name": "RSI extreme reversion",
        "hypothesis": "Extremos de RSI revertem quando o movimento estica alem do fluxo recente.",
        "causal_mechanism": "Exaustao de compradores/vendedores cria retorno parcial a media apos regimes de excesso.",
        "engines": ["rsi"],
        "base_score": 0.66,
    },
    {
        "slug": "bollinger-volatility-reversion",
        "name": "Bollinger volatility reversion",
        "hypothesis": "Fechamentos fora da banda carregam probabilidade de reversao quando a volatilidade normaliza.",
        "causal_mechanism": "Expansoes locais de volatilidade tendem a contrair e devolver parte do deslocamento.",
        "engines": ["bollinger"],
        "base_score": 0.64,
    },
    {
        "slug": "ma-trend-continuation",
        "name": "MA trend continuation",
        "hypothesis": "Cruzamentos de medias capturam continuidade quando o custo nao consome o deslocamento.",
        "causal_mechanism": "Persistencia de fluxo direcional supera ruido em janelas de tendencia limpa.",
        "engines": ["ma_crossover"],
        "base_score": 0.62,
    },
    {
        "slug": "donchian-breakout",
        "name": "Donchian breakout",
        "hypothesis": "Rompimentos de maxima/minima recentes carregam continuacao assimetrica.",
        "causal_mechanism": "Quebra de range concentra stops e fluxo de momentum apos consolidacao.",
        "engines": ["donchian"],
        "base_score": 0.6,
    },
    {
        "slug": "multi-family-scout",
        "name": "Multi-family scout",
        "hypothesis": "Uma varredura curta entre familias simples identifica onde vale gastar validacao profunda.",
        "causal_mechanism": "Scout barato compara sinais independentes antes de comprometer compute caro.",
        "engines": [
            "ma_crossover", "bollinger", "rsi", "donchian", "supertrend",
            "macd", "stochastic", "adx", "keltner", "vwap_dev",
            "roc", "williams_r", "cci", "mfi", "obv_trend",
            "psar", "atr_breakout", "zscore",
        ],
        "base_score": 0.58,
    },
]


def role_for_timeframe(timeframe: str) -> str:
    return {
        "1m": "agent_1m_micro",
        "5m": "agent_5m_scalp",
        "15m": "agent_15m",
        "1h": "agent_1h",
        "4h": "agent_4h",
        "1d": "agent_1d",
    }[timeframe]


def _scoped_id(campaign_id: str, base_id: str) -> str:
    if campaign_id == CURRENT_CAMPAIGN_ID:
        return base_id
    return f"{campaign_id}:{base_id}"


def ensure_agents(conn: sqlite3.Connection) -> None:
    ts = now_iso()
    for role, label, kind, timeframe in AGENT_DEFINITIONS:
        conn.execute(
            """
            INSERT INTO agents(role, label, kind, timeframe, status, stats_json, updated_at)
            VALUES (?, ?, ?, ?, 'standby', '{}', ?)
            ON CONFLICT(role) DO NOTHING
            """,
            (role, label, kind, timeframe, ts),
        )


def set_agent_status(
    conn: sqlite3.Connection,
    role: str,
    status: str,
    *,
    current_task_id: str | None = None,
    stats: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        UPDATE agents
        SET status=?, current_task_id=?, stats_json=?, updated_at=?
        WHERE role=?
        """,
        (status, current_task_id, dumps(stats or {}), now_iso(), role),
    )


def upsert_node(
    conn: sqlite3.Connection,
    *,
    id: str,
    campaign_id: str | None = None,
    code: str,
    name: str,
    hypothesis: str,
    parent_id: str | None = None,
    causal_mechanism: str = "",
    mutation_from_parent: str = "",
    timeframe: str | None = None,
    agent_role: str | None = None,
    status: str = "candidate",
    score: float = 0.5,
    depth: int = 0,
    priority: float | None = None,
    prune_reason: str | None = None,
    result_summary: str | None = None,
    metrics: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    source_type: str | None = None,
    source_path: str | None = None,
) -> None:
    ts = now_iso()
    campaign_v = node_campaign_id(node_id=id, source_type=source_type, explicit=campaign_id)
    if campaign_v == CURRENT_CAMPAIGN_ID and not (id == "root" or id.startswith("tf:")):
        campaign_v = get_active_campaign_id(conn)
    priority_v = score if priority is None else priority
    conn.execute(
        """
        INSERT INTO nodes(
            id, campaign_id, parent_id, code, name, hypothesis, causal_mechanism,
            mutation_from_parent, timeframe, agent_role, status, score, depth,
            priority, prune_reason, result_summary, metrics_json, tags_json,
            source_type, source_path, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            campaign_id=excluded.campaign_id,
            parent_id=excluded.parent_id,
            code=excluded.code,
            name=excluded.name,
            hypothesis=excluded.hypothesis,
            causal_mechanism=excluded.causal_mechanism,
            mutation_from_parent=excluded.mutation_from_parent,
            timeframe=excluded.timeframe,
            agent_role=excluded.agent_role,
            status=excluded.status,
            score=excluded.score,
            depth=excluded.depth,
            priority=excluded.priority,
            prune_reason=excluded.prune_reason,
            result_summary=excluded.result_summary,
            metrics_json=excluded.metrics_json,
            tags_json=excluded.tags_json,
            source_type=excluded.source_type,
            source_path=excluded.source_path,
            updated_at=excluded.updated_at
        """,
        (
            id,
            campaign_v,
            parent_id,
            code,
            name,
            hypothesis,
            causal_mechanism,
            mutation_from_parent,
            timeframe,
            agent_role,
            status,
            float(score),
            int(depth),
            float(priority_v),
            prune_reason,
            result_summary,
            dumps(metrics or {}),
            dumps(tags or []),
            source_type,
            source_path,
            ts,
            ts,
        ),
    )
    # Atualiza memoria semantica (vetor de aprendizado entre ciclos).
    if id != "root" and not str(id).startswith("tf:"):
        try:
            from . import semantic_memory as _sm

            _sm.record_node(
                conn,
                {
                    "id": id,
                    "name": name,
                    "hypothesis": hypothesis,
                    "causal_mechanism": causal_mechanism,
                    "invalidation": "",
                    "timeframe": timeframe,
                    "status": status,
                    "score": float(score),
                    "prune_reason": prune_reason or "",
                    "metrics": metrics or {},
                },
            )
        except Exception:
            pass


def create_task(
    conn: sqlite3.Connection,
    *,
    node_id: str | None,
    campaign_id: str | None = None,
    agent_role: str,
    task_type: str,
    priority: float,
    payload: dict[str, Any],
    task_id: str | None = None,
) -> str:
    tid = task_id or f"task-{uuid.uuid4().hex[:12]}"
    ts = now_iso()
    campaign_v = campaign_id
    if campaign_v is None and node_id:
        row = conn.execute("SELECT campaign_id FROM nodes WHERE id=?", (node_id,)).fetchone()
        campaign_v = row["campaign_id"] if row else None
    campaign_v = campaign_v or get_active_campaign_id(conn)
    conn.execute(
        """
        INSERT INTO tasks(
            id, campaign_id, node_id, agent_role, task_type, status, priority,
            payload_json, attempts, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, 0, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            campaign_id=excluded.campaign_id,
            status='queued',
            priority=excluded.priority,
            payload_json=excluded.payload_json,
            updated_at=excluded.updated_at
        """,
        (tid, campaign_v, node_id, agent_role, task_type, float(priority), dumps(payload), ts, ts),
    )
    return tid


def update_task_status(
    conn: sqlite3.Connection,
    task_id: str,
    status: str,
    *,
    error: str | None = None,
    attempts_delta: int = 0,
) -> None:
    conn.execute(
        """
        UPDATE tasks
        SET status=?,
            error=?,
            attempts=attempts + ?,
            updated_at=?
        WHERE id=?
        """,
        (status, error, attempts_delta, now_iso(), task_id),
    )


def add_artifact(
    conn: sqlite3.Connection,
    *,
    node_id: str | None,
    campaign_id: str | None = None,
    kind: str,
    path: str,
    title: str,
    payload: dict[str, Any] | None = None,
) -> None:
    campaign_v = campaign_id
    if campaign_v is None and node_id:
        row = conn.execute("SELECT campaign_id FROM nodes WHERE id=?", (node_id,)).fetchone()
        campaign_v = row["campaign_id"] if row else None
    campaign_v = campaign_v or get_active_campaign_id(conn)
    conn.execute(
        """
        INSERT INTO artifacts(campaign_id, node_id, kind, path, title, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (campaign_v, node_id, kind, path, title, dumps(payload or {}), now_iso()),
    )


def seed_base_tree(conn: sqlite3.Connection) -> int:
    existing = conn.execute("SELECT 1 FROM nodes WHERE id='root'").fetchone()
    if existing:
        return 0
    upsert_node(
        conn,
        id="root",
        code="R",
        name="Aether Strategy Search",
        hypothesis="Explorar continuamente hipoteses de estrategias cripto com poda agressiva.",
        causal_mechanism="Busca RAIO adaptativa: scout barato, score, expansao e poda.",
        status="expand",
        score=0.78,
        priority=0.78,
        depth=0,
        tags=["root"],
    )
    created = 1
    for idx, timeframe in enumerate(TIMEFRAMES, start=1):
        role = role_for_timeframe(timeframe)
        nid = f"tf:{timeframe}"
        upsert_node(
            conn,
            id=nid,
            parent_id="root",
            code=f"TF-{timeframe}",
            name=f"Timeframe {timeframe}",
            hypothesis=f"Testar familias candidatas no timeframe {timeframe}.",
            causal_mechanism="Comparar familias simples sob custos realistas e promover apenas sobreviventes.",
            mutation_from_parent=f"Especializacao de timeframe {timeframe}.",
            timeframe=timeframe,
            agent_role=role,
            status="candidate",
            score=0.62 - idx * 0.02,
            priority=0.62 - idx * 0.02,
            depth=1,
            tags=["timeframe", timeframe],
        )
        created += 1
    add_event(conn, type="seed", message="Arvore RAIO base criada com 5 agentes por timeframe.")
    return created


def generate_next_hypothesis_batch(conn: sqlite3.Connection, *, count: int = 1) -> int:
    campaign_id = get_active_campaign_id(conn)
    existing = {
        row["id"]
        for row in conn.execute("SELECT id FROM nodes WHERE parent_id='root' AND campaign_id=?", (campaign_id,))
    }
    created = 0
    for spec in HYPOTHESIS_LIBRARY:
        if created >= count:
            break
        parent_id = _scoped_id(campaign_id, f"hyp:{spec['slug']}")
        if parent_id in existing:
            continue
        base_score = float(spec["base_score"])
        upsert_node(
            conn,
            id=parent_id,
            campaign_id=campaign_id,
            parent_id="root",
            code=f"HYP-{created + 1}",
            name=spec["name"],
            hypothesis=spec["hypothesis"],
            causal_mechanism=spec["causal_mechanism"],
            mutation_from_parent="Gerado automaticamente pelo Hypothesis Generator.",
            agent_role="hypothesis_generator",
            status="visited",
            score=base_score,
            priority=0.05,
            depth=1,
            metrics={"engines": spec["engines"], "generator": "built_in_hypothesis_library"},
            tags=["hypothesis", "generated"],
        )
        for idx, timeframe in enumerate(TIMEFRAMES, start=1):
            child_score = max(0.1, base_score - idx * 0.02)
            upsert_node(
                conn,
                id=f"{parent_id}:tf:{timeframe}",
                campaign_id=campaign_id,
                parent_id=parent_id,
                code=f"{spec['slug'][:3].upper()}-{timeframe}",
                name=f"{spec['name']} / {timeframe}",
                hypothesis=f"{spec['hypothesis']} Timeframe alvo: {timeframe}.",
                causal_mechanism=spec["causal_mechanism"],
                mutation_from_parent=f"Especializacao automatica para {timeframe}.",
                timeframe=timeframe,
                agent_role=role_for_timeframe(timeframe),
                status="candidate",
                score=child_score,
                priority=child_score,
                depth=2,
                metrics={"engines": spec["engines"], "generator": "built_in_hypothesis_library"},
                tags=["hypothesis", "generated", timeframe, *[f"engine:{e}" for e in spec["engines"]]],
            )
        add_event(
            conn,
            type="hypothesis_generated",
            campaign_id=campaign_id,
            node_id=parent_id,
            agent_role="hypothesis_generator",
            message=f"Hipotese gerada: {spec['name']} com 5 filhos por timeframe.",
            payload={"engines": spec["engines"]},
        )
        created += 1
    return created * (len(TIMEFRAMES) + 1)


def _compact_state_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    best = metrics.get("best_probe") if isinstance(metrics.get("best_probe"), dict) else {}
    compact_best = {
        key: best.get(key)
        for key in (
            "symbol",
            "timeframe",
            "engine",
            "params",
            "dataset_id",
            "pnl_pct",
            "trade_count",
            "mdd_pct",
            "sharpe",
            "hit_rate",
            "fee_bps",
            "bars",
            "capital",
            "final_equity",
        )
        if best.get(key) is not None
    }
    out = {
        key: metrics.get(key)
        for key in (
            "ok",
            "error",
            "critic_status",
            "critic_reason",
            "validation_passed",
            "validation_stage",
            "validation_reason",
            "generator",
            "provider",
            "model",
            "invalidation",
            "probe_count",
            "ok_count",
            "combo_count",
            "failed_count",
            "pass_count",
            "pnl_pct",
            "trade_count",
            "mdd_pct",
            "sharpe",
            "fee_bps",
            "bars",
            "source_title",
            "source_url",
            "source_query",
            "source",
        )
        if metrics.get(key) is not None
    }
    if compact_best:
        out["best_probe"] = compact_best
    engines = metrics.get("engines")
    if isinstance(engines, list):
        out["engines"] = engines[:6]
    probe_specs = metrics.get("probe_specs")
    if isinstance(probe_specs, list):
        out["probe_specs"] = probe_specs[:8]
    return out


def list_state(
    conn: sqlite3.Connection,
    campaign_id: str | None = None,
    *,
    max_nodes: int | None = None,
    compact_metrics: bool | None = None,
) -> dict[str, Any]:
    campaign_v = campaign_id or get_active_campaign_id(conn)
    if compact_metrics is None:
        compact_metrics = os.environ.get("AETHER_STATE_COMPACT_METRICS", "1").strip().lower() not in {"0", "false", "no"}
    max_nodes_v = max_nodes
    if max_nodes_v is None:
        try:
            max_nodes_v = int(os.environ.get("AETHER_UI_STATE_MAX_NODES", "850"))
        except (TypeError, ValueError):
            max_nodes_v = 850
    nodes = []
    if max_nodes_v and max_nodes_v > 0:
        node_rows = conn.execute(
            """
            SELECT * FROM nodes
            WHERE id='root'
               OR id IN (
                    SELECT id FROM nodes
                    WHERE campaign_id=?
                      AND id != 'root'
                      AND id NOT LIKE 'tf:%'
                    ORDER BY
                      CASE status
                        WHEN 'validated' THEN 0
                        WHEN 'running' THEN 1
                        WHEN 'queued' THEN 2
                        WHEN 'candidate' THEN 3
                        WHEN 'visited' THEN 4
                        WHEN 'expanded' THEN 5
                        WHEN 'expand' THEN 6
                        WHEN 'promising' THEN 7
                        ELSE 8
                      END,
                      updated_at DESC
                    LIMIT ?
               )
            ORDER BY depth, code, created_at
            """,
            (campaign_v, int(max_nodes_v)),
        ).fetchall()
    else:
        node_rows = conn.execute(
            """
            SELECT * FROM nodes
            WHERE id='root' OR campaign_id=?
            ORDER BY depth, code, created_at
            """,
            (campaign_v,),
        ).fetchall()
    for row in node_rows:
        item = row_to_dict(row)
        metrics = loads(item.pop("metrics_json"), {})
        item["metrics"] = _compact_state_metrics(metrics) if compact_metrics else metrics
        item["tags"] = loads(item.pop("tags_json"), [])
        nodes.append(item)

    total_nodes = conn.execute(
        "SELECT COUNT(*) AS n FROM nodes WHERE id='root' OR campaign_id=?",
        (campaign_v,),
    ).fetchone()["n"]

    events = []
    for row in conn.execute(
        """
        SELECT * FROM events
        WHERE campaign_id=? OR campaign_id IS NULL
        ORDER BY id DESC LIMIT 120
        """,
        (campaign_v,),
    ):
        item = row_to_dict(row)
        item["payload"] = loads(item.pop("payload_json"), {})
        events.append(item)

    tasks = []
    for row in conn.execute(
        """
        SELECT * FROM tasks
        WHERE campaign_id=?
        ORDER BY status, priority DESC, created_at DESC LIMIT 200
        """,
        (campaign_v,),
    ):
        item = row_to_dict(row)
        item["payload"] = loads(item.pop("payload_json"), {})
        tasks.append(item)

    runs = []
    for row in conn.execute(
        """
        SELECT * FROM runs
        WHERE campaign_id=?
        ORDER BY started_at DESC, id DESC LIMIT 80
        """,
        (campaign_v,),
    ):
        item = row_to_dict(row)
        item["summary"] = loads(item.pop("summary_json"), {})
        runs.append(item)

    agents = []
    for row in conn.execute("SELECT * FROM agents ORDER BY kind, role"):
        item = row_to_dict(row)
        item["stats"] = loads(item.pop("stats_json"), {})
        agents.append(item)

    artifacts = []
    for row in conn.execute(
        """
        SELECT * FROM artifacts
        WHERE campaign_id=?
        ORDER BY id DESC LIMIT 120
        """,
        (campaign_v,),
    ):
        item = row_to_dict(row)
        item["payload"] = loads(item.pop("payload_json"), {})
        artifacts.append(item)

    return {
        "campaign_id": campaign_v,
        "nodes": nodes,
        "nodes_total": total_nodes,
        "nodes_truncated": bool(max_nodes_v and max_nodes_v > 0 and total_nodes > len(nodes)),
        "nodes_limit": max_nodes_v,
        "events": events,
        "tasks": tasks,
        "runs": runs,
        "agents": agents,
        "artifacts": artifacts,
        "llm_traces": list_llm_traces(conn, campaign_id=campaign_v, limit=80),
    }


def best_candidate_nodes(
    conn: sqlite3.Connection,
    limit: int = 5,
    campaign_id: str | None = None,
    *,
    per_timeframe: bool = False,
) -> list[dict[str, Any]]:
    """Devolve os melhores candidatos.

    Quando `per_timeframe=True`, espalha as escolhas por timeframe (5m/15m/1h/4h/1d)
    para que TODOS os timeframe_workers fiquem ocupados em paralelo, em vez de o
    timeframe dominante monopolizar os slots. `limit` vira teto total.
    """
    campaign_v = campaign_id or get_active_campaign_id(conn)
    reset_after = None
    if os.environ.get("AETHER_CYCLE_RESPECT_RANKING_RESET", "1").strip().lower() in {"1", "true", "yes", "on"}:
        reset_state = get_setting(conn, f"ranking_hidden_node_ids:{campaign_v}", default={})
        if isinstance(reset_state, dict) and reset_state.get("reset_at"):
            reset_after = str(reset_state["reset_at"])
    if not per_timeframe:
        reset_clause = "AND created_at > ?" if reset_after else ""
        params: list[Any] = [campaign_v]
        if reset_after:
            params.append(reset_after)
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT * FROM nodes
            WHERE status IN ('candidate', 'expand', 'promising')
              AND campaign_id=?
              AND id != 'root'
              AND id NOT LIKE 'tf:%'
              {reset_clause}
            ORDER BY
              CASE status WHEN 'expand' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
              priority DESC,
              created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        out = []
        for row in rows:
            item = row_to_dict(row)
            item["metrics"] = loads(item.pop("metrics_json"), {})
            item["tags"] = loads(item.pop("tags_json"), [])
            out.append(item)
        return out

    # Spread mode: pega ate `per_tf` candidatos de cada timeframe e intercala.
    # Use a fonte unica de configuracao para nao deixar um worker sem trabalho
    # quando o gerador criar candidatos para novos timeframes (ex.: 1m).
    timeframes = list(TIMEFRAMES)
    per_tf = max(1, limit // len(timeframes)) + 1
    buckets: dict[str, list[dict[str, Any]]] = {tf: [] for tf in timeframes}
    for tf in timeframes:
        reset_clause = "AND created_at > ?" if reset_after else ""
        params: list[Any] = [campaign_v, tf]
        if reset_after:
            params.append(reset_after)
        params.append(per_tf)
        rows = conn.execute(
            f"""
            SELECT * FROM nodes
            WHERE status IN ('candidate', 'expand', 'promising')
              AND campaign_id=?
              AND id != 'root'
              AND id NOT LIKE 'tf:%'
              AND timeframe=?
              {reset_clause}
            ORDER BY
              CASE status WHEN 'expand' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
              priority DESC,
              created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        for row in rows:
            item = row_to_dict(row)
            item["metrics"] = loads(item.pop("metrics_json"), {})
            item["tags"] = loads(item.pop("tags_json"), [])
            buckets[tf].append(item)

    # Round-robin para que pelo menos 1 candidato de cada timeframe vivo apareca cedo
    out: list[dict[str, Any]] = []
    while len(out) < limit:
        progressed = False
        for tf in timeframes:
            if buckets[tf]:
                out.append(buckets[tf].pop(0))
                progressed = True
                if len(out) >= limit:
                    break
        if not progressed:
            break
    return out


def mark_agents_without_work_standby(conn: sqlite3.Connection) -> None:
    active_roles = {
        row["agent_role"]
        for row in conn.execute("SELECT DISTINCT agent_role FROM tasks WHERE status IN ('queued', 'running')")
    }
    for role, _, _, _ in AGENT_DEFINITIONS:
        if role not in active_roles:
            set_agent_status(
                conn,
                role,
                "standby",
                stats={
                    "reason": "sem tarefa elegivel",
                    "bubble": STANDBY_BUBBLES.get(role, "Standby."),
                },
            )
