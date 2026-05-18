import pandas as pd

from src.strategies.multi_strategy import MultiStrategy, VoteSpec


class DummyTrader:
    actual_trade_position = False
    last_buy_price = 0
    last_buy_time = None


class PositionedTrader(DummyTrader):
    actual_trade_position = True
    last_buy_price = 100


def buy_signal(stock_data, verbose=False):
    return True


def sell_signal(stock_data, verbose=False):
    return False


def neutral_signal(stock_data, verbose=False):
    return None


def test_multi_strategy_buys_with_minimum_consensus():
    data = pd.DataFrame({"close_price": [100, 101]})
    strategies = [
        VoteSpec("buy1", buy_signal),
        VoteSpec("buy2", buy_signal),
        VoteSpec("neutral", neutral_signal),
    ]

    decision = MultiStrategy(
        DummyTrader(),
        stock_data=data,
        verbose=False,
        min_buy_votes=2,
        strategies=strategies,
    )

    assert decision is True


def test_multi_strategy_waits_when_flat_without_consensus():
    data = pd.DataFrame({"close_price": [100, 101]})
    strategies = [
        VoteSpec("buy1", buy_signal),
        VoteSpec("sell1", sell_signal),
        VoteSpec("neutral", neutral_signal),
    ]

    decision = MultiStrategy(
        DummyTrader(),
        stock_data=data,
        verbose=False,
        min_buy_votes=2,
        strategies=strategies,
    )

    assert decision is None


def test_multi_strategy_sells_position_with_sell_consensus():
    data = pd.DataFrame({"close_price": [100, 99]})
    strategies = [
        VoteSpec("sell1", sell_signal),
        VoteSpec("sell2", sell_signal),
        VoteSpec("buy1", buy_signal),
    ]

    decision = MultiStrategy(
        PositionedTrader(),
        stock_data=data,
        verbose=False,
        min_sell_votes=2,
        strategies=strategies,
    )

    assert decision is False


def test_multi_strategy_includes_active_specs_as_independent_votes(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    data = pd.DataFrame({"close_price": [100, 101]})
    strategies = [
        VoteSpec("fixed-buy", buy_signal),  # >=2 votos exigido pelo piso
    ]

    class ActiveBuy:
        name = "AetherBuy"
        params = {}
        needs_instance = False

        @staticmethod
        def function(stock_data, verbose=False):
            return True

    decision = MultiStrategy(
        DummyTrader(),
        stock_data=data,
        verbose=False,
        strategies=strategies,
        active_strategy_specs=[ActiveBuy()],
    )

    assert decision is True


def test_multi_strategy_uses_copied_data_per_strategy(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    data = pd.DataFrame({"close_price": [100, 101]})

    def mutating_buy(stock_data, verbose=False):
        stock_data["temporary_column"] = 1
        return True

    def assert_clean_buy(stock_data, verbose=False):
        assert "temporary_column" not in stock_data.columns
        return True

    decision = MultiStrategy(
        DummyTrader(),
        stock_data=data,
        verbose=False,
        min_buy_votes=2,
        strategies=[
            VoteSpec("mutating-buy", mutating_buy),
            VoteSpec("clean-buy", assert_clean_buy),
        ],
    )

    assert decision is True
    assert "temporary_column" not in data.columns


def test_multi_strategy_filters_active_specs_by_timeframe(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    data = pd.DataFrame({"close_price": [100, 101]})

    class ActiveBuy4h:
        name = "AetherBuy4h"
        params = {"_timeframe": "4h"}
        needs_instance = False

        @staticmethod
        def function(stock_data, verbose=False):
            return True

    decision = MultiStrategy(
        DummyTrader(),
        stock_data=data,
        verbose=False,
        min_buy_votes=1,
        strategies=[VoteSpec("fixed-neutral", neutral_signal)],
        active_strategy_specs=[ActiveBuy4h()],
        bot_timeframe="1h",
    )

    assert decision is None


def test_multi_strategy_respects_vote_weight(monkeypatch):
    monkeypatch.setenv("PAPER_TRADING", "true")
    data = pd.DataFrame({"close_price": [100, 101]})

    decision = MultiStrategy(
        DummyTrader(),
        stock_data=data,
        verbose=False,
        min_buy_votes=1,
        min_buy_weight=1.0,
        strategies=[VoteSpec("weak-buy", buy_signal, weight=0.5)],
    )

    assert decision is None


def test_multi_strategy_skips_dead_active_specs(monkeypatch):
    import src.modules.PaperBalanceLogger as paper

    monkeypatch.setenv("AETHER_SKIP_DEAD_STRATEGIES", "true")
    monkeypatch.setattr(paper, "get_paused_strategies", lambda: {})
    monkeypatch.setattr(
        paper,
        "get_dead_strategies",
        lambda min_calls=1000, max_active_pct=0.1: ["AetherDead"],
    )
    data = pd.DataFrame({"close_price": [100, 101]})

    class ActiveDead:
        name = "AetherDead"
        params = {}
        needs_instance = False

        @staticmethod
        def function(stock_data, verbose=False):
            raise AssertionError("dead strategy should not run")

    class ActiveLive:
        name = "AetherLive"
        params = {}
        needs_instance = False

        @staticmethod
        def function(stock_data, verbose=False):
            return True

    trader = DummyTrader()
    decision = MultiStrategy(
        trader,
        stock_data=data,
        verbose=False,
        active_strategy_specs=[ActiveDead(), ActiveLive()],
    )

    assert decision is True
    snapshot = trader.last_strategy_snapshot
    assert snapshot["buy_votes"] == ["AetherLive"]
    assert "AetherDead:sem_sinal" in snapshot["skipped_votes"]
