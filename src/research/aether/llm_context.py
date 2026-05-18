"""Context builders do Hypothesis Generator.

Funcoes que leem o estado da arvore (via DB) e produzem context strings ou
metricas usadas pelo prompt LLM. Extraidas de `llm_hypothesis.py` para
isolar a camada de dados da camada de orquestracao.

Read-only (SELECT only). Nao escrevem no DB.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import TIMEFRAMES
from .llm_prompts import ALLOWED_ENGINES


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _ranking_reset_after(conn, campaign_id: str) -> str | None:
    if os.environ.get("AETHER_LLM_CONTEXT_RESPECT_RANKING_RESET", "1").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None
    try:
        from .db import get_setting

        state = get_setting(conn, f"ranking_hidden_node_ids:{campaign_id}", default={})
    except Exception:
        return None
    if isinstance(state, dict) and state.get("reset_at"):
        return str(state["reset_at"])
    return None


def collect_exploration_context(conn, campaign_id: str):
    """Returns (context_block, dominant_engine, dominance_pct, family_active, family_validated, tf_active)
    para o prompt + diversity gate + per-family cap + TF-aware spawning.
    """
    reset_after = _ranking_reset_after(conn, campaign_id)
    reset_clause = "AND created_at > ?" if reset_after else ""
    params: list[Any] = [campaign_id]
    if reset_after:
        params.append(reset_after)
    params.append(_env_int("AETHER_LLM_CONTEXT_ROWS", 120))
    rows = conn.execute(
        f"""
        SELECT id, name, status, score, prune_reason, agent_role, metrics_json, tags_json, timeframe, updated_at
        FROM nodes
        WHERE campaign_id=? AND id != 'root' AND id NOT LIKE 'tf:%'
          {reset_clause}
        ORDER BY updated_at DESC LIMIT ?
        """,
        params,
    ).fetchall()

    family_active: dict[str, int] = {}
    family_validated: dict[str, int] = {}
    tf_active: dict[str, int] = {}
    pruned_recent: list[dict[str, Any]] = []
    validated_recent: list[dict[str, Any]] = []

    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except Exception:
            metrics = {}
        try:
            tags = json.loads(row["tags_json"] or "[]")
        except Exception:
            tags = []
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
        engine = (engine or "unknown").lower()
        status = row["status"]

        if status not in ("pruned", "paused", "archive"):
            family_active[engine] = family_active.get(engine, 0) + 1
            tf = (row["timeframe"] or "").strip()
            if tf:
                tf_active[tf] = tf_active.get(tf, 0) + 1
        if status == "validated":
            family_validated[engine] = family_validated.get(engine, 0) + 1

        if status in ("pruned", "paused") and len(pruned_recent) < _env_int("AETHER_LLM_CONTEXT_PRUNED", 6):
            pruned_recent.append({
                "name": str(row["name"] or row["id"]),
                "engine": engine,
                "reason": str(row["prune_reason"] or "sem motivo"),
            })

        if status == "validated" and len(validated_recent) < _env_int("AETHER_LLM_CONTEXT_VALIDATED", 4):
            validated_recent.append({
                "name": str(row["name"] or row["id"]),
                "engine": engine,
                "score": float(row["score"] or 0.0),
            })

    total_active = sum(family_active.values()) or 1
    sorted_families = sorted(family_active.items(), key=lambda kv: kv[1], reverse=True)
    dominant_engine = sorted_families[0][0] if sorted_families else ""
    dominance_pct = (sorted_families[0][1] / total_active * 100.0) if sorted_families else 0.0

    family_lines = ", ".join(f"{name}={count}" for name, count in sorted_families[:6]) or "(nenhuma)"
    pruned_lines = (
        "\n".join(f"- {h['name'][:40]} ({h['engine']}): {h['reason'][:55]}" for h in pruned_recent)
        or "- (nenhuma poda recente)"
    )
    validated_lines = (
        "\n".join(f"- {h['name'][:40]} ({h['engine']}, score {h['score']:.2f})" for h in validated_recent)
        or "- (nenhuma validada ainda)"
    )

    saturated_lines = (
        ", ".join(
            f"{name}={family_active.get(name,0)}a/{family_validated.get(name,0)}v"
            for name in sorted(set(family_active) | set(family_validated))
        )
        or "(nenhuma)"
    )
    used_engines = set(family_active) | set(family_validated)
    virgin_engines = [e for e in sorted(ALLOWED_ENGINES) if e not in used_engines and e != "combo"]
    if virgin_engines:
        virgin_lines = ", ".join(virgin_engines[:8])
        if len(virgin_engines) > 8:
            virgin_lines += f", +{len(virgin_engines) - 8}"
    else:
        virgin_lines = "(nenhuma — todas exploradas)"
    # Distribuicao de timeframes ativos (pra prompt + boost de TF rara nos filhos)
    tf_total = sum(tf_active.values()) or 1
    tf_lines = ", ".join(
        f"{tf}={tf_active.get(tf,0)} ({tf_active.get(tf,0)/tf_total*100:.0f}%)"
        for tf in TIMEFRAMES
    )
    underused_tfs = [tf for tf in TIMEFRAMES if tf_active.get(tf, 0) / tf_total < 0.10]
    underused_line = ", ".join(underused_tfs) if underused_tfs else "(distribuicao razoavel)"
    block = (
        f"Familias: {family_lines}\n"
        f"Dominante: {dominant_engine or 'n/a'} ({dominance_pct:.0f}%)\n"
        f"Saturacao por familia (ativas/validadas): {saturated_lines}\n"
        f"ENGINES VIRGENS (zero uso, PRIORIZE ESTAS): {virgin_lines}\n"
        f"Distribuicao de timeframe (ativos): {tf_lines}\n"
        f"TIMEFRAMES SUB-EXPLORADOS (<10%, PRIORIZE NA HIPOTESE): {underused_line}\n"
        f"Podadas recentes:\n{pruned_lines}\n"
        f"Validadas recentes:\n{validated_lines}"
    )
    return block, dominant_engine, dominance_pct, family_active, family_validated, tf_active


def recent_critic_failures(conn, campaign_id: str, *, days: int = 7, top_n: int = 5) -> list[tuple[int, str]]:
    """Top razoes de rejeicao do Critic dos ultimos N dias.

    Retorna lista [(count, reason_short)]. Usado pra injetar no prompt e fechar
    o loop: o pesquisador deixa de propor hipoteses que ja sabe que serao
    rejeitadas pelos mesmos motivos.
    """
    try:
        rows = conn.execute(
            """
            SELECT json_extract(payload_json, '$.reason') AS reason, COUNT(*) AS n
            FROM events
            WHERE type='critic_rejected'
              AND campaign_id=?
              AND ts > datetime('now', ?)
            GROUP BY substr(json_extract(payload_json, '$.reason'), 1, 60)
            ORDER BY n DESC LIMIT ?
            """,
            (campaign_id, f"-{int(days)} days", int(top_n)),
        ).fetchall()
    except Exception:
        return []
    out: list[tuple[int, str]] = []
    for row in rows:
        reason = row["reason"] if hasattr(row, "keys") else row[0]
        n = row["n"] if hasattr(row, "keys") else row[1]
        if not reason:
            continue
        out.append((int(n), str(reason)[:160]))
    return out


def last_cycle_useful_ratio(conn, campaign_id: str) -> float:
    """Fracao de hipoteses uteis no ultimo ciclo do hypothesis_generator.

    util = created / (created + rejected_diversity + rejected_family_cap +
    rejected_duplicate). Usado pra adaptive cooldown -- se o ciclo anterior
    teve baixa utilidade (LLM gerando redundancia), aumenta o cooldown e
    evita queimar mais $.
    """
    row = conn.execute(
        """
        SELECT payload_json FROM events
        WHERE type='llm_research_completed' AND campaign_id=?
        ORDER BY ts DESC LIMIT 1
        """,
        (campaign_id,),
    ).fetchone()
    if not row:
        return 1.0
    try:
        payload = json.loads(row["payload_json"] if hasattr(row, "keys") else row[0] or "{}")
        created = int(payload.get("created") or 0)
        rd = int(payload.get("rejected_diversity") or 0)
        rf = int(payload.get("rejected_family_cap") or 0)
        rdup = int(payload.get("rejected_duplicate") or 0)
        total = created + rd + rf + rdup
        return float(created / total) if total > 0 else 1.0
    except Exception:
        return 1.0


def hourly_llm_cap_exhausted(conn, campaign_id: str) -> bool:
    """Esteira industrial: cap rolling de chamadas LLM por hora.

    Le hyper_controls.max_llm_calls_per_hour (DB). Se 0 ou nao setado, sem cap.
    """
    try:
        from .db import get_setting
        ctrl = get_setting(conn, "hyper_controls", default=None)
        cap = 0
        if isinstance(ctrl, dict) and ctrl.get("max_llm_calls_per_hour") is not None:
            cap = max(0, int(ctrl["max_llm_calls_per_hour"]))
        if cap <= 0:
            return False
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        n = conn.execute(
            """
            SELECT COUNT(*) AS n FROM llm_traces
            WHERE campaign_id=? AND agent_role='hypothesis_generator'
              AND action='hypothesis_generation' AND ts > ?
            """,
            (campaign_id, cutoff),
        ).fetchone()[0]
        return int(n or 0) >= cap
    except Exception:
        return False


def recent_generation_count(conn, campaign_id: str) -> int:
    """Numero de chamadas LLM (geracao de hipoteses) na janela de cooldown.

    Combinou tres mecanismos:
    - Cap rolling 1h (hyper_controls.max_llm_calls_per_hour) tem prioridade absoluta:
      retorna 999 quando atingido, forcando skip no caller.
    - Cooldown configuravel via env (AETHER_LLM_HYPOTHESIS_COOLDOWN_MINUTES, default 15)
      ou hyper_controls.llm_cooldown_minutes (DB).
    - Adaptive cooldown: se o ultimo ciclo teve <50% de utilidade (criou poucas
      vs muitas redundancias), dobra a janela pra dar tempo dos signals
      (validador, critic, hyper) abrirem espaco antes de chamar o LLM de novo.
    """
    if hourly_llm_cap_exhausted(conn, campaign_id):
        return 999  # forca skip "cooldown" no caller
    cooldown_minutes = _env_int("AETHER_LLM_HYPOTHESIS_COOLDOWN_MINUTES", 15)
    try:
        from .db import get_setting
        ctrl = get_setting(conn, "hyper_controls", default=None)
        if isinstance(ctrl, dict) and ctrl.get("llm_cooldown_minutes") is not None:
            cooldown_minutes = max(0, int(ctrl["llm_cooldown_minutes"]))
    except Exception:
        pass
    if cooldown_minutes <= 0:
        return 0
    try:
        ratio = last_cycle_useful_ratio(conn, campaign_id)
        if ratio < 0.5:
            cooldown_minutes = int(cooldown_minutes * 2)
    except Exception:
        pass
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM llm_traces
        WHERE campaign_id=?
          AND agent_role='hypothesis_generator'
          AND action='hypothesis_generation'
          AND status='ok'
          AND ts >= ?
        """,
        (campaign_id, cutoff),
    ).fetchone()
    return int(rows["n"] if rows else 0)
