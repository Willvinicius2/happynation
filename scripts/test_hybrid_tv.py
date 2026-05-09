"""Testa a estrategia 'Hybrid: RSI + Breakout + Dashboard' (TradingView)
em 4 ativos x 4 timeframes usando o motor de backtest do Aether.

Implementacao SOMENTE LONG: spot Binance nao permite short. As entradas
SHORT do Pine original sao ignoradas.

Referencia Pine Script:
- ADX(14, smooth 14, thr 20) -> regime trending vs ranging
- EMA(200) -> bias bullish/bearish
- RSI(14): rsiBuy=40, rsiExit=50 (ranging+bullish)
- Breakout: close > highest(close[1], 20) (trending+bullish)
- Trail saida breakout: ATR(14) * 2

Roda standalone -- nao toca no DB do Aether nem no catalogo do bot.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.research.aether.compute import _run_local_backtest, load_dataset_fast


# =========================================================================
# Indicadores (pandas/numpy puro -- sem dependencia externa).
# =========================================================================
def rsi_wilder(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def ema(close: pd.Series, length: int = 200) -> pd.Series:
    return close.ewm(span=length, adjust=False).mean()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr_wilder(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def adx_wilder(high: pd.Series, low: pd.Series, close: pd.Series,
               length: int = 14, smooth: int = 14) -> pd.Series:
    """ADX classico Wilder. Retorna so a serie do ADX."""
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = true_range(high, low, close)
    atr_v = tr.ewm(alpha=1 / length, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr_v
    minus_di = 100 * minus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr_v
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_v = dx.ewm(alpha=1 / smooth, adjust=False).mean()
    return adx_v.fillna(0)


# =========================================================================
# Logica da estrategia hibrida (so LONG).
# =========================================================================
def hybrid_signals(
    df: pd.DataFrame,
    *,
    adx_len: int = 14,
    adx_smooth: int = 14,
    adx_thr: float = 20.0,
    ema_len: int = 200,
    rsi_len: int = 14,
    rsi_buy: int = 40,
    rsi_sell: int = 60,
    rsi_exit: int = 50,
    breakout_len: int = 20,
    atr_len: int = 14,
    atr_mult: float = 2.0,
    enable_short: bool = False,
) -> pd.Series:
    high = df["high"].astype(float).reset_index(drop=True)
    low = df["low"].astype(float).reset_index(drop=True)
    close = df["close"].astype(float).reset_index(drop=True)

    adx_v = adx_wilder(high, low, close, adx_len, adx_smooth)
    is_trending = adx_v > adx_thr
    is_ranging = ~is_trending

    ema_v = ema(close, ema_len)
    bullish = close > ema_v
    bearish = close < ema_v

    rsi_v = rsi_wilder(close, rsi_len)

    # close[1] (Pine) = close.shift(1) aqui. highest dos N candles anteriores.
    highest_break = close.shift(1).rolling(breakout_len, min_periods=breakout_len).max()
    lowest_break = close.shift(1).rolling(breakout_len, min_periods=breakout_len).min()
    atr_v = atr_wilder(high, low, close, atr_len)

    rsi_long_entry = (is_ranging & (rsi_v < rsi_buy) & bullish).fillna(False).values
    long_break_entry = (is_trending & bullish & (close > highest_break)).fillna(False).values
    rsi_short_entry = (is_ranging & (rsi_v > rsi_sell) & bearish).fillna(False).values
    short_break_entry = (is_trending & bearish & (close < lowest_break)).fillna(False).values

    rsi_arr = rsi_v.values
    close_arr = close.values
    atr_arr = atr_v.values

    sig = np.zeros(len(df), dtype=np.int8)
    pos = 0
    entry_type = None  # 'rsi' | 'break'
    peak = 0.0  # max favoravel (long: max close, short: min close)
    for i in range(len(df)):
        if pos == 0:
            if rsi_long_entry[i]:
                pos = 1; entry_type = "rsi"
            elif long_break_entry[i]:
                pos = 1; entry_type = "break"; peak = close_arr[i]
            elif enable_short and rsi_short_entry[i]:
                pos = -1; entry_type = "rsi"
            elif enable_short and short_break_entry[i]:
                pos = -1; entry_type = "break"; peak = close_arr[i]
        elif pos == 1:
            if entry_type == "rsi":
                if rsi_arr[i] > rsi_exit:
                    pos = 0; entry_type = None
            elif entry_type == "break":
                if close_arr[i] > peak:
                    peak = close_arr[i]
                trail_stop = peak - atr_mult * atr_arr[i]
                if close_arr[i] < trail_stop:
                    pos = 0; entry_type = None; peak = 0.0
        else:  # pos == -1 (short)
            if entry_type == "rsi":
                if rsi_arr[i] < rsi_exit:
                    pos = 0; entry_type = None
            elif entry_type == "break":
                if close_arr[i] < peak:
                    peak = close_arr[i]
                trail_stop = peak + atr_mult * atr_arr[i]
                if close_arr[i] > trail_stop:
                    pos = 0; entry_type = None; peak = 0.0
        sig[i] = pos
    return pd.Series(sig, index=df.index)


# =========================================================================
# V3 -- 3 melhorias derivadas dos achados do V1:
#   A) Remove componente RSI mean-revert (causa -47%/-67% em TFs curtos)
#   B) Stop inicial fixo: -1.5 ATR abaixo do entry, protege antes do trail ativar
#   C) Filtro de volume: so entra se volume > SMA(volume,20) * 1.3
#   Trail so ativa apos +1 ATR de lucro; depois usa max(initial_stop, peak - 2*ATR).
# =========================================================================
def hybrid_v3_signals(
    df: pd.DataFrame,
    *,
    adx_len: int = 14,
    adx_smooth: int = 14,
    adx_thr: float = 20.0,
    ema_len: int = 200,
    breakout_len: int = 20,
    atr_len: int = 14,
    atr_mult_trail: float = 2.0,
    atr_mult_stop: float = 1.5,
    vol_sma_len: int = 20,
    vol_mult: float = 1.3,
) -> pd.Series:
    high = df["high"].astype(float).reset_index(drop=True)
    low = df["low"].astype(float).reset_index(drop=True)
    close = df["close"].astype(float).reset_index(drop=True)
    volume = df["volume"].astype(float).reset_index(drop=True) if "volume" in df.columns else pd.Series([1.0] * len(df))

    adx_v = adx_wilder(high, low, close, adx_len, adx_smooth)
    is_trending = (adx_v > adx_thr).fillna(False)

    ema_v = ema(close, ema_len)
    bullish = (close > ema_v).fillna(False)

    highest_break = close.shift(1).rolling(breakout_len, min_periods=breakout_len).max()
    atr_v = atr_wilder(high, low, close, atr_len)

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
        else:  # pos == 1
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


# =========================================================================
# V4 -- 3 melhorias adicionais sobre V3, focadas em MDD:
#   1) Stop inicial mais apertado: -1.0 ATR (V3 usa -1.5)
#   2) Time stop: sai apos 50 candles em posicao (50d em 1d, ~8d em 4h)
#   3) Exit por ADX colapso: se ADX caiu pra <15, regime morreu, sai imediato
# =========================================================================
def hybrid_v4_signals(
    df: pd.DataFrame,
    *,
    adx_len: int = 14,
    adx_smooth: int = 14,
    adx_thr: float = 20.0,
    adx_collapse: float = 15.0,
    ema_len: int = 200,
    breakout_len: int = 20,
    atr_len: int = 14,
    atr_mult_trail: float = 2.0,
    atr_mult_stop: float = 1.0,
    vol_sma_len: int = 20,
    vol_mult: float = 1.3,
    time_stop_bars: int = 50,
) -> pd.Series:
    high = df["high"].astype(float).reset_index(drop=True)
    low = df["low"].astype(float).reset_index(drop=True)
    close = df["close"].astype(float).reset_index(drop=True)
    volume = df["volume"].astype(float).reset_index(drop=True) if "volume" in df.columns else pd.Series([1.0] * len(df))

    adx_v = adx_wilder(high, low, close, adx_len, adx_smooth)
    is_trending = (adx_v > adx_thr).fillna(False)
    is_collapsed = (adx_v < adx_collapse).fillna(False)

    ema_v = ema(close, ema_len)
    bullish = (close > ema_v).fillna(False)

    highest_break = close.shift(1).rolling(breakout_len, min_periods=breakout_len).max()
    atr_v = atr_wilder(high, low, close, atr_len)

    vol_sma = volume.rolling(vol_sma_len, min_periods=vol_sma_len).mean()
    vol_ok = (volume > vol_sma * vol_mult).fillna(False)

    long_break_entry = (is_trending & bullish & (close > highest_break).fillna(False) & vol_ok).values
    adx_collapse_arr = is_collapsed.values

    close_arr = close.values
    atr_arr = atr_v.values

    sig = np.zeros(len(df), dtype=np.int8)
    pos = 0
    entry_price = 0.0
    entry_bar = -1
    initial_stop = 0.0
    peak = 0.0
    trail_active = False

    for i in range(len(df)):
        if pos == 0:
            if long_break_entry[i]:
                pos = 1
                entry_price = close_arr[i]
                entry_bar = i
                initial_stop = entry_price - atr_mult_stop * atr_arr[i]
                peak = close_arr[i]
                trail_active = False
        else:
            if close_arr[i] > peak:
                peak = close_arr[i]
            if close_arr[i] >= entry_price + atr_arr[i]:
                trail_active = True

            should_exit = False
            # 1) ADX colapso: regime morreu
            if adx_collapse_arr[i]:
                should_exit = True
            # 2) Time stop
            elif (i - entry_bar) >= time_stop_bars:
                should_exit = True
            # 3) Stop inicial / trail
            elif not trail_active:
                if close_arr[i] < initial_stop:
                    should_exit = True
            else:
                trail_stop = peak - atr_mult_trail * atr_arr[i]
                effective_stop = max(initial_stop, trail_stop)
                if close_arr[i] < effective_stop:
                    should_exit = True

            if should_exit:
                pos = 0
                entry_price = 0.0
                entry_bar = -1
                initial_stop = 0.0
                peak = 0.0
                trail_active = False
        sig[i] = pos
    return pd.Series(sig, index=df.index)


# =========================================================================
# Matriz de teste.
# =========================================================================
ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
TIMEFRAMES = ["15m", "1h", "4h", "1d"]


def run_matrix(*, version: str = "v1", enable_short: bool = False) -> list[dict]:
    rows: list[dict] = []
    print(f"Rodando matriz (version={version}, enable_short={enable_short})...\n")
    for asset in ASSETS:
        for tf in TIMEFRAMES:
            dataset_id = f"binance:{asset}:{tf}"
            t0 = time.time()
            try:
                df = load_dataset_fast(dataset_id)
                bars = len(df)
                if bars < 250:
                    rows.append({"asset": asset, "tf": tf, "ok": False,
                                 "error": f"poucos candles ({bars})"})
                    continue
                if version == "v4":
                    sig = hybrid_v4_signals(df)
                elif version == "v3":
                    sig = hybrid_v3_signals(df)
                else:
                    sig = hybrid_signals(df, enable_short=enable_short)
                r = _run_local_backtest(
                    df, sig,
                    capital=1000.0, fee_bps=10.0, slippage_bps=2.0,
                    timeframe=tf,
                )
                r["asset"] = asset
                r["tf"] = tf
                r["elapsed_s"] = round(time.time() - t0, 2)
                rows.append(r)
            except Exception as exc:
                rows.append({
                    "asset": asset, "tf": tf, "ok": False,
                    "error": f"{exc.__class__.__name__}: {str(exc)[:100]}",
                    "elapsed_s": round(time.time() - t0, 2),
                })
    return rows


def report(rows: list[dict]) -> None:
    print("\n" + "=" * 100)
    print(" Hybrid TV (RSI mean-revert + Breakout) -- backtest 4 ativos x 4 TFs (LONG only)")
    print("=" * 100)
    header = f"{'asset':<10} {'tf':<5} {'bars':>6} {'pnl%':>9} {'trades':>7} {'mdd%':>7} {'hit%':>6} {'sharpe':>7} {'time':>6}"
    print(header)
    print("-" * len(header))
    ok_rows = []
    for r in rows:
        if r.get("ok"):
            hit = (r["hit_rate"] or 0) * 100 if r.get("hit_rate") is not None else 0
            print(f"{r['asset']:<10} {r['tf']:<5} {r['bars']:>6} "
                  f"{r['pnl_pct']:>+8.2f}% {r['trade_count']:>7} "
                  f"{r['mdd_pct']:>6.2f}% {hit:>5.1f}% {r['sharpe']:>+7.2f} "
                  f"{r['elapsed_s']:>5.1f}s")
            ok_rows.append(r)
        else:
            print(f"{r['asset']:<10} {r['tf']:<5}  FAILED  {r.get('error', '?')[:60]}")
    if ok_rows:
        avg_pnl = np.mean([r["pnl_pct"] for r in ok_rows])
        positives = sum(1 for r in ok_rows if r["pnl_pct"] > 0)
        avg_mdd = np.mean([r["mdd_pct"] for r in ok_rows])
        avg_trades = np.mean([r["trade_count"] for r in ok_rows])
        print("-" * len(header))
        print(f"resumo: {len(ok_rows)} backtests OK | "
              f"positivos {positives}/{len(ok_rows)} | "
              f"pnl medio {avg_pnl:+.2f}% | mdd medio {avg_mdd:.2f}% | "
              f"trades medio {avg_trades:.0f}")


if __name__ == "__main__":
    import sys as _sys
    mode = _sys.argv[1] if len(_sys.argv) > 1 else "v3"
    if mode == "all":
        print("\n" + "#" * 100)
        print("# V1 -- LONG ONLY (Pine original adaptado pro spot)")
        print("#" * 100)
        rows_v1 = run_matrix(version="v1", enable_short=False)
        report(rows_v1)
    print("\n" + "#" * 100)
    print("# V3 -- BREAKOUT-ONLY + STOP INICIAL + FILTRO DE VOLUME")
    print("#" * 100)
    rows_v3 = run_matrix(version="v3")
    report(rows_v3)
    if mode == "all":
        # comparativo V1 vs V3
        print("\n" + "=" * 100)
        print(" Comparativo V1 (Pine) vs V3 (melhorado) -- delta PnL%")
        print("=" * 100)
        print(f"{'asset':<10} {'tf':<5} {'V1 PnL':>10} {'V3 PnL':>10} {'delta':>9} {'V1 trades':>10} {'V3 trades':>10} {'V1 MDD':>8} {'V3 MDD':>8}")
        print("-" * 88)
        by_v3 = {(r['asset'], r['tf']): r for r in rows_v3 if r.get('ok')}
        for r in rows_v1:
            if not r.get('ok'):
                continue
            v3 = by_v3.get((r['asset'], r['tf']))
            if not v3:
                continue
            d_pnl = v3['pnl_pct'] - r['pnl_pct']
            mark = "[+]" if d_pnl > 0 else "[-]" if d_pnl < -1 else " . "
            print(f"{r['asset']:<10} {r['tf']:<5} "
                  f"{r['pnl_pct']:>+9.2f}% {v3['pnl_pct']:>+9.2f}% {d_pnl:>+8.2f}% "
                  f"{r['trade_count']:>10} {v3['trade_count']:>10} "
                  f"{r['mdd_pct']:>6.2f}% {v3['mdd_pct']:>6.2f}% {mark}")
