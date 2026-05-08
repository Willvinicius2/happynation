import pandas as pd

from src.modules.StrategyResearchEngine import (
    ResearchRules,
    StrategyResearchResult,
    StrategySpec,
    _result_sort_key,
    research_strategies_parallel,
    validate_strategy,
)
from src.research.validator import ValidationCriteria, full_validation
from src.strategy_research import write_research_tree_report


def create_research_data(days=130, candles_per_day=24, direction="up"):
    rows = days * candles_per_day
    factor = 1.002 if direction == "up" else 0.998
    close = [100 * (factor ** index) for index in range(rows)]
    data = pd.DataFrame(
        {
            "open_time": pd.date_range("2025-01-01", periods=rows, freq="h", tz="UTC"),
            "open_price": close,
            "high_price": [price * 1.001 for price in close],
            "low_price": [price * 0.999 for price in close],
            "close_price": close,
            "volume": [1000.0] * rows,
        }
    )
    return data


def buy_every_day_strategy(stock_data, verbose=False):
    hour = stock_data["open_time"].iloc[-1].hour
    return True if hour == 0 else False if hour == 12 else None


def losing_strategy(stock_data, verbose=False):
    hour = stock_data["open_time"].iloc[-1].hour
    return True if hour == 12 else False if hour == 0 else None


def no_trade_strategy(stock_data, verbose=False):
    return None


def buy_and_hold_strategy(stock_data, verbose=False):
    return True


def test_validate_strategy_passes_strict_60_day_rules():
    rules = ResearchRules(
        window_days=60,
        min_window_profit_pct=25,
        min_window_trades=20,
        min_total_trades=40,
        min_winrate_pct=50,
        trade_fee_pct=0,
    )
    result = validate_strategy(
        asset="TESTUSDT",
        stock_data=create_research_data(),
        strategy=StrategySpec("DailyTrend", buy_every_day_strategy),
        rules=rules,
    )

    assert result.error is None
    assert result.passed is True
    assert result.total_trades >= 40
    assert result.winrate_pct == 100
    assert result.max_drawdown_pct == 0
    assert result.profit_factor == 999.0
    assert result.max_loss_streak == 0
    assert result.consistency_score >= rules.min_consistency_score
    assert result.score > 500
    assert result.windows_passed == result.windows_total


def test_validate_strategy_rejects_low_winrate_and_profit():
    rules = ResearchRules(
        window_days=60,
        min_window_profit_pct=1,
        min_window_trades=20,
        min_total_trades=40,
        min_winrate_pct=50,
        trade_fee_pct=0,
    )
    result = validate_strategy(
        asset="TESTUSDT",
        stock_data=create_research_data(direction="down"),
        strategy=StrategySpec("DailyLoser", losing_strategy),
        rules=rules,
    )

    assert result.passed is False
    assert result.winrate_pct == 0
    assert result.max_loss_streak > rules.max_loss_streak
    assert result.profit_factor == 0
    assert result.min_window_profit_pct < 0


def test_research_strategies_parallel_orders_approved_first():
    rules = ResearchRules(
        window_days=60,
        min_window_profit_pct=25,
        min_window_trades=20,
        min_total_trades=40,
        min_winrate_pct=50,
        trade_fee_pct=0,
    )
    results = research_strategies_parallel(
        {"TESTUSDT": create_research_data()},
        strategies=[
            StrategySpec("NoTrade", no_trade_strategy),
            StrategySpec("DailyTrend", buy_every_day_strategy),
        ],
        rules=rules,
        max_workers=2,
    )

    assert results[0].strategy == "DailyTrend"
    assert results[0].passed is True
    assert results[-1].passed is False


def test_research_strategies_parallel_prefers_more_trades_on_tie():
    def make_result(name, trades):
        return StrategyResearchResult(
            asset="TESTUSDT",
            strategy=name,
            params={},
            take_profit_pct=None,
            stop_loss_pct=None,
            max_hold_candles=None,
            passed=True,
            total_profit_pct=10.0,
            total_trades=trades,
            winrate_pct=60.0,
            max_drawdown_pct=5.0,
            profit_factor=1.5,
            max_loss_streak=1,
            consistency_score=70.0,
            score=100.0,
            windows_passed=2,
            windows_total=2,
            avg_window_profit_pct=5.0,
            min_window_profit_pct=4.0,
        )

    low_trades = make_result("FewTrades", 10)
    high_trades = make_result("MoreTrades", 40)

    results = sorted([low_trades, high_trades], key=_result_sort_key, reverse=True)

    assert results[0].strategy == "MoreTrades"


def test_research_strategies_parallel_handles_empty_inputs():
    assert research_strategies_parallel({}, strategies=[], max_workers=0) == []
    assert research_strategies_parallel({"TESTUSDT": create_research_data()}, strategies=[], max_workers=0) == []


def test_full_validation_cuts_bad_candidate_after_full_phase():
    report = full_validation(
        strategy_id="no-trade",
        template="NoTrade",
        params={},
        risk={"take_profit_pct": 2.0, "stop_loss_pct": 1.5, "max_hold_candles": 96},
        fn=no_trade_strategy,
        data_by_asset={"TESTUSDT": create_research_data(days=90)},
        rules=ResearchRules(trade_fee_pct=0),
        criteria=ValidationCriteria(fee_levels=(0.1,), max_scenario_workers=0),
    )

    assert report.approved is False
    assert report.rejection_reason is not None
    assert {scenario.split for scenario in report.scenarios} == {"full"}


def test_take_profit_closes_trade_before_strategy_sell():
    rules = ResearchRules(
        window_days=60,
        min_window_profit_pct=1,
        min_window_trades=1,
        min_total_trades=1,
        min_winrate_pct=50,
        trade_fee_pct=0,
    )
    result = validate_strategy(
        asset="TESTUSDT",
        stock_data=create_research_data(days=70),
        strategy=StrategySpec("BuyHold", buy_and_hold_strategy, take_profit_pct=2.0, stop_loss_pct=5.0),
        rules=rules,
    )

    assert result.total_trades >= 1
    assert result.winrate_pct == 100
    assert result.total_profit_pct > 0


def test_research_tree_report_is_written(tmp_path):
    rules = ResearchRules(
        window_days=60,
        min_window_profit_pct=25,
        min_window_trades=20,
        min_total_trades=40,
        min_winrate_pct=50,
        trade_fee_pct=0,
    )
    result = validate_strategy(
        asset="TESTUSDT",
        stock_data=create_research_data(),
        strategy=StrategySpec("DailyTrend", buy_every_day_strategy),
        rules=rules,
    )

    out = tmp_path / "research_tree.html"
    write_research_tree_report([result], out)

    html = out.read_text(encoding="utf-8")
    assert "Strategy Search" in html
    assert "Nucleo de decisao" in html
    assert "DailyTrend" in html
    assert "Pulso robusto" in html
