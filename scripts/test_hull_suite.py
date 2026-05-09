"""Hull Suite Strategy (DashTrader/InSilico) -- backtest no motor do Aether.

Estrategia simples:
- Calcula Hull MA (HMA, EHMA ou THMA) com length 55
- Sinal: HMA[0] > HMA[2] -> LONG; HMA[0] < HMA[2] -> SHORT (TV) / EXIT (spot)
- Sem stop/TP/trail explicito -- saida pela inversao da Hull

Roda 4 ativos x 4 TFs LONG-only (spot Binance) + LONG+SHORT (replica TV).
"""
from __future__ import annotations

import math
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
# Hull Moving Averages
# =========================================================================
def wma(series: pd.Series, length: int) -> pd.Series:
    """Weighted moving average classico (peso linear: n, n-1, ..., 1)."""
    weights = np.arange(1, length + 1, dtype=float)
    weights /= weights.sum()
    return series.rolling(length, min_periods=length).apply(
        lambda x: (x * weights).sum(), raw=True
    )


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def hma(src: pd.Series, length: int) -> pd.Series:
    """HMA = wma(2*wma(src, n/2) - wma(src, n), round(sqrt(n)))"""
    half = max(1, length // 2)
    sqrt_len = max(1, int(round(math.sqrt(length))))
    return wma(2 * wma(src, half) - wma(src, length), sqrt_len)


def ehma(src: pd.Series, length: int) -> pd.Series:
    """EHMA = ema(2*ema(src, n/2) - ema(src, n), round(sqrt(n)))"""
    half = max(1, length // 2)
    sqrt_len = max(1, int(round(math.sqrt(length))))
    return ema(2 * ema(src, half) - ema(src, length), sqrt_len)


def thma(src: pd.Series, length: int) -> pd.Series:
    """THMA com length passado como length/2 conforme switch do Pine."""
    n = max(1, length // 2)
    third = max(1, n // 3)
    half = max(1, n // 2)
    return wma(wma(src, third) * 3 - wma(src, half) - wma(src, n), n)


def hull_value(src: pd.Series, length: int, mode: str) -> pd.Series:
    if mode == "Hma":
        return hma(src, length)
    if mode == "Ehma":
        return ehma(src, length)
    if mode == "Thma":
        return thma(src, length)
    raise ValueError(f"mode invalido: {mode}")


# =========================================================================
# Sinais
# =========================================================================
def hull_signals(
    df: pd.DataFrame,
    *,
    length: int = 55,
    mode: str = "Hma",
    enable_short: bool = False,
) -> pd.Series:
    """Sinal: HMA[0] > HMA[2] -> 1; HMA[0] < HMA[2] -> -1 (se enable_short) ou 0."""
    src = df["close"].astype(float).reset_index(drop=True)
    hull = hull_value(src, length, mode)
    # Pine: HULL[0] vs HULL[2] (atual vs 2 atras)
    cond_long = (hull > hull.shift(2)).fillna(False).values
    if enable_short:
        sig = np.where(cond_long, 1, -1).astype(np.int8)
    else:
        sig = np.where(cond_long, 1, 0).astype(np.int8)
    return pd.Series(sig, index=df.index)


# =========================================================================
# Matriz
# =========================================================================
ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
TIMEFRAMES = ["15m", "1h", "4h", "1d"]
LENGTHS = [55]  # default da estrategia
MODES = ["Hma"]  # default da estrategia


def run_matrix(*, enable_short: bool = False) -> list[dict]:
    rows: list[dict] = []
    print(f"Rodando matriz Hull Suite (enable_short={enable_short})...\n")
    for asset in ASSETS:
        for tf in TIMEFRAMES:
            dataset_id = f"binance:{asset}:{tf}"
            t0 = time.time()
            try:
                df = load_dataset_fast(dataset_id)
                if len(df) < 250:
                    rows.append({"asset": asset, "tf": tf, "ok": False,
                                 "error": f"poucos candles ({len(df)})"})
                    continue
                sig = hull_signals(df, enable_short=enable_short)
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


def report(rows: list[dict], title: str) -> None:
    print("\n" + "=" * 100)
    print(f" {title}")
    print("=" * 100)
    header = f"{'asset':<10} {'tf':<5} {'bars':>6} {'pnl%':>9} {'trades':>7} {'mdd%':>7} {'hit%':>6} {'sharpe':>7}"
    print(header)
    print("-" * len(header))
    ok_rows = []
    for r in rows:
        if r.get("ok"):
            hit = (r["hit_rate"] or 0) * 100 if r.get("hit_rate") is not None else 0
            print(f"{r['asset']:<10} {r['tf']:<5} {r['bars']:>6} "
                  f"{r['pnl_pct']:>+8.2f}% {r['trade_count']:>7} "
                  f"{r['mdd_pct']:>6.2f}% {hit:>5.1f}% {r['sharpe']:>+7.2f}")
            ok_rows.append(r)
        else:
            print(f"{r['asset']:<10} {r['tf']:<5}  FAILED  {r.get('error', '?')[:60]}")
    if ok_rows:
        avg_pnl = np.mean([r["pnl_pct"] for r in ok_rows])
        positives = sum(1 for r in ok_rows if r["pnl_pct"] > 0)
        avg_mdd = np.mean([r["mdd_pct"] for r in ok_rows])
        print("-" * len(header))
        print(f"resumo: {len(ok_rows)} OK | positivos {positives}/{len(ok_rows)} | "
              f"pnl medio {avg_pnl:+.2f}% | mdd medio {avg_mdd:.2f}%")


if __name__ == "__main__":
    print("\n" + "#" * 100)
    print("# HULL SUITE V1 -- LONG ONLY (spot Binance, sai quando HMA[0]<HMA[2])")
    print("#" * 100)
    rows_long = run_matrix(enable_short=False)
    report(rows_long, "Hull Suite LONG-only -- length 55, mode Hma")

    print("\n" + "#" * 100)
    print("# HULL SUITE V2 -- LONG+SHORT (replica TV; precisa futures pra rodar real)")
    print("#" * 100)
    rows_ls = run_matrix(enable_short=True)
    report(rows_ls, "Hull Suite LONG+SHORT -- length 55, mode Hma")

    # Comparativo
    print("\n" + "=" * 96)
    print(" Comparativo LONG vs LONG+SHORT (delta PnL%)")
    print("=" * 96)
    print(f"{'asset':<10} {'tf':<5} {'LONG':>10} {'LONG+SHORT':>12} {'delta':>10} {'L trades':>9} {'LS trades':>10}")
    print("-" * 70)
    by_ls = {(r['asset'], r['tf']): r for r in rows_ls if r.get('ok')}
    for r in rows_long:
        if not r.get('ok'):
            continue
        ls = by_ls.get((r['asset'], r['tf']))
        if not ls:
            continue
        delta = ls['pnl_pct'] - r['pnl_pct']
        mark = "[+]" if delta > 0 else "[-]" if delta < -1 else " . "
        print(f"{r['asset']:<10} {r['tf']:<5} {r['pnl_pct']:>+9.2f}% "
              f"{ls['pnl_pct']:>+11.2f}% {delta:>+9.2f}% {r['trade_count']:>9} {ls['trade_count']:>10} {mark}")
