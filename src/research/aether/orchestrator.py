from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .compute import default_probes_for_timeframe, run_sweep
from .config import DEFAULT_WORKERS, LEGACY_ROOT
from .db import add_event, connect, dumps, get_setting, loads
from .llm_runtime import call_llm_agent
from .llm_traces import record_llm_trace
from .repository import (
    best_candidate_nodes,
    create_task,
    ensure_agents,
    generate_next_hypothesis_batch,
    mark_agents_without_work_standby,
    seed_base_tree,
    set_agent_status,
    update_task_status,
    upsert_node,
)
from .scoring import aggregate_node_metrics, score_from_metrics
from .timeutils import now_iso
from .llm_hypothesis import ALLOWED_ENGINES, create_llm_hypothesis_batch
from .web_research import create_web_hypothesis_batch

_RUN_LOCK = threading.Lock()
_ACTIVE_THREAD: threading.Thread | None = None
_STOP_EVENT = threading.Event()
_CYCLE_MODE = "standby"
EXPAND_SCORE_THRESHOLD = 0.42
MAX_EXPAND_DEPTH = 6
ACTIONABLE_RESEARCH_STATUSES = ("candidate", "promising", "expand", "queued", "running")
RESCUE_TAG = "rescue_attempt"


def _env_enabled(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _is_rescue_node(node: dict[str, Any]) -> bool:
    return RESCUE_TAG in (_node_tags(node) or [])


def _can_attempt_rescue(node: dict[str, Any], metrics: dict[str, Any]) -> bool:
    """True se um node prestes a ser podado merece 1 filho de tentativa.

    Regras:
    - feature ligada via AETHER_RESCUE_ENABLED (default 1)
    - filho rescue nao gera novo rescue (cadeia limitada a 1)
    - precisa ter best_probe.engine para nascer mutation parametrica
    - respeita cap de profundidade
    - probes que quebraram tecnicamente (ok=False) nao recebem rescue
    """
    if not _env_enabled("AETHER_RESCUE_ENABLED", "1"):
        return False
    if _is_rescue_node(node):
        return False
    if int(node.get("depth") or 0) >= MAX_EXPAND_DEPTH:
        return False
    if not metrics or metrics.get("ok") is False:
        return False
    best_probe = metrics.get("best_probe") or {}
    if not best_probe.get("engine"):
        return False
    return True


MUTATION_LIBRARY: dict[str, list[dict[str, Any]]] = {
    "rsi": [
        {"slug": "strict", "label": "RSI strict extremes", "probe_specs": [{"engine": "rsi", "params": {"period": 14, "oversold": 25.0, "overbought": 75.0, "long_only": False}}]},
        {"slug": "wide", "label": "RSI wide extremes", "probe_specs": [{"engine": "rsi", "params": {"period": 14, "oversold": 20.0, "overbought": 80.0, "long_only": False}}]},
        {"slug": "long-only", "label": "RSI long only", "probe_specs": [{"engine": "rsi", "params": {"period": 14, "oversold": 30.0, "overbought": 70.0, "long_only": True}}]},
    ],
    "bollinger": [
        {"slug": "narrow", "label": "Bollinger narrow band", "probe_specs": [{"engine": "bollinger", "params": {"window": 20, "num_std": 1.5, "long_only": False}}]},
        {"slug": "wide", "label": "Bollinger wide band", "probe_specs": [{"engine": "bollinger", "params": {"window": 20, "num_std": 2.5, "long_only": False}}]},
        {"slug": "long-only", "label": "Bollinger long only", "probe_specs": [{"engine": "bollinger", "params": {"window": 20, "num_std": 2.0, "long_only": True}}]},
    ],
    "ma_crossover": [
        {"slug": "fast", "label": "MA fast continuation", "probe_specs": [{"engine": "ma_crossover", "params": {"short_window": 8, "long_window": 21, "long_only": True}}]},
        {"slug": "slow", "label": "MA slow continuation", "probe_specs": [{"engine": "ma_crossover", "params": {"short_window": 30, "long_window": 90, "long_only": True}}]},
        {"slug": "two-way", "label": "MA two way", "probe_specs": [{"engine": "ma_crossover", "params": {"short_window": 20, "long_window": 50, "long_only": False}}]},
    ],
    "donchian": [
        {"slug": "fast", "label": "Donchian fast breakout", "probe_specs": [{"engine": "donchian", "params": {"entry_window": 10, "exit_window": 5, "long_only": True}}]},
        {"slug": "slow", "label": "Donchian slow breakout", "probe_specs": [{"engine": "donchian", "params": {"entry_window": 55, "exit_window": 20, "long_only": True}}]},
        {"slug": "two-way", "label": "Donchian two way", "probe_specs": [{"engine": "donchian", "params": {"entry_window": 20, "exit_window": 10, "long_only": False}}]},
    ],
}


def _node_metrics(node: dict[str, Any]) -> dict[str, Any]:
    if isinstance(node.get("metrics"), dict):
        return node["metrics"]
    return loads(node.get("metrics_json"), {})


def _node_tags(node: dict[str, Any]) -> list[str]:
    if isinstance(node.get("tags"), list):
        return node["tags"]
    return loads(node.get("tags_json"), [])


def bootstrap() -> None:
    with connect() as conn:
        ensure_agents(conn)
        seed_base_tree(conn)
        mark_agents_without_work_standby(conn)
        _purge_stale_pruned(conn)


def _purge_stale_pruned(conn, *, keep_recent: int | None = None) -> None:
    """Apaga nodes pruned/paused antigos + events/runs velhos para manter a base enxuta.

    Mantem os `keep_recent` nodes pruned/paused mais novos. Tambem trunca:
      - events com mais de N dias (AETHER_PURGE_EVENTS_DAYS, default 3)
      - runs com mais de N dias (AETHER_PURGE_RUNS_DAYS, default 3)
      - eventos/runs orfaos (sem node)
    """
    if keep_recent is None:
        keep_recent = _env_int("AETHER_PURGE_KEEP_PRUNED", 30)
    events_days = _env_int("AETHER_PURGE_EVENTS_DAYS", 3)
    runs_days = _env_int("AETHER_PURGE_RUNS_DAYS", 3)
    events_max = _env_int("AETHER_PURGE_EVENTS_MAX", 5000)
    runs_max = _env_int("AETHER_PURGE_RUNS_MAX", 2000)
    try:
        from datetime import datetime, timedelta, timezone
        cutoff_events = (datetime.now(timezone.utc) - timedelta(days=events_days)).isoformat()
        cutoff_runs = (datetime.now(timezone.utc) - timedelta(days=runs_days)).isoformat()
        conn.execute("DELETE FROM events WHERE ts < ?", (cutoff_events,))
        conn.execute(
            "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY ts DESC LIMIT ?)",
            (events_max,),
        )
        conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff_runs,))
        conn.execute(
            "DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY started_at DESC LIMIT ?)",
            (runs_max,),
        )

        rows = conn.execute(
            "SELECT id FROM nodes WHERE status IN ('pruned','paused') ORDER BY updated_at DESC"
        ).fetchall()
        if len(rows) > keep_recent:
            to_delete = [r["id"] for r in rows[keep_recent:]]
            for _ in range(20):
                placeholders = ",".join("?" * len(to_delete))
                n = conn.execute(
                    f"DELETE FROM nodes WHERE id IN ({placeholders})",
                    to_delete,
                ).rowcount
                n2 = conn.execute(
                    "DELETE FROM nodes WHERE parent_id IS NOT NULL AND parent_id != '' "
                    "AND parent_id NOT IN (SELECT id FROM nodes)"
                ).rowcount
                conn.commit()
                if n == 0 and n2 == 0:
                    break
                to_delete = []
        conn.execute(
            "DELETE FROM events WHERE node_id IS NOT NULL AND node_id != '' "
            "AND node_id NOT IN (SELECT id FROM nodes)"
        )
        conn.execute(
            "DELETE FROM tasks WHERE node_id IS NOT NULL AND node_id != '' "
            "AND node_id NOT IN (SELECT id FROM nodes)"
        )
        conn.execute(
            "DELETE FROM runs WHERE node_id IS NOT NULL AND node_id != '' "
            "AND node_id NOT IN (SELECT id FROM nodes)"
        )
        conn.execute(
            "DELETE FROM artifacts WHERE node_id IS NOT NULL AND node_id != '' "
            "AND node_id NOT IN (SELECT id FROM nodes)"
        )
        conn.commit()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
    except Exception:
        pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


FORCE_EXPLORATION_EVERY = _env_int("AETHER_FORCE_EXPLORATION_EVERY", 4)
LLM_HEARTBEAT_EVERY = _env_int("AETHER_LLM_HEARTBEAT_EVERY", 2)  # gera 1 hipotese LLM a cada N ciclos mesmo se houver candidatos


def _inflight_count(conn, source_type: str) -> int:
    """Conta hipoteses do pesquisador `source_type` ainda em pesquisa/teste.

    Conta apenas folhas RECENTES (criadas dentro da janela). Nodes em
    'candidate' parados por horas nao bloqueiam novas chamadas LLM — o
    proposito do gate eh evitar chamadas concorrentes, nao bloquear quando
    backlog estagna.

    Janela via AETHER_INFLIGHT_WINDOW_MINUTES (default 30min).
    """
    window_min = _env_int("AETHER_INFLIGHT_WINDOW_MINUTES", 30)
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
    if row is None:
        return 0
    try:
        return int(row[0] if not hasattr(row, "keys") else row["c"])
    except (TypeError, ValueError, IndexError):
        return 0


def _llm_batch_size(
    candidate_count: int,
    limit_nodes: int,
    *,
    force_exploration: bool,
    heartbeat: bool,
) -> int:
    # Cap interno default 5; pode subir via AETHER_LLM_MAX_NEW_HARD_CAP (industrial mode).
    _hard_cap = max(1, _env_int("AETHER_LLM_MAX_NEW_HARD_CAP", 5))
    max_new = max(1, min(_hard_cap, _env_int("AETHER_LLM_MAX_NEW", 3)))
    deficit = max(0, limit_nodes - candidate_count)
    if deficit > 0:
        return min(max_new, max(1, math.ceil(deficit / 5)))
    if force_exploration:
        return max_new
    if heartbeat:
        return min(max_new, max(1, _env_int("AETHER_LLM_HEARTBEAT_BATCH", max_new)))
    return 0


def _adaptive_cycle_budget(workers: int, limit_nodes: int, max_tasks: int) -> dict[str, int]:
    if not _env_enabled("AETHER_ADAPTIVE_TASK_BATCH", "1"):
        return {
            "limit_nodes": limit_nodes,
            "max_tasks": max_tasks,
            "target_probe_count": 0,
        }
    tasks_per_worker = max(1, _env_int("AETHER_TASKS_PER_WORKER", 2))
    # Watchdog throttle: hyper_controls.tasks_per_worker sobrescreve env var
    try:
        with connect() as _c:
            _ctrl = get_setting(_c, "hyper_controls", default=None)
        if isinstance(_ctrl, dict) and _ctrl.get("tasks_per_worker"):
            tasks_per_worker = max(1, int(_ctrl["tasks_per_worker"]))
    except Exception:
        pass
    probes_per_worker = max(1, _env_int("AETHER_TARGET_PROBES_PER_WORKER", 6))
    nodes_cap = max(5, _env_int("AETHER_LIMIT_NODES_CAP", 25))
    effective_limit = max(limit_nodes, min(nodes_cap, workers * tasks_per_worker))
    effective_tasks = max(max_tasks, effective_limit)
    return {
        "limit_nodes": min(nodes_cap, effective_limit),
        "max_tasks": min(nodes_cap, effective_tasks),
        "target_probe_count": max(1, workers * probes_per_worker),
    }


def _force_every() -> int:
    """Ciclo de exploracao forcada — pode ser ajustado dinamicamente pelo Hyper Agent."""
    try:
        with connect() as conn:
            controls = get_setting(conn, "hyper_controls", default=None)
        if isinstance(controls, dict):
            n = int(controls.get("force_exploration_every") or FORCE_EXPLORATION_EVERY)
            return max(1, min(20, n))
    except Exception:
        pass
    return FORCE_EXPLORATION_EVERY


def _run_research_phase(limit_nodes: int, *, loop_idx: int = 1) -> dict[str, Any]:
    with connect() as conn:
        ensure_agents(conn)
        set_agent_status(
            conn,
            "chief_orchestrator",
            "running",
            current_task_id="research",
            stats={"bubble": "Abrindo ciclo RAIO.", "phase": "research"},
        )
        set_agent_status(
            conn,
            "hypothesis_generator",
            "running",
            current_task_id="research",
            stats={"bubble": "Pesquisando o proximo ramo.", "phase": "research"},
        )
        set_agent_status(
            conn,
            "archivist_agent",
            "running",
            current_task_id="research",
            stats={"bubble": "Consultando memoria e refutacoes.", "phase": "research"},
        )
        add_event(
            conn,
            type="research_started",
            agent_role="hypothesis_generator",
            message="Pesquisa iniciada antes de abrir testes por timeframe.",
        )

    time.sleep(1.2)

    with connect() as conn:
        candidates = best_candidate_nodes(conn, limit=limit_nodes)
        generated = 0
        llm_generated = {"created_hypotheses": 0, "created_nodes": 0, "provider": None, "model": None}
        web_generated = {"created_hypotheses": 0, "created_nodes": 0, "source_count": 0}
        every = _force_every()
        force_exploration = loop_idx > 0 and loop_idx % every == 0
        # Heartbeat dinamico: hyper_controls.llm_heartbeat_every (DB) sobrescreve env.
        _hb_every = LLM_HEARTBEAT_EVERY
        try:
            _ctrl_hb = get_setting(conn, "hyper_controls", default=None)
            if isinstance(_ctrl_hb, dict) and _ctrl_hb.get("llm_heartbeat_every") is not None:
                _hb_every = max(1, int(_ctrl_hb["llm_heartbeat_every"]))
        except Exception:
            pass
        heartbeat = loop_idx > 0 and loop_idx % _hb_every == 0
        # Gera em lotes: uma chamada Claude pode abrir varias raizes, cada uma com
        # filhos por timeframe. Isso reduz tokens desperdicados em chamadas unitarias.
        llm_inflight = _inflight_count(conn, "llm_agent")
        web_inflight = _inflight_count(conn, "web_research")
        llm_batch = _llm_batch_size(
            len(candidates),
            limit_nodes,
            force_exploration=force_exploration,
            heartbeat=heartbeat,
        )
        # Gate: o pesquisador so espera folhas acionaveis. Pais visited/expanded
        # nao seguram mais a criacao de hipoteses novas.
        if llm_inflight == 0 and llm_batch > 0:
            set_agent_status(
                conn,
                "hypothesis_generator",
                "running",
                current_task_id="llm_research",
                stats={"bubble": f"Chamando Claude para ate {llm_batch} hipoteses.", "phase": "llm_research", "batch_size": llm_batch},
            )
            llm_generated = create_llm_hypothesis_batch(conn, max_new=llm_batch, force_exploration=force_exploration)
            candidates = best_candidate_nodes(conn, limit=limit_nodes)
        elif llm_inflight > 0:
            set_agent_status(
                conn,
                "hypothesis_generator",
                "standby",
                current_task_id=None,
                stats={"bubble": f"Aguardando folhas acionaveis ({llm_inflight} em teste).", "phase": "awaiting_validation"},
            )
        if web_inflight == 0 and len(candidates) < limit_nodes:
            set_agent_status(
                conn,
                "hypothesis_generator",
                "running",
                current_task_id="web_research",
                stats={"bubble": "Buscando estrategias na internet.", "phase": "web_research"},
            )
            web_generated = create_web_hypothesis_batch(conn, max_new=max(1, min(5, math.ceil((limit_nodes - len(candidates)) / 5))))
            candidates = best_candidate_nodes(conn, limit=limit_nodes)
        if not candidates:
            generated = generate_next_hypothesis_batch(conn, count=1)
            candidates = best_candidate_nodes(conn, limit=limit_nodes)

        set_agent_status(
            conn,
            "archivist_agent",
            "standby",
            current_task_id=None,
            stats={"bubble": "Memoria consultada.", "generated_nodes": generated},
        )
        set_agent_status(
            conn,
            "hypothesis_generator",
            "standby",
            current_task_id=None,
            stats={
                "bubble": f"{len(candidates)} candidatos prontos.",
                "generated_nodes": generated,
                "web_generated_nodes": web_generated.get("created_nodes", 0),
                "web_sources": web_generated.get("source_count", 0),
                "llm_generated_nodes": llm_generated.get("created_nodes", 0),
                "llm_provider": llm_generated.get("provider"),
            },
        )
        set_agent_status(
            conn,
            "scorekeeper",
            "running",
            current_task_id="research",
            stats={"bubble": "Priorizando os melhores nos.", "candidate_count": len(candidates)},
        )
        add_event(
            conn,
            type="research_completed",
            agent_role="hypothesis_generator",
            message=(
                f"Pesquisa concluida: {len(candidates)} candidatos elegiveis, "
                f"{generated} nos locais, {web_generated.get('created_nodes', 0)} nos web, "
                f"{llm_generated.get('created_nodes', 0)} nos LLM."
            ),
            payload={"candidates": [node["id"] for node in candidates], "generated": generated, "web": web_generated, "llm": llm_generated},
        )

    time.sleep(0.8)
    return {"candidate_count": len(candidates), "generated_nodes": generated}


def queue_scout_tasks(
    limit_nodes: int = 5,
    probes_per_node: int = 60,
    *,
    allow_generate: bool = True,
    target_probe_count: int | None = None,
) -> int:
    with connect() as conn:
        ensure_agents(conn)
        created = 0
        total_probes = 0
        target = max(0, int(target_probe_count or 0))
        scan_limit = max(limit_nodes, _env_int("AETHER_SCOUT_CANDIDATE_SCAN_LIMIT", 100))
        candidate_limit = limit_nodes if not target else max(limit_nodes, min(scan_limit, target * 2))
        for attempt in range(2):
            candidates = best_candidate_nodes(conn, limit=candidate_limit, per_timeframe=True)
            for node in candidates:
                if created >= limit_nodes and (not target or total_probes >= target):
                    break
                timeframe = node.get("timeframe")
                role = node.get("agent_role") or "chief_orchestrator"
                if not timeframe:
                    continue
                node_metrics = node.get("metrics") or {}
                probe_specs = node_metrics.get("probe_specs")
                allowed_engines = set(node_metrics.get("engines") or [])
                if probe_specs:
                    probes = default_probes_for_timeframe(LEGACY_ROOT, timeframe, limit=probes_per_node, engine_specs=probe_specs)
                else:
                    probe_limit = probes_per_node * 5 if allowed_engines else probes_per_node
                    probes = default_probes_for_timeframe(LEGACY_ROOT, timeframe, limit=probe_limit)
                    if allowed_engines:
                        probes = [probe for probe in probes if probe.get("engine") in allowed_engines][:probes_per_node]
                if not probes:
                    upsert_node(
                        conn,
                        id=node["id"],
                        campaign_id=node.get("campaign_id"),
                        code=node["code"],
                        name=node["name"],
                        hypothesis=node["hypothesis"],
                        parent_id=node.get("parent_id"),
                        causal_mechanism=node.get("causal_mechanism") or "",
                        mutation_from_parent=node.get("mutation_from_parent") or "",
                        timeframe=timeframe,
                        agent_role=role,
                        status="paused",
                        score=max(0.01, float(node["score"]) * 0.25),
                        priority=0.01,
                        depth=int(node["depth"]),
                        prune_reason="sem dataset/probe elegivel para scout",
                        metrics=node.get("metrics") or {},
                        tags=node.get("tags") or [],
                        source_type=node.get("source_type"),
                        source_path=node.get("source_path"),
                    )
                    add_event(
                        conn,
                        type="paused",
                        campaign_id=node.get("campaign_id"),
                        node_id=node["id"],
                        agent_role="infra_data_agent",
                        message=f"{node['name']} pausado: sem dataset/probe para {timeframe}.",
                    )
                    continue
                task_id = f"scout:{node['id']}"
                create_task(
                    conn,
                    task_id=task_id,
                    node_id=node["id"],
                    agent_role=role,
                    task_type="scout_sweep",
                    priority=float(node.get("priority") or node.get("score") or 0.5),
                    payload={"probes": probes, "timeframe": timeframe},
                )
                set_agent_status(
                    conn,
                    role,
                    "queued",
                    current_task_id=task_id,
                    stats={"bubble": f"Na fila com {len(probes)} probes.", "probes": len(probes)},
                )
                created += 1
                total_probes += len(probes)
            if created or attempt == 1:
                break
            if allow_generate:
                generated = generate_next_hypothesis_batch(conn, count=1)
                if not generated:
                    break
            else:
                break
        mark_agents_without_work_standby(conn)
        if created:
            add_event(
                conn,
                type="tasks_queued",
                campaign_id=None,
                message=f"{created} tarefas scout enfileiradas ({total_probes} probes).",
                payload={
                    "tasks": created,
                    "probes": total_probes,
                    "target_probe_count": target,
                    "candidate_limit": candidate_limit,
                },
            )
        return created


def _update_run_row(
    conn,
    *,
    run_id: str,
    campaign_id: str | None,
    node_id: str | None,
    task_id: str | None,
    sweep: dict[str, Any],
    status: str,
) -> None:
    summary = sweep.get("summary") or {}
    conn.execute(
        """
        INSERT INTO runs(
            id, campaign_id, node_id, task_id, sweep_id, status, worker_count, total,
            completed, failed, started_at, finished_at, eta_seconds,
            throughput_per_min, checkpoint_path, summary_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            campaign_id=excluded.campaign_id,
            status=excluded.status,
            worker_count=excluded.worker_count,
            total=excluded.total,
            completed=excluded.completed,
            failed=excluded.failed,
            finished_at=excluded.finished_at,
            eta_seconds=excluded.eta_seconds,
            throughput_per_min=excluded.throughput_per_min,
            checkpoint_path=excluded.checkpoint_path,
            summary_json=excluded.summary_json
        """,
        (
            run_id,
            campaign_id,
            node_id,
            task_id,
            sweep.get("sweep_id"),
            status,
            int(sweep.get("workers") or 0),
            int(sweep.get("total") or 0),
            int(sweep.get("completed") or 0),
            int(sweep.get("failed") or 0),
            now_iso(),
            now_iso() if status in {"completed", "failed"} else None,
            sweep.get("eta_seconds"),
            sweep.get("throughput_per_min"),
            sweep.get("checkpoint_path"),
            dumps(summary),
        ),
    )


def _hard_delete_subtree(conn, root_id: str) -> int:
    """Apaga node + descendentes + eventos/runs/tasks/artifacts ligados.

    Usado quando AETHER_HARD_DELETE_FAILS=1: estrategias que nao vao para ranking
    saem da arvore imediatamente em vez de virar 'pruned'.
    """
    if not root_id:
        return 0
    to_delete = {root_id}
    frontier = [root_id]
    for _ in range(64):
        if not frontier:
            break
        placeholders = ",".join("?" * len(frontier))
        children = [r[0] for r in conn.execute(
            f"SELECT id FROM nodes WHERE parent_id IN ({placeholders})", frontier
        ).fetchall()]
        new = [c for c in children if c not in to_delete]
        if not new:
            break
        to_delete.update(new)
        frontier = new
    ids = list(to_delete)
    placeholders = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM events WHERE node_id IN ({placeholders})", ids)
    conn.execute(f"DELETE FROM tasks WHERE node_id IN ({placeholders})", ids)
    conn.execute(f"DELETE FROM runs WHERE node_id IN ({placeholders})", ids)
    conn.execute(f"DELETE FROM artifacts WHERE node_id IN ({placeholders})", ids)
    n = conn.execute(f"DELETE FROM nodes WHERE id IN ({placeholders})", ids).rowcount
    conn.commit()
    return n


def _apply_scout_result(conn, node: dict[str, Any], sweep: dict[str, Any]) -> None:
    metrics = sweep.get("summary") or {}
    prior_metrics = _node_metrics(node)
    scout_count = int(prior_metrics.get("scout_count") or 0) + 1
    stored_metrics = {**prior_metrics, **metrics, "last_scout": metrics, "scout_count": scout_count}
    score, reason, should_prune = score_from_metrics(metrics)
    old_score = float(node.get("score") or 0.0)
    hard_delete = os.environ.get("AETHER_HARD_DELETE_FAILS", "1") == "1"

    rescue_attempt = False
    if should_prune and _can_attempt_rescue(node, metrics):
        rescue_attempt = True
        status = "expand"
        reason = f"{reason} (rescue: 1 mutacao de tentativa antes de prunar)"
    elif should_prune:
        status = "pruned"
    elif score >= EXPAND_SCORE_THRESHOLD:
        status = "expand"
    elif scout_count >= 3:
        status = "paused"
        reason = f"{reason} (paused apos {scout_count} scouts sem score>={EXPAND_SCORE_THRESHOLD})"
    else:
        status = "promising"

    if hard_delete and status in ("pruned", "paused"):
        n = _hard_delete_subtree(conn, node["id"])
        add_event(
            conn,
            type="hard_deleted",
            campaign_id=node.get("campaign_id"),
            node_id=None,
            agent_role=node.get("agent_role") or "scorekeeper",
            message=f"{node['name']}: {old_score:.2f} -> {score:.2f}. {reason} (galho removido, {n} nodes apagados)",
            payload={"old_score": old_score, "new_score": score, "metrics": metrics, "deleted": n},
        )
        return

    priority = 0.0 if status in ("pruned", "paused") else score
    upsert_node(
        conn,
        id=node["id"],
        campaign_id=node.get("campaign_id"),
        parent_id=node.get("parent_id"),
        code=node["code"],
        name=node["name"],
        hypothesis=node["hypothesis"],
        causal_mechanism=node.get("causal_mechanism") or "",
        mutation_from_parent=node.get("mutation_from_parent") or "",
        timeframe=node.get("timeframe"),
        agent_role=node.get("agent_role"),
        status=status,
        score=score,
        priority=priority,
        prune_reason=reason if should_prune else None,
        result_summary=reason,
        metrics=stored_metrics,
        tags=_node_tags(node),
        depth=int(node.get("depth") or 0),
        source_type=node.get("source_type"),
        source_path=node.get("source_path"),
    )
    expanded = (
        _expand_survivor(conn, node, score, metrics, rescue=rescue_attempt)
        if status == "expand"
        else 0
    )
    if status == "expand":
        if rescue_attempt and expanded == 0:
            # Nao conseguiu gerar mutacao rescue (eixos esgotados etc): cai pra prune normal.
            conn.execute(
                "UPDATE nodes SET status='pruned', priority=0, prune_reason=?, "
                "result_summary=?, updated_at=? WHERE id=?",
                (
                    f"{reason} (rescue indisponivel)",
                    f"{reason} (rescue indisponivel)",
                    now_iso(),
                    node["id"],
                ),
            )
            if hard_delete:
                _hard_delete_subtree(conn, node["id"])
        else:
            if rescue_attempt:
                summary = f"{reason} Rescue: gerou 1 tentativa final."
            elif expanded:
                summary = f"{reason} Gerou {expanded} mutacoes candidatas."
            else:
                summary = f"{reason} Sem mutacao nova ou cap de profundidade; aguardando validacao."
            conn.execute(
                "UPDATE nodes SET status='expanded', result_summary=?, updated_at=? WHERE id=?",
                (summary, now_iso(), node["id"]),
            )
    add_event(
        conn,
        type="pruned" if should_prune else "score",
        campaign_id=node.get("campaign_id"),
        node_id=node["id"],
        agent_role=node.get("agent_role") or "scorekeeper",
        message=f"{node['name']}: {old_score:.2f} -> {score:.2f}. {reason}",
        payload={"old_score": old_score, "new_score": score, "metrics": metrics},
    )


def _params_signature(engine: str, params: dict[str, Any]) -> str:
    return hashlib.sha1(
        f"{engine}|{json.dumps(params, sort_keys=True)}".encode("utf-8")
    ).hexdigest()[:12]


def _ancestor_mutation_signatures(conn, node_id: str) -> set[str]:
    sigs: set[str] = set()
    current = node_id
    seen_nodes: set[str] = set()
    for _ in range(64):
        if not current or current in seen_nodes:
            break
        seen_nodes.add(current)
        row = conn.execute(
            "SELECT parent_id, metrics_json FROM nodes WHERE id=?",
            (current,),
        ).fetchone()
        if not row:
            break
        m = loads(row["metrics_json"], {})
        for spec in m.get("probe_specs") or []:
            engine = str(spec.get("engine") or "")
            params = spec.get("params") or {}
            if engine:
                sigs.add(_params_signature(engine, params))
        current = row["parent_id"]
    sibling_rows = conn.execute(
        "SELECT metrics_json FROM nodes WHERE parent_id=? AND id LIKE ?",
        (node_id, f"{node_id}:mut:%"),
    ).fetchall()
    for row in sibling_rows:
        m = loads(row["metrics_json"], {})
        for spec in m.get("probe_specs") or []:
            engine = str(spec.get("engine") or "")
            params = spec.get("params") or {}
            if engine:
                sigs.add(_params_signature(engine, params))
    return sigs


_PARAM_AXES: dict[str, list[tuple[str, list[Any]]]] = {
    "rsi": [
        ("period", [7, 14, 21, 28]),
        ("oversold", [20.0, 25.0, 30.0, 35.0]),
        ("overbought", [65.0, 70.0, 75.0, 80.0]),
        ("long_only", [True, False]),
    ],
    "bollinger": [
        ("window", [10, 20, 30, 50]),
        ("num_std", [1.5, 2.0, 2.5, 3.0]),
        ("long_only", [True, False]),
    ],
    "ma_crossover": [
        ("short_window", [5, 8, 10, 20, 30]),
        ("long_window", [21, 50, 100, 200]),
        ("long_only", [True, False]),
    ],
    "donchian": [
        ("entry_window", [10, 20, 30, 55]),
        ("exit_window", [5, 10, 20]),
        ("long_only", [True, False]),
    ],
}


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned[:40] or "mut"


def _propose_mutations(
    engine: str,
    base_params: dict[str, Any],
    used: set[str],
) -> list[dict[str, Any]]:
    axes = _PARAM_AXES.get(engine, [])
    if not axes:
        return []
    proposals: list[dict[str, Any]] = []
    for axis_name, values in axes:
        for value in values:
            params = {**base_params, axis_name: value}
            if engine == "ma_crossover":
                short = int(params.get("short_window", 10))
                long = int(params.get("long_window", 50))
                if short >= long:
                    continue
            sig = _params_signature(engine, params)
            if sig in used:
                continue
            label_value = value if not isinstance(value, float) else round(value, 2)
            label = f"{engine} {axis_name}={label_value}"
            slug = _slugify(f"{axis_name}-{label_value}-{sig[:6]}")
            proposals.append(
                {
                    "slug": slug,
                    "label": label,
                    "hypothesis": f"Variacao no eixo {axis_name}={label_value} a partir do melhor probe do pai.",
                    "probe_specs": [{"engine": engine, "params": params}],
                    "signature": sig,
                }
            )
    return proposals


def _llm_propose_mutations(
    conn,
    node: dict[str, Any],
    engine: str,
    base_params: dict[str, Any],
    used: set[str],
    score: float,
    metrics: dict[str, Any],
) -> list[dict[str, Any]]:
    if _env_enabled("AETHER_ECONOMY_MODE", "1") and not _env_enabled("AETHER_LLM_MUTATIONS_ENABLED", "0"):
        return []
    if engine not in ALLOWED_ENGINES:
        return []
    system = (
        "Voce e um pesquisador quantitativo. Recebe um nó sobrevivente da arvore RAIO "
        "do Aether com seu melhor probe atual. Sua tarefa: propor 1 a 3 mutacoes de "
        "parametros realmente diferentes das ja testadas, na mesma engine. Responda "
        "somente JSON valido."
    )
    used_brief = list(used)[:30]
    prompt = json.dumps(
        {
            "engine": engine,
            "current_params": base_params,
            "parent_score": score,
            "best_metrics": {
                "sharpe": metrics.get("sharpe"),
                "pnl_pct": metrics.get("pnl_pct"),
                "mdd_pct": metrics.get("mdd_pct"),
                "trade_count": metrics.get("trade_count"),
            },
            "exhausted_signatures_sample": used_brief,
            "schema": {
                "mutations": [
                    {
                        "label": "nome curto",
                        "rationale": "porque essa mudanca",
                        "params": {"...": "params completos da engine"},
                    }
                ]
            },
        },
        ensure_ascii=False,
    )
    response = call_llm_agent(
        "hypothesis_generator",
        system=system,
        prompt=prompt,
        max_tokens=450,
    )
    record_llm_trace(
        conn,
        agent_role="hypothesis_generator",
        action="mutation_proposal",
        response=response,
        system=system,
        prompt=prompt,
        node_id=node.get("id"),
        campaign_id=node.get("campaign_id"),
    )
    if not response.get("ok"):
        return []
    text = str(response.get("text") or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return []
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    raw = payload.get("mutations") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    proposals: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        params = item.get("params") if isinstance(item.get("params"), dict) else None
        if not params:
            continue
        sig = _params_signature(engine, params)
        if sig in used:
            continue
        label = str(item.get("label") or f"{engine} llm").strip()[:80]
        rationale = str(item.get("rationale") or "")[:300]
        proposals.append(
            {
                "slug": _slugify(f"llm-{label}-{sig[:6]}"),
                "label": label,
                "hypothesis": rationale or f"Mutacao sugerida pelo LLM ({engine}).",
                "probe_specs": [{"engine": engine, "params": params}],
                "signature": sig,
            }
        )
    return proposals


def _expand_survivor(
    conn,
    node: dict[str, Any],
    score: float,
    metrics: dict[str, Any],
    *,
    rescue: bool = False,
) -> int:
    node_id = node.get("id")
    timeframe = node.get("timeframe")
    if not node_id or not timeframe:
        return 0

    current_depth = int(node.get("depth") or 0)
    if current_depth >= MAX_EXPAND_DEPTH:
        add_event(
            conn,
            type="expansion_capped",
            campaign_id=node.get("campaign_id"),
            node_id=node_id,
            agent_role="hypothesis_generator",
            message=f"{node.get('name')}: cap de profundidade atingido (depth={current_depth} >= {MAX_EXPAND_DEPTH}).",
        )
        return 0

    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM nodes WHERE parent_id=? AND status IN ('candidate','queued','running')",
        (node_id,),
    ).fetchone()
    if pending and int(pending["n"]) > 0:
        return 0

    best_probe = metrics.get("best_probe") or {}
    engine = best_probe.get("engine")
    if not engine:
        engines = _node_metrics(node).get("engines") or []
        engine = engines[0] if engines else None
    if not engine:
        return 0

    used_signatures = _ancestor_mutation_signatures(conn, node_id)
    base_params = dict(best_probe.get("params") or {})
    proposals = _propose_mutations(engine, base_params, used_signatures)
    proposal_source = "parametric" if proposals else ""
    if not proposals:
        proposals = _llm_propose_mutations(conn, node, engine, base_params, used_signatures, score, metrics)
        proposal_source = "llm" if proposals else "exhausted"
    if not proposals:
        add_event(
            conn,
            type="expansion_exhausted",
            campaign_id=node.get("campaign_id"),
            node_id=node_id,
            agent_role="hypothesis_generator",
            message=f"{node.get('name')}: sem mutacoes novas (paramétricas e LLM esgotadas).",
        )
        return 0

    if rescue:
        fanout = 1
    else:
        fanout = 1 if score < EXPAND_SCORE_THRESHOLD + 0.05 else (2 if score < 0.6 else 3)
    chosen = proposals[:fanout]
    rescue_tags = [RESCUE_TAG] if rescue else []

    base_name = str(node.get("name") or "").split(" / ")[0]
    sibling_count = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM nodes WHERE parent_id=? AND id LIKE ?",
            (node_id, f"{node_id}:mut:%"),
        ).fetchone()["n"]
    )

    created = 0
    for offset, proposal in enumerate(chosen, start=1):
        attempt = sibling_count + offset
        child_score = max(0.1, score - offset * 0.02)
        slug = proposal["slug"]
        upsert_node(
            conn,
            id=f"{node_id}:mut:{slug}",
            campaign_id=node.get("campaign_id"),
            parent_id=node_id,
            code=f"{node.get('code') or 'N'}-M{attempt}",
            name=f"{base_name} / {proposal['label']} (M{attempt})",
            hypothesis=proposal.get("hypothesis") or f"Mutacao parametrica: {proposal['label']}.",
            causal_mechanism=node.get("causal_mechanism") or "",
            mutation_from_parent=f"{proposal_source}: {proposal['label']} (parent score {score:.2f}, PnL {metrics.get('pnl_pct', 0):.2f}%).",
            timeframe=node.get("timeframe"),
            agent_role=node.get("agent_role"),
            status="candidate",
            score=child_score,
            priority=child_score,
            depth=int(node.get("depth") or 0) + 1,
            metrics={
                "engines": [engine],
                "probe_specs": proposal["probe_specs"],
                "generator": f"survivor_expansion:{proposal_source}",
                "parent_score": score,
                "source_url": _node_metrics(node).get("source_url"),
                "source_title": _node_metrics(node).get("source_title"),
            },
            tags=[*_node_tags(node), "mutation", f"engine:{engine}", proposal_source, *rescue_tags],
            source_type=node.get("source_type"),
            source_path=node.get("source_path"),
        )
        created += 1
    add_event(
        conn,
        type="node_rescued" if rescue else "node_expanded",
        campaign_id=node.get("campaign_id"),
        node_id=node_id,
        agent_role="hypothesis_generator",
        message=(
            f"{node.get('name')} {'rescue' if rescue else 'expandido'} com {created} "
            f"mutacoes ({proposal_source})."
        ),
        payload={
            "engine": engine,
            "children": created,
            "source": proposal_source,
            "fanout": fanout,
            "rescue": rescue,
        },
    )
    return created


def _run_queued_scouts(workers: int, max_tasks: int) -> None:
    with connect() as conn:
        ensure_agents(conn)
        tasks = conn.execute(
            """
            SELECT * FROM tasks
            WHERE status='queued' AND task_type='scout_sweep'
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
            """,
            (max_tasks,),
        ).fetchall()
    if not tasks:
        with connect() as conn:
            mark_agents_without_work_standby(conn)
        return

    sweep_id = f"sweep-batch-{uuid.uuid4().hex[:10]}"
    task_infos: list[dict[str, Any]] = []
    all_probes: list[dict[str, Any]] = []
    with connect() as conn:
        for task in tasks:
            task_id = task["id"]
            node_id = task["node_id"]
            role = task["agent_role"]
            payload = json.loads(task["payload_json"])
            probes = list(payload.get("probes") or [])
            run_id = f"run-{uuid.uuid4().hex[:10]}"
            node = conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            node_dict = dict(node) if node else {}
            campaign_id = node_dict.get("campaign_id") or task["campaign_id"]

            update_task_status(conn, task_id, "running", attempts_delta=1)
            set_agent_status(
                conn,
                role,
                "running",
                current_task_id=task_id,
                stats={
                    "bubble": f"Testando {len(probes)} probes.",
                    "probes": len(probes),
                    "sweep_id": sweep_id,
                },
            )
            add_event(
                conn,
                type="task_started",
                campaign_id=campaign_id,
                node_id=node_id,
                agent_role=role,
                message=f"Scout iniciado com {len(probes)} probes.",
            )
            _update_run_row(
                conn,
                run_id=run_id,
                campaign_id=campaign_id,
                node_id=node_id,
                task_id=task_id,
                sweep={"sweep_id": sweep_id, "workers": workers, "total": len(probes), "completed": 0, "failed": 0},
                status="running",
            )

            task_infos.append(
                {
                    "task_id": task_id,
                    "node_id": node_id,
                    "role": role,
                    "run_id": run_id,
                    "node": node_dict,
                    "probe_count": len(probes),
                    "campaign_id": campaign_id,
                }
            )
            for probe in probes:
                all_probes.append({**probe, "_task_id": task_id, "_node_id": node_id, "_agent_role": role})

    try:
        sweep = run_sweep(
            probes=all_probes,
            legacy_root=LEGACY_ROOT,
            workers=workers,
            sweep_id=sweep_id,
        )
        results_by_task: dict[str, list[dict[str, Any]]] = {info["task_id"]: [] for info in task_infos}
        for result in sweep.get("results") or []:
            task_id = result.get("_task_id")
            if task_id in results_by_task:
                results_by_task[task_id].append(result)

        with connect() as conn:
            for info in task_infos:
                results = results_by_task.get(info["task_id"], [])
                task_sweep = {
                    **sweep,
                    "total": info["probe_count"],
                    "completed": len(results),
                    "failed": sum(1 for r in results if not r.get("ok")),
                    "summary": aggregate_node_metrics(results),
                    "results": results,
                }
                _update_run_row(
                    conn,
                    run_id=info["run_id"],
                    campaign_id=info["campaign_id"],
                    node_id=info["node_id"],
                    task_id=info["task_id"],
                    sweep=task_sweep,
                    status="completed",
                )
                _apply_scout_result(conn, info["node"], task_sweep)
                update_task_status(conn, info["task_id"], "completed")
                set_agent_status(
                    conn,
                    info["role"],
                    "standby",
                    current_task_id=None,
                    stats={
                        "bubble": "Scout concluido.",
                        "last_run": info["run_id"],
                        "reason": "tarefa concluida",
                    },
                )
                add_event(
                    conn,
                    type="task_completed",
                    campaign_id=info["campaign_id"],
                    node_id=info["node_id"],
                    agent_role=info["role"],
                    message=f"Scout concluido: {task_sweep['completed']}/{task_sweep['total']} probes, {task_sweep['failed']} falhas.",
                    payload={"run_id": info["run_id"], "sweep_id": sweep_id},
                )
    except Exception as exc:
        with connect() as conn:
            for info in task_infos:
                failed_sweep = {
                    "sweep_id": sweep_id,
                    "workers": workers,
                    "total": info["probe_count"],
                    "completed": 0,
                    "failed": info["probe_count"],
                }
                _update_run_row(
                    conn,
                    run_id=info["run_id"],
                    campaign_id=info["campaign_id"],
                    node_id=info["node_id"],
                    task_id=info["task_id"],
                    sweep=failed_sweep,
                    status="failed",
                )
                update_task_status(conn, info["task_id"], "failed", error=str(exc))
                set_agent_status(
                    conn,
                    info["role"],
                    "standby",
                    current_task_id=None,
                    stats={
                        "bubble": "Falha tecnica registrada.",
                        "error": str(exc),
                        "reason": "falha tecnica",
                    },
                )
                add_event(
                    conn,
                    type="task_failed",
                    campaign_id=info["campaign_id"],
                    node_id=info["node_id"],
                    agent_role=info["role"],
                    message=str(exc)[:500],
                )

    with connect() as conn:
        mark_agents_without_work_standby(conn)


def start_cycle_background(
    *,
    workers: int | None = None,
    limit_nodes: int = 5,
    probes_per_node: int = 60,
    max_tasks: int = 5,
    continuous: bool = False,
    idle_seconds: float = 10.0,
) -> dict[str, Any]:
    global _ACTIVE_THREAD, _CYCLE_MODE
    if not _RUN_LOCK.acquire(blocking=False):
        return {"started": False, "reason": "cycle already running"}
    effective_workers = max(1, int(workers or DEFAULT_WORKERS))
    budget = _adaptive_cycle_budget(effective_workers, limit_nodes, max_tasks)
    effective_limit_nodes = budget["limit_nodes"]
    effective_max_tasks = budget["max_tasks"]
    target_probe_count = budget["target_probe_count"]
    try:
        _STOP_EVENT.clear()
        _CYCLE_MODE = "continuous" if continuous else "single"

        with connect() as conn:
            ensure_agents(conn)
            set_agent_status(
                conn,
                "chief_orchestrator",
                "running",
                current_task_id="research",
                stats={"bubble": "Iniciando pesquisa.", "phase": "research"},
            )
            set_agent_status(
                conn,
                "hypothesis_generator",
                "running",
                current_task_id="research",
                stats={"bubble": "Procurando uma hipotese.", "phase": "research"},
            )
    except Exception:
        _CYCLE_MODE = "standby"
        _RUN_LOCK.release()
        raise

    def target() -> None:
        global _CYCLE_MODE
        try:
            bootstrap()
            loop_idx = 0
            while not _STOP_EVENT.is_set():
                loop_idx += 1
                research = _run_research_phase(limit_nodes=effective_limit_nodes, loop_idx=loop_idx)
                if _STOP_EVENT.is_set():
                    break
                # Esteira industrial: permitir gerar hipoteses dentro do loop
                # continuo (default True). Antes era False, o que deixava o
                # ciclo idle quando _run_research_phase nao produzia nada novo.
                _allow_gen = _env_enabled("AETHER_CONTINUOUS_ALLOW_GENERATE", "1")
                queued = queue_scout_tasks(
                    limit_nodes=effective_limit_nodes,
                    probes_per_node=probes_per_node,
                    allow_generate=_allow_gen,
                    target_probe_count=target_probe_count,
                )
                with connect() as conn:
                    set_agent_status(
                        conn,
                        "chief_orchestrator",
                        "running",
                        current_task_id="dispatch",
                        stats={
                            "bubble": f"Despachei {queued} testes.",
                            "research": research,
                            "loop": loop_idx,
                            "mode": _CYCLE_MODE,
                            "effective_limit_nodes": effective_limit_nodes,
                            "effective_max_tasks": effective_max_tasks,
                            "target_probe_count": target_probe_count,
                        },
                    )
                if queued:
                    _run_queued_scouts(effective_workers, max_tasks=effective_max_tasks)
                else:
                    with connect() as conn:
                        set_agent_status(
                            conn,
                            "chief_orchestrator",
                            "running",
                            current_task_id="idle",
                            stats={
                                "bubble": "Sem candidatos agora; vou tentar de novo.",
                                "loop": loop_idx,
                                "mode": _CYCLE_MODE,
                            },
                        )
                        add_event(
                            conn,
                            type="cycle_idle",
                            agent_role="chief_orchestrator",
                            message="Ciclo continuo sem tarefa elegivel; aguardando nova tentativa.",
                        )
                    _STOP_EVENT.wait(idle_seconds)
                if not continuous:
                    break
            with connect() as conn:
                if _STOP_EVENT.is_set():
                    add_event(conn, type="cycle_stopped", agent_role="chief_orchestrator", message="Ciclo parado por comando humano.")
                mark_agents_without_work_standby(conn)
        finally:
            _CYCLE_MODE = "standby"
            _RUN_LOCK.release()

    _ACTIVE_THREAD = threading.Thread(target=target, name="aether-cycle", daemon=True)
    try:
        _ACTIVE_THREAD.start()
    except Exception:
        _CYCLE_MODE = "standby"
        _RUN_LOCK.release()
        raise
    return {
        "started": True,
        "workers": effective_workers,
        "limit_nodes": limit_nodes,
        "effective_limit_nodes": effective_limit_nodes,
        "probes_per_node": probes_per_node,
        "max_tasks": max_tasks,
        "effective_max_tasks": effective_max_tasks,
        "target_probe_count": target_probe_count,
        "continuous": continuous,
    }


def is_cycle_running() -> bool:
    return _RUN_LOCK.locked()


def stop_cycle() -> dict[str, Any]:
    if not is_cycle_running():
        return {"stopping": False, "reason": "cycle not running"}
    _STOP_EVENT.set()
    with connect() as conn:
        set_agent_status(
            conn,
            "chief_orchestrator",
            "running",
            current_task_id="stopping",
            stats={"bubble": "Parada solicitada; encerrando apos o lote atual.", "mode": _CYCLE_MODE},
        )
        add_event(conn, type="cycle_stop_requested", agent_role="chief_orchestrator", message="Parada solicitada pelo usuario.")
    return {"stopping": True, "mode": _CYCLE_MODE}


def is_stop_requested() -> bool:
    return _STOP_EVENT.is_set()


def cycle_mode() -> str:
    return _CYCLE_MODE if is_cycle_running() else "standby"
