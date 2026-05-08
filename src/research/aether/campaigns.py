from __future__ import annotations

import sqlite3
from typing import Any

from .campaign_constants import ACTIVE_CAMPAIGN_SETTING, CURRENT_CAMPAIGN_ID, LEGACY_CAMPAIGN_ID, LEGACY_SOURCE_TYPES
from .db import add_event, dumps, get_setting, loads, set_setting
from .timeutils import now_iso


def node_campaign_id(*, node_id: str, source_type: str | None, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if node_id.startswith("legacy:") or node_id in {"legacy-root", "legacy-diag-root"}:
        return LEGACY_CAMPAIGN_ID
    if source_type in LEGACY_SOURCE_TYPES:
        return LEGACY_CAMPAIGN_ID
    return CURRENT_CAMPAIGN_ID


def get_active_campaign_id(conn: sqlite3.Connection) -> str:
    return str(get_setting(conn, ACTIVE_CAMPAIGN_SETTING, CURRENT_CAMPAIGN_ID) or CURRENT_CAMPAIGN_ID)


def ensure_campaigns(conn: sqlite3.Connection) -> None:
    # init_db already performs migrations. This function is intentionally small:
    # it is safe to call from request handlers that only need campaign rows present.
    if not conn.execute("SELECT 1 FROM campaigns WHERE id=?", (CURRENT_CAMPAIGN_ID,)).fetchone():
        ts = now_iso()
        conn.execute(
            """
            INSERT INTO campaigns(id, label, kind, description, status, created_at, updated_at)
            VALUES (?, ?, 'current', ?, 'active', ?, ?)
            """,
            (CURRENT_CAMPAIGN_ID, "Agora / Aether atual", "Campanha atual criada a partir do Aether.", ts, ts),
        )
    if not conn.execute("SELECT 1 FROM campaigns WHERE id=?", (LEGACY_CAMPAIGN_ID,)).fetchone():
        ts = now_iso()
        conn.execute(
            """
            INSERT INTO campaigns(id, label, kind, description, status, created_at, updated_at)
            VALUES (?, ?, 'legacy', ?, 'archived', ?, ?)
            """,
            (LEGACY_CAMPAIGN_ID, "Antes / legado", "Historico importado do projeto anterior.", ts, ts),
        )


def list_campaigns(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_campaigns(conn)
    active_id = get_active_campaign_id(conn)
    rows = conn.execute(
        """
        SELECT c.id, c.label, c.kind, c.description, c.status, c.created_at, c.updated_at,
               COUNT(n.id) AS node_count,
               SUM(CASE WHEN n.status='validated' THEN 1 ELSE 0 END) AS rankable_count
        FROM campaigns c
        LEFT JOIN nodes n ON n.campaign_id=c.id
        GROUP BY c.id
        ORDER BY
            CASE WHEN c.id=? THEN 0 WHEN c.id=? THEN 1 ELSE 2 END,
            c.created_at DESC
        """,
        (active_id, LEGACY_CAMPAIGN_ID),
    ).fetchall()
    return {
        "active_campaign_id": active_id,
        "campaigns": [
            {
                "id": row["id"],
                "label": row["label"],
                "kind": row["kind"],
                "description": row["description"],
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "node_count": int(row["node_count"] or 0),
                "rankable_count": int(row["rankable_count"] or 0),
                "active": row["id"] == active_id,
            }
            for row in rows
        ],
    }


def create_campaign(conn: sqlite3.Connection, *, label: str | None = None, activate: bool = True) -> dict[str, Any]:
    ts = now_iso()
    compact_ts = ts.replace("-", "").replace(":", "").replace("T", "_").replace("Z", "")
    campaign_id = f"aether_{compact_ts}"
    title = label or f"Aether campanha {compact_ts}"
    conn.execute(
        """
        INSERT INTO campaigns(id, label, kind, description, status, created_at, updated_at)
        VALUES (?, ?, 'current', ?, ?, ?, ?)
        """,
        (
            campaign_id,
            title,
            "Campanha limpa criada sem apagar historico anterior.",
            "active" if activate else "standby",
            ts,
            ts,
        ),
    )
    if activate:
        conn.execute(
            "UPDATE campaigns SET status='archived', updated_at=? WHERE kind='current' AND id<>? AND status='active'",
            (ts, campaign_id),
        )
        set_setting(conn, ACTIVE_CAMPAIGN_SETTING, campaign_id)
    add_event(
        conn,
        type="campaign_created",
        campaign_id=campaign_id,
        agent_role="chief_orchestrator",
        message=f"Nova campanha criada: {title}. Historico anterior preservado.",
        payload={"campaign_id": campaign_id, "label": title, "activated": activate},
    )
    return {"id": campaign_id, "label": title, "active": activate}


def campaign_label_map(conn: sqlite3.Connection) -> dict[str, str]:
    ensure_campaigns(conn)
    return {row["id"]: row["label"] for row in conn.execute("SELECT id, label FROM campaigns")}
