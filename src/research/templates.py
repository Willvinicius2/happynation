"""
Templates de estrategias geradas pelo pesquisador.

Cada template eh uma fabrica que retorna uma funcao compativel com o engine
existente: `(stock_data: pd.DataFrame, verbose: bool, **params) -> bool | None`.

A funcao recebe a janela de candles ate o momento e devolve:
  - True  -> sinal de compra
  - False -> sinal de venda
  - None  -> manter posicao
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd


StrategyFn = Callable[..., bool | None]


@dataclass(frozen=True)
class TemplateSpec:
    template: str
    params: dict[str, Any]

    def label(self) -> str:
        parts = ";".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.template}[{parts}]"


# -------------------- helpers --------------------

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(data: pd.DataFrame, period: int = 14) -> pd.Series:
    high = data["high_price"]
    low = data["low_price"]
    close = data["close_price"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _bollinger(series: pd.Series, period: int, mult: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = _sma(series, period)
    std = series.rolling(window=period, min_periods=period).std()
    upper = mid + mult * std
    lower = mid - mult * std
    return lower, mid, upper


def _macd(series: pd.Series, fast: int, slow: int, signal: int) -> tuple[pd.Series, pd.Series]:
    macd = _ema(series, fast) - _ema(series, slow)
    signal_line = _ema(macd, signal)
    return macd, signal_line


def _donchian(data: pd.DataFrame, period: int) -> tuple[pd.Series, pd.Series]:
    high = data["high_price"].rolling(window=period, min_periods=period).max()
    low = data["low_price"].rolling(window=period, min_periods=period).min()
    return low, high


def _last_two_valid(series: pd.Series) -> tuple[float, float] | None:
    arr = series.dropna()
    if len(arr) < 2:
        return None
    return float(arr.iloc[-2]), float(arr.iloc[-1])


# -------------------- templates robustos (confluencia multi-indicador) --------------------

def _make_trend_momentum_confluence(
    fast: int, slow: int, trend: int,
    rsi_period: int, rsi_buy: int, rsi_sell: int,
    vol_mult: float,
) -> StrategyFn:
    """3 sinais alinhados: tendencia (EMA + SMA200) + momento (RSI) + volume."""
    def fn(stock_data: pd.DataFrame, verbose: bool = False, **_: Any) -> bool | None:
        if len(stock_data) < trend + 5:
            return None
        close = stock_data["close_price"].astype(float)
        volume = stock_data["volume"].astype(float)
        ema_fast = _ema(close, fast)
        ema_slow = _ema(close, slow)
        sma_trend = _sma(close, trend)
        rsi = _rsi(close, rsi_period)
        avg_vol = volume.rolling(window=fast * 2, min_periods=fast).mean()
        try:
            last_close = float(close.iloc[-1])
            f = float(ema_fast.dropna().iloc[-1])
            s = float(ema_slow.dropna().iloc[-1])
            t = float(sma_trend.dropna().iloc[-1])
            r = float(rsi.dropna().iloc[-1])
            v = float(volume.iloc[-1])
            av = float(avg_vol.dropna().iloc[-1])
        except Exception:
            return None
        if av <= 0:
            return None
        trend_up = f > s and last_close > t
        momentum_up = rsi_buy <= r <= 75
        volume_ok = v >= vol_mult * av
        if trend_up and momentum_up and volume_ok:
            return True
        if r >= rsi_sell or last_close < t * 0.985:
            return False
        return None
    return fn


def _make_mean_reversion_atr(
    rsi_period: int, rsi_buy: int, rsi_exit: int,
    bb_period: int, bb_mult: float,
    atr_period: int, atr_tp: float,
    trend: int,
) -> StrategyFn:
    """RSI extremo + close perto BB inferior + tendencia macro alta. Saida no RSI alto ou TP via ATR."""
    def fn(stock_data: pd.DataFrame, verbose: bool = False, **_: Any) -> bool | None:
        if len(stock_data) < max(trend, bb_period, atr_period) + 5:
            return None
        close = stock_data["close_price"].astype(float)
        rsi = _rsi(close, rsi_period)
        lower, mid, upper = _bollinger(close, bb_period, bb_mult)
        sma_trend = _sma(close, trend)
        atr = _atr(stock_data, atr_period)
        try:
            last_close = float(close.iloc[-1])
            r = float(rsi.dropna().iloc[-1])
            l = float(lower.dropna().iloc[-1])
            t = float(sma_trend.dropna().iloc[-1])
            a = float(atr.dropna().iloc[-1])
        except Exception:
            return None
        macro_up = last_close > t
        bb_zone = last_close <= l * 1.005
        if r <= rsi_buy and bb_zone and macro_up and a > 0:
            return True
        if r >= rsi_exit or last_close > l + (atr_tp * a):
            return False
        return None
    return fn


def _make_breakout_strength(
    period: int, atr_period: int, atr_grow: float,
    rsi_period: int, rsi_min: int, vol_mult: float,
) -> StrategyFn:
    """Quebra Donchian + ATR expandindo + volume forte + RSI saudavel."""
    def fn(stock_data: pd.DataFrame, verbose: bool = False, **_: Any) -> bool | None:
        if len(stock_data) < max(period, atr_period, rsi_period) + 5:
            return None
        close = stock_data["close_price"].astype(float)
        volume = stock_data["volume"].astype(float)
        low_ch, high_ch = _donchian(stock_data, period)
        atr = _atr(stock_data, atr_period)
        rsi = _rsi(close, rsi_period)
        avg_vol = volume.rolling(window=period, min_periods=period).mean()
        try:
            last_close = float(close.iloc[-1])
            prev_high = float(high_ch.iloc[-2])
            prev_low = float(low_ch.iloc[-2])
            prev_atr, last_atr = float(atr.iloc[-2]), float(atr.iloc[-1])
            r = float(rsi.dropna().iloc[-1])
            v = float(volume.iloc[-1])
            av = float(avg_vol.dropna().iloc[-1])
        except Exception:
            return None
        if av <= 0 or prev_atr <= 0:
            return None
        atr_expanding = last_atr >= prev_atr * atr_grow
        vol_strong = v >= vol_mult * av
        rsi_ok = r >= rsi_min
        if last_close > prev_high and atr_expanding and vol_strong and rsi_ok:
            return True
        if last_close < prev_low or r < 35:
            return False
        return None
    return fn


def _make_macd_rsi_volume(
    macd_fast: int, macd_slow: int, signal: int,
    rsi_period: int, rsi_low: int, rsi_high: int,
    trend: int,
) -> StrategyFn:
    """MACD cruzando + RSI faixa saudavel + filtro tendencia."""
    def fn(stock_data: pd.DataFrame, verbose: bool = False, **_: Any) -> bool | None:
        if len(stock_data) < max(macd_slow + signal, trend) + 5:
            return None
        close = stock_data["close_price"].astype(float)
        macd, signal_line = _macd(close, macd_fast, macd_slow, signal)
        rsi = _rsi(close, rsi_period)
        sma_trend = _sma(close, trend)
        m = _last_two_valid(macd)
        sg = _last_two_valid(signal_line)
        if not m or not sg:
            return None
        prev_m, last_m = m
        prev_s, last_s = sg
        try:
            last_close = float(close.iloc[-1])
            r = float(rsi.dropna().iloc[-1])
            t = float(sma_trend.dropna().iloc[-1])
        except Exception:
            return None
        crossed_up = prev_m <= prev_s and last_m > last_s and last_m > 0
        crossed_down = prev_m >= prev_s and last_m < last_s
        rsi_ok = rsi_low <= r <= rsi_high
        trend_up = last_close > t
        if crossed_up and rsi_ok and trend_up:
            return True
        if crossed_down or r > rsi_high + 5:
            return False
        return None
    return fn


def _make_volatility_regime_breakout(
    atr_period: int, atr_window: int, vol_squeeze: float,
    breakout_period: int, rsi_period: int, rsi_min: int,
) -> StrategyFn:
    """Opera so apos compressao de volatilidade (squeeze): ATR atual abaixo da media + entao breakout."""
    def fn(stock_data: pd.DataFrame, verbose: bool = False, **_: Any) -> bool | None:
        if len(stock_data) < max(atr_window, breakout_period) + 10:
            return None
        close = stock_data["close_price"].astype(float)
        atr = _atr(stock_data, atr_period)
        atr_avg = atr.rolling(window=atr_window, min_periods=atr_window).mean()
        low_ch, high_ch = _donchian(stock_data, breakout_period)
        rsi = _rsi(close, rsi_period)
        try:
            last_close = float(close.iloc[-1])
            prev_close = float(close.iloc[-2])
            prev_high = float(high_ch.iloc[-2])
            prev_low = float(low_ch.iloc[-2])
            a = float(atr.dropna().iloc[-1])
            aa = float(atr_avg.dropna().iloc[-1])
            r = float(rsi.dropna().iloc[-1])
        except Exception:
            return None
        if aa <= 0:
            return None
        squeezed_recently = float(atr.iloc[-3]) <= aa * vol_squeeze
        breakout_up = prev_close <= prev_high and last_close > prev_high
        breakout_down = prev_close >= prev_low and last_close < prev_low
        if squeezed_recently and breakout_up and r >= rsi_min:
            return True
        if breakout_down or r < 35:
            return False
        return None
    return fn


# -------------------- templates basicos --------------------

def _make_dual_ma_trend(fast: int, slow: int, trend: int) -> StrategyFn:
    def fn(stock_data: pd.DataFrame, verbose: bool = False, **_: Any) -> bool | None:
        if len(stock_data) < trend + 5:
            return None
        close = stock_data["close_price"].astype(float)
        ema_fast = _ema(close, fast)
        ema_slow = _ema(close, slow)
        sma_trend = _sma(close, trend)
        prev_fast, last_fast = _last_two_valid(ema_fast) or (None, None)
        prev_slow, last_slow = _last_two_valid(ema_slow) or (None, None)
        if last_fast is None or last_slow is None or prev_fast is None or prev_slow is None:
            return None
        last_close = float(close.iloc[-1])
        last_trend = sma_trend.dropna().iloc[-1] if sma_trend.dropna().size else None
        if last_trend is None:
            return None
        crossed_up = prev_fast <= prev_slow and last_fast > last_slow
        crossed_down = prev_fast >= prev_slow and last_fast < last_slow
        if crossed_up and last_close > last_trend:
            return True
        if crossed_down:
            return False
        return None

    return fn


def _make_rsi_reversal(rsi_period: int, low: int, high: int, confirm: int) -> StrategyFn:
    def fn(stock_data: pd.DataFrame, verbose: bool = False, **_: Any) -> bool | None:
        if len(stock_data) < rsi_period + confirm + 5:
            return None
        close = stock_data["close_price"].astype(float)
        rsi = _rsi(close, rsi_period)
        rsi_drop = rsi.dropna()
        if len(rsi_drop) < confirm + 1:
            return None
        last_rsi = float(rsi_drop.iloc[-1])
        recent = rsi_drop.iloc[-(confirm + 1) : -1]
        if last_rsi < low:
            return None
        if (recent < low).any() and last_rsi > low and close.iloc[-1] > close.iloc[-2]:
            return True
        if last_rsi > high and close.iloc[-1] < close.iloc[-2]:
            return False
        return None

    return fn


def _make_bollinger_breakout(period: int, mult: float, vol_mult: float) -> StrategyFn:
    def fn(stock_data: pd.DataFrame, verbose: bool = False, **_: Any) -> bool | None:
        if len(stock_data) < period + 5:
            return None
        close = stock_data["close_price"].astype(float)
        volume = stock_data["volume"].astype(float)
        lower, mid, upper = _bollinger(close, period, mult)
        if pd.isna(upper.iloc[-1]) or pd.isna(mid.iloc[-1]):
            return None
        avg_vol = volume.rolling(window=period, min_periods=period).mean().iloc[-1]
        if pd.isna(avg_vol) or avg_vol == 0:
            return None
        broke_up = close.iloc[-2] <= upper.iloc[-2] and close.iloc[-1] > upper.iloc[-1]
        broke_down = close.iloc[-2] >= lower.iloc[-2] and close.iloc[-1] < lower.iloc[-1]
        vol_ok = volume.iloc[-1] >= vol_mult * avg_vol
        if broke_up and vol_ok:
            return True
        if broke_down:
            return False
        return None

    return fn


def _make_macd_trend(fast: int, slow: int, signal: int) -> StrategyFn:
    def fn(stock_data: pd.DataFrame, verbose: bool = False, **_: Any) -> bool | None:
        if len(stock_data) < slow + signal + 5:
            return None
        close = stock_data["close_price"].astype(float)
        macd, signal_line = _macd(close, fast, slow, signal)
        m = _last_two_valid(macd)
        s = _last_two_valid(signal_line)
        if not m or not s:
            return None
        prev_m, last_m = m
        prev_s, last_s = s
        crossed_up = prev_m <= prev_s and last_m > last_s
        crossed_down = prev_m >= prev_s and last_m < last_s
        if crossed_up and last_m > 0:
            return True
        if crossed_down:
            return False
        return None

    return fn


def _make_donchian_breakout(period: int, atr_period: int, atr_grow: float) -> StrategyFn:
    def fn(stock_data: pd.DataFrame, verbose: bool = False, **_: Any) -> bool | None:
        if len(stock_data) < max(period, atr_period) + 5:
            return None
        low_ch, high_ch = _donchian(stock_data, period)
        atr = _atr(stock_data, atr_period)
        a = _last_two_valid(atr)
        if not a:
            return None
        prev_atr, last_atr = a
        atr_rising = last_atr >= prev_atr * atr_grow
        prev_close = float(stock_data["close_price"].iloc[-2])
        last_close = float(stock_data["close_price"].iloc[-1])
        prev_high = high_ch.iloc[-2]
        prev_low = low_ch.iloc[-2]
        if pd.isna(prev_high) or pd.isna(prev_low):
            return None
        if last_close > prev_high and atr_rising:
            return True
        if last_close < prev_low:
            return False
        return None

    return fn


# -------------------- registry / generator --------------------

TEMPLATE_BUILDERS: dict[str, Callable[..., StrategyFn]] = {
    # robustos (confluencia)
    "TrendMomentumConfluence": _make_trend_momentum_confluence,
    "MeanReversionATR": _make_mean_reversion_atr,
    "BreakoutStrength": _make_breakout_strength,
    "MACD_RSI_Volume": _make_macd_rsi_volume,
    "VolatilityRegimeBreakout": _make_volatility_regime_breakout,
    # basicos
    "DualMA_Trend": _make_dual_ma_trend,
    "RSI_Reversal": _make_rsi_reversal,
    "Bollinger_Breakout": _make_bollinger_breakout,
    "MACD_Trend": _make_macd_trend,
    "Donchian_Breakout": _make_donchian_breakout,
}


TEMPLATE_GRIDS: dict[str, dict[str, list[Any]]] = {
    "TrendMomentumConfluence": {
        "fast": [8, 12, 18, 24],
        "slow": [34, 50, 80],
        "trend": [120, 200],
        "rsi_period": [9, 14],
        "rsi_buy": [50, 55, 60],
        "rsi_sell": [40, 45],
        "vol_mult": [1.0, 1.3, 1.6],
    },
    "MeanReversionATR": {
        "rsi_period": [9, 14],
        "rsi_buy": [22, 28, 32],
        "rsi_exit": [55, 62, 70],
        "bb_period": [20, 30],
        "bb_mult": [2.0, 2.4],
        "atr_period": [14, 20],
        "atr_tp": [1.5, 2.0, 3.0],
        "trend": [150, 200],
    },
    "BreakoutStrength": {
        "period": [20, 30, 50],
        "atr_period": [10, 14],
        "atr_grow": [1.05, 1.1, 1.15],
        "rsi_period": [14],
        "rsi_min": [50, 55],
        "vol_mult": [1.4, 1.8, 2.2],
    },
    "MACD_RSI_Volume": {
        "macd_fast": [8, 12, 16],
        "macd_slow": [26, 34, 50],
        "signal": [9, 12],
        "rsi_period": [14],
        "rsi_low": [42, 48],
        "rsi_high": [62, 68],
        "trend": [100, 200],
    },
    "VolatilityRegimeBreakout": {
        "atr_period": [14, 20],
        "atr_window": [40, 80],
        "vol_squeeze": [0.7, 0.85],
        "breakout_period": [20, 30, 50],
        "rsi_period": [14],
        "rsi_min": [48, 55],
    },
    # basicos (mantidos no grid mas com peso menor pela mistura random)
    "DualMA_Trend": {
        "fast": [5, 8, 12, 18],
        "slow": [21, 34, 50, 80],
        "trend": [100, 150, 200],
    },
    "RSI_Reversal": {
        "rsi_period": [9, 14, 21],
        "low": [20, 25, 30, 35],
        "high": [65, 70, 75, 80],
        "confirm": [2, 3, 5],
    },
    "Bollinger_Breakout": {
        "period": [14, 20, 30, 40],
        "mult": [1.8, 2.0, 2.4, 2.8],
        "vol_mult": [1.0, 1.4, 1.8],
    },
    "MACD_Trend": {
        "fast": [8, 12, 16],
        "slow": [21, 26, 34, 50],
        "signal": [7, 9, 12],
    },
    "Donchian_Breakout": {
        "period": [14, 20, 30, 50],
        "atr_period": [10, 14, 20],
        "atr_grow": [1.0, 1.05, 1.1],
    },
}


RISK_PROFILES: list[dict[str, Any]] = [
    {"take_profit_pct": 2.0, "stop_loss_pct": 1.5, "max_hold_candles": 96},
    {"take_profit_pct": 3.0, "stop_loss_pct": 2.0, "max_hold_candles": 72},
    {"take_profit_pct": 4.5, "stop_loss_pct": 2.5, "max_hold_candles": 168},
    {"take_profit_pct": 6.0, "stop_loss_pct": 3.0, "max_hold_candles": 168},
]


def _product(grid: dict[str, list[Any]]) -> Iterable[dict[str, Any]]:
    if not grid:
        yield {}
        return
    keys = list(grid)
    from itertools import product
    for values in product(*(grid[key] for key in keys)):
        yield dict(zip(keys, values))


def _is_valid_combo(template: str, params: dict[str, Any]) -> bool:
    if template == "DualMA_Trend":
        return params["fast"] < params["slow"] < params["trend"]
    if template == "TrendMomentumConfluence":
        return params["fast"] < params["slow"] < params["trend"] and params["rsi_buy"] > params["rsi_sell"]
    if template == "MeanReversionATR":
        return params["rsi_buy"] < params["rsi_exit"]
    if template == "MACD_RSI_Volume":
        return params["macd_fast"] < params["macd_slow"] and params["rsi_low"] < params["rsi_high"]
    if template == "RSI_Reversal":
        return params["low"] < params["high"]
    if template == "MACD_Trend":
        return params["fast"] < params["slow"]
    return True


def iter_template_specs() -> Iterable[TemplateSpec]:
    for template, grid in TEMPLATE_GRIDS.items():
        for params in _product(grid):
            if not _is_valid_combo(template, params):
                continue
            yield TemplateSpec(template=template, params=params)


def build_strategy_fn(spec: TemplateSpec) -> StrategyFn:
    builder = TEMPLATE_BUILDERS[spec.template]
    return builder(**spec.params)


def total_candidate_count() -> int:
    return sum(1 for _ in iter_template_specs()) * len(RISK_PROFILES)
