"""Bollinger Band Mean Reversion — engine de swing.

So opera mean-reversion DENTRO de tendencia de alta (preco > EMA200), pra
evitar pegar faca caindo. Entrada: toque na banda inferior + RSI baixo.
Saida: retorno a banda media, stop ou take.
Timeframe alvo: 4h.
"""
from __future__ import annotations

import pandas as pd


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return (100 - (100 / (1 + rs))).fillna(50)


def BollingerMeanRevSwing(
    self,
    stock_data: pd.DataFrame,
    verbose: bool = True,
    bb_period: int = 20,
    bb_std: float = 2.0,
    rsi_max_entry: float = 35.0,
    take_profit_pct: float = 4.0,
    stop_loss_pct: float = 3.0,
    touch_lookback: int = 3,
    max_hold_bars: int = 40,
) -> bool | None:
    min_bars = max(bb_period, 200) + 30
    if stock_data is None or len(stock_data) < min_bars:
        return self.actual_trade_position

    df = stock_data.copy().reset_index(drop=True)
    close = df["close_price"]

    df["bb_mid"] = close.rolling(bb_period, min_periods=bb_period).mean()
    bb_std_series = close.rolling(bb_period, min_periods=bb_period).std()
    df["bb_lower"] = df["bb_mid"] - bb_std * bb_std_series
    df["bb_upper"] = df["bb_mid"] + bb_std * bb_std_series
    df["rsi"] = _rsi(close, period=14)
    df["ema200"] = close.ewm(span=200, adjust=False).mean()

    last = df.iloc[-1]
    price = float(last["close_price"])

    if self.actual_trade_position and self.last_buy_price:
        entry = float(self.last_buy_price)
        pnl_pct = (price - entry) / entry * 100
        bars_since_buy = 0
        if getattr(self, "last_buy_time", None) is not None and "open_time" in df.columns:
            bars_since_buy = int((df["open_time"] >= self.last_buy_time).sum())

        hit_target = pnl_pct >= take_profit_pct
        hit_stop = pnl_pct <= -stop_loss_pct
        reverted_to_mid = price >= float(last["bb_mid"])
        time_exit = bars_since_buy >= max_hold_bars
        sell_signal = hit_target or hit_stop or reverted_to_mid or time_exit

        if verbose:
            print("-------")
            print("Bollinger MeanRev Swing")
            print(f" | Preco: {price:.4f}")
            print(f" | Entrada: {entry:.4f} | PnL: {pnl_pct:.2f}%")
            print(f" | BB mid: {float(last['bb_mid']):.4f}")
            print(f" | Decisao: {'Vender' if sell_signal else 'Manter'}")
            print("-------")

        return False if sell_signal else True

    recent = df.iloc[-touch_lookback:]
    touched_lower = bool((recent["low_price"] <= recent["bb_lower"]).any())
    rsi_low = float(last["rsi"]) <= rsi_max_entry
    uptrend = price > float(last["ema200"])
    above_lower = price > float(last["bb_lower"])

    buy_signal = touched_lower and rsi_low and uptrend and above_lower

    if verbose:
        print("-------")
        print("Bollinger MeanRev Swing")
        print(f" | Preco: {price:.4f}")
        print(f" | BB lower: {float(last['bb_lower']):.4f} | mid: {float(last['bb_mid']):.4f}")
        print(f" | RSI: {float(last['rsi']):.1f} (max entry {rsi_max_entry})")
        print(f" | Uptrend (>EMA200): {uptrend}")
        print(f" | Tocou banda inf: {touched_lower}")
        print(f" | Decisao: {'Comprar' if buy_signal else 'Aguardar'}")
        print("-------")

    return True if buy_signal else None


BollingerMeanRevSwing.__strategy_name__ = "BollingerMeanRevSwing"  # type: ignore[attr-defined]
