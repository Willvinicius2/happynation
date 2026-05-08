from __future__ import annotations

from src.research.aether.campaign_constants import CURRENT_CAMPAIGN_ID
from src.research.aether.db import connect, init_db
from src.research.aether.repository import upsert_node


def _candidate(node_id: str, *, timeframe: str, score: float = 0.6) -> dict:
    return {
        "id": node_id,
        "campaign_id": CURRENT_CAMPAIGN_ID,
        "code": node_id.upper(),
        "name": node_id,
        "hypothesis": "h",
        "causal_mechanism": "c",
        "timeframe": timeframe,
        "agent_role": f"timeframe_{timeframe.replace('m', 'm').replace('h', 'h').replace('d', 'd')}",
        "status": "candidate",
        "score": score,
        "priority": score,
        "depth": 2,
        "metrics": {
            "engines": ["rsi"],
            "probe_specs": [{"engine": "rsi", "params": {"period": 14}}],
        },
        "tags": ["engine:rsi", timeframe],
        "source_type": "llm_agent",
    }


def test_inflight_ignores_neutral_parent_nodes(tmp_path):
    from src.research.aether.orchestrator import _inflight_count

    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        upsert_node(
            conn,
            id="llm-parent-visited",
            campaign_id=CURRENT_CAMPAIGN_ID,
            code="L1",
            name="visited parent",
            hypothesis="h",
            status="visited",
            score=0.5,
            priority=0.1,
            depth=1,
            source_type="llm_agent",
        )
        upsert_node(
            conn,
            id="llm-parent-expanded",
            campaign_id=CURRENT_CAMPAIGN_ID,
            code="L2",
            name="expanded parent",
            hypothesis="h",
            status="expanded",
            score=0.7,
            priority=0.1,
            depth=2,
            source_type="llm_agent",
        )
        assert _inflight_count(conn, "llm_agent") == 0

        upsert_node(conn, **_candidate("llm-child", timeframe="5m"))
        assert _inflight_count(conn, "llm_agent") == 1


def test_tree_focus_does_not_block_on_neutral_nodes(tmp_path):
    from src.research.aether.tree_focus import should_open_new_branch

    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        for idx in range(20):
            upsert_node(
                conn,
                id=f"neutral-{idx}",
                campaign_id=CURRENT_CAMPAIGN_ID,
                code=f"N{idx}",
                name=f"neutral {idx}",
                hypothesis="h",
                status="expanded" if idx % 2 else "visited",
                score=0.5,
                priority=0.1,
                depth=1,
            )

        allow, _reason, state = should_open_new_branch(conn, CURRENT_CAMPAIGN_ID)

    assert allow is True
    assert state["actionable_total"] == 0
    assert state["neutral_count"] == 20


def test_llm_batch_size_uses_one_call_for_multiple_roots(monkeypatch):
    from src.research.aether.orchestrator import _llm_batch_size

    monkeypatch.setenv("AETHER_LLM_MAX_NEW", "3")

    assert _llm_batch_size(0, 15, force_exploration=False, heartbeat=False) == 3
    assert _llm_batch_size(13, 15, force_exploration=False, heartbeat=False) == 1
    assert _llm_batch_size(15, 15, force_exploration=True, heartbeat=False) == 3


def test_queue_scout_tasks_extends_batch_to_hit_probe_target(tmp_path, monkeypatch):
    from src.research.aether import orchestrator

    monkeypatch.setenv("AETHER_RUNTIME_DIR", str(tmp_path))
    init_db()

    def fake_default_probes(_root, timeframe, *, limit, engine_specs=None):
        return [{"engine": "rsi", "symbol": f"SYM{i}", "timeframe": timeframe} for i in range(6)]

    monkeypatch.setattr(orchestrator, "default_probes_for_timeframe", fake_default_probes)

    timeframes = ["5m", "15m", "1h", "4h", "1d"]
    with connect() as conn:
        for idx in range(10):
            upsert_node(conn, **_candidate(f"node-{idx}", timeframe=timeframes[idx % len(timeframes)]))

    queued = orchestrator.queue_scout_tasks(
        limit_nodes=2,
        probes_per_node=60,
        allow_generate=False,
        target_probe_count=18,
    )

    with connect() as conn:
        task_count = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='queued'").fetchone()["n"]

    assert queued == 3
    assert task_count == 3


def test_capped_expand_node_leaves_actionable_queue(tmp_path):
    from src.research.aether.db import row_to_dict
    from src.research.aether.orchestrator import MAX_EXPAND_DEPTH, _apply_scout_result

    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        upsert_node(
            conn,
            **{
                **_candidate("capped-node", timeframe="15m", score=0.7),
                "depth": MAX_EXPAND_DEPTH,
            },
        )
        row = conn.execute("SELECT * FROM nodes WHERE id='capped-node'").fetchone()
        node = row_to_dict(row)
        _apply_scout_result(
            conn,
            node,
            {
                "summary": {
                    "ok": True,
                    "sharpe": 3.0,
                    "trade_count": 60,
                    "mdd_pct": 5.0,
                    "pnl_pct": 20.0,
                    "fee_bps": 10.0,
                    "best_probe": {
                        "engine": "rsi",
                        "params": {"period": 14},
                        "symbol": "BTCUSDT",
                        "timeframe": "15m",
                    },
                }
            },
        )
        status = conn.execute("SELECT status FROM nodes WHERE id='capped-node'").fetchone()["status"]

    assert status == "expanded"
