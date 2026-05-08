"""Tree Focus: prioriza FECHAR ramos abertos antes de criar novas raizes.

Politica:
  - status "abertos"     = candidate, queued, running, expand, promising
  - status "finalizados" = validated, pruned, paused, archive
  - status "neutros"     = visited, expanded

Sinais expostos:
  - open_count, closed_count, completion_ratio
  - should_open_new_branch(): True se podemos abrir hipotese-raiz nova
  - priority_targets(conn): lista de IDs/nomes de ramos abertos prioritarios

Threshold via env:
  - AETHER_FOCUS_OPEN_BUDGET=12     (max ramos abertos antes de bloquear novas raizes)
  - AETHER_FOCUS_COMPLETION_MIN=0.55 (% finalizados minimo para abrir novas raizes)
  - AETHER_FOCUS_DISABLED=0          (1 desativa o gate)
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any


OPEN_STATUSES = {"candidate", "queued", "running", "expand", "promising"}
CLOSED_STATUSES = {"validated", "pruned", "paused", "archive"}
NEUTRAL_STATUSES = {"visited", "expanded"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def assess(conn: sqlite3.Connection, campaign_id: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT status, depth
        FROM nodes
        WHERE campaign_id=? AND id != 'root' AND id NOT LIKE 'tf:%'
        """,
        (campaign_id,),
    ).fetchall()
    open_count = 0
    closed_count = 0
    neutral_count = 0
    other_count = 0
    deepest_open = 0
    for row in rows:
        st = str(row["status"] or "").lower()
        d = int(row["depth"] or 0)
        if st in OPEN_STATUSES:
            open_count += 1
            if d > deepest_open:
                deepest_open = d
        elif st in CLOSED_STATUSES:
            closed_count += 1
        elif st in NEUTRAL_STATUSES:
            neutral_count += 1
        else:
            other_count += 1
    total = open_count + closed_count + neutral_count + other_count
    actionable_total = open_count + closed_count
    completion = (closed_count / actionable_total) if actionable_total else (1.0 if total else 0.0)
    return {
        "total": total,
        "actionable_total": actionable_total,
        "open_count": open_count,
        "closed_count": closed_count,
        "neutral_count": neutral_count,
        "other_count": other_count,
        "completion_ratio": round(completion, 3),
        "deepest_open_depth": deepest_open,
    }


def should_open_new_branch(conn: sqlite3.Connection, campaign_id: str) -> tuple[bool, str, dict[str, Any]]:
    """Decide se deve gerar hipotese-raiz nova ou expandir/finalizar abertas."""
    if _env_truthy("AETHER_FOCUS_DISABLED"):
        return True, "focus desativado por env", assess(conn, campaign_id)
    a = assess(conn, campaign_id)
    # Esteira industrial: hyper_controls.focus_open_budget (DB) sobrescreve env.
    budget = _env_int("AETHER_FOCUS_OPEN_BUDGET", 12)
    completion_min = _env_float("AETHER_FOCUS_COMPLETION_MIN", 0.55)
    try:
        from .db import get_setting
        ctrl = get_setting(conn, "hyper_controls", default=None)
        if isinstance(ctrl, dict):
            if ctrl.get("focus_open_budget") is not None:
                budget = max(1, int(ctrl["focus_open_budget"]))
            if ctrl.get("focus_completion_min") is not None:
                completion_min = max(0.0, float(ctrl["focus_completion_min"]))
            if ctrl.get("focus_disabled"):
                return True, "focus desativado por DB", a
    except Exception:
        pass
    # Arvore vazia: deixa criar.
    if a["actionable_total"] < budget:
        return True, f"arvore pequena (acionaveis={a['actionable_total']} < budget={budget})", a
    if a["open_count"] >= budget:
        return False, (
            f"abertos={a['open_count']} >= budget={budget} — finalizar antes de abrir novos"
        ), a
    if a["completion_ratio"] < completion_min:
        return False, (
            f"completion={a['completion_ratio']:.2f} < min={completion_min:.2f} — focar em finalizar"
        ), a
    return True, f"ok (open={a['open_count']}, completion={a['completion_ratio']:.2f})", a


def priority_targets(conn: sqlite3.Connection, campaign_id: str, limit: int = 12) -> list[dict[str, Any]]:
    """Lista ramos abertos prioritarios — quanto maior priority/score, mais critico finalizar."""
    rows = conn.execute(
        """
        SELECT id, name, status, score, priority, depth, parent_id, updated_at
        FROM nodes
        WHERE campaign_id=? AND status IN ('candidate','queued','running','expand','promising')
        ORDER BY priority DESC, score DESC, updated_at ASC
        LIMIT ?
        """,
        (campaign_id, limit),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "status": row["status"],
            "score": float(row["score"] or 0.0),
            "priority": float(row["priority"] or 0.0),
            "depth": int(row["depth"] or 0),
            "parent_id": row["parent_id"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
