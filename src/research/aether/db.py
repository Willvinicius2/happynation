from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import db_path, sqlite_busy_timeout_ms
from .campaign_constants import ACTIVE_CAMPAIGN_SETTING, CURRENT_CAMPAIGN_ID, DEFAULT_CAMPAIGNS, LEGACY_CAMPAIGN_ID, LEGACY_SOURCE_TYPES
from .timeutils import now_iso


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL DEFAULT 'aether_current',
    parent_id TEXT,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    causal_mechanism TEXT NOT NULL DEFAULT '',
    mutation_from_parent TEXT NOT NULL DEFAULT '',
    timeframe TEXT,
    agent_role TEXT,
    status TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0.5,
    depth INTEGER NOT NULL DEFAULT 0,
    priority REAL NOT NULL DEFAULT 0.5,
    prune_reason TEXT,
    result_summary TEXT,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    tags_json TEXT NOT NULL DEFAULT '[]',
    source_type TEXT,
    source_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_status_priority ON nodes(status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_nodes_timeframe ON nodes(timeframe);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    node_id TEXT,
    agent_role TEXT,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_node ON events(node_id);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    campaign_id TEXT,
    node_id TEXT,
    agent_role TEXT NOT NULL,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL,
    priority REAL NOT NULL DEFAULT 0.5,
    payload_json TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    lease_owner TEXT,
    lease_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_agent_status ON tasks(agent_role, status);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    campaign_id TEXT,
    node_id TEXT,
    task_id TEXT,
    sweep_id TEXT,
    status TEXT NOT NULL,
    worker_count INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    eta_seconds REAL,
    throughput_per_min REAL,
    checkpoint_path TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_node ON runs(node_id);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT,
    node_id TEXT,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_node ON artifacts(node_id);

CREATE TABLE IF NOT EXISTS llm_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT,
    ts TEXT NOT NULL,
    agent_role TEXT NOT NULL,
    action TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    transport TEXT,
    status TEXT NOT NULL,
    node_id TEXT,
    session_id TEXT,
    cost_usd REAL,
    prompt_excerpt TEXT NOT NULL DEFAULT '',
    output_excerpt TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_llm_traces_campaign_ts ON llm_traces(campaign_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_llm_traces_role_ts ON llm_traces(agent_role, ts DESC);

CREATE TABLE IF NOT EXISTS agents (
    role TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    timeframe TEXT,
    status TEXT NOT NULL,
    current_task_id TEXT,
    stats_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'current',
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    busy_timeout = sqlite_busy_timeout_ms()
    conn = sqlite3.connect(p, timeout=busy_timeout / 1000.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # Multiplos agentes (orchestrator/critic/hyper/llm) abrem a mesma DB.
    # busy_timeout em ms espera a writer lock antes de levantar SQLITE_BUSY.
    conn.execute(f"PRAGMA busy_timeout={busy_timeout}")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(path: Path | None = None) -> Path:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with connect(p) as conn:
        conn.executescript(SCHEMA)
        migrate_db(conn)
    return p


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def migrate_db(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "nodes", "campaign_id", "campaign_id TEXT NOT NULL DEFAULT 'aether_current'")
    _ensure_column(conn, "events", "campaign_id", "campaign_id TEXT")
    _ensure_column(conn, "tasks", "campaign_id", "campaign_id TEXT")
    _ensure_column(conn, "runs", "campaign_id", "campaign_id TEXT")
    _ensure_column(conn, "artifacts", "campaign_id", "campaign_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_campaign_status ON nodes(campaign_id, status, score DESC)")

    ts = now_iso()
    for campaign in DEFAULT_CAMPAIGNS:
        conn.execute(
            """
            INSERT INTO campaigns(id, label, kind, description, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                label=excluded.label,
                kind=excluded.kind,
                description=excluded.description,
                updated_at=excluded.updated_at
            """,
            (
                campaign["id"],
                campaign["label"],
                campaign["kind"],
                campaign["description"],
                campaign["status"],
                ts,
                ts,
            ),
        )

    source_marks = tuple(LEGACY_SOURCE_TYPES)
    placeholders = ",".join("?" for _ in source_marks)
    conn.execute(
        f"""
        UPDATE nodes
        SET campaign_id=?
        WHERE id LIKE 'legacy:%'
           OR id IN ('legacy-root', 'legacy-diag-root')
           OR source_type IN ({placeholders})
        """,
        (LEGACY_CAMPAIGN_ID, *source_marks),
    )
    conn.execute(
        "UPDATE nodes SET campaign_id=? WHERE campaign_id IS NULL OR campaign_id=''",
        (CURRENT_CAMPAIGN_ID,),
    )
    for table in ("tasks", "runs", "artifacts"):
        conn.execute(
            f"""
            UPDATE {table}
            SET campaign_id=(
                SELECT nodes.campaign_id FROM nodes WHERE nodes.id={table}.node_id
            )
            WHERE campaign_id IS NULL AND node_id IS NOT NULL
            """
        )
        conn.execute(
            f"UPDATE {table} SET campaign_id=? WHERE campaign_id IS NULL OR campaign_id=''",
            (CURRENT_CAMPAIGN_ID,),
        )
    conn.execute(
        """
        UPDATE events
        SET campaign_id=(
            SELECT nodes.campaign_id FROM nodes WHERE nodes.id=events.node_id
        )
        WHERE campaign_id IS NULL AND node_id IS NOT NULL
        """
    )
    conn.execute(
        "UPDATE events SET campaign_id=? WHERE campaign_id IS NULL OR campaign_id=''",
        (CURRENT_CAMPAIGN_ID,),
    )
    if not conn.execute("SELECT 1 FROM settings WHERE key=?", (ACTIVE_CAMPAIGN_SETTING,)).fetchone():
        conn.execute(
            """
            INSERT INTO settings(key, value_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (ACTIVE_CAMPAIGN_SETTING, dumps(CURRENT_CAMPAIGN_ID), ts),
        )


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def add_event(
    conn: sqlite3.Connection,
    *,
    type: str,
    message: str,
    campaign_id: str | None = None,
    node_id: str | None = None,
    agent_role: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO events(campaign_id, ts, type, node_id, agent_role, message, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (campaign_id, now_iso(), type, node_id, agent_role, message, dumps(payload or {})),
    )


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        """
        INSERT INTO settings(key, value_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value_json=excluded.value_json,
            updated_at=excluded.updated_at
        """,
        (key, dumps(value), now_iso()),
    )


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
    return loads(row["value_json"], default) if row else default
