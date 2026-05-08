from __future__ import annotations

import logging
import sys

from fastapi.testclient import TestClient

from src.research.aether.campaigns import list_campaigns
from src.research.aether.auto_validator import run_once as run_auto_validator_once
from src.research.aether.campaign_constants import CURRENT_CAMPAIGN_ID
from src.research.aether.db import connect, init_db, loads
from src.research.aether.ranking import build_strategy_ranking
from src.research.aether.repository import upsert_node


def _metrics(**overrides):
    data = {
        "validation_passed": True,
        "validation_stage": "walk_forward",
        "critic_status": "approved",
        "pnl_pct": 12.5,
        "trade_count": 36,
        "mdd_pct": 8.0,
        "sharpe": 1.4,
        "best_probe": {
            "engine": "rsi",
            "params": {"low": 25, "high": 75},
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "capital": 1000,
            "final_equity": 1125,
        },
    }
    data.update(overrides)
    return data


def _node(conn, node_id: str, *, status: str, metrics: dict, score: float = 0.7) -> None:
    upsert_node(
        conn,
        id=node_id,
        campaign_id=CURRENT_CAMPAIGN_ID,
        code=node_id.upper()[:8],
        name=node_id,
        hypothesis="Teste de ranking Aether.",
        status=status,
        score=score,
        priority=score,
        metrics=metrics,
    )


def test_ranking_only_returns_deep_validated_nodes(tmp_path):
    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        _node(conn, "expanded-good", status="expanded", metrics=_metrics())
        _node(conn, "validated-without-deep", status="validated", metrics=_metrics(validation_passed=False, validation_stage=""))
        _node(conn, "validated-good", status="validated", metrics=_metrics())

        rows = build_strategy_ranking(conn)

    assert [row["node_id"] for row in rows] == ["validated-good"]
    assert rows[0]["readiness"] == "paper_ready"


def test_ranking_rejects_validated_nodes_that_fail_gates(tmp_path):
    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        _node(conn, "bad-pnl", status="validated", metrics=_metrics(pnl_pct=-1.0, best_probe={}))
        _node(conn, "low-trades", status="validated", metrics=_metrics(trade_count=29))
        _node(conn, "high-mdd", status="validated", metrics=_metrics(mdd_pct=36.0))
        _node(conn, "low-score", status="validated", metrics=_metrics(), score=0.41)
        _node(conn, "good", status="validated", metrics=_metrics())

        rows = build_strategy_ranking(conn)

    assert [row["node_id"] for row in rows] == ["good"]


def test_ranking_requires_critic_approval_by_default(tmp_path):
    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        _node(conn, "critic-pending", status="validated", metrics=_metrics(critic_status=None))
        _node(conn, "critic-approved", status="validated", metrics=_metrics())

        rows = build_strategy_ranking(conn)

    assert [row["node_id"] for row in rows] == ["critic-approved"]


def test_auto_deploy_defaults_to_critic_and_trade_gates():
    from src.research.aether.auto_deploy import _passes_deploy_gates

    assert _passes_deploy_gates(_metrics(), 0.7) is True
    assert _passes_deploy_gates(_metrics(critic_status=None), 0.7) is False
    assert _passes_deploy_gates(_metrics(trade_count=11), 0.7) is False


def test_auto_deploy_caps_same_asset_family_timeframe(monkeypatch):
    import src.research.aether.auto_deploy as auto_deploy

    monkeypatch.setattr(auto_deploy, "MAX_PER_ASSET_FAMILY_TF", 1)

    ranked = [
        (0.9, {"id": "best", "engine": "bollinger", "asset": "LINKUSDT", "timeframe": "1d"}),
        (0.8, {"id": "same-bucket", "engine": "bollinger", "asset": "LINKUSDT", "timeframe": "1d"}),
        (0.7, {"id": "other-tf", "engine": "bollinger", "asset": "LINKUSDT", "timeframe": "1h"}),
    ]

    selected = auto_deploy._select_deploy_records(ranked)

    assert [rec["id"] for _score, rec in selected] == ["best", "other-tf"]


def test_auto_deploy_defaults_to_bot_assets(monkeypatch):
    import importlib
    import src.research.aether.auto_deploy as auto_deploy

    monkeypatch.delenv("AETHER_DEPLOY_ALLOWED_ASSETS", raising=False)
    monkeypatch.delenv("AETHER_DEPLOY_ALLOWED_TIMEFRAMES", raising=False)
    auto_deploy = importlib.reload(auto_deploy)

    assert auto_deploy._asset_allowed("ADAUSDT") is True
    assert auto_deploy._asset_allowed("LINKUSDT") is False
    assert auto_deploy._timeframe_allowed("1h") is True
    assert auto_deploy._timeframe_allowed("4h") is False


def test_validator_and_critic_defaults_match_paper_pipeline(monkeypatch):
    import importlib
    import src.research.aether.auto_validator as auto_validator
    import src.research.aether.critic_agent as critic_agent

    for name in (
        "AETHER_VALIDATOR_MAX_MDD_PCT",
        "AETHER_CRITIC_GRID_MIN_P25_PNL",
        "AETHER_CRITIC_MTF_DISABLE",
        "AETHER_CRITIC_BOOTSTRAP_MIN_SHARPE_P5",
    ):
        monkeypatch.delenv(name, raising=False)

    auto_validator = importlib.reload(auto_validator)
    critic_agent = importlib.reload(critic_agent)

    assert auto_validator.MAX_MDD_PCT == 35.0
    assert critic_agent.GRID_MIN_P25_PNL == -7.0
    assert critic_agent.MTF_DISABLED is True
    assert critic_agent.BOOTSTRAP_MIN_SHARPE_P5 == -0.3


def test_ranking_uses_best_probe_equity_and_keeps_best_duplicate(tmp_path):
    db = init_db(tmp_path / "aether.sqlite3")
    shared_probe = {
        "engine": "rsi",
        "params": {"low": 28, "high": 72},
        "symbol": "ETHUSDT",
        "dataset_id": "same-window",
        "timeframe": "15m",
        "capital": 1000,
        "final_equity": 1090,
    }
    with connect(db) as conn:
        _node(
            conn,
            "duplicate-lower-score",
            status="validated",
            score=0.6,
            metrics=_metrics(pnl_pct=-99.0, best_probe=shared_probe),
        )
        _node(
            conn,
            "duplicate-higher-score",
            status="validated",
            score=0.8,
            metrics=_metrics(pnl_pct=-99.0, best_probe=shared_probe),
        )

        rows = build_strategy_ranking(conn, dedupe=True)

    assert [row["node_id"] for row in rows] == ["duplicate-higher-score"]
    assert rows[0]["duplicate_count"] == 2
    assert rows[0]["pnl_pct"] == 9.0


def test_campaign_rankable_count_counts_only_validated(tmp_path):
    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        _node(conn, "candidate", status="candidate", metrics=_metrics())
        _node(conn, "promising", status="promising", metrics=_metrics())
        _node(conn, "expanded", status="expanded", metrics=_metrics())
        _node(conn, "validated", status="validated", metrics=_metrics())

        campaigns = list_campaigns(conn)

    current = next(c for c in campaigns["campaigns"] if c["id"] == CURRENT_CAMPAIGN_ID)
    assert current["rankable_count"] == 1


def test_auto_validator_marks_survivor_as_validated(tmp_path):
    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        _node(
            conn,
            "survivor",
            status="expanded",
            metrics=_metrics(validation_passed=False, validation_stage=""),
        )

        promoted = run_auto_validator_once(conn, logging.getLogger("test.auto_validator"))

        row = conn.execute("SELECT status, metrics_json FROM nodes WHERE id='survivor'").fetchone()

    assert promoted == 1
    assert row["status"] == "validated"
    metrics = loads(row["metrics_json"], {})
    assert metrics["validation_passed"] is True
    assert metrics["validation_stage"] == "walk_forward"


def test_auto_validator_does_not_promote_rejected_or_bad_gate_nodes(tmp_path):
    db = init_db(tmp_path / "aether.sqlite3")
    with connect(db) as conn:
        _node(
            conn,
            "llm-rejected",
            status="expanded",
            metrics=_metrics(
                validation_passed=False,
                validation_stage="",
                llm_review={"decision": "reject", "confidence": 0.9},
            ),
        )
        _node(
            conn,
            "bad-gates",
            status="expanded",
            metrics=_metrics(validation_passed=False, validation_stage="", pnl_pct=-2.0),
        )

        promoted = run_auto_validator_once(conn, logging.getLogger("test.auto_validator"))

        rows = conn.execute("SELECT id, status FROM nodes ORDER BY id").fetchall()

    assert promoted == 0
    assert {row["id"]: row["status"] for row in rows} == {
        "bad-gates": "expanded",
        "llm-rejected": "expanded",
    }


def test_ranking_deploy_endpoint_only_deploys_paper_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHER_RUNTIME_DIR", str(tmp_path))
    for name in list(sys.modules):
        if name.startswith("src.research.aether"):
            del sys.modules[name]

    from src.research.aether.db import connect as runtime_connect
    from src.research.aether.repository import upsert_node as runtime_upsert_node
    from src.research.aether.server import app
    import src.research.aether.server as server_module

    deployed = []
    monkeypatch.setattr(server_module, "deploy_record", lambda record: deployed.append(record) or record)

    with TestClient(app) as client:
        with runtime_connect() as conn:
            runtime_upsert_node(
                conn,
                id="not-deep",
                campaign_id=CURRENT_CAMPAIGN_ID,
                code="NOTDEEP",
                name="not-deep",
                hypothesis="Nao deve ir para deploy.",
                status="validated",
                score=0.9,
                priority=0.9,
                metrics=_metrics(validation_passed=False, validation_stage=""),
            )
            runtime_upsert_node(
                conn,
                id="ready",
                campaign_id=CURRENT_CAMPAIGN_ID,
                code="READY",
                name="ready",
                hypothesis="Deve ir para deploy.",
                status="validated",
                score=0.8,
                priority=0.8,
                metrics=_metrics(),
            )
            runtime_upsert_node(
                conn,
                id="expanded-good",
                campaign_id=CURRENT_CAMPAIGN_ID,
                code="EXPAND",
                name="expanded-good",
                hypothesis="Scout bom, mas nao validado.",
                status="expanded",
                score=0.95,
                priority=0.95,
                metrics=_metrics(),
            )

        response = client.post("/api/ranking/deploy?include_needs_validation=true&max_deploy=10")

    assert response.status_code == 200
    body = response.json()
    assert body["deployed_count"] == 1
    assert [record["node_id"] for record in deployed] == ["ready"]
