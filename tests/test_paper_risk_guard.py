import json

import pytest


def test_risk_guard_blocks_after_consecutive_losses(tmp_path, monkeypatch):
    import src.modules.PaperBalanceLogger as paper

    monkeypatch.setattr(paper, "OUT_DIR", tmp_path)
    monkeypatch.setattr(paper, "TRADES_CSV", tmp_path / "trades.csv")
    monkeypatch.setattr(paper, "STATE_JSON", tmp_path / "state.json")
    monkeypatch.setattr(paper, "SUMMARY_TXT", tmp_path / "summary.txt")
    monkeypatch.setattr(paper, "RISK_GUARD_ENABLED", True)
    monkeypatch.setattr(paper, "MAX_CONSECUTIVE_LOSSES", 2)
    monkeypatch.setattr(paper, "MAX_DAILY_LOSS_PCT", 99.0)
    monkeypatch.setattr(paper, "MAX_ASSET_DRAWDOWN_PCT", 99.0)

    paper.log_buy("BTCUSDT", "TestStrategy", 100.0, 1.0)
    paper.log_sell("BTCUSDT", 99.0, 1.0)
    allowed, reason = paper.can_open_new_position("BTCUSDT", "TestStrategy")
    assert allowed is True
    assert reason == ""

    paper.log_buy("BTCUSDT", "TestStrategy", 100.0, 1.0)
    paper.log_sell("BTCUSDT", 99.0, 1.0)
    allowed, reason = paper.can_open_new_position("BTCUSDT", "TestStrategy")
    assert allowed is False
    assert "perdas seguidas" in reason


def test_risk_guard_resets_loss_streak_after_win(tmp_path, monkeypatch):
    import src.modules.PaperBalanceLogger as paper

    monkeypatch.setattr(paper, "OUT_DIR", tmp_path)
    monkeypatch.setattr(paper, "TRADES_CSV", tmp_path / "trades.csv")
    monkeypatch.setattr(paper, "STATE_JSON", tmp_path / "state.json")
    monkeypatch.setattr(paper, "SUMMARY_TXT", tmp_path / "summary.txt")
    monkeypatch.setattr(paper, "RISK_GUARD_ENABLED", True)
    monkeypatch.setattr(paper, "MAX_CONSECUTIVE_LOSSES", 2)
    monkeypatch.setattr(paper, "MAX_DAILY_LOSS_PCT", 99.0)
    monkeypatch.setattr(paper, "MAX_ASSET_DRAWDOWN_PCT", 99.0)

    paper.log_buy("ETHUSDT", "TestStrategy", 100.0, 1.0)
    paper.log_sell("ETHUSDT", 99.0, 1.0)
    paper.log_buy("ETHUSDT", "TestStrategy", 100.0, 1.0)
    paper.log_sell("ETHUSDT", 101.0, 1.0)

    snapshot = paper.get_risk_snapshot("ETHUSDT", "TestStrategy")
    assert snapshot["consecutive_losses"] == 0
    allowed, reason = paper.can_open_new_position("ETHUSDT", "TestStrategy")
    assert allowed is True
    assert reason == ""


def test_paper_buy_tracks_cash_and_allocated_value(tmp_path, monkeypatch):
    import src.modules.PaperBalanceLogger as paper

    monkeypatch.setattr(paper, "OUT_DIR", tmp_path)
    monkeypatch.setattr(paper, "TRADES_CSV", tmp_path / "trades.csv")
    monkeypatch.setattr(paper, "STATE_JSON", tmp_path / "state.json")
    monkeypatch.setattr(paper, "SUMMARY_TXT", tmp_path / "summary.txt")

    paper.log_buy("SOLUSDT", "TestStrategy", 80.0, 2.0)
    state = paper._load_state()
    sol = state["SOLUSDT"]
    meta = state["_meta"]

    assert sol["allocated_usd"] == 160.192
    assert sol["cash_balance"] == 0.0
    assert sol["balance"] == 160.0
    assert meta["cash_balance"] == 839.808
    assert meta["equity"] == 999.808

    paper.mark_to_market("SOLUSDT", 82.0)
    state = paper._load_state()
    sol = state["SOLUSDT"]
    meta = state["_meta"]

    assert sol["market_value"] == 164.0
    assert sol["unrealized_pnl_usd"] == pytest.approx(3.808)
    assert sol["balance"] == 164.0
    assert meta["cash_balance"] == 839.808
    assert meta["equity"] == pytest.approx(1003.808)


def test_plan_paper_buy_caps_to_position_limit_and_cash(tmp_path, monkeypatch):
    import src.modules.PaperBalanceLogger as paper

    monkeypatch.setattr(paper, "OUT_DIR", tmp_path)
    monkeypatch.setattr(paper, "TRADES_CSV", tmp_path / "trades.csv")
    monkeypatch.setattr(paper, "STATE_JSON", tmp_path / "state.json")
    monkeypatch.setattr(paper, "SUMMARY_TXT", tmp_path / "summary.txt")
    monkeypatch.setattr(paper, "MAX_POSITION_USD", 250.0)
    monkeypatch.setattr(paper, "MAX_POSITION_PCT", 100.0)

    allowed, qty, reason = paper.plan_paper_buy("BTCUSDT", "TestStrategy", 100.0, 10.0)

    assert allowed is True
    assert qty == 2.5
    assert "ajustada" in reason


def test_paused_strategies_refresh_existing_stats(tmp_path, monkeypatch):
    import src.modules.PaperBalanceLogger as paper

    state_json = tmp_path / "state.json"
    monkeypatch.setattr(paper, "OUT_DIR", tmp_path)
    monkeypatch.setattr(paper, "TRADES_CSV", tmp_path / "trades.csv")
    monkeypatch.setattr(paper, "STATE_JSON", state_json)
    monkeypatch.setattr(paper, "SUMMARY_TXT", tmp_path / "summary.txt")
    monkeypatch.setattr(paper, "AUTO_JUDGE_ENABLED", True)
    monkeypatch.setattr(paper, "JUDGE_MAX_LOSS_STREAK", 3)

    state_json.write_text(json.dumps({
        "_meta": {
            "strategy_stats": {
                "Aether:bad#1": {
                    "trades": 3,
                    "wins": 0,
                    "losses": 3,
                    "pnl_usd": -6.0,
                    "loss_streak": 3,
                    "paused": False,
                    "pause_reason": "",
                }
            },
            "paused_strategies": {},
        }
    }), encoding="utf-8")

    paused = paper.get_paused_strategies()

    assert paused == {"Aether:bad#1": "3 perdas seguidas"}
    refreshed = json.loads(state_json.read_text(encoding="utf-8"))
    stats = refreshed["_meta"]["strategy_stats"]["Aether:bad#1"]
    assert stats["paused"] is True
    assert stats["pause_reason"] == "3 perdas seguidas"
