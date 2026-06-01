from __future__ import annotations

import json
import sqlite3

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


def test_hyper_agent_writes_controls_when_event_insert_is_locked(tmp_path, monkeypatch):
    from src.research.aether import hyper_agent

    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        set_setting(
            conn,
            "hyper_controls",
            {
                "force_exploration_every": 6,
                "verdict": "exploring",
                "last_event_at": "2026-01-01T00:00:00Z",
            },
        )

        def locked_event(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(hyper_agent, "_emit_event", locked_event)
        controls = hyper_agent.run_once(conn, hyper_agent._setup_logger())
        stored = json.loads(conn.execute("SELECT value_json FROM settings WHERE key='hyper_controls'").fetchone()[0])

    assert controls["force_exploration_every"] == 4
    assert stored["force_exploration_every"] == 4
    assert stored["last_seen_at"]
    assert stored["last_event_at"] == "2026-01-01T00:00:00Z"


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


def test_hyper_agent_assess_respects_ranking_reset(tmp_path):
    from src.research.aether import hyper_agent

    db = init_db(tmp_path / "aether.sqlite3")
    reset_at = "2026-05-18T20:14:03Z"
    with connect(db) as conn:
        _add_validated(conn, "old-hidden-best", score=0.99, engine="rsi")
        conn.execute(
            """
            UPDATE nodes
            SET updated_at='2026-05-11T20:44:34Z',
                created_at='2026-05-11T20:41:29Z'
            WHERE id='old-hidden-best'
            """
        )
        for idx in range(6):
            _add_validated(conn, f"new-visible-{idx}", score=0.60 + idx / 100, engine="donchian")
            conn.execute(
                """
                UPDATE nodes
                SET updated_at='2026-05-27T07:31:11Z',
                    created_at='2026-05-27T07:23:38Z'
                WHERE id=?
                """,
                (f"new-visible-{idx}",),
            )
        set_setting(
            conn,
            f"ranking_hidden_node_ids:{CURRENT_CAMPAIGN_ID}",
            {"reset_at": reset_at, "hidden_node_ids": ["old-hidden-best"]},
        )

        assessment = hyper_agent.assess(conn)

    assert assessment["ranking_reset_after"] == reset_at
    assert assessment["active_count"] == 6
    assert assessment["best_age_min"] is not None
    assert assessment["best_age_min"] < 20000


def test_hyper_agent_expands_focus_budget_when_stuck_and_completion_is_high():
    from src.research.aether import hyper_agent

    controls = hyper_agent.decide(
        {
            "verdict": "stuck",
            "diversity": 0.9,
            "dominance_pct": 10.0,
            "best_age_min": 100.0,
            "critic_reject_rate": 0.2,
            "active_count": 100,
            "focus_open_count": 19,
            "focus_actionable_total": 100,
            "focus_completion_ratio": 0.9,
            "running_tasks": 18,
            "queued_tasks": 0,
        },
        None,
    )

    assert controls["focus_open_budget"] >= 24
    assert controls["focus_open_budget"] > controls["focus_open_count"]


def test_hyper_agent_scales_focus_budget_for_productive_stuck_backlog():
    from src.research.aether import hyper_agent

    controls = hyper_agent.decide(
        {
            "verdict": "stuck",
            "diversity": 0.97,
            "dominance_pct": 9.0,
            "best_age_min": 30000.0,
            "critic_reject_rate": 0.07,
            "active_count": 500,
            "focus_open_count": 66,
            "focus_actionable_total": 283,
            "focus_completion_ratio": 0.76,
            "running_tasks": 36,
            "queued_tasks": 0,
        },
        None,
    )

    assert controls["focus_open_budget"] > controls["focus_open_count"]
    assert controls["focus_open_budget"] <= hyper_agent.FOCUS_BUDGET_MAX


def test_hyper_agent_keeps_focus_budget_conservative_when_quality_drops():
    from src.research.aether import hyper_agent

    controls = hyper_agent.decide(
        {
            "verdict": "balanced",
            "diversity": 0.4,
            "dominance_pct": 70.0,
            "best_age_min": 5.0,
            "critic_reject_rate": 0.75,
            "active_count": 100,
            "focus_open_count": 20,
            "focus_actionable_total": 100,
            "focus_completion_ratio": 0.9,
            "running_tasks": 18,
            "queued_tasks": 0,
        },
        None,
    )

    assert controls["focus_open_budget"] == hyper_agent.FOCUS_BUDGET_MIN
