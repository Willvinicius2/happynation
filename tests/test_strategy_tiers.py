import json


def test_tier_ranking_falls_back_to_validated_aether_backtest(tmp_path, monkeypatch):
    import src.modules.StrategyTiers as tiers

    state_path = tmp_path / "state.json"
    active_path = tmp_path / "active_strategies.json"
    monkeypatch.setattr(tiers, "STATE_PATH", state_path)
    monkeypatch.setattr(tiers, "ACTIVE_STRATEGIES_PATH", active_path)
    monkeypatch.setenv("TIER_BACKTEST_FALLBACK", "true")

    state_path.write_text(json.dumps({
        "_meta": {
            "strategy_stats": {
                "Aether:combo#badpaper": {"trades": 1, "pnl_usd": -1.0},
                "Aether:combo#paused01": {"trades": 0, "pnl_usd": 0.0},
            },
            "paused_strategies": {"Aether:combo#paused01": "pausada"},
            "strategy_activity": {
                "Aether:combo#dead0001": {"calls": 2000, "non_neutral": 0},
            },
        }
    }), encoding="utf-8")
    active_path.write_text(json.dumps({
        "strategies": [
            {
                "id": "strategy-dead-dead0001",
                "engine": "combo",
                "summary": {
                    "validation_passed": True,
                    "critic_status": "approved",
                    "trade_count": 100,
                    "pnl_pct": 300,
                    "sharpe": 2.2,
                    "mdd_pct": 10,
                },
            },
            {
                "id": "strategy-top-11111111",
                "engine": "combo",
                "summary": {
                    "validation_passed": True,
                    "critic_status": "approved",
                    "trade_count": 50,
                    "pnl_pct": 120,
                    "sharpe": 1.4,
                    "mdd_pct": 20,
                },
            },
            {
                "id": "strategy-second-22222222",
                "engine": "zscore",
                "summary": {
                    "validation_passed": True,
                    "critic_status": "approved",
                    "trade_count": 40,
                    "pnl_pct": 90,
                    "sharpe": 1.2,
                    "mdd_pct": 12,
                },
            },
            {
                "id": "strategy-bad-badpaper",
                "engine": "combo",
                "summary": {
                    "validation_passed": True,
                    "critic_status": "approved",
                    "trade_count": 100,
                    "pnl_pct": 200,
                    "sharpe": 2.0,
                    "mdd_pct": 10,
                },
            },
            {
                "id": "strategy-paused-paused01",
                "engine": "combo",
                "summary": {
                    "validation_passed": True,
                    "critic_status": "approved",
                    "trade_count": 100,
                    "pnl_pct": 180,
                    "sharpe": 2.0,
                    "mdd_pct": 10,
                },
            },
        ]
    }), encoding="utf-8")

    ranks = tiers.rank_strategies()

    assert ranks["Aether:combo#11111111"] == 1
    assert ranks["Aether:zscore#22222222"] == 2
    assert "Aether:combo#dead0001" not in ranks
    assert "Aether:combo#badpaper" not in ranks
    assert "Aether:combo#paused01" not in ranks
    assert tiers.tier_for_strategy("Aether:combo#11111111", ranks) == 1
    assert tiers.tier_for_strategy("Aether:zscore#22222222", ranks) == 2


def test_tier_ranking_keeps_paper_profit_first(tmp_path, monkeypatch):
    import src.modules.StrategyTiers as tiers

    state_path = tmp_path / "state.json"
    active_path = tmp_path / "active_strategies.json"
    monkeypatch.setattr(tiers, "STATE_PATH", state_path)
    monkeypatch.setattr(tiers, "ACTIVE_STRATEGIES_PATH", active_path)
    monkeypatch.setenv("TIER_BACKTEST_FALLBACK", "true")

    state_path.write_text(json.dumps({
        "_meta": {
            "strategy_stats": {
                "Aether:paper#winner01": {"trades": 5, "pnl_usd": 3.0},
            },
            "paused_strategies": {},
        }
    }), encoding="utf-8")
    active_path.write_text(json.dumps({
        "strategies": [
            {
                "id": "strategy-huge-99999999",
                "engine": "combo",
                "summary": {
                    "validation_passed": True,
                    "critic_status": "approved",
                    "trade_count": 60,
                    "pnl_pct": 999,
                    "sharpe": 2.0,
                    "mdd_pct": 15,
                },
            }
        ]
    }), encoding="utf-8")

    ranks = tiers.rank_strategies()

    assert ranks["Aether:paper#winner01"] == 1
    assert ranks["Aether:combo#99999999"] == 2
