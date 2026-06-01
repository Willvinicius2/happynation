from __future__ import annotations

import json

from src.research.aether.campaign_constants import CURRENT_CAMPAIGN_ID
from src.research.aether.db import connect, init_db, set_setting
from src.research.aether.repository import upsert_node


def _add_validated(conn, node_id: str, *, score: float, engine: str = "donchian") -> None:
    upsert_node(
        conn,
        id=node_id,
        campaign_id=CURRENT_CAMPAIGN_ID,
        code=node_id.upper(),
        name=node_id,
        hypothesis="h",
        causal_mechanism="c",
        status="validated",
        score=score,
        priority=score,
        depth=2,
        metrics={"best_probe": {"engine": engine}, "critic_status": "approved"},
        tags=[f"engine:{engine}"],
    )


def test_hyper_agent_refreshes_unchanged_controls_and_preserves_manual_knobs(tmp_path, monkeypatch):
    from src.research.aether import hyper_agent

    monkeypatch.setattr(hyper_agent, "HEARTBEAT_EVENT_MINUTES", 10)
    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        for idx in range(7):
            _add_validated(conn, f"node-{idx}", score=0.9 - idx * 0.01)
        conn.execute(
            """
            UPDATE nodes
            SET updated_at='2026-01-01T00:00:00Z',
                created_at='2026-01-01T00:00:00Z'
            """
        )
        previous = hyper_agent.decide(hyper_agent.assess(conn), None)
        previous["max_llm_calls_per_hour"] = 12
        previous["last_event_at"] = "2026-01-01T00:00:00Z"
        set_setting(conn, "hyper_controls", previous)

        controls = hyper_agent.run_once(conn, hyper_agent._setup_logger())
        stored = json.loads(conn.execute("SELECT value_json FROM settings WHERE key='hyper_controls'").fetchone()[0])
        events = conn.execute("SELECT type FROM events WHERE agent_role='hyper_agent'").fetchall()

    assert controls["force_exploration_every"] == previous["force_exploration_every"]
    assert stored["max_llm_calls_per_hour"] == 12
    assert stored["last_seen_at"]
    assert stored["last_event_at"] != "2026-01-01T00:00:00Z"
    assert [row["type"] for row in events] == ["hyper_controls_heartbeat"]


def test_hyper_agent_updates_controls_without_dropping_extra_fields(tmp_path):
    from src.research.aether import hyper_agent

    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        set_setting(
            conn,
            "hyper_controls",
            {
                "force_exploration_every": 2,
                "verdict": "stuck",
                "focus_open_budget": 8,
            },
        )

        controls = hyper_agent.run_once(conn, hyper_agent._setup_logger())
        stored = json.loads(conn.execute("SELECT value_json FROM settings WHERE key='hyper_controls'").fetchone()[0])
        event_type = conn.execute("SELECT type FROM events WHERE agent_role='hyper_agent'").fetchone()["type"]

    assert controls["force_exploration_every"] == 4
    assert controls["verdict"] == "balanced"
    assert stored["focus_open_budget"] == 8
    assert event_type == "hyper_controls_updated"


def test_hyper_agent_assess_reports_best_age(tmp_path):
    from src.research.aether import hyper_agent

    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        _add_validated(conn, "old-best", score=0.9)
        conn.execute(
            """
            UPDATE nodes
            SET updated_at='2026-01-01T00:00:00Z',
                created_at='2026-01-01T00:00:00Z'
            WHERE id='old-best'
            """
        )

        assessment = hyper_agent.assess(conn)

    assert assessment["best_age_min"] is not None
    assert assessment["best_age_min"] > 1
