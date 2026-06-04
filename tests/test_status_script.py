from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import scripts.status as status


def test_process_summary_treats_parent_child_pair_as_single_ok_module():
    procs = [
        {"Module": "aether.auto_deploy", "ProcessId": 100, "ParentProcessId": 10},
        {"Module": "aether.auto_deploy", "ProcessId": 101, "ParentProcessId": 100},
    ]

    summary = status.summarize_module_processes("aether.auto_deploy", procs)

    assert summary["flag"] == "OK"
    assert summary["roots"] == [100]
    assert summary["workers"] == [101]


def test_process_summary_detects_duplicate_roots_and_optional_promote_loop():
    duplicates = [
        {"Module": "aether.critic_agent", "ProcessId": 200, "ParentProcessId": 10},
        {"Module": "aether.critic_agent", "ProcessId": 201, "ParentProcessId": 11},
    ]

    duplicate_summary = status.summarize_module_processes("aether.critic_agent", duplicates)
    promote_off = status.summarize_module_processes("promote_loop", [], promote_expected=False)
    promote_running = status.summarize_module_processes(
        "promote_loop",
        [{"Module": "promote_loop", "ProcessId": 300, "ParentProcessId": 10}],
        promote_expected=False,
    )

    assert duplicate_summary["flag"] == "DUP"
    assert promote_off["flag"] == "OFF"
    assert promote_running["flag"] == "RISCO"


def _make_productivity_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE events (ts TEXT, type TEXT, payload_json TEXT)")
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT)")
        conn.execute("CREATE TABLE tasks (status TEXT, updated_at TEXT, agent_role TEXT)")
        conn.executemany(
            "INSERT INTO events VALUES (?, ?, ?)",
            [
                (
                    "2026-06-01T17:20:00Z",
                    "research_completed",
                    json.dumps(
                        {
                            "llm": {"created_hypotheses": 1, "created_nodes": 7},
                            "web": {"created_hypotheses": 2, "created_nodes": 14},
                        }
                    ),
                ),
                ("2026-06-01T17:25:00Z", "llm_research_completed", json.dumps({"created": 1})),
                ("2026-06-01T17:30:00Z", "critic_approved", "{}"),
                ("2026-06-01T17:31:00Z", "critic_rejected", "{}"),
                ("2026-06-01T17:40:00Z", "continuous_research_busy", "{}"),
                ("2026-06-01T17:45:00Z", "web_research_backoff", "{}"),
                ("2026-06-01T16:30:00Z", "task_failed", "{}"),
                (
                    "2026-06-01T16:00:00Z",
                    "research_completed",
                    json.dumps({"llm": {"created_nodes": 99}, "web": {"created_nodes": 99}}),
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO tasks VALUES (?, ?, ?)",
            [
                ("running", "2026-06-01T17:55:00Z", "agent_1m"),
                ("running", "2026-06-01T17:58:00Z", "agent_5m"),
                ("queued", "2026-06-01T17:56:00Z", "agent_1h"),
                ("completed", "2026-06-01T17:57:00Z", "agent_1h"),
                ("completed", "2026-06-01T16:00:00Z", "agent_1h"),
            ],
        )
        conn.execute(
            "INSERT INTO settings VALUES (?, ?)",
            (
                "hyper_controls",
                json.dumps(
                    {
                        "verdict": "stuck",
                        "force_exploration_every": 2,
                        "best_age_min": 3000,
                        "critic_reject_rate": 0.5,
                        "focus_open_count": 12,
                        "focus_open_budget": 24,
                        "running_tasks": 1,
                        "queued_tasks": 1,
                        "last_seen_at": "2026-06-01T17:35:00Z",
                    }
                ),
            ),
        )


def test_aether_productivity_snapshot_summarizes_recent_agent_work(tmp_path):
    db = tmp_path / "aether.sqlite3"
    _make_productivity_db(db)

    snapshot = status.latest_aether_productivity_snapshot(
        db,
        window_minutes=60,
        now=datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc),
    )

    assert snapshot["research_cycles"] == 1
    assert snapshot["llm_nodes"] == 7
    assert snapshot["web_nodes"] == 14
    assert snapshot["llm_created"] == 1
    assert snapshot["critic_approved"] == 1
    assert snapshot["critic_rejected"] == 1
    assert snapshot["tasks"] == {"queued": 1, "running": 2, "recent_completed": 1}
    assert snapshot["task_runtime"]["oldest_running_age_min"] == 5.0
    assert snapshot["task_runtime"]["running_by_agent"] == {"agent_1m": 1, "agent_5m": 1}
    assert snapshot["health"]["task_failed"]["count"] == 0
    assert snapshot["health"]["continuous_busy"]["count"] == 1
    assert snapshot["health"]["web_backoff"]["count"] == 1
    assert snapshot["hyper"]["verdict"] == "stuck"


def test_show_aether_productivity_prints_hyper_and_task_health(tmp_path, monkeypatch, capsys):
    db = tmp_path / "aether.sqlite3"
    _make_productivity_db(db)
    monkeypatch.setattr(status, "resolve_aether_db_path", lambda: db)
    monkeypatch.setattr(
        status,
        "utc_cutoff",
        lambda _minutes, _now=None: "2026-06-01T17:00:00Z",
    )

    status.show_aether_productivity()

    out = capsys.readouterr().out
    assert "PRODUTIVIDADE AETHER" in out
    assert "pesquisa: ciclos=1 llm_nodes=7 web_nodes=14" in out
    assert "critic: approved=1 rejected=1 reject_rate=50.0%" in out
    assert "tasks: queued=1 recent_completed=1 running=2 oldest_running=" in out
    assert "tasks por agente: agent_1m=1 agent_5m=1" in out
    assert "saude recente: task_failed=0 continuous_error=0 cycle_busy=0 continuous_busy=1 web_backoff=1 restart_requeued=0" in out
    assert "hyper: verdict=stuck every=2" in out


def _write_auto_deploy_log(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "2026-06-01 15:01:38,893 INFO deploy: candidates=184 struct_dedupe=128 func_dropped=17 after_dedupe=29 deployed=0 skipped_unchanged=29 removed_stale=0 blocked=15",
                "2026-06-01 15:01:38,893 INFO deploy detail: gate=score_below_min:211,mdd_above_max:25 blocklist=heavy_validation_cut_or_revalidate:5,manifest_top_validation_trade_sample:3 caps=asset_cap:55,family_cap:24 cap_detail=asset:BTCUSDT:17,family:combo:12",
            ]
        ),
        encoding="utf-8",
    )


def test_auto_deploy_log_snapshot_parses_latest_summary_and_reasons(tmp_path):
    log_path = tmp_path / "auto_deploy.log"
    _write_auto_deploy_log(log_path)

    snapshot = status.latest_auto_deploy_snapshot(log_path)

    assert snapshot["summary"]["candidates"] == 184
    assert snapshot["summary"]["after_dedupe"] == 29
    assert snapshot["summary"]["blocked"] == 15
    assert snapshot["details"]["gate"] == {"score_below_min": 211, "mdd_above_max": 25}
    assert snapshot["details"]["blocklist"]["heavy_validation_cut_or_revalidate"] == 5
    assert snapshot["details"]["caps"] == {"asset_cap": 55, "family_cap": 24}
    assert snapshot["details"]["cap_detail"] == {"asset:BTCUSDT": 17, "family:combo": 12}


def test_auto_deploy_log_snapshot_does_not_attach_stale_detail(tmp_path):
    log_path = tmp_path / "auto_deploy.log"
    _write_auto_deploy_log(log_path)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(
            "\n2026-06-01 15:02:38,893 INFO deploy: candidates=200 struct_dedupe=130 "
            "func_dropped=20 after_dedupe=31 deployed=1 skipped_unchanged=30 removed_stale=0 blocked=0"
        )

    snapshot = status.latest_auto_deploy_snapshot(log_path)

    assert snapshot["summary"]["candidates"] == 200
    assert snapshot["details"] == {}


def test_show_auto_deploy_flow_prints_actionable_bottlenecks(tmp_path, monkeypatch, capsys):
    log_path = tmp_path / "auto_deploy.log"
    _write_auto_deploy_log(log_path)
    monkeypatch.setattr(status, "AUTO_DEPLOY_LOG", log_path)

    status.show_auto_deploy_flow()

    out = capsys.readouterr().out
    assert "FUNIL AUTO-DEPLOY AETHER" in out
    assert "candidates=184 after_dedupe=29 deployed=0" in out
    assert "score_below_min: 211" in out
    assert "asset_cap: 55" in out
    assert "asset:BTCUSDT: 17" in out
