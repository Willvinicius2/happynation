"""Sweep multi-estrategia contra todos os ativos.

Para cada (estrategia, ativo) faz grid search, escolhe o melhor combo que
passa nos criterios do Juiz (PF >=1.15, trades >=20, DD <=25%) e gera
matriz de decisao.

Estrategias testadas (numpy puro, vetorizado, baseadas em padroes do bot):
  - ma_crossover         (medias rapidas/lentas)
  - ma_antecipation      (medias com filtro RSI)
  - rsi_meanrev          (RSI mean-reversion classico)
  - ma_rsi_volume        (combo MA + RSI + volume)
  - vortex               (Vortex Indicator crossover)
  - hwr_scalp            (high win rate scalp - filtros conservadores)
  - atr_breakout         (breakout com filtro ATR)

Saida: src/logs/agents/sweep_strategies.json
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from binance.client import Client  # noqa: E402

ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"]

MIN_TRADES = 20
MIN_PF = 1.15
MAX_DD_PCT = 25.0


# ---------------------------------------------------------------------------
# Indicadores numpy (rapidos, sem dependencia em ta)
# ---------------------------------------------------------------------------

def ema(values, span):
    alpha = 2 / (span + 1)
    out = np.empty_like(values, dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(values, period=14):
    delta = np.diff(values, prepend=values[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.zeros_like(gain)
    avg_loss = np.zeros_like(loss)
    avg_gain[period] = gain[1: period + 1].mean()
    avg_loss[period] = loss[1: period + 1].mean()
    for i in range(period + 1, len(values)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i]) / period
    rs = np.where(avg_loss == 0, 100.0, avg_gain / np.where(avg_loss == 0, 1, avg_loss))
    out = 100 - (100 / (1 + rs))
    out[:period] = 50.0
    return out


def atr_pct(high, low, close, period=14):
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    out = np.zeros_like(close)
    out[period - 1] = tr[:period].mean()
    for i in range(period, len(close)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out / close * 100


def vortex(high, low, close, period=14):
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    prev_high = np.roll(high, 1)
    prev_high[0] = high[0]
    prev_low = np.roll(low, 1)
    prev_low[0] = low[0]
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    vp = np.abs(high - prev_low)
    vn = np.abs(low - prev_high)
    sum_tr = pd.Series(tr).rolling(period, min_periods=period).sum().fillna(0).to_numpy()
    sum_vp = pd.Series(vp).rolling(period, min_periods=period).sum().fillna(0).to_numpy()
    sum_vn = pd.Series(vn).rolling(period, min_periods=period).sum().fillna(0).to_numpy()
    safe_tr = np.where(sum_tr == 0, 1, sum_tr)
    return sum_vp / safe_tr, sum_vn / safe_tr


# ---------------------------------------------------------------------------
# Engine de backtest generica
# ---------------------------------------------------------------------------

def run_engine(close, high, low, volume, signal_long, exit_signal, *,
               take_profit_pct, stop_loss_pct, max_hold_bars, warmup=200):
    balance = 1000.0
    position = False
    entry_price = 0.0
    entry_index = 0
    trades = []
    equity = []
    n = len(close)

    for i in range(warmup, n):
        price = close[i]
        if position:
            stop = entry_price * (1 - stop_loss_pct / 100)
            tp = entry_price * (1 + take_profit_pct / 100)
            if low[i] <= stop:
                pnl_pct = -stop_loss_pct
            elif high[i] >= tp:
                pnl_pct = take_profit_pct
            else:
                pnl_pct = (price - entry_price) / entry_price * 100
                hit_max_hold = (i - entry_index) >= max_hold_bars
                if hit_max_hold or exit_signal[i]:
                    pass
                else:
                    equity.append(balance * (price / entry_price))
                    continue
            balance *= 1 + pnl_pct / 100
            trades.append(pnl_pct)
            position = False
        elif signal_long[i]:
            position = True
            entry_price = price
            entry_index = i
        equity.append(balance if not position else balance * (price / entry_price))

    if position:
        pnl_pct = (close[-1] - entry_price) / entry_price * 100
        balance *= 1 + pnl_pct / 100
        trades.append(pnl_pct)

    if not trades:
        return None
    trades_arr = np.array(trades)
    wins = int((trades_arr > 0).sum())
    losses = len(trades_arr) - wins
    avg_win = trades_arr[trades_arr > 0].mean() if wins else 0.0
    avg_loss = abs(trades_arr[trades_arr <= 0].mean()) if losses else 0.0
    eq = np.array(equity or [1000.0])
    cummax = np.maximum.accumulate(eq)
    dd = ((eq - cummax) / cummax * 100).min()

    return {
        "total_trades": len(trades_arr),
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(wins / len(trades_arr) * 100, 2),
        "profit_pct": round((balance - 1000.0) / 10.0, 2),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(-avg_loss, 3),
        "max_drawdown_pct": round(dd, 2),
    }


def profit_factor(result):
    if not result:
        return 0.0
    avg_win = float(result.get("avg_win_pct", 0.0) or 0.0)
    avg_loss = abs(float(result.get("avg_loss_pct", 0.0) or 0.0))
    wins = int(result.get("wins", 0) or 0)
    losses = int(result.get("losses", 0) or 0)
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    gross_profit = avg_win * wins
    gross_loss = avg_loss * losses
    if gross_loss <= 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def passes_juiz(result):
    if not result:
        return False
    return (
        result["total_trades"] >= MIN_TRADES
        and profit_factor(result) >= MIN_PF
        and abs(result["max_drawdown_pct"]) <= MAX_DD_PCT
    )


def score(result):
    if not result:
        return -1e9
    pf = min(profit_factor(result), 5.0)
    return pf * 25 + result["winrate_pct"] * 0.5 + min(result["total_trades"], 80) * 0.2 - abs(result["max_drawdown_pct"])


# ---------------------------------------------------------------------------
# Estrategias
# ---------------------------------------------------------------------------

def strategy_ma_crossover(df, fast, slow, take_profit_pct, stop_loss_pct, max_hold_bars):
    close = df["close_price"].to_numpy()
    high = df["high_price"].to_numpy()
    low = df["low_price"].to_numpy()
    vol = df["volume"].to_numpy()
    e_fast = ema(close, fast)
    e_slow = ema(close, slow)
    long_signal = (e_fast > e_slow) & (np.roll(e_fast, 1) <= np.roll(e_slow, 1))
    exit_signal = (e_fast < e_slow) & (np.roll(e_fast, 1) >= np.roll(e_slow, 1))
    return run_engine(close, high, low, vol, long_signal, exit_signal,
                      take_profit_pct=take_profit_pct, stop_loss_pct=stop_loss_pct,
                      max_hold_bars=max_hold_bars)


def strategy_ma_antecipation(df, fast, slow, rsi_min, take_profit_pct, stop_loss_pct, max_hold_bars):
    close = df["close_price"].to_numpy()
    high = df["high_price"].to_numpy()
    low = df["low_price"].to_numpy()
    vol = df["volume"].to_numpy()
    e_fast = ema(close, fast)
    e_slow = ema(close, slow)
    r = rsi(close, 14)
    long_signal = (e_fast > e_slow) & (r > rsi_min) & (close > e_fast)
    exit_signal = (e_fast < e_slow) | (r < rsi_min - 5)
    return run_engine(close, high, low, vol, long_signal, exit_signal,
                      take_profit_pct=take_profit_pct, stop_loss_pct=stop_loss_pct,
                      max_hold_bars=max_hold_bars)


def strategy_rsi_meanrev(df, period, oversold, overbought, take_profit_pct, stop_loss_pct, max_hold_bars):
    close = df["close_price"].to_numpy()
    high = df["high_price"].to_numpy()
    low = df["low_price"].to_numpy()
    vol = df["volume"].to_numpy()
    r = rsi(close, period)
    long_signal = (r < oversold) & (np.roll(r, 1) >= oversold)
    exit_signal = r > overbought
    return run_engine(close, high, low, vol, long_signal, exit_signal,
                      take_profit_pct=take_profit_pct, stop_loss_pct=stop_loss_pct,
                      max_hold_bars=max_hold_bars)


def strategy_ma_rsi_volume(df, fast, slow, rsi_min, rsi_max, take_profit_pct, stop_loss_pct, max_hold_bars):
    close = df["close_price"].to_numpy()
    high = df["high_price"].to_numpy()
    low = df["low_price"].to_numpy()
    vol = df["volume"].to_numpy()
    e_fast = ema(close, fast)
    e_slow = ema(close, slow)
    r = rsi(close, 14)
    vol_ma = pd.Series(vol).rolling(20, min_periods=20).mean().fillna(0).to_numpy()
    long_signal = (e_fast > e_slow) & (rsi_min <= r) & (r <= rsi_max) & (vol > vol_ma * 1.1)
    exit_signal = (e_fast < e_slow) | (r > rsi_max + 3)
    return run_engine(close, high, low, vol, long_signal, exit_signal,
                      take_profit_pct=take_profit_pct, stop_loss_pct=stop_loss_pct,
                      max_hold_bars=max_hold_bars)


def strategy_vortex(df, period, take_profit_pct, stop_loss_pct, max_hold_bars):
    close = df["close_price"].to_numpy()
    high = df["high_price"].to_numpy()
    low = df["low_price"].to_numpy()
    vol = df["volume"].to_numpy()
    vp, vn = vortex(high, low, close, period)
    long_signal = (vp > vn) & (np.roll(vp, 1) <= np.roll(vn, 1))
    exit_signal = (vp < vn) & (np.roll(vp, 1) >= np.roll(vn, 1))
    return run_engine(close, high, low, vol, long_signal, exit_signal,
                      take_profit_pct=take_profit_pct, stop_loss_pct=stop_loss_pct,
                      max_hold_bars=max_hold_bars)


def strategy_hwr_scalp(df, ema_period, rsi_max, take_profit_pct, stop_loss_pct, max_hold_bars):
    close = df["close_price"].to_numpy()
    high = df["high_price"].to_numpy()
    low = df["low_price"].to_numpy()
    vol = df["volume"].to_numpy()
    e = ema(close, ema_period)
    r = rsi(close, 14)
    a = atr_pct(high, low, close, 14)
    long_signal = (close > e) & (r < rsi_max) & (a > 0.3) & (a < 4.0) & (np.roll(close, 1) <= np.roll(e, 1))
    exit_signal = close < e
    return run_engine(close, high, low, vol, long_signal, exit_signal,
                      take_profit_pct=take_profit_pct, stop_loss_pct=stop_loss_pct,
                      max_hold_bars=max_hold_bars)


def strategy_atr_breakout(df, lookback, atr_min, atr_max, take_profit_pct, stop_loss_pct, max_hold_bars):
    close = df["close_price"].to_numpy()
    high = df["high_price"].to_numpy()
    low = df["low_price"].to_numpy()
    vol = df["volume"].to_numpy()
    rolling_high = pd.Series(high).rolling(lookback, min_periods=lookback).max().fillna(0).to_numpy()
    a = atr_pct(high, low, close, 14)
    long_signal = (close > np.roll(rolling_high, 1)) & (a >= atr_min) & (a <= atr_max)
    exit_signal = close < pd.Series(low).rolling(5, min_periods=5).min().fillna(0).to_numpy()
    return run_engine(close, high, low, vol, long_signal, exit_signal,
                      take_profit_pct=take_profit_pct, stop_loss_pct=stop_loss_pct,
                      max_hold_bars=max_hold_bars)


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------

GRIDS = {
    "ma_crossover": {
        "fast": [5, 9, 12],
        "slow": [21, 34, 50],
        "take_profit_pct": [0.8, 1.2, 1.8],
        "stop_loss_pct": [1.5, 2.0, 2.5],
        "max_hold_bars": [18, 24, 36],
    },
    "ma_antecipation": {
        "fast": [9, 12],
        "slow": [21, 50],
        "rsi_min": [40.0, 45.0],
        "take_profit_pct": [0.8, 1.2, 1.8],
        "stop_loss_pct": [1.5, 2.0],
        "max_hold_bars": [18, 24, 36],
    },
    "rsi_meanrev": {
        "period": [14, 21],
        "oversold": [25.0, 30.0, 35.0],
        "overbought": [60.0, 65.0, 70.0],
        "take_profit_pct": [0.8, 1.2, 2.0],
        "stop_loss_pct": [1.5, 2.0, 2.5],
        "max_hold_bars": [12, 18, 24],
    },
    "ma_rsi_volume": {
        "fast": [9],
        "slow": [21, 50],
        "rsi_min": [40.0, 45.0],
        "rsi_max": [60.0, 65.0],
        "take_profit_pct": [0.8, 1.2],
        "stop_loss_pct": [1.5, 2.0],
        "max_hold_bars": [18, 24],
    },
    "vortex": {
        "period": [14, 21],
        "take_profit_pct": [0.8, 1.2, 1.8],
        "stop_loss_pct": [1.5, 2.0, 2.5],
        "max_hold_bars": [18, 24, 36],
    },
    "hwr_scalp": {
        "ema_period": [21, 50],
        "rsi_max": [50.0, 55.0, 60.0],
        "take_profit_pct": [0.6, 0.9],
        "stop_loss_pct": [1.0, 1.5],
        "max_hold_bars": [12, 18],
    },
    "atr_breakout": {
        "lookback": [10, 20, 30],
        "atr_min": [0.5, 0.8],
        "atr_max": [4.0, 6.0],
        "take_profit_pct": [1.0, 1.8, 2.5],
        "stop_loss_pct": [1.5, 2.5],
        "max_hold_bars": [18, 36],
    },
}

STRATEGIES = {
    "ma_crossover": strategy_ma_crossover,
    "ma_antecipation": strategy_ma_antecipation,
    "rsi_meanrev": strategy_rsi_meanrev,
    "ma_rsi_volume": strategy_ma_rsi_volume,
    "vortex": strategy_vortex,
    "hwr_scalp": strategy_hwr_scalp,
    "atr_breakout": strategy_atr_breakout,
}


def fetch_paginated(client, symbol, total=3000):
    cols = ["open_time", "open_price", "high_price", "low_price", "close_price", "volume",
            "close_time", "qav", "trades", "tbbav", "tbqav", "_"]
    chunks = []
    end_time = None
    remaining = total
    while remaining > 0:
        limit = min(1000, remaining)
        params = {"symbol": symbol, "interval": Client.KLINE_INTERVAL_1HOUR, "limit": limit}
        if end_time is not None:
            params["endTime"] = end_time
        raw = client.get_klines(**params)
        if not raw:
            break
        df = pd.DataFrame(raw, columns=cols)
        chunks.append(df)
        end_time = int(df["open_time"].iloc[0]) - 1
        remaining -= len(df)
        if len(df) < limit:
            break
    if not chunks:
        return pd.DataFrame(columns=cols)
    df_all = pd.concat(chunks, ignore_index=True).drop_duplicates(subset=["open_time"]).sort_values("open_time")
    for col in ["open_price", "high_price", "low_price", "close_price", "volume"]:
        df_all[col] = pd.to_numeric(df_all[col], errors="coerce")
    df_all["open_time"] = pd.to_datetime(df_all["open_time"], unit="ms", utc=True)
    return df_all.reset_index(drop=True)


def sweep_strategy(df, name, grid):
    keys = list(grid.keys())
    fn = STRATEGIES[name]
    best_pass = None
    best_pass_score = -1e9
    best_overall = None
    best_overall_score = -1e9
    total = 0
    for values in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, values))
        try:
            result = fn(df, **params)
        except Exception:
            continue
        if result is None:
            continue
        total += 1
        s = score(result)
        if s > best_overall_score:
            best_overall_score = s
            best_overall = (params, result)
        if passes_juiz(result) and s > best_pass_score:
            best_pass_score = s
            best_pass = (params, result)
    return best_pass, best_overall, total


def main():
    api_key = os.getenv("BINANCE_API_KEY", "") or ""
    api_secret = os.getenv("BINANCE_SECRET_KEY", "") or ""
    client = Client(api_key.strip('"'), api_secret.strip('"'))

    started = time.time()
    matrix = {}
    for asset in ASSETS:
        print(f"\n>>> {asset}", flush=True)
        df_raw = fetch_paginated(client, asset, total=3000)
        if df_raw.empty:
            continue
        matrix[asset] = {}
        for strat_name, grid in GRIDS.items():
            t0 = time.time()
            best_pass, best_overall, total = sweep_strategy(df_raw, strat_name, grid)
            elapsed = time.time() - t0
            if best_pass:
                params, result = best_pass
                pf = profit_factor(result)
                print(
                    f"  ✓ {strat_name:15s} trades={result['total_trades']:3d} "
                    f"WR={result['winrate_pct']:5.1f}% PF={pf:5.2f} "
                    f"DD={abs(result['max_drawdown_pct']):4.1f}% "
                    f"({total} combos, {elapsed:.1f}s)",
                    flush=True,
                )
                matrix[asset][strat_name] = {
                    "passes": True,
                    "params": params,
                    "metrics": {**result, "profit_factor": round(pf, 3)},
                }
            else:
                params, result = best_overall if best_overall else ({}, {})
                pf = profit_factor(result) if result else 0
                trades = result.get("total_trades", 0) if result else 0
                wr = result.get("winrate_pct", 0) if result else 0
                dd = abs(result.get("max_drawdown_pct", 0)) if result else 0
                print(
                    f"  ✗ {strat_name:15s} trades={trades:3d} "
                    f"WR={wr:5.1f}% PF={pf:5.2f} DD={dd:4.1f}% "
                    f"({total} combos, {elapsed:.1f}s)",
                    flush=True,
                )
                matrix[asset][strat_name] = {
                    "passes": False,
                    "params": params,
                    "metrics": {**result, "profit_factor": round(pf, 3)} if result else None,
                }

    out = ROOT / "src/logs/agents/sweep_strategies.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "criteria": {"min_trades": MIN_TRADES, "min_pf": MIN_PF, "max_dd_pct": MAX_DD_PCT},
        "matrix": matrix,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== TOTAL {time.time() - started:.0f}s ===")
    print("\nMATRIZ (linhas=ativos, colunas=estrategias) — ✓ passa Juiz")
    print(f"  {'asset':10s} | {' | '.join(s[:10].rjust(10) for s in GRIDS.keys())}")
    print(f"  {'-' * 10} | {'-+-'.join(['-' * 10 for _ in GRIDS])}")
    for asset, by_strat in matrix.items():
        cells = []
        for s in GRIDS.keys():
            entry = by_strat.get(s, {})
            if entry.get("passes"):
                pf = entry["metrics"]["profit_factor"]
                cells.append(f"  ✓ PF{pf:.2f}".rjust(10))
            else:
                cells.append("    ✗".rjust(10))
        print(f"  {asset:10s} | {' | '.join(cells)}")
    print(f"\nrelatorio: {out}")


if __name__ == "__main__":
    main()
