"""Tenta resgatar SOL 1d e BNB 1d aplicando 2 mudancas:
  V2_SL  = Hull Suite + SL fixo -1.5 ATR (sai antes do flip se trade ja afundou)
  V2_LEN = testa lengths 50, 55, 60, 65, 75

Tambem rodal SOL 1d em janela recente (so ultimos 12 meses) pra ver se alpha
ainda existe ou se foi enterrado pelo bull macro de 2024.

Critic v4 re-aplicado nas variantes que melhorarem.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.test_hull_suite import hull_signals, hull_value
from src.research.aether.compute import _run_local_backtest, load_dataset_fast


CAPITAL = 1000.0
FEE = 10.0
SLIP = 2.0


def bt(df, sig, tf):
    return _run_local_backtest(df, sig, capital=CAPITAL, fee_bps=FEE, slippage_bps=SLIP, timeframe=tf)


def atr(df, length=14):
    high = df["high"].astype(float).reset_index(drop=True)
    low = df["low"].astype(float).reset_index(drop=True)
    close = df["close"].astype(float).reset_index(drop=True)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
                   axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def hull_signals_with_sl(df, *, length=55, mode="Hma", atr_mult_stop=1.5, atr_len=14):
    """Hull Suite + SL inicial fixo. Sai por flip OU stop, o que vier primeiro."""
    src = df["close"].astype(float).reset_index(drop=True)
    hull = hull_value(src, length, mode)
    cond_long = (hull > hull.shift(2)).fillna(False).values
    atr_v = atr(df, atr_len).values
    close_arr = src.values

    sig = np.zeros(len(df), dtype=np.int8)
    pos = 0
    entry_price = 0.0
    initial_stop = 0.0
    for i in range(len(df)):
        if pos == 0:
            if cond_long[i]:
                pos = 1
                entry_price = close_arr[i]
                initial_stop = entry_price - atr_mult_stop * atr_v[i]
        else:  # pos == 1
            # SL fixo OU flip do hull
            if close_arr[i] < initial_stop:
                pos = 0
                entry_price = 0.0
                initial_stop = 0.0
            elif not cond_long[i]:
                pos = 0
                entry_price = 0.0
                initial_stop = 0.0
        sig[i] = pos
    return pd.Series(sig, index=df.index)


def explore(asset):
    print(f"\n{'='*80}\n EXPLORA -- {asset} 1d\n{'='*80}")
    df = load_dataset_fast(f"binance:{asset}:1d")

    # Original (V1) baseline
    sig = hull_signals(df, length=55, mode="Hma")
    r = bt(df, sig, "1d")
    print(f"\n[V1 baseline length=55] pnl={r['pnl_pct']:+.2f}% mdd={r['mdd_pct']:.2f}% trades={r['trade_count']} sharpe={r['sharpe']:+.2f}")

    # V2 -- com SL
    print("\n[V2 -- Hull + SL fixo -1.5 ATR]")
    for atr_mult in [1.0, 1.5, 2.0]:
        sig_sl = hull_signals_with_sl(df, length=55, atr_mult_stop=atr_mult)
        r = bt(df, sig_sl, "1d")
        print(f"  atr_mult={atr_mult}: pnl={r['pnl_pct']:+.2f}%  mdd={r['mdd_pct']:.2f}%  trades={r['trade_count']}  sharpe={r['sharpe']:+.2f}")

    # V2 -- length tuning
    print("\n[V2 -- length tuning (sem SL)]")
    for length in [40, 50, 55, 60, 65, 75, 90, 100]:
        sig = hull_signals(df, length=length)
        r = bt(df, sig, "1d")
        print(f"  length={length:>3}: pnl={r['pnl_pct']:+.2f}%  mdd={r['mdd_pct']:.2f}%  trades={r['trade_count']}  sharpe={r['sharpe']:+.2f}")

    # Combinacao SL + length
    print("\n[V3 -- length tuning + SL -1.5 ATR (combo)]")
    for length in [50, 60, 65, 75]:
        sig_sl = hull_signals_with_sl(df, length=length, atr_mult_stop=1.5)
        r = bt(df, sig_sl, "1d")
        print(f"  length={length:>3} + SL=1.5atr: pnl={r['pnl_pct']:+.2f}%  mdd={r['mdd_pct']:.2f}%  trades={r['trade_count']}  sharpe={r['sharpe']:+.2f}")


def janela_recente(asset, *, ultimos_meses=12):
    df_full = load_dataset_fast(f"binance:{asset}:1d")
    n_recent = min(ultimos_meses * 30, len(df_full))
    df = df_full.tail(n_recent).reset_index(drop=True)
    print(f"\n[{asset} 1d -- ultimos {ultimos_meses} meses ({len(df)} bars)]")
    sig = hull_signals(df, length=55)
    r = bt(df, sig, "1d")
    print(f"  Hull length=55 long-only: pnl={r['pnl_pct']:+.2f}%  mdd={r['mdd_pct']:.2f}%  trades={r['trade_count']}  sharpe={r['sharpe']:+.2f}")
    sig_sl = hull_signals_with_sl(df, length=55, atr_mult_stop=1.5)
    r2 = bt(df, sig_sl, "1d")
    print(f"  Hull length=55 + SL 1.5atr: pnl={r2['pnl_pct']:+.2f}%  mdd={r2['mdd_pct']:.2f}%  trades={r2['trade_count']}  sharpe={r2['sharpe']:+.2f}")


if __name__ == "__main__":
    explore("SOLUSDT")
    explore("BNBUSDT")

    print("\n" + "=" * 80)
    print(" SOL 1d -- janela RECENTE (alpha ainda vivo?)")
    print("=" * 80)
    for meses in [6, 12, 18, 24]:
        janela_recente("SOLUSDT", ultimos_meses=meses)

    print("\n" + "=" * 80)
    print(" BNB 1d -- janela recente (sanity check)")
    print("=" * 80)
    for meses in [12, 24]:
        janela_recente("BNBUSDT", ultimos_meses=meses)
