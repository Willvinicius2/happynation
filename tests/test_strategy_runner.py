import pandas as pd

from src.modules.StrategyRunner import StrategyRunner


class DummyTrader:
    operation_code = "BTCUSDT"
    actual_trade_position = False
    fallback_activated = False


def test_active_sell_signal_does_not_block_main_buy_when_flat(monkeypatch):
    data = pd.DataFrame({"close_price": [1.0, 2.0]})

    class ActiveSpec:
        name = "ActiveSell"
        params = {}

        @staticmethod
        def function(stock_data, verbose=False):
            return False

    def active_manifest_enabled():
        return True

    def get_active_specs(asset=None):
        return [ActiveSpec()]

    monkeypatch.setattr("src.research.active.active_manifest_enabled", active_manifest_enabled)
    monkeypatch.setattr("src.research.active.get_active_specs", get_active_specs)

    def main_strategy(stock_data, verbose=False):
        return True

    decision = StrategyRunner.execute(
        DummyTrader(),
        main_strategy=main_strategy,
        fallback_strategy=None,
        stock_data=data,
        main_strategy_args={},
        fallback_strategy_args={},
        verbose=False,
    )

    assert decision is True


def test_active_sell_signal_is_used_when_in_position(monkeypatch):
    data = pd.DataFrame({"close_price": [1.0, 2.0]})

    class PositionedTrader(DummyTrader):
        actual_trade_position = True

    class ActiveSpec:
        name = "ActiveSell"
        params = {}

        @staticmethod
        def function(stock_data, verbose=False):
            return False

    monkeypatch.setattr("src.research.active.active_manifest_enabled", lambda: True)
    monkeypatch.setattr("src.research.active.get_active_specs", lambda asset=None: [ActiveSpec()])

    def main_strategy(stock_data, verbose=False):
        return True

    decision = StrategyRunner.execute(
        PositionedTrader(),
        main_strategy=main_strategy,
        fallback_strategy=None,
        stock_data=data,
        main_strategy_args={},
        fallback_strategy_args={},
        verbose=False,
    )

    assert decision is False


def test_active_specs_are_passed_to_orchestrator_without_short_circuit(monkeypatch):
    data = pd.DataFrame({"close_price": [1.0, 2.0]})

    class ActiveSpec:
        name = "ActiveBuy"
        params = {}
        needs_instance = False

        @staticmethod
        def function(stock_data, verbose=False):
            return True

    monkeypatch.setattr("src.research.active.active_manifest_enabled", lambda: True)
    monkeypatch.setattr("src.research.active.get_active_specs", lambda asset=None: [ActiveSpec()])

    def orchestrator(stock_data, active_strategy_specs=None, verbose=False):
        assert len(active_strategy_specs) == 1
        assert active_strategy_specs[0].name == "ActiveBuy"
        return None

    decision = StrategyRunner.execute(
        DummyTrader(),
        main_strategy=orchestrator,
        fallback_strategy=None,
        stock_data=data,
        main_strategy_args={},
        fallback_strategy_args={},
        verbose=False,
    )

    assert decision is None
