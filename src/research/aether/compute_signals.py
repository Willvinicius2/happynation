"""Signal generators do Aether.

Funcoes _signals_<engine>(...) que produzem pd.Series de sinais (-1/0/+1)
a partir de OHLC. Extraidas de compute.py para manter o arquivo principal
focado em data loading + sweep orchestration.

Mantem prefixo underscore: sao usadas APENAS via compute._build_signals,
nao expostas como API publica.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def _signals_ma_crossover(close: pd.Series, params: dict[str, Any]) -> pd.Series:
    short = max(2, int(params.get("short_window", params.get("short", 20))))
    long = max(short + 1, int(params.get("long_window", params.get("long", 50))))
    long_only = bool(params.get("long_only", True))
    s = close.rolling(short, min_periods=short).mean()
    l = close.rolling(long, min_periods=long).mean()
    sig = pd.Series(0, index=close.index, dtype=int)
    sig[s > l] = 1
    sig[s < l] = -1
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_bollinger(close: pd.Series, params: dict[str, Any]) -> pd.Series:
    window = max(5, int(params.get("window", 20)))
    num_std = float(params.get("num_std", 2.0))
    long_only = bool(params.get("long_only", False))
    ma = close.rolling(window, min_periods=window).mean()
    sd = close.rolling(window, min_periods=window).std(ddof=0)
    upper = ma + num_std * sd
    lower = ma - num_std * sd
    sig = pd.Series(0, index=close.index, dtype=int)
    sig[close < lower] = 1
    sig[close > upper] = -1
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _rsi_series(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def _signals_rsi(close: pd.Series, params: dict[str, Any]) -> pd.Series:
    period = max(2, int(params.get("period", 14)))
    oversold = float(params.get("oversold", params.get("os", 30.0)))
    overbought = float(params.get("overbought", params.get("ob", 70.0)))
    long_only = bool(params.get("long_only", False))
    rsi = _rsi_series(close, period)
    sig = pd.Series(0, index=close.index, dtype=int)
    sig[rsi < oversold] = 1
    sig[rsi > overbought] = -1
    sig = sig.replace(0, np.nan).ffill().fillna(0).astype(int)
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_donchian(high: pd.Series, low: pd.Series, close: pd.Series, params: dict[str, Any]) -> pd.Series:
    entry = max(2, int(params.get("entry_window", 20)))
    exit_w = max(2, int(params.get("exit_window", 10)))
    long_only = bool(params.get("long_only", True))
    upper = high.rolling(entry, min_periods=entry).max()
    lower = low.rolling(entry, min_periods=entry).min()
    upper_exit = high.rolling(exit_w, min_periods=exit_w).max()
    lower_exit = low.rolling(exit_w, min_periods=exit_w).min()
    sig = pd.Series(0, index=close.index, dtype=int)
    pos = 0
    for i in range(len(close)):
        if pos == 0:
            if pd.notna(upper.iloc[i]) and close.iloc[i] >= upper.iloc[i]:
                pos = 1
            elif not long_only and pd.notna(lower.iloc[i]) and close.iloc[i] <= lower.iloc[i]:
                pos = -1
        elif pos == 1:
            if pd.notna(lower_exit.iloc[i]) and close.iloc[i] <= lower_exit.iloc[i]:
                pos = 0
        elif pos == -1:
            if pd.notna(upper_exit.iloc[i]) and close.iloc[i] >= upper_exit.iloc[i]:
                pos = 0
        sig.iloc[i] = pos
    return sig


def _signals_supertrend(
    high: pd.Series, low: pd.Series, close: pd.Series, params: dict[str, Any]
) -> pd.Series:
    period = max(2, int(params.get("atr_period", 10)))
    mult = float(params.get("atr_mult", 3.0))
    long_only = bool(params.get("long_only", True))
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period, min_periods=period).mean()
    hl2 = (high + low) / 2.0
    upper = hl2 + mult * atr
    lower = hl2 - mult * atr
    sig = pd.Series(0, index=close.index, dtype=int)
    direction = 1
    for i in range(len(close)):
        if pd.isna(atr.iloc[i]):
            sig.iloc[i] = 0
            continue
        if direction == 1 and close.iloc[i] < lower.iloc[i]:
            direction = -1
        elif direction == -1 and close.iloc[i] > upper.iloc[i]:
            direction = 1
        sig.iloc[i] = direction
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_macd(close: pd.Series, params: dict[str, Any]) -> pd.Series:
    fast = max(2, int(params.get("fast", 12)))
    slow = max(fast + 1, int(params.get("slow", 26)))
    signal_p = max(2, int(params.get("signal", 9)))
    long_only = bool(params.get("long_only", False))
    ema_fast = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd = ema_fast - ema_slow
    sig_line = macd.ewm(span=signal_p, adjust=False, min_periods=signal_p).mean()
    sig = pd.Series(0, index=close.index, dtype=int)
    sig[macd > sig_line] = 1
    sig[macd < sig_line] = -1
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_stochastic(
    high: pd.Series, low: pd.Series, close: pd.Series, params: dict[str, Any]
) -> pd.Series:
    period = max(2, int(params.get("period", params.get("k_period", 14))))
    smooth_k = max(1, int(params.get("smooth_k", 3)))
    smooth_d = max(1, int(params.get("smooth_d", 3)))
    oversold = float(params.get("oversold", 20.0))
    overbought = float(params.get("overbought", 80.0))
    long_only = bool(params.get("long_only", False))
    ll = low.rolling(period, min_periods=period).min()
    hh = high.rolling(period, min_periods=period).max()
    raw_k = 100 * (close - ll) / (hh - ll).replace(0, np.nan)
    k = raw_k.rolling(smooth_k, min_periods=smooth_k).mean()
    d = k.rolling(smooth_d, min_periods=smooth_d).mean()
    sig = pd.Series(0, index=close.index, dtype=int)
    sig[(k < oversold) & (k > d)] = 1
    sig[(k > overbought) & (k < d)] = -1
    sig = sig.replace(0, np.nan).ffill().fillna(0).astype(int)
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_adx(
    high: pd.Series, low: pd.Series, close: pd.Series, params: dict[str, Any]
) -> pd.Series:
    period = max(2, int(params.get("period", 14)))
    threshold = float(params.get("threshold", 20.0))
    long_only = bool(params.get("long_only", False))
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=close.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=close.index)
    tr = pd.concat(
        [(high - low).abs(), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    sig = pd.Series(0, index=close.index, dtype=int)
    strong = adx >= threshold
    sig[strong & (plus_di > minus_di)] = 1
    sig[strong & (minus_di > plus_di)] = -1
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_keltner(
    high: pd.Series, low: pd.Series, close: pd.Series, params: dict[str, Any]
) -> pd.Series:
    window = max(5, int(params.get("window", 20)))
    atr_mult = float(params.get("atr_mult", 2.0))
    long_only = bool(params.get("long_only", False))
    typical = (high + low + close) / 3.0
    ema = typical.ewm(span=window, adjust=False, min_periods=window).mean()
    tr = pd.concat(
        [(high - low).abs(), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(span=window, adjust=False, min_periods=window).mean()
    upper = ema + atr_mult * atr
    lower = ema - atr_mult * atr
    sig = pd.Series(0, index=close.index, dtype=int)
    sig[close < lower] = 1
    sig[close > upper] = -1
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_vwap_dev(
    df: pd.DataFrame, params: dict[str, Any]
) -> pd.Series:
    window = max(5, int(params.get("window", 48)))
    z_threshold = float(params.get("z_threshold", 1.5))
    long_only = bool(params.get("long_only", False))
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    typical = (high + low + close) / 3.0
    volume = df["volume"].astype(float) if "volume" in df.columns else pd.Series(1.0, index=close.index)
    pv = typical * volume
    vwap = pv.rolling(window, min_periods=window).sum() / volume.rolling(window, min_periods=window).sum().replace(0, np.nan)
    dev = close - vwap
    std = dev.rolling(window, min_periods=window).std(ddof=0)
    z = dev / std.replace(0, np.nan)
    sig = pd.Series(0, index=close.index, dtype=int)
    sig[z < -z_threshold] = 1
    sig[z > z_threshold] = -1
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_roc(close: pd.Series, params: dict[str, Any]) -> pd.Series:
    period = max(2, int(params.get("period", 12)))
    upper = float(params.get("upper", 5.0))
    lower = float(params.get("lower", -5.0))
    long_only = bool(params.get("long_only", False))
    roc = (close / close.shift(period) - 1.0) * 100.0
    sig = pd.Series(0, index=close.index, dtype=int)
    sig[roc > upper] = 1
    sig[roc < lower] = -1
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_williams_r(
    high: pd.Series, low: pd.Series, close: pd.Series, params: dict[str, Any]
) -> pd.Series:
    period = max(2, int(params.get("period", 14)))
    oversold = float(params.get("oversold", -80.0))
    overbought = float(params.get("overbought", -20.0))
    long_only = bool(params.get("long_only", False))
    hh = high.rolling(period, min_periods=period).max()
    ll = low.rolling(period, min_periods=period).min()
    wr = -100.0 * (hh - close) / (hh - ll).replace(0, np.nan)
    sig = pd.Series(0, index=close.index, dtype=int)
    sig[wr < oversold] = 1
    sig[wr > overbought] = -1
    sig = sig.replace(0, np.nan).ffill().fillna(0).astype(int)
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_cci(
    high: pd.Series, low: pd.Series, close: pd.Series, params: dict[str, Any]
) -> pd.Series:
    period = max(2, int(params.get("period", 20)))
    threshold = float(params.get("threshold", 100.0))
    long_only = bool(params.get("long_only", False))
    typical = (high + low + close) / 3.0
    ma = typical.rolling(period, min_periods=period).mean()
    mad = typical.rolling(period, min_periods=period).apply(
        lambda x: np.fabs(x - x.mean()).mean(), raw=True
    )
    cci = (typical - ma) / (0.015 * mad.replace(0, np.nan))
    sig = pd.Series(0, index=close.index, dtype=int)
    sig[cci < -threshold] = 1
    sig[cci > threshold] = -1
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_mfi(df: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    period = max(2, int(params.get("period", 14)))
    oversold = float(params.get("oversold", 20.0))
    overbought = float(params.get("overbought", 80.0))
    long_only = bool(params.get("long_only", False))
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    volume = df["volume"].astype(float) if "volume" in df.columns else pd.Series(1.0, index=close.index)
    typical = (high + low + close) / 3.0
    money_flow = typical * volume
    delta = typical.diff()
    pos_flow = money_flow.where(delta > 0, 0.0)
    neg_flow = money_flow.where(delta < 0, 0.0)
    pos_sum = pos_flow.rolling(period, min_periods=period).sum()
    neg_sum = neg_flow.rolling(period, min_periods=period).sum()
    mr = pos_sum / neg_sum.replace(0, np.nan)
    mfi = (100 - (100 / (1 + mr))).fillna(50)
    sig = pd.Series(0, index=close.index, dtype=int)
    sig[mfi < oversold] = 1
    sig[mfi > overbought] = -1
    sig = sig.replace(0, np.nan).ffill().fillna(0).astype(int)
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_obv_trend(df: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    window = max(5, int(params.get("window", 20)))
    long_only = bool(params.get("long_only", False))
    close = df["close"].astype(float)
    volume = df["volume"].astype(float) if "volume" in df.columns else pd.Series(1.0, index=close.index)
    direction = np.sign(close.diff()).fillna(0).astype(float)
    obv = (direction * volume).cumsum()
    obv_ma = obv.rolling(window, min_periods=window).mean()
    sig = pd.Series(0, index=close.index, dtype=int)
    sig[obv > obv_ma] = 1
    sig[obv < obv_ma] = -1
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_psar(
    high: pd.Series, low: pd.Series, close: pd.Series, params: dict[str, Any]
) -> pd.Series:
    af_step = float(params.get("af_step", 0.02))
    af_max = float(params.get("af_max", 0.2))
    long_only = bool(params.get("long_only", False))
    n = len(close)
    sig = pd.Series(0, index=close.index, dtype=int)
    if n < 3:
        return sig
    bull = True
    af = af_step
    ep = float(high.iloc[0])
    sar = float(low.iloc[0])
    for i in range(1, n):
        prev_sar = sar
        sar = prev_sar + af * (ep - prev_sar)
        if bull:
            sar = min(sar, float(low.iloc[i - 1]), float(low.iloc[i - 2]) if i >= 2 else float(low.iloc[i - 1]))
            if float(low.iloc[i]) < sar:
                bull = False
                sar = ep
                ep = float(low.iloc[i])
                af = af_step
            else:
                if float(high.iloc[i]) > ep:
                    ep = float(high.iloc[i])
                    af = min(af + af_step, af_max)
        else:
            sar = max(sar, float(high.iloc[i - 1]), float(high.iloc[i - 2]) if i >= 2 else float(high.iloc[i - 1]))
            if float(high.iloc[i]) > sar:
                bull = True
                sar = ep
                ep = float(high.iloc[i])
                af = af_step
            else:
                if float(low.iloc[i]) < ep:
                    ep = float(low.iloc[i])
                    af = min(af + af_step, af_max)
        sig.iloc[i] = 1 if bull else -1
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_atr_breakout(
    high: pd.Series, low: pd.Series, close: pd.Series, params: dict[str, Any]
) -> pd.Series:
    period = max(2, int(params.get("atr_period", 14)))
    mult = float(params.get("atr_mult", 1.5))
    long_only = bool(params.get("long_only", False))
    tr = pd.concat(
        [(high - low).abs(), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period, min_periods=period).mean()
    prev = close.shift(1)
    sig = pd.Series(0, index=close.index, dtype=int)
    sig[close > prev + mult * atr] = 1
    sig[close < prev - mult * atr] = -1
    sig = sig.replace(0, np.nan).ffill().fillna(0).astype(int)
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_zscore(close: pd.Series, params: dict[str, Any]) -> pd.Series:
    window = max(5, int(params.get("window", 50)))
    threshold = float(params.get("threshold", 2.0))
    long_only = bool(params.get("long_only", False))
    ma = close.rolling(window, min_periods=window).mean()
    sd = close.rolling(window, min_periods=window).std(ddof=0)
    z = (close - ma) / sd.replace(0, np.nan)
    sig = pd.Series(0, index=close.index, dtype=int)
    sig[z < -threshold] = 1
    sig[z > threshold] = -1
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_regime_filter(
    high: pd.Series, low: pd.Series, close: pd.Series, params: dict[str, Any]
) -> pd.Series:
    """Filtro de regime estrutural — usa MA longa + slope + vol pra classificar
    o estado do mercado e gerar sinal direcional alinhado com o regime.

    Mecanismo causal: estrategias direcionais sao sensiveis a regime; este engine
    forca alinhamento — long so em trend_up confirmado, short em trend_down,
    posicao neutra em range. Diferente de MA cross simples porque usa slope da
    MA (nao apenas cruzamento) e filtra por percentile de volatilidade.

    Params:
        method: 'trend_align' (default) | 'low_vol_breakout' | 'high_vol_fade'
        ma_window: lookback da MA estrutural (default 50)
        slope_window: janela pra inclinacao (default 20)
        vol_window: janela pra ATR percentile (default 100)
        vol_percentile_max: para low_vol_breakout, vol abaixo deste percentil (default 0.4)
        long_only: bool
    """
    method = str(params.get("method", "trend_align"))
    ma_window = max(5, int(params.get("ma_window", 50)))
    slope_window = max(3, int(params.get("slope_window", 20)))
    vol_window = max(20, int(params.get("vol_window", 100)))
    vol_pct_max = float(params.get("vol_percentile_max", 0.4))
    long_only = bool(params.get("long_only", False))

    ma = close.rolling(ma_window, min_periods=ma_window).mean()
    slope = (ma - ma.shift(slope_window)) / ma.shift(slope_window).replace(0, np.nan)

    tr = pd.concat(
        [(high - low).abs(), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14, min_periods=14).mean()
    vol_pct = atr.rolling(vol_window, min_periods=vol_window).rank(pct=True)

    sig = pd.Series(0, index=close.index, dtype=int)
    if method == "trend_align":
        # long em trend_up: close > MA E slope > 0; short em trend_down: oposto
        sig[(close > ma) & (slope > 0)] = 1
        sig[(close < ma) & (slope < 0)] = -1
    elif method == "low_vol_breakout":
        # so opera em compressao de vol (vol_pct baixo), na direcao do trend
        ok = vol_pct < vol_pct_max
        sig[ok & (close > ma) & (slope > 0)] = 1
        sig[ok & (close < ma) & (slope < 0)] = -1
    elif method == "high_vol_fade":
        # fade quando vol alto E preco esticado pra fora da MA
        dev = (close - ma) / ma.replace(0, np.nan)
        ok = vol_pct > (1.0 - vol_pct_max)
        sig[ok & (dev > 0.05)] = -1  # fade rally
        sig[ok & (dev < -0.05)] = 1   # fade dump
    if long_only:
        sig = sig.clip(lower=0)
    return sig.fillna(0).astype(int)


def _signals_vol_compression(
    high: pd.Series, low: pd.Series, close: pd.Series, params: dict[str, Any]
) -> pd.Series:
    """Compressao de volatilidade seguida de rompimento (Bollinger squeeze
    expandido). Hipotese: ATR no quartil baixo + close rompendo extremo recente
    sinaliza release de energia. Robusto cross-asset porque eh estrutural.

    Params:
        atr_window: janela ATR (default 20)
        percentile_window: lookback pra rank percentil do ATR (default 100)
        percentile_max: ATR rank tem que ser <= este pra dar sinal (default 0.3)
        breakout_window: rompimento do high/low destes N candles (default 10)
        long_only: bool
    """
    atr_window = max(5, int(params.get("atr_window", 20)))
    percentile_window = max(20, int(params.get("percentile_window", 100)))
    percentile_max = float(params.get("percentile_max", 0.3))
    breakout_window = max(3, int(params.get("breakout_window", 10)))
    long_only = bool(params.get("long_only", False))

    tr = pd.concat(
        [(high - low).abs(), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(atr_window, min_periods=atr_window).mean()
    atr_pct = atr.rolling(percentile_window, min_periods=percentile_window).rank(pct=True)

    upper = high.rolling(breakout_window, min_periods=breakout_window).max().shift(1)
    lower = low.rolling(breakout_window, min_periods=breakout_window).min().shift(1)

    compressed = atr_pct <= percentile_max
    sig = pd.Series(0, index=close.index, dtype=int)
    sig[compressed & (close > upper)] = 1
    sig[compressed & (close < lower)] = -1
    # mantem posicao ate inverter ou close atravessar a media do range
    mid = (upper + lower) / 2
    sig = sig.replace(0, np.nan).ffill()
    # exit quando close cruza mid contra a posicao
    exit_long = (sig == 1) & (close < mid)
    exit_short = (sig == -1) & (close > mid)
    sig = sig.where(~(exit_long | exit_short), 0).fillna(0).astype(int)
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_structure_break(
    high: pd.Series, low: pd.Series, close: pd.Series, params: dict[str, Any]
) -> pd.Series:
    """Rompimento de swing high/low (price action estrutural). Diferente de
    Donchian porque usa swing PIVOTS (max local), nao janela fixa — captura
    nivel de oferta/demanda de fato testado pelo mercado.

    Params:
        swing_lookback: bars antes/depois pra confirmar pivot (default 5)
        confirm_bars: candles consecutivos rompendo pra confirmar (default 1)
        long_only: bool
    """
    swing = max(2, int(params.get("swing_lookback", 5)))
    confirm = max(1, int(params.get("confirm_bars", 1)))
    long_only = bool(params.get("long_only", False))

    n = len(close)
    sig = pd.Series(0, index=close.index, dtype=int)
    if n < 2 * swing + 2:
        return sig

    # swing highs/lows: ponto i eh pivot se eh max/min na janela [i-swing, i+swing]
    high_arr = high.values
    low_arr = low.values
    swing_high: list[tuple[int, float]] = []  # (index, price)
    swing_low: list[tuple[int, float]] = []
    for i in range(swing, n - swing):
        win_h = high_arr[i - swing:i + swing + 1]
        win_l = low_arr[i - swing:i + swing + 1]
        if high_arr[i] == win_h.max():
            swing_high.append((i, float(high_arr[i])))
        if low_arr[i] == win_l.min():
            swing_low.append((i, float(low_arr[i])))

    close_arr = close.values
    last_high: float | None = None
    last_low: float | None = None
    sh_idx = 0
    sl_idx = 0
    state = 0
    consec = 0
    for i in range(n):
        # so considera pivots ja confirmados (i > pivot_idx + swing)
        while sh_idx < len(swing_high) and swing_high[sh_idx][0] + swing <= i:
            last_high = swing_high[sh_idx][1]
            sh_idx += 1
        while sl_idx < len(swing_low) and swing_low[sl_idx][0] + swing <= i:
            last_low = swing_low[sl_idx][1]
            sl_idx += 1
        new_state = state
        if last_high is not None and close_arr[i] > last_high:
            consec = consec + 1 if state >= 0 else 1
            if consec >= confirm:
                new_state = 1
        elif last_low is not None and close_arr[i] < last_low:
            consec = consec + 1 if state <= 0 else 1
            if consec >= confirm:
                new_state = -1
        else:
            consec = 0
        state = new_state
        sig.iloc[i] = state
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_golden_cross(
    high: pd.Series, low: pd.Series, close: pd.Series, params: dict[str, Any]
) -> pd.Series:
    """Swing-grade MA crossover (golden/death cross). Forca janelas estruturais
    longas (50/200 classico) e exige slope da MA longa alinhado pra evitar
    catch-knife em correcoes profundas.

    Diferente de ma_crossover: (a) long_window >= 100 obrigatorio, (b) filtro
    de slope da MA longa, (c) opcional distance_min entre MAs pra evitar
    chops em regimes range. Alvo: trades de semanas/meses, nao de horas.

    Params:
        short_window: MA curta (default 50, faixa swing 20-100)
        long_window: MA longa (default 200, faixa swing 100-300, forcado >=100)
        slope_window: janela pra inclinacao da MA longa (default 30)
        slope_min: slope minimo da long MA pra liberar long (default 0.0)
        distance_min: distancia min entre MAs como fracao do close (default 0.0)
        long_only: bool
    """
    short = max(5, int(params.get("short_window", 50)))
    long_w = max(100, int(params.get("long_window", 200)))
    if long_w <= short:
        long_w = short + 50
    slope_window = max(5, int(params.get("slope_window", 30)))
    slope_min = float(params.get("slope_min", 0.0))
    distance_min = float(params.get("distance_min", 0.0))
    long_only = bool(params.get("long_only", True))

    ma_s = close.rolling(short, min_periods=short).mean()
    ma_l = close.rolling(long_w, min_periods=long_w).mean()
    slope = (ma_l - ma_l.shift(slope_window)) / ma_l.shift(slope_window).replace(0, np.nan)
    dist = (ma_s - ma_l).abs() / close.replace(0, np.nan)

    sig = pd.Series(0, index=close.index, dtype=int)
    # Golden cross + slope da MA longa subindo + distancia minima
    long_ok = (ma_s > ma_l) & (slope > slope_min) & (dist >= distance_min)
    # Death cross + slope descendo
    short_ok = (ma_s < ma_l) & (slope < -slope_min) & (dist >= distance_min)
    sig[long_ok] = 1
    sig[short_ok] = -1
    if long_only:
        sig = sig.clip(lower=0)
    return sig.fillna(0).astype(int)


def _signals_swing_pivot(
    high: pd.Series, low: pd.Series, close: pd.Series, params: dict[str, Any]
) -> pd.Series:
    """Pullback a swing pivot em tendencia (oposto de structure_break, que
    opera no rompimento). Setup classico de swing trader: em uptrend
    confirmado, comprar quando preco retorna pra perto do ultimo swing_low;
    em downtrend, vender no retorno pro swing_high.

    Mecanismo causal: niveis de pivot funcionam como suporte/resistencia
    enquanto a tendencia mae continua viva. Saida quando preco invalida
    o pivot ou o regime muda.

    Params:
        swing_lookback: bars pra confirmar pivot (default 8)
        trend_ma: MA longa pra definir regime (default 100)
        pullback_tolerance: distancia max do pivot como fracao (default 0.03)
        invalidation_buffer: pivot quebrado por mais que X% invalida (default 0.015)
        long_only: bool
    """
    swing = max(3, int(params.get("swing_lookback", 8)))
    trend_ma_w = max(20, int(params.get("trend_ma", 100)))
    tol = float(params.get("pullback_tolerance", 0.03))
    inv = float(params.get("invalidation_buffer", 0.015))
    long_only = bool(params.get("long_only", True))

    n = len(close)
    sig = pd.Series(0, index=close.index, dtype=int)
    if n < max(trend_ma_w, 2 * swing + 2):
        return sig

    ma = close.rolling(trend_ma_w, min_periods=trend_ma_w).mean()
    high_arr = high.values
    low_arr = low.values
    close_arr = close.values
    ma_arr = ma.values

    last_low: float | None = None
    last_high: float | None = None
    state = 0  # +1 long, -1 short, 0 flat
    for i in range(n):
        # pivots confirmados (i atras de swing bars do ultimo pivot)
        j = i - swing
        if j >= swing:
            win_h = high_arr[j - swing:j + swing + 1]
            win_l = low_arr[j - swing:j + swing + 1]
            if high_arr[j] == win_h.max():
                last_high = float(high_arr[j])
            if low_arr[j] == win_l.min():
                last_low = float(low_arr[j])

        if pd.isna(ma_arr[i]):
            continue

        in_uptrend = close_arr[i] > ma_arr[i]
        # Entry long: uptrend + pullback proximo do ultimo swing_low + nao quebrou
        if state <= 0 and in_uptrend and last_low is not None:
            dist_pct = (close_arr[i] - last_low) / last_low if last_low > 0 else 0
            if 0 <= dist_pct <= tol:
                state = 1
        # Exit long: quebrou pivot pra baixo ou virou downtrend
        elif state == 1:
            if last_low is not None and close_arr[i] < last_low * (1 - inv):
                state = 0
            elif not in_uptrend:
                state = 0

        # Entry short: downtrend + retorno proximo do swing_high
        if not long_only:
            if state >= 0 and not in_uptrend and last_high is not None:
                dist_pct = (last_high - close_arr[i]) / last_high if last_high > 0 else 0
                if 0 <= dist_pct <= tol:
                    state = -1
            elif state == -1:
                if last_high is not None and close_arr[i] > last_high * (1 + inv):
                    state = 0
                elif in_uptrend:
                    state = 0

        sig.iloc[i] = state
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_weekly_divergence(
    high: pd.Series, low: pd.Series, close: pd.Series, params: dict[str, Any]
) -> pd.Series:
    """Divergencia RSI em janela swing (50-100 bars). Bullish: preco faz
    lower low mas RSI faz higher low (esgotamento de venda). Bearish: preco
    higher high mas RSI lower high (esgotamento de compra).

    Diferente de rsi_divergence: usa janela MAIOR (default 60 vs ~14), exige
    distancia min entre os 2 pivots e filtra por momentum subjacente. Alvo:
    reversoes de tendencia em swing/position, nao scalp.

    Params:
        rsi_period: periodo RSI (default 14)
        lookback: janela pra detectar divergencia (default 60, faixa 40-100)
        min_gap: bars min entre os 2 pivots de preco (default 10)
        rsi_oversold: threshold pra bullish div ser "qualificada" (default 40)
        rsi_overbought: threshold pra bearish div (default 60)
        long_only: bool
    """
    rsi_period = max(2, int(params.get("rsi_period", 14)))
    lookback = max(20, int(params.get("lookback", 60)))
    min_gap = max(3, int(params.get("min_gap", 10)))
    os_th = float(params.get("rsi_oversold", 40.0))
    ob_th = float(params.get("rsi_overbought", 60.0))
    long_only = bool(params.get("long_only", False))

    rsi = _rsi_series(close, rsi_period)
    n = len(close)
    sig = pd.Series(0, index=close.index, dtype=int)
    if n < lookback + 1:
        return sig

    low_arr = low.values
    high_arr = high.values
    rsi_arr = rsi.values

    state = 0
    for i in range(lookback, n):
        win_lo = low_arr[i - lookback:i + 1]
        win_hi = high_arr[i - lookback:i + 1]
        win_rsi = rsi_arr[i - lookback:i + 1]
        if np.isnan(win_rsi).any():
            continue

        # Bullish divergence: 2 lows com lower price mas higher RSI
        lo1_idx = int(np.argmin(win_lo[:lookback - min_gap]))
        lo2_idx = int(np.argmin(win_lo[lookback - min_gap:])) + (lookback - min_gap)
        if lo2_idx - lo1_idx >= min_gap:
            p1, p2 = win_lo[lo1_idx], win_lo[lo2_idx]
            r1, r2 = win_rsi[lo1_idx], win_rsi[lo2_idx]
            if p2 < p1 and r2 > r1 and r2 < os_th + 10:
                state = 1

        # Bearish divergence: 2 highs com higher price mas lower RSI
        if not long_only:
            hi1_idx = int(np.argmax(win_hi[:lookback - min_gap]))
            hi2_idx = int(np.argmax(win_hi[lookback - min_gap:])) + (lookback - min_gap)
            if hi2_idx - hi1_idx >= min_gap:
                ph1, ph2 = win_hi[hi1_idx], win_hi[hi2_idx]
                rh1, rh2 = win_rsi[hi1_idx], win_rsi[hi2_idx]
                if ph2 > ph1 and rh2 < rh1 and rh2 > ob_th - 10:
                    state = -1

        # Reset state quando RSI atravessa 50 contra a posicao
        if state == 1 and rsi_arr[i] > 65:
            state = 0
        elif state == -1 and rsi_arr[i] < 35:
            state = 0

        sig.iloc[i] = state
    if long_only:
        sig = sig.clip(lower=0)
    return sig


def _signals_combo(df: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    specs = params.get("specs") or params.get("probe_specs") or []
    if not isinstance(specs, list) or not specs:
        raise ValueError("combo sem specs")
    signals: list[pd.Series] = []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        engine = str(spec.get("engine") or "")
        if not engine or engine == "combo":
            continue
        spec_params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
        signals.append(_build_signals(engine, df, dict(spec_params)).reset_index(drop=True).fillna(0).astype(int))
    if not signals:
        raise ValueError("combo sem engines suportadas")

    required_votes = max(1, len(signals) // 2 + 1)
    out = pd.Series(0, index=df.index, dtype=int)
    stacked = pd.concat(signals, axis=1).fillna(0).astype(int)
    buy_votes = (stacked > 0).sum(axis=1)
    sell_votes = (stacked < 0).sum(axis=1)
    out[(buy_votes >= required_votes) & (sell_votes == 0)] = 1
    out[(sell_votes >= required_votes) & (buy_votes == 0)] = -1
    return out


def _build_signals(engine: str, df: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    if engine == "combo":
        return _signals_combo(df, params)
    if engine == "ma_crossover":
        return _signals_ma_crossover(close, params)
    if engine == "bollinger":
        return _signals_bollinger(close, params)
    if engine == "rsi":
        return _signals_rsi(close, params)
    if engine == "donchian":
        return _signals_donchian(high, low, close, params)
    if engine == "supertrend":
        return _signals_supertrend(high, low, close, params)
    if engine == "macd":
        return _signals_macd(close, params)
    if engine == "stochastic":
        return _signals_stochastic(high, low, close, params)
    if engine == "adx":
        return _signals_adx(high, low, close, params)
    if engine == "keltner":
        return _signals_keltner(high, low, close, params)
    if engine == "vwap_dev":
        return _signals_vwap_dev(df, params)
    if engine == "roc":
        return _signals_roc(close, params)
    if engine == "williams_r":
        return _signals_williams_r(high, low, close, params)
    if engine == "cci":
        return _signals_cci(high, low, close, params)
    if engine == "mfi":
        return _signals_mfi(df, params)
    if engine == "obv_trend":
        return _signals_obv_trend(df, params)
    if engine == "psar":
        return _signals_psar(high, low, close, params)
    if engine == "atr_breakout":
        return _signals_atr_breakout(high, low, close, params)
    if engine == "zscore":
        return _signals_zscore(close, params)
    if engine == "regime_filter":
        return _signals_regime_filter(high, low, close, params)
    if engine == "vol_compression":
        return _signals_vol_compression(high, low, close, params)
    if engine == "structure_break":
        return _signals_structure_break(high, low, close, params)
    if engine == "golden_cross":
        return _signals_golden_cross(high, low, close, params)
    if engine == "swing_pivot":
        return _signals_swing_pivot(high, low, close, params)
    if engine == "weekly_divergence":
        return _signals_weekly_divergence(high, low, close, params)
    if engine == "hybrid_breakout":
        return _signals_hybrid_breakout(df, params)
    if engine == "hull_suite":
        return _signals_hull_suite(close, params)
    if engine == "rsi_divergence":
        return _signals_rsi_divergence(df, params)
    if engine == "sniper_csbender":
        return _signals_sniper_csbender(df, params)
    if engine == "sma_cross_exits":
        return _signals_sma_cross_exits(df, params)
    raise ValueError(f"engine nao suportada: {engine}")


def _signals_sma_cross_exits(df: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    """SMA crossover (adolgov) com TP/SL fixos -- adaptado para spot long-only.

    LONG entry: SMA(short) cruza acima de SMA(long).
    Saida: TP, SL ou cross contrario (SMA short < long).

    Aprovada por Critic v4 em BTCUSDT 1d com:
      - V2 (TP+4%, SL-2%): pnl +26.28%, mdd 8.13%, sharpe 0.82
      - WF [-3.86, +9.54, +16.62, -1.82] (3/4 ok)
      - CS SOL +16.80% (1/3 alts)
      - Perturb +/-30% nos params: median +3.28%, 80% pos
      - Bootstrap P5 +0.227

    Estrategia original Pine usa scaled exits (25% em cada nivel +1/+2/+3/+4%).
    Como motor aceita so signal binario, usamos TP unico (+4% target final).
    """
    short_len = int(params.get("short_len", 14))
    long_len = int(params.get("long_len", 28))
    tp_pct = params.get("take_profit_pct")
    sl_pct = params.get("stop_loss_pct")

    close = df["close"].astype(float).reset_index(drop=True)
    high = df["high"].astype(float).reset_index(drop=True) if "high" in df.columns else close
    low = df["low"].astype(float).reset_index(drop=True) if "low" in df.columns else close

    sma_s = close.rolling(short_len, min_periods=short_len).mean()
    sma_l = close.rolling(long_len, min_periods=long_len).mean()

    cross_up = ((sma_s > sma_l) & (sma_s.shift(1) <= sma_l.shift(1))).fillna(False).values
    cross_down = ((sma_s < sma_l) & (sma_s.shift(1) >= sma_l.shift(1))).fillna(False).values

    n = len(df)
    close_arr = close.values
    high_arr = high.values
    low_arr = low.values

    sig = np.zeros(n, dtype=np.int8)
    pos = 0
    entry = 0.0
    for i in range(n):
        if pos == 0:
            if cross_up[i]:
                pos = 1
                entry = close_arr[i]
        else:
            should_exit = False
            if tp_pct is not None and high_arr[i] >= entry * (1.0 + tp_pct / 100.0):
                should_exit = True
            elif sl_pct is not None and low_arr[i] <= entry * (1.0 - sl_pct / 100.0):
                should_exit = True
            elif cross_down[i]:
                should_exit = True
            if should_exit:
                pos = 0
                entry = 0.0
        sig[i] = pos
    return pd.Series(sig, index=df.index)


def _signals_sniper_csbender(df: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    """Sniper CSBender_ -- cruzamento de open/close em candles agregados.

    Aprovada por Critic v4 em BNBUSDT 1d (4/4 gates) com pnl +58.37%, mdd 32.65%.
    Em TFs <= 12h faz resample pra 720min (sem look-ahead) e propaga sinal.
    Em TFs >= 12h (1d) usa cruzamento candle-a-candle do proprio TF.
    """
    period_minutes = int(params.get("period_minutes", 720))
    tf_label = str(params.get("timeframe", ""))
    tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
    base_min = tf_minutes.get(tf_label, 60)
    n = len(df)
    if base_min < period_minutes:
        df_idx = df.copy()
        if "open_time" in df_idx.columns:
            ot = df_idx["open_time"]
            if not pd.api.types.is_datetime64_any_dtype(ot):
                ot = pd.to_datetime(ot, utc=True)
            df_idx = df_idx.assign(_t=ot).set_index("_t").sort_index()
        else:
            df_idx.index = pd.date_range("2020-01-01", periods=n, freq=f"{base_min}min")
        rule = f"{period_minutes}min"
        agg = df_idx.resample(rule, label="right", closed="right").agg({
            "open": "first", "high": "max", "low": "min", "close": "last",
        }).dropna()
        o = agg["open"]; c = agg["close"]
        long_cond = ((c > o) & (c.shift(1) <= o.shift(1))).fillna(False).values
        short_cond = ((c < o) & (c.shift(1) >= o.shift(1))).fillna(False).values
        pos_arr = np.zeros(len(agg), dtype=np.int8)
        cur = 0
        for i in range(len(agg)):
            if long_cond[i]:
                cur = 1
            elif short_cond[i]:
                cur = 0  # spot long-only
            pos_arr[i] = cur
        pos_series = pd.Series(pos_arr, index=agg.index)
        # Shift +1 pra evitar look-ahead
        pos_full = pos_series.reindex(df_idx.index, method="ffill").shift(1).fillna(0).astype(int)
        return pd.Series(pos_full.values.astype(np.int8), index=df.index)
    else:
        o = df["open"].astype(float).reset_index(drop=True)
        c = df["close"].astype(float).reset_index(drop=True)
        long_cond = ((c > o) & (c.shift(1) <= o.shift(1))).fillna(False).values
        short_cond = ((c < o) & (c.shift(1) >= o.shift(1))).fillna(False).values
        pos_arr = np.zeros(n, dtype=np.int8)
        cur = 0
        for i in range(n):
            if long_cond[i]:
                cur = 1
            elif short_cond[i]:
                cur = 0
            pos_arr[i] = cur
        return pd.Series(pos_arr, index=df.index)


def _signals_rsi_divergence(df: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    """RSI Divergence (TV) -- so Regular Bull entry (V2 sem Hidden Bull).

    Detecta divergencias entre RSI e preco usando pivots:
    - Regular Bull: preco LL + RSI HL -> LONG
    - Saida: RSI cruza > take_profit_rsi (default 80) OU Regular Bear detectado
    Long-only para spot Binance.

    Aprovada por Critic v4 (sem Hidden Bull) em:
      BTC 4h: WF 4/4, sharpe 0.93, pnl +59.90%, mdd 26.82%
      SOL 4h: WF 4/4, sharpe 1.39, pnl +189.04%, mdd 37.91%
      BNB 1d: WF 4/4, sharpe 0.94, pnl +48.40%, mdd 14.15%
    """
    rsi_len = int(params.get("rsi_len", 9))
    lbL = int(params.get("lbL", 1))
    lbR = int(params.get("lbR", 3))
    range_lower = int(params.get("range_lower", 5))
    range_upper = int(params.get("range_upper", 60))
    take_profit_rsi = int(params.get("take_profit_rsi", 80))

    close = df["close"].astype(float).reset_index(drop=True)
    high = df["high"].astype(float).reset_index(drop=True)
    low = df["low"].astype(float).reset_index(drop=True)

    # RSI Wilder
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / rsi_len, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / rsi_len, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    osc = (100 - (100 / (1 + rs))).fillna(50)
    osc_arr = osc.values

    n = len(close)
    high_arr = high.values
    low_arr = low.values

    # Pivot detection
    def find_piv(arr, kind):
        out = np.full(n, np.nan)
        for i in range(lbL + lbR, n):
            center = i - lbR
            win_start = max(0, center - lbL)
            win_end = i + 1
            window = arr[win_start:win_end]
            if not np.isfinite(arr[center]):
                continue
            if kind == "low" and arr[center] == window.min():
                out[i] = arr[center]
            elif kind == "high" and arr[center] == window.max():
                out[i] = arr[center]
        return out

    pl_val = find_piv(osc_arr, "low")
    ph_val = find_piv(osc_arr, "high")
    pl_found = ~np.isnan(pl_val)
    ph_found = ~np.isnan(ph_val)

    osc_at_pl = np.where(pl_found, np.array([osc_arr[i - lbR] if i >= lbR else np.nan for i in range(n)]), np.nan)
    low_at_pl = np.where(pl_found, np.array([low_arr[i - lbR] if i >= lbR else np.nan for i in range(n)]), np.nan)
    osc_at_ph = np.where(ph_found, np.array([osc_arr[i - lbR] if i >= lbR else np.nan for i in range(n)]), np.nan)
    high_at_ph = np.where(ph_found, np.array([high_arr[i - lbR] if i >= lbR else np.nan for i in range(n)]), np.nan)

    def valuewhen_n(cond, source, k):
        out = np.full(n, np.nan)
        idxs: list[int] = []
        for i in range(n):
            if cond[i]:
                idxs.append(i)
            if len(idxs) > k:
                out[i] = source[idxs[-(k + 1)]]
        return out

    osc_prev_pl = valuewhen_n(pl_found, osc_at_pl, 1)
    low_prev_pl = valuewhen_n(pl_found, low_at_pl, 1)
    osc_prev_ph = valuewhen_n(ph_found, osc_at_ph, 1)
    high_prev_ph = valuewhen_n(ph_found, high_at_ph, 1)

    def bars_since_prev(cond):
        out = np.full(n, -1)
        last = -1
        for i in range(n):
            if i > 0 and cond[i - 1]:
                last = i - 1
            out[i] = (i - last) if last >= 0 else -1
        return out

    bars_pl = bars_since_prev(pl_found)
    bars_ph = bars_since_prev(ph_found)
    in_range_pl = (bars_pl >= range_lower) & (bars_pl <= range_upper)
    in_range_ph = (bars_ph >= range_lower) & (bars_ph <= range_upper)

    osc_hl = (osc_at_pl > osc_prev_pl) & in_range_pl
    price_ll = low_at_pl < low_prev_pl
    bull_cond = pl_found & osc_hl & price_ll

    osc_lh = (osc_at_ph < osc_prev_ph) & in_range_ph
    price_hh = high_at_ph > high_prev_ph
    bear_cond = ph_found & osc_lh & price_hh

    cross_above = np.zeros(n, dtype=bool)
    for i in range(1, n):
        if osc_arr[i] > take_profit_rsi and osc_arr[i - 1] <= take_profit_rsi:
            cross_above[i] = True

    long_entry = bull_cond
    long_exit = cross_above | bear_cond

    sig = np.zeros(n, dtype=np.int8)
    pos = 0
    for i in range(n):
        if pos == 0:
            if long_entry[i]:
                pos = 1
        else:
            if long_exit[i]:
                pos = 0
        sig[i] = pos
    return pd.Series(sig, index=df.index)


def _signals_hull_suite(close: pd.Series, params: dict[str, Any]) -> pd.Series:
    """Hull Suite (DashTrader/InSilico) -- adaptado long-only para spot Binance.

    HMA, EHMA ou THMA com length configuravel. Sinal:
    - HMA[atual] > HMA[2 candles atras] -> LONG (1)
    - HMA[atual] < HMA[2 candles atras] -> EXIT (0); spot nao shorta.

    Aprovada por Critic v4 em BTCUSDT 1d (4/4 gates) com PnL +154%, MDD 33.9%,
    Sharpe 1.13. ETH/SOL/BNB 1d reprovaram (WF inconsistente).
    """
    import math as _math

    length = int(params.get("length", 55))
    mode = str(params.get("mode", "Hma"))

    def _wma(s: pd.Series, n: int) -> pd.Series:
        if n <= 1:
            return s.copy()
        weights = np.arange(1, n + 1, dtype=float)
        weights /= weights.sum()
        return s.rolling(n, min_periods=n).apply(
            lambda x: float(np.dot(x, weights)), raw=True
        )

    def _ema(s: pd.Series, n: int) -> pd.Series:
        return s.ewm(span=max(1, n), adjust=False).mean()

    def _hma(src: pd.Series, n: int) -> pd.Series:
        half = max(1, n // 2)
        sqrt_n = max(1, int(round(_math.sqrt(n))))
        return _wma(2 * _wma(src, half) - _wma(src, n), sqrt_n)

    def _ehma(src: pd.Series, n: int) -> pd.Series:
        half = max(1, n // 2)
        sqrt_n = max(1, int(round(_math.sqrt(n))))
        return _ema(2 * _ema(src, half) - _ema(src, n), sqrt_n)

    def _thma(src: pd.Series, n_in: int) -> pd.Series:
        # Pine usa length/2 como n para THMA
        nn = max(1, n_in // 2)
        third = max(1, nn // 3)
        half = max(1, nn // 2)
        return _wma(_wma(src, third) * 3 - _wma(src, half) - _wma(src, nn), nn)

    if mode == "Hma":
        hull = _hma(close, length)
    elif mode == "Ehma":
        hull = _ehma(close, length)
    elif mode == "Thma":
        hull = _thma(close, length)
    else:
        raise ValueError(f"hull_suite mode invalido: {mode}")

    cond_long = (hull > hull.shift(2)).fillna(False).astype(int)
    return cond_long.astype(np.int8).reset_index(drop=True)


def _signals_hybrid_breakout(df: pd.DataFrame, params: dict[str, Any]) -> pd.Series:
    """Hybrid breakout adaptado do TradingView (RugSurvivor) com 3 melhorias V3:
    - Remove componente RSI mean-revert (Pine original tinha tambem)
    - Stop inicial fixo: -atr_mult_stop * ATR abaixo do entry
    - Filtro de volume: so entra se volume > SMA(vol, vol_sma_len) * vol_mult
    Trail ativa apos +1 ATR de lucro; usa max(initial_stop, peak - atr_mult_trail*ATR).

    Aprovada por Critic v4 (walk-forward 3/4, cross-symbol BTC, perturb 100% pos,
    bootstrap P5+0.24) em ETHUSDT 4h. Sharpe 1.25, MDD 21.91%, PnL +83.71%.
    """
    high = df["high"].astype(float).reset_index(drop=True)
    low = df["low"].astype(float).reset_index(drop=True)
    close = df["close"].astype(float).reset_index(drop=True)
    volume = (df["volume"].astype(float).reset_index(drop=True)
              if "volume" in df.columns else pd.Series([1.0] * len(df)))

    adx_len = int(params.get("adx_len", 14))
    adx_smooth = int(params.get("adx_smooth", 14))
    adx_thr = float(params.get("adx_thr", 20.0))
    ema_len = int(params.get("ema_len", 200))
    breakout_len = int(params.get("breakout_len", 20))
    atr_len = int(params.get("atr_len", 14))
    atr_mult_trail = float(params.get("atr_mult_trail", 2.0))
    atr_mult_stop = float(params.get("atr_mult_stop", 1.5))
    vol_sma_len = int(params.get("vol_sma_len", 20))
    vol_mult = float(params.get("vol_mult", 1.3))

    # ADX (Wilder)
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
                   axis=1).max(axis=1)
    atr_for_adx = tr.ewm(alpha=1 / adx_len, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / adx_len, adjust=False).mean() / atr_for_adx
    minus_di = 100 * minus_dm.ewm(alpha=1 / adx_len, adjust=False).mean() / atr_for_adx
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_v = dx.ewm(alpha=1 / adx_smooth, adjust=False).mean().fillna(0)

    is_trending = (adx_v > adx_thr).fillna(False)
    ema_v = close.ewm(span=ema_len, adjust=False).mean()
    bullish = (close > ema_v).fillna(False)
    highest_break = close.shift(1).rolling(breakout_len, min_periods=breakout_len).max()
    atr_v = tr.ewm(alpha=1 / atr_len, adjust=False).mean()

    vol_sma = volume.rolling(vol_sma_len, min_periods=vol_sma_len).mean()
    vol_ok = (volume > vol_sma * vol_mult).fillna(False)

    long_break_entry = (is_trending & bullish & (close > highest_break).fillna(False) & vol_ok).values

    close_arr = close.values
    atr_arr = atr_v.values

    sig = np.zeros(len(df), dtype=np.int8)
    pos = 0
    entry_price = 0.0
    initial_stop = 0.0
    peak = 0.0
    trail_active = False
    for i in range(len(df)):
        if pos == 0:
            if long_break_entry[i]:
                pos = 1
                entry_price = close_arr[i]
                initial_stop = entry_price - atr_mult_stop * atr_arr[i]
                peak = close_arr[i]
                trail_active = False
        else:
            if close_arr[i] > peak:
                peak = close_arr[i]
            if close_arr[i] >= entry_price + atr_arr[i]:
                trail_active = True
            if not trail_active:
                if close_arr[i] < initial_stop:
                    pos = 0
            else:
                trail_stop = peak - atr_mult_trail * atr_arr[i]
                effective_stop = max(initial_stop, trail_stop)
                if close_arr[i] < effective_stop:
                    pos = 0
            if pos == 0:
                entry_price = 0.0
                initial_stop = 0.0
                peak = 0.0
                trail_active = False
        sig[i] = pos
    return pd.Series(sig, index=df.index)
