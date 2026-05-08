from __future__ import annotations

import importlib
import inspect
import itertools
import math
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd


warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

StrategyFn = Callable[..., bool | None]


@dataclass(frozen=True)
class StrategySpec:
    name: str
    function: StrategyFn
    params: dict[str, Any] = field(default_factory=dict)
    needs_instance: bool = False
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    max_hold_candles: int | None = None


@dataclass(frozen=True)
class ResearchRules:
    window_days: int = 60
    min_window_profit_pct: float = 50.0
    min_window_trades: int = 6
    min_total_trades: int = 20
    min_winrate_pct: float = 50.0
    max_drawdown_pct: float = 25.0
    min_profit_factor: float = 1.15
    max_loss_streak: int = 4
    min_consistency_score: float = 55.0
    initial_balance: float = 1000.0
    lookback_candles: int = 500
    trade_fee_pct: float = 0.1


@dataclass
class TradeResult:
    entry_time: Any
    exit_time: Any
    entry_price: float
    exit_price: float
    profit_pct: float
    profit_value: float
    won: bool


@dataclass
class WindowResult:
    start: str
    end: str
    profit_pct: float
    trades: int
    winrate_pct: float
    passed: bool


@dataclass
class StrategyResearchResult:
    asset: str
    strategy: str
    params: dict[str, Any]
    take_profit_pct: float | None
    stop_loss_pct: float | None
    max_hold_candles: int | None
    passed: bool
    total_profit_pct: float
    total_trades: int
    winrate_pct: float
    max_drawdown_pct: float
    profit_factor: float
    max_loss_streak: int
    consistency_score: float
    score: float
    windows_passed: int
    windows_total: int
    avg_window_profit_pct: float
    min_window_profit_pct: float
    error: str | None = None
    seconds: float = 0.0
    windows: list[WindowResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["params"] = ";".join(f"{key}={value}" for key, value in sorted(self.params.items()))
        data["windows"] = [asdict(window) for window in self.windows]
        return data


class ResearchTraderState:
    def __init__(self) -> None:
        self.operation_code = "RESEARCH"
        self.actual_trade_position = False
        self.last_buy_price: float | None = None
        self.last_buy_time = None
        self.fallback_activated = False

    def open_position(self, price: float, open_time: Any) -> None:
        self.actual_trade_position = True
        self.last_buy_price = price
        self.last_buy_time = open_time

    def close_position(self) -> None:
        self.actual_trade_position = False
        self.last_buy_price = None
        self.last_buy_time = None

    def get_asset_state(self, _asset: str) -> dict[str, Any]:
        return {}

    def update_asset_state(self, _asset: str, **_kwargs: Any) -> None:
        return None


def _product_dict(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    if not grid:
        return [{}]

    keys = list(grid)
    return [dict(zip(keys, values)) for values in itertools.product(*(grid[key] for key in keys))]


def get_default_strategy_specs() -> list[StrategySpec]:
    from src.strategies.ma_rsi_volume_strategy import getMovingAverageRSIVolumeStrategy
    from src.strategies.moving_average import getMovingAverageTradeStrategy
    from src.strategies.rsi_strategy import getRsiTradeStrategy
    from src.strategies.ut_bot_alerts import utBotAlerts
    from src.strategies.vortex_strategy import getVortexTradeStrategy

    try:
        from src.strategies.high_win_rate_scalp import HighWinRateScalpStrategy
    except Exception:
        HighWinRateScalpStrategy = None

    strategy_grids: list[tuple[str, StrategyFn, dict[str, list[Any]], bool]] = [
        (
            "MovingAverage",
            getMovingAverageTradeStrategy,
            {"fast_window": [3, 7, 12], "slow_window": [20, 40, 60]},
            False,
        ),
        (
            "RSI",
            getRsiTradeStrategy,
            {"low": [20, 25, 30, 35], "high": [65, 70, 75, 80]},
            False,
        ),
        ("Vortex", getVortexTradeStrategy, {}, False),
        (
            "MA_RSI_Volume",
            getMovingAverageRSIVolumeStrategy,
            {
                "fast_window": [5, 7, 10],
                "slow_window": [30, 40, 60],
                "rsi_oversold": [25, 30, 35],
                "rsi_overbought": [65, 70, 75],
                "volume_multiplier": [1.0, 1.2, 1.5],
            },
            False,
        ),
        (
            "UTBotAlerts",
            utBotAlerts,
            {"atr_period": [7, 10, 14], "atr_multiplier": [1.5, 2.0, 2.5, 3.0]},
            False,
        ),
    ]

    if HighWinRateScalpStrategy is not None:
        strategy_grids.append(
            (
                "HighWinRateScalp",
                HighWinRateScalpStrategy,
                {
                    "take_profit_pct": [0.5, 0.8, 1.0],
                    "stop_loss_pct": [2.0, 3.0],
                    "rsi_min": [35.0, 38.0, 42.0],
                    "rsi_max": [58.0, 62.0, 66.0],
                    "pullback_pct": [0.2, 0.4],
                    "max_hold_bars": [24, 48, 72],
                },
                True,
            )
        )

    specs: list[StrategySpec] = []
    for name, function, grid, needs_instance in strategy_grids:
        for params in _product_dict(grid):
            if "fast_window" in params and "slow_window" in params and params["fast_window"] >= params["slow_window"]:
                continue
            if "low" in params and "high" in params and params["low"] >= params["high"]:
                continue
            specs.append(StrategySpec(name=name, function=function, params=params, needs_instance=needs_instance))

    return specs


def get_strategy_risk_grid(strategy_name: str) -> list[dict[str, Any]]:
    grids: dict[str, dict[str, list[Any]]] = {
        "MovingAverage": {
            "take_profit_pct": [4.0, 6.0, 8.0, 12.0],
            "stop_loss_pct": [2.0, 3.0, 4.0],
            "max_hold_candles": [None, 168],
        },
        "RSI": {
            "take_profit_pct": [1.5, 2.0, 3.0, 4.0],
            "stop_loss_pct": [1.0, 1.5, 2.5],
            "max_hold_candles": [48, 96, None],
        },
        "Vortex": {
            "take_profit_pct": [2.0, 3.0, 5.0, 8.0],
            "stop_loss_pct": [1.5, 2.5, 3.5],
            "max_hold_candles": [72, 168, None],
        },
        "MA_RSI_Volume": {
            "take_profit_pct": [2.0, 3.0, 4.0, 6.0],
            "stop_loss_pct": [1.5, 2.0, 3.0],
            "max_hold_candles": [48, 96, None],
        },
        "UTBotAlerts": {
            "take_profit_pct": [2.0, 3.0, 5.0, 7.0],
            "stop_loss_pct": [1.5, 2.0, 3.0],
            "max_hold_candles": [72, 168, None],
        },
        "HighWinRateScalp": {
            "take_profit_pct": [0.5, 0.8, 1.0, 1.5, 2.0],
            "stop_loss_pct": [1.0, 1.5, 2.0, 3.0],
            "max_hold_candles": [24, 48, 72],
        },
    }
    fallback = {
        "take_profit_pct": [2.0, 3.0, 5.0],
        "stop_loss_pct": [1.5, 2.5, 3.5],
        "max_hold_candles": [72, None],
    }
    return _product_dict(grids.get(strategy_name, fallback))


def expand_specs_with_risk_profiles(strategies: list[StrategySpec]) -> list[StrategySpec]:
    expanded: list[StrategySpec] = []
    for strategy in strategies:
        for risk in get_strategy_risk_grid(strategy.name):
            expanded.append(
                StrategySpec(
                    name=strategy.name,
                    function=strategy.function,
                    params=dict(strategy.params),
                    needs_instance=strategy.needs_instance,
                    take_profit_pct=risk["take_profit_pct"],
                    stop_loss_pct=risk["stop_loss_pct"],
                    max_hold_candles=risk["max_hold_candles"],
                )
            )
    return expanded


def discover_extra_strategy_specs() -> list[StrategySpec]:
    specs: list[StrategySpec] = []
    extras_dir = Path("src/strategies/extras")
    if not extras_dir.exists():
        return specs

    for py_file in sorted(extras_dir.glob("*.py")):
        if py_file.name in {"__init__.py", "create_strategies.py"}:
            continue

        module_name = f"src.strategies.extras.{py_file.stem}"
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue

        for function_name, function in inspect.getmembers(module, inspect.isfunction):
            if function_name.startswith("get") and function_name.endswith("TradeStrategy"):
                specs.append(StrategySpec(name=f"X_{function_name[3:-13]}", function=function))
                break

    return specs


def prepare_market_data(stock_data: pd.DataFrame) -> pd.DataFrame:
    data = stock_data.copy(deep=True)
    required = ["open_time", "open_price", "high_price", "low_price", "close_price", "volume"]
    missing = [column for column in required if column not in data.columns]
    if missing:
        raise ValueError(f"Dados sem colunas obrigatorias: {', '.join(missing)}")

    data.loc[:, "open_time"] = pd.to_datetime(data["open_time"], utc=True, errors="coerce")
    for column in ["open_price", "high_price", "low_price", "close_price", "volume"]:
        data.loc[:, column] = pd.to_numeric(data[column], errors="coerce")

    data = data.dropna(subset=required).sort_values("open_time").reset_index(drop=True)
    data.loc[:, "open"] = data["open_price"]
    data.loc[:, "high"] = data["high_price"]
    data.loc[:, "low"] = data["low_price"]
    data.loc[:, "close"] = data["close_price"]
    return data


def run_strategy_backtest(
    stock_data: pd.DataFrame,
    strategy: StrategySpec,
    rules: ResearchRules | None = None,
) -> tuple[float, list[TradeResult]]:
    rules = rules or ResearchRules()
    data = prepare_market_data(stock_data)
    if len(data) < 50:
        raise ValueError("Dados insuficientes para backtest")

    trader = ResearchTraderState()
    balance = rules.initial_balance
    entry_price = 0.0
    entry_time = None
    entry_index = 0
    trades: list[TradeResult] = []

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for index in range(1, len(data)):
            start = max(0, index + 1 - rules.lookback_candles)
            current = data.iloc[start : index + 1].copy().reset_index(drop=True)
            close_price = float(data.loc[index, "close_price"])
            open_time = data.loc[index, "open_time"]

            if trader.actual_trade_position:
                risk_trade = _try_close_by_risk(
                    balance=balance,
                    row=data.loc[index],
                    entry_price=entry_price,
                    entry_time=entry_time,
                    entry_index=entry_index,
                    current_index=index,
                    fee_pct=rules.trade_fee_pct,
                    strategy=strategy,
                )
                if risk_trade is not None:
                    balance += risk_trade.profit_value
                    trades.append(risk_trade)
                    trader.close_position()
                    continue

            try:
                if strategy.needs_instance:
                    signal = strategy.function(trader, current, verbose=False, **strategy.params)
                else:
                    signal = strategy.function(current, verbose=False, **strategy.params)
            except TypeError:
                signal = strategy.function(current, **strategy.params)

            if signal is True and not trader.actual_trade_position:
                entry_price = close_price
                entry_time = open_time
                entry_index = index
                trader.open_position(entry_price, entry_time)
            elif signal is False and trader.actual_trade_position:
                trade = _close_trade(
                    balance=balance,
                    entry_price=entry_price,
                    exit_price=close_price,
                    entry_time=entry_time,
                    exit_time=open_time,
                    fee_pct=rules.trade_fee_pct,
                )
                balance += trade.profit_value
                trades.append(trade)
                trader.close_position()

    if trader.actual_trade_position:
        final_price = float(data.iloc[-1]["close_price"])
        final_time = data.iloc[-1]["open_time"]
        trade = _close_trade(
            balance=balance,
            entry_price=entry_price,
            exit_price=final_price,
            entry_time=entry_time,
            exit_time=final_time,
            fee_pct=rules.trade_fee_pct,
        )
        balance += trade.profit_value
        trades.append(trade)

    total_profit_pct = ((balance - rules.initial_balance) / rules.initial_balance) * 100
    return total_profit_pct, trades


def _close_trade(
    balance: float,
    entry_price: float,
    exit_price: float,
    entry_time: Any,
    exit_time: Any,
    fee_pct: float,
) -> TradeResult:
    gross_profit_pct = ((exit_price - entry_price) / entry_price) * 100
    net_profit_pct = gross_profit_pct - (fee_pct * 2)
    profit_value = balance * (net_profit_pct / 100)
    return TradeResult(
        entry_time=entry_time,
        exit_time=exit_time,
        entry_price=entry_price,
        exit_price=exit_price,
        profit_pct=net_profit_pct,
        profit_value=profit_value,
        won=net_profit_pct > 0,
    )


def _close_trade_with_profit_pct(
    balance: float,
    profit_pct: float,
    entry_price: float,
    exit_price: float,
    entry_time: Any,
    exit_time: Any,
    fee_pct: float,
) -> TradeResult:
    net_profit_pct = profit_pct - (fee_pct * 2)
    profit_value = balance * (net_profit_pct / 100)
    return TradeResult(
        entry_time=entry_time,
        exit_time=exit_time,
        entry_price=entry_price,
        exit_price=exit_price,
        profit_pct=net_profit_pct,
        profit_value=profit_value,
        won=net_profit_pct > 0,
    )


def _try_close_by_risk(
    balance: float,
    row: pd.Series,
    entry_price: float,
    entry_time: Any,
    entry_index: int,
    current_index: int,
    fee_pct: float,
    strategy: StrategySpec,
) -> TradeResult | None:
    low_price = float(row["low_price"])
    high_price = float(row["high_price"])
    close_price = float(row["close_price"])
    exit_time = row["open_time"]

    if strategy.stop_loss_pct is not None:
        stop_price = entry_price * (1 - strategy.stop_loss_pct / 100)
        if low_price <= stop_price:
            return _close_trade_with_profit_pct(
                balance=balance,
                profit_pct=-strategy.stop_loss_pct,
                entry_price=entry_price,
                exit_price=stop_price,
                entry_time=entry_time,
                exit_time=exit_time,
                fee_pct=fee_pct,
            )

    if strategy.take_profit_pct is not None:
        target_price = entry_price * (1 + strategy.take_profit_pct / 100)
        if high_price >= target_price:
            return _close_trade_with_profit_pct(
                balance=balance,
                profit_pct=strategy.take_profit_pct,
                entry_price=entry_price,
                exit_price=target_price,
                entry_time=entry_time,
                exit_time=exit_time,
                fee_pct=fee_pct,
            )

    if strategy.max_hold_candles is not None and current_index - entry_index >= strategy.max_hold_candles:
        gross_profit_pct = ((close_price - entry_price) / entry_price) * 100
        return _close_trade_with_profit_pct(
            balance=balance,
            profit_pct=gross_profit_pct,
            entry_price=entry_price,
            exit_price=close_price,
            entry_time=entry_time,
            exit_time=exit_time,
            fee_pct=fee_pct,
        )

    return None


def validate_strategy(
    asset: str,
    stock_data: pd.DataFrame,
    strategy: StrategySpec,
    rules: ResearchRules | None = None,
) -> StrategyResearchResult:
    rules = rules or ResearchRules()
    start_time = time.time()
    try:
        total_profit_pct, trades = run_strategy_backtest(stock_data, strategy, rules)
        data = prepare_market_data(stock_data)
        windows = _evaluate_windows(data, trades, rules)
        total_trades = len(trades)
        winrate_pct = _winrate(trades)
        max_drawdown = _max_drawdown_pct(trades, rules.initial_balance)
        profit_factor = _profit_factor(trades)
        max_loss_streak = _max_loss_streak(trades)
        windows_total = len(windows)
        windows_passed = sum(1 for window in windows if window.passed)
        profits = [window.profit_pct for window in windows]
        min_window_profit = min(profits) if profits else 0.0
        avg_window_profit = sum(profits) / len(profits) if profits else 0.0
        consistency_score = _consistency_score(
            windows=windows,
            trades=trades,
            total_profit_pct=total_profit_pct,
            winrate_pct=winrate_pct,
            max_drawdown_pct=max_drawdown,
            profit_factor=profit_factor,
            max_loss_streak=max_loss_streak,
            rules=rules,
        )

        passed = (
            total_trades >= rules.min_total_trades
            and winrate_pct >= rules.min_winrate_pct
            and max_drawdown <= rules.max_drawdown_pct
            and profit_factor >= rules.min_profit_factor
            and max_loss_streak <= rules.max_loss_streak
            and consistency_score >= rules.min_consistency_score
            and windows_total > 0
            and windows_passed == windows_total
        )
        score = _research_score(
            passed=passed,
            total_profit_pct=total_profit_pct,
            winrate_pct=winrate_pct,
            consistency_score=consistency_score,
            profit_factor=profit_factor,
            max_drawdown_pct=max_drawdown,
            total_trades=total_trades,
        )

        return StrategyResearchResult(
            asset=asset,
            strategy=strategy.name,
            params=strategy.params,
            take_profit_pct=strategy.take_profit_pct,
            stop_loss_pct=strategy.stop_loss_pct,
            max_hold_candles=strategy.max_hold_candles,
            passed=passed,
            total_profit_pct=total_profit_pct,
            total_trades=total_trades,
            winrate_pct=winrate_pct,
            max_drawdown_pct=max_drawdown,
            profit_factor=profit_factor,
            max_loss_streak=max_loss_streak,
            consistency_score=consistency_score,
            score=score,
            windows_passed=windows_passed,
            windows_total=windows_total,
            avg_window_profit_pct=avg_window_profit,
            min_window_profit_pct=min_window_profit,
            seconds=time.time() - start_time,
            windows=windows,
        )
    except Exception as exc:
        return StrategyResearchResult(
            asset=asset,
            strategy=strategy.name,
            params=strategy.params,
            take_profit_pct=strategy.take_profit_pct,
            stop_loss_pct=strategy.stop_loss_pct,
            max_hold_candles=strategy.max_hold_candles,
            passed=False,
            total_profit_pct=0.0,
            total_trades=0,
            winrate_pct=0.0,
            max_drawdown_pct=0.0,
            profit_factor=0.0,
            max_loss_streak=0,
            consistency_score=0.0,
            score=0.0,
            windows_passed=0,
            windows_total=0,
            avg_window_profit_pct=0.0,
            min_window_profit_pct=0.0,
            error=f"{exc.__class__.__name__}: {exc}",
            seconds=time.time() - start_time,
        )


def _evaluate_windows(
    data: pd.DataFrame,
    trades: list[TradeResult],
    rules: ResearchRules,
) -> list[WindowResult]:
    start = data["open_time"].min()
    end = data["open_time"].max()
    if pd.isna(start) or pd.isna(end):
        return []

    windows: list[WindowResult] = []
    current = start
    window_delta = pd.Timedelta(days=rules.window_days)
    while current < end:
        window_end = min(current + window_delta, end)
        full_window = (window_end - current) >= window_delta * 0.9
        if not full_window:
            break

        window_trades = [
            trade for trade in trades if pd.Timestamp(trade.exit_time) >= current and pd.Timestamp(trade.exit_time) < window_end
        ]
        profit_pct = _compound_profit_pct(window_trades)
        winrate_pct = _winrate(window_trades)
        trade_count = len(window_trades)
        passed = (
            profit_pct >= rules.min_window_profit_pct
            and trade_count >= rules.min_window_trades
            and winrate_pct >= rules.min_winrate_pct
        )
        windows.append(
            WindowResult(
                start=current.strftime("%Y-%m-%d"),
                end=window_end.strftime("%Y-%m-%d"),
                profit_pct=profit_pct,
                trades=trade_count,
                winrate_pct=winrate_pct,
                passed=passed,
            )
        )
        current = window_end

    return windows


def _compound_profit_pct(trades: list[TradeResult]) -> float:
    value = 1.0
    for trade in trades:
        value *= 1 + (trade.profit_pct / 100)
    return (value - 1) * 100


def _winrate(trades: list[TradeResult]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for trade in trades if trade.won)
    return (wins / len(trades)) * 100


def _profit_factor(trades: list[TradeResult]) -> float:
    gross_profit = sum(trade.profit_value for trade in trades if trade.profit_value > 0)
    gross_loss = abs(sum(trade.profit_value for trade in trades if trade.profit_value < 0))
    if gross_loss == 0:
        return 999.0 if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _max_loss_streak(trades: list[TradeResult]) -> int:
    longest = 0
    current = 0
    for trade in trades:
        if trade.won:
            current = 0
            continue
        current += 1
        longest = max(longest, current)
    return longest


def _max_drawdown_pct(trades: list[TradeResult], initial_balance: float) -> float:
    equity = initial_balance
    peak = initial_balance
    max_drawdown = 0.0
    for trade in trades:
        equity += trade.profit_value
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, ((peak - equity) / peak) * 100)
    return max_drawdown


def _consistency_score(
    windows: list[WindowResult],
    trades: list[TradeResult],
    total_profit_pct: float,
    winrate_pct: float,
    max_drawdown_pct: float,
    profit_factor: float,
    max_loss_streak: int,
    rules: ResearchRules,
) -> float:
    if not trades or not windows:
        return 0.0

    window_pass_ratio = sum(1 for window in windows if window.passed) / len(windows)
    positive_window_ratio = sum(1 for window in windows if window.profit_pct > 0) / len(windows)
    profits = [window.profit_pct for window in windows]
    avg_window_profit = sum(profits) / len(profits)
    variance = sum((profit - avg_window_profit) ** 2 for profit in profits) / len(profits)
    std_window_profit = math.sqrt(variance)

    profit_component = min(max(total_profit_pct, 0.0), 150.0) / 150.0 * 20.0
    winrate_component = min(max(winrate_pct, 0.0), 100.0) / 100.0 * 18.0
    window_component = window_pass_ratio * 22.0 + positive_window_ratio * 10.0
    factor_component = min(max(profit_factor, 0.0), 3.0) / 3.0 * 12.0
    drawdown_component = max(0.0, 1.0 - (max_drawdown_pct / max(rules.max_drawdown_pct, 1.0))) * 12.0
    streak_component = max(0.0, 1.0 - (max_loss_streak / max(rules.max_loss_streak + 1, 1))) * 6.0
    stability_penalty = min(std_window_profit, 80.0) / 80.0 * 15.0

    score = (
        profit_component
        + winrate_component
        + window_component
        + factor_component
        + drawdown_component
        + streak_component
        - stability_penalty
    )
    return round(max(0.0, min(100.0, score)), 2)


def _research_score(
    passed: bool,
    total_profit_pct: float,
    winrate_pct: float,
    consistency_score: float,
    profit_factor: float,
    max_drawdown_pct: float,
    total_trades: int,
) -> float:
    score = (
        consistency_score * 2.0
        + total_profit_pct * 0.8
        + winrate_pct * 0.35
        + min(profit_factor, 5.0) * 8.0
        + min(total_trades, 250) * 0.05
        - max_drawdown_pct * 1.8
    )
    if passed:
        score += 500.0
    return round(score, 4)


def research_strategies_parallel(
    data_by_asset: dict[str, pd.DataFrame],
    strategies: list[StrategySpec] | None = None,
    rules: ResearchRules | None = None,
    max_workers: int = 4,
) -> list[StrategyResearchResult]:
    rules = rules or ResearchRules()
    if not data_by_asset:
        return []
    if strategies is None:
        strategies = get_default_strategy_specs()
    if not strategies:
        return []

    jobs = [
        (asset, data, strategy)
        for asset, data in data_by_asset.items()
        for strategy in strategies
    ]

    if not jobs:
        return []

    results: list[StrategyResearchResult] = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        futures = {
            executor.submit(validate_strategy, asset, data, strategy, rules): (asset, strategy)
            for asset, data, strategy in jobs
        }
        for future in as_completed(futures):
            results.append(future.result())

    return sorted(results, key=_result_sort_key, reverse=True)


def _result_sort_key(item: StrategyResearchResult) -> tuple[Any, ...]:
    return (
        item.passed,
        item.score,
        item.consistency_score,
        -item.max_drawdown_pct,
        item.profit_factor,
        item.min_window_profit_pct,
        item.total_profit_pct,
        item.total_trades,
    )


def results_to_dataframe(results: list[StrategyResearchResult]) -> pd.DataFrame:
    flat_rows: list[dict[str, Any]] = []
    for result in results:
        row = result.to_dict()
        row.pop("windows", None)
        flat_rows.append(row)
    return pd.DataFrame(flat_rows)
