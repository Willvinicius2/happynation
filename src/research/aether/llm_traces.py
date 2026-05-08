from __future__ import annotations

import sqlite3
import os
from typing import Any

from .campaigns import get_active_campaign_id
from .db import dumps, loads, row_to_dict
from .timeutils import now_iso


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _summary(text: str, error: str | None = None) -> str:
    if error:
        return _clip(error, 260)
    try:
        payload = loads(str(text or ""), {})
        hypotheses = payload.get("hypotheses") if isinstance(payload, dict) else None
        if isinstance(hypotheses, list) and hypotheses:
            first = hypotheses[0] if isinstance(hypotheses[0], dict) else {}
            name = first.get("name") or first.get("slug") or "hipotese"
            thesis = first.get("hypothesis") or ""
            return _clip(f"{name}: {thesis}", 260)
    except Exception:
        pass
    clean = " ".join(str(text or "").split())
    return _clip(clean, 260)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def record_llm_trace(
    conn: sqlite3.Connection,
    *,
    agent_role: str,
    action: str,
    response: dict[str, Any],
    system: str,
    prompt: str,
    node_id: str | None = None,
    campaign_id: str | None = None,
) -> None:
    metadata = response.get("metadata") if isinstance(response.get("metadata"), dict) else {}
    metadata = {**metadata, "effort": response.get("effort") or metadata.get("effort")}
    usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else None
    session_id = metadata.get("session_id")
    cost = metadata.get("cost_usd")
    conn.execute(
        """
        INSERT INTO llm_traces(
            campaign_id, ts, agent_role, action, provider, model, transport,
            status, node_id, session_id, cost_usd, prompt_excerpt,
            output_excerpt, summary, error, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            campaign_id or get_active_campaign_id(conn),
            now_iso(),
            agent_role,
            action,
            response.get("provider"),
            response.get("model"),
            response.get("transport"),
            "ok" if response.get("ok") else "error",
            node_id,
            session_id,
            float(cost) if isinstance(cost, (int, float)) else None,
            _clip(f"SYSTEM:\n{system}\n\nPROMPT:\n{prompt}", _env_int("AETHER_LLM_TRACE_PROMPT_CHARS", 2000)),
            _clip(response.get("text") or "", _env_int("AETHER_LLM_TRACE_OUTPUT_CHARS", 3000)),
            _summary(str(response.get("text") or ""), str(response.get("error") or "") or None),
            _clip(response.get("error") or "", 1000) or None,
            dumps({"usage": usage, **metadata}),
        ),
    )


def list_llm_traces(conn: sqlite3.Connection, *, campaign_id: str | None = None, limit: int = 80) -> list[dict[str, Any]]:
    campaign_v = campaign_id or get_active_campaign_id(conn)
    rows = conn.execute(
        """
        SELECT * FROM llm_traces
        WHERE campaign_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (campaign_v, limit),
    ).fetchall()
    traces = []
    for row in rows:
        item = row_to_dict(row)
        item["metadata"] = loads(item.pop("metadata_json"), {})
        traces.append(item)
    return traces
