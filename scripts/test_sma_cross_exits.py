"""SMA Crossover com Scaled Exits (adolgov) -- backtest no motor do Aether.

Estrategia original tem saidas escalonadas (25% em +1%, +2%, +3%, +4%)
+ SL -2%. O motor do Aether so suporta signal binario, entao testamos
4 variantes pra cobrir o efeito:
  V1 = TP +2.5% (media ponderada das saidas) / SL -2%
  V2 = TP +4%   (target final)               / SL -2%
  V3 = TP +1%   (primeiro alvo, conservador)  / SL -2%
  V4 = sem TP/SL, sai so no cross contrario   (puro trend-following)

Long-only para spot Binance, todos os 6 TFs (1m, 5m, 15m, 1h, 4h, 1d).
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
# Sinais
# =========================================================================
def sma_cross_signals(
    df: pd.DataFrame,
    *,
    short_len: int = 14,
    long_len: int = 28,
    tp_pct: float | None = None,
    sl_pct: float | None = 2.0,
) -> pd.Series:
    """SMA cross long-only com TP/SL opcionais.

    LONG entry: SMA(short) cruza acima de SMA(long).
    Saidas: TP (preco >= entry*(1+tp/100)), SL (preco <= entry*(1-sl/100)),
    ou cross de saida (SMA short cruza abaixo de SMA long).
    """
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
        else:  # pos == 1
            should_exit = False
            # TP/SL: usa high/low pra simular toque intra-candle
            if tp_pct is not None:
                tp_price = entry * (1.0 + tp_pct / 100.0)
                if high_arr[i] >= tp_price:
                    should_exit = True
            if not should_exit and sl_pct is not None:
                sl_price = entry * (1.0 - sl_pct / 100.0)
                if low_arr[i] <= sl_price:
                    should_exit = True
            if not should_exit and cross_down[i]:
                should_exit = True
            if should_exit:
                pos = 0
                entry = 0.0
        sig[i] = pos
    return pd.Series(sig, index=df.index)


# =========================================================================
# Matriz
# =========================================================================
ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]

VARIANTS = [
    ("V1 TP+2.5%/SL-2%", {"tp_pct": 2.5, "sl_pct": 2.0}),
    ("V2 TP+4%/SL-2%",   {"tp_pct": 4.0, "sl_pct": 2.0}),
    ("V3 TP+1%/SL-2%",   {"tp_pct": 1.0, "sl_pct": 2.0}),
    ("V4 sem TP/SL",     {"tp_pct": None, "sl_pct": None}),
]


def run_variant(label, kwargs):
    rows = []
    for asset in ASSETS:
        for tf in TIMEFRAMES:
            t0 = time.time()
            try:
                df = load_dataset_fast(f"binance:{asset}:{tf}")
                if len(df) < 250:
                    rows.append({"asset": asset, "tf": tf, "ok": False, "error": f"few candles"})
                    continue
                sig = sma_cross_signals(df, **kwargs)
                r = _run_local_backtest(df, sig, capital=1000.0, fee_bps=10.0,
                                        slippage_bps=2.0, timeframe=tf)
                r["asset"] = asset; r["tf"] = tf
                r["elapsed_s"] = round(time.time() - t0, 2)
                rows.append(r)
            except Exception as exc:
                rows.append({"asset": asset, "tf": tf, "ok": False,
                             "error": f"{exc.__class__.__name__}: {str(exc)[:80]}"})
    return rows


def report(rows, title):
    print(f"\n{'=' * 100}\n {title}\n{'=' * 100}")
    h = f"{'asset':<10} {'tf':<5} {'bars':>8} {'pnl%':>9} {'trades':>7} {'mdd%':>7} {'hit%':>6} {'sharpe':>8}"
    print(h); print("-" * len(h))
    ok = []
    for r in rows:
        if r.get("ok"):
            hit = (r["hit_rate"] or 0) * 100 if r.get("hit_rate") is not None else 0
            print(f"{r['asset']:<10} {r['tf']:<5} {r['bars']:>8} "
                  f"{r['pnl_pct']:>+8.2f}% {r['trade_count']:>7} "
                  f"{r['mdd_pct']:>6.2f}% {hit:>5.1f}% {r['sharpe']:>+8.2f}")
            ok.append(r)
        else:
            print(f"{r['asset']:<10} {r['tf']:<5}  FAILED  {r.get('error', '?')[:60]}")
    if ok:
        avg_pnl = float(np.mean([r["pnl_pct"] for r in ok]))
        positives = sum(1 for r in ok if r["pnl_pct"] > 0)
        avg_mdd = float(np.mean([r["mdd_pct"] for r in ok]))
        print("-" * len(h))
        print(f"resumo: {len(ok)} OK | positivos {positives}/{len(ok)} | "
              f"pnl medio {avg_pnl:+.2f}% | mdd medio {avg_mdd:.2f}%")


if __name__ == "__main__":
    all_results = {}
    for label, kwargs in VARIANTS:
        print(f"\n{'#' * 100}\n# {label}\n{'#' * 100}")
        rows = run_variant(label, kwargs)
        all_results[label] = rows
        report(rows, label)

    # Top positivas em todas as variantes
    print(f"\n{'=' * 100}\n TOP positivas (consolidado)\n{'=' * 100}")
    print(f"{'variante':<25} {'asset':<10} {'tf':<5} {'pnl%':>9} {'mdd%':>7} {'sharpe':>8} {'trades':>7}")
    print("-" * 75)
    flat = []
    for label, rows in all_results.items():
        for r in rows:
            if r.get("ok") and r["pnl_pct"] > 10:
                flat.append((label, r))
    flat.sort(key=lambda x: -x[1]["pnl_pct"])
    for label, r in flat[:20]:
        print(f"{label:<25} {r['asset']:<10} {r['tf']:<5} "
              f"{r['pnl_pct']:>+8.2f}% {r['mdd_pct']:>6.2f}% {r['sharpe']:>+7.2f} {r['trade_count']:>7}")
    print(f"\ntotal positivas (>+10% pnl): {len(flat)}")
