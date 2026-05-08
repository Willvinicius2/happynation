"""EMA 50/200 Trend Following — engine de swing classico.

Entrada: EMA50 > EMA200 (golden cross ja consumado), preco acima da EMA50,
ADX > limite (tendencia confirmada). So opera com vento a favor.
Saida: fechamento abaixo da EMA50, ADX cai, ou stop/take por ATR.
Timeframe alvo: 4h.
"""
from __future__ import annotations

import pandas as pd


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high_price"]
    low = df["low_price"]
    close = df["close_price"]
    plus_dm = (high.diff()).where(lambda x: x > 0, 0.0)
    minus_dm = (-low.diff()).where(lambda x: x > 0, 0.0)
    plus_dm = plus_dm.where(plus_dm > minus_dm, 0.0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0.0)

    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, float("nan")))
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, float("nan")))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0)


def EmaTrendSwing(
    self,
    stock_data: pd.DataFrame,
    verbose: bool = True,
    fast_ema: int = 50,
    slow_ema: int = 200,
    adx_min: float = 20.0,
    adx_exit: float = 15.0,
    take_profit_pct: float = 8.0,
    stop_loss_pct: float = 4.0,
    max_hold_bars: int = 90,
) -> bool | None:
    min_bars = slow_ema + 50
    if stock_data is None or len(stock_data) < min_bars:
        return self.actual_trade_position

    df = stock_data.copy().reset_index(drop=True)
    close = df["close_price"]

    df["ema_fast"] = close.ewm(span=fast_ema, adjust=False).mean()
    df["ema_slow"] = close.ewm(span=slow_ema, adjust=False).mean()
    df["adx"] = _adx(df, period=14)

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
        below_fast = price < float(last["ema_fast"])
        adx_dead = float(last["adx"]) < adx_exit
        time_exit = bars_since_buy >= max_hold_bars and pnl_pct < take_profit_pct / 2
        sell_signal = hit_target or hit_stop or below_fast or adx_dead or time_exit

        if verbose:
            print("-------")
            print("EMA Trend Swing")
            print(f" | Preco: {price:.4f}")
            print(f" | Entrada: {entry:.4f} | PnL: {pnl_pct:.2f}%")
            print(f" | EMA{fast_ema}: {float(last['ema_fast']):.4f}")
            print(f" | ADX: {float(last['adx']):.1f}")
            print(f" | Decisao: {'Vender' if sell_signal else 'Manter'}")
            print("-------")

        return False if sell_signal else True

    trend_ok = float(last["ema_fast"]) > float(last["ema_slow"])
    above_fast = price > float(last["ema_fast"])
    adx_ok = float(last["adx"]) >= adx_min

    buy_signal = trend_ok and above_fast and adx_ok

    if verbose:
        print("-------")
        print("EMA Trend Swing")
        print(f" | Preco: {price:.4f}")
        print(f" | EMA{fast_ema}: {float(last['ema_fast']):.4f} | EMA{slow_ema}: {float(last['ema_slow']):.4f}")
        print(f" | ADX: {float(last['adx']):.1f} (min {adx_min})")
        print(f" | Tendencia OK: {trend_ok} | Acima EMA fast: {above_fast} | ADX OK: {adx_ok}")
        print(f" | Decisao: {'Comprar' if buy_signal else 'Aguardar'}")
        print("-------")

    return True if buy_signal else None


EmaTrendSwing.__strategy_name__ = "EmaTrendSwing"  # type: ignore[attr-defined]
