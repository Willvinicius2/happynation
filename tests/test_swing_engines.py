"""Smoke tests para engines de swing.

Executa: python -m pytest tests/test_swing_engines.py -v
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.strategies.swing import (
    BollingerMeanRevSwing,
    DonchianBreakoutSwing,
    EmaTrendSwing,
)


class _FakeBot:
    def __init__(self):
        self.actual_trade_position = False
        self.last_buy_price = None
        self.last_buy_time = None


def _make_data(n=400, trend="up", seed=7):
    rng = np.random.default_rng(seed)
    if trend == "up":
        base = np.linspace(100, 140, n)
    elif trend == "down":
        base = np.linspace(140, 100, n)
    else:
        base = np.full(n, 120.0)
    noise = rng.normal(0, 0.8, n)
    close = base + noise
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=n, freq="4h"),
        "open_price": close + rng.normal(0, 0.1, n),
        "high_price": close + np.abs(rng.normal(0.6, 0.2, n)),
        "low_price": close - np.abs(rng.normal(0.6, 0.2, n)),
        "close_price": close,
        "volume": rng.uniform(1000, 5000, n),
    })


def test_donchian_returns_bool_or_none():
    bot = _FakeBot()
    result = DonchianBreakoutSwing(bot, _make_data(trend="up"), verbose=False)
    assert result in (True, False, None)


def test_donchian_handles_short_data():
    bot = _FakeBot()
    result = DonchianBreakoutSwing(bot, _make_data(n=20), verbose=False)
    assert result in (True, False, None)


def test_donchian_canonical_name():
    assert getattr(DonchianBreakoutSwing, "__strategy_name__", None) == "DonchianBreakoutSwing"


def test_ema_trend_returns_bool_or_none():
    bot = _FakeBot()
    result = EmaTrendSwing(bot, _make_data(trend="up"), verbose=False)
    assert result in (True, False, None)


def test_ema_trend_no_buy_in_downtrend():
    bot = _FakeBot()
    result = EmaTrendSwing(bot, _make_data(trend="down"), verbose=False)
    assert result is not True


def test_ema_trend_canonical_name():
    assert getattr(EmaTrendSwing, "__strategy_name__", None) == "EmaTrendSwing"


def test_bollinger_returns_bool_or_none():
    bot = _FakeBot()
    result = BollingerMeanRevSwing(bot, _make_data(trend="up"), verbose=False)
    assert result in (True, False, None)


def test_bollinger_canonical_name():
    assert getattr(BollingerMeanRevSwing, "__strategy_name__", None) == "BollingerMeanRevSwing"


def test_swing_engines_position_aware():
    bot = _FakeBot()
    bot.actual_trade_position = True
    bot.last_buy_price = 100.0
    data = _make_data(trend="up")
    for fn in (DonchianBreakoutSwing, EmaTrendSwing, BollingerMeanRevSwing):
        result = fn(bot, data, verbose=False)
        assert result in (True, False)
