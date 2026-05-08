"""Donchian Breakout (Turtle-style) — engine de swing.

Entrada: rompimento da maxima dos ultimos N candles.
Saida: rompimento da minima dos ultimos M candles, stop ou take.
Timeframe alvo: 4h ou 1d. Em 4h, ~5 sinais/mes por par liquido.

Inerte por padrao: declara bot_timeframe='4h', entao o MultiStrategy
ignora se o bot estiver rodando em 1h.
"""
from __future__ import annotations

import pandas as pd


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close_price"].shift(1)
    tr = pd.concat(
        [
            df["high_price"] - df["low_price"],
            (df["high_price"] - prev_close).abs(),
            (df["low_price"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def DonchianBreakoutSwing(
    self,
    stock_data: pd.DataFrame,
    verbose: bool = True,
    entry_window: int = 20,
    exit_window: int = 10,
    take_profit_pct: float = 6.0,
    stop_loss_pct: float = 3.0,
    min_atr_pct: float = 0.5,
    max_hold_bars: int = 60,
) -> bool | None:
    min_bars = max(entry_window, exit_window) + 50
    if stock_data is None or len(stock_data) < min_bars:
        return self.actual_trade_position

    df = stock_data.copy().reset_index(drop=True)
    close = df["close_price"]

    df["donchian_high"] = df["high_price"].rolling(entry_window, min_periods=entry_window).max().shift(1)
    df["donchian_low"] = df["low_price"].rolling(exit_window, min_periods=exit_window).min().shift(1)
    df["atr"] = _atr(df, period=14)
    df["atr_pct"] = df["atr"] / close * 100
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
        breakdown = price < float(last["donchian_low"])
        time_exit = bars_since_buy >= max_hold_bars
        sell_signal = hit_target or hit_stop or breakdown or time_exit

        if verbose:
            print("-------")
            print("Donchian Breakout Swing")
            print(f" | Preco: {price:.4f}")
            print(f" | Entrada: {entry:.4f} | PnL: {pnl_pct:.2f}%")
            print(f" | Saida Donchian: {float(last['donchian_low']):.4f}")
            print(f" | Decisao: {'Vender' if sell_signal else 'Manter'}")
            print("-------")

        return False if sell_signal else True

    breakout = price > float(last["donchian_high"])
    trend_ok = price > float(last["ema200"])
    vol_ok = float(last["atr_pct"]) >= min_atr_pct

    buy_signal = breakout and trend_ok and vol_ok

    if verbose:
        print("-------")
        print("Donchian Breakout Swing")
        print(f" | Preco: {price:.4f}")
        print(f" | Donchian High({entry_window}): {float(last['donchian_high']):.4f}")
        print(f" | EMA200: {float(last['ema200']):.4f}")
        print(f" | ATR%: {float(last['atr_pct']):.2f}")
        print(f" | Decisao: {'Comprar' if buy_signal else 'Aguardar'}")
        print("-------")

    return True if buy_signal else None


DonchianBreakoutSwing.__strategy_name__ = "DonchianBreakoutSwing"  # type: ignore[attr-defined]
