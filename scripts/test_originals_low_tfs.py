"""Testa as 3 estrategias TV em VERSOES ORIGINAIS (Pine) nos TFs 1m, 5m, 15m.

Diferentes das versoes integradas (V3) que ja foram testadas:
  - Hybrid TV V1 ORIGINAL: com componente RSI mean-revert (rsiLong) + breakout
  - Hull Suite V2: com SHORT habilitado (replica TV exata, mesmo invadendo spot)
  - RSI Divergence V1: com Hidden Bull habilitado (default Pine)

Confirmar se os achados em TFs maiores se mantem (deveriam piorar).
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

from scripts.test_hybrid_tv import hybrid_signals  # V1 original (com RSI mean-revert)
from scripts.test_hull_suite import hull_signals  # com enable_short
from scripts.test_rsi_divergence import rsi_div_signals  # com use_hidden_bull
from src.research.aether.compute import _run_local_backtest, load_dataset_fast


ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
TFS = ["15m", "5m", "1m"]


def bt(df, sig, tf):
    return _run_local_backtest(df, sig, capital=1000.0, fee_bps=10.0, slippage_bps=2.0, timeframe=tf)


def run_one(strategy_name, sig_fn, asset, tf):
    t0 = time.time()
    try:
        df = load_dataset_fast(f"binance:{asset}:{tf}")
        if len(df) < 250:
            return {"ok": False, "error": f"few candles ({len(df)})", "time": 0.0,
                    "strategy": strategy_name, "asset": asset, "tf": tf}
        sig = sig_fn(df)
        r = bt(df, sig, tf)
        r["time"] = round(time.time() - t0, 1)
        r["bars"] = len(df)
        r["strategy"] = strategy_name
        r["asset"] = asset
        r["tf"] = tf
        return r
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:80], "time": round(time.time() - t0, 1),
                "strategy": strategy_name, "asset": asset, "tf": tf}


def report_table(results, title):
    print(f"\n{'=' * 110}")
    print(f" {title}")
    print(f"{'=' * 110}")
    header = f"{'strategy':<28} {'asset':<10} {'tf':<5} {'bars':>8} {'pnl%':>9} {'trades':>7} {'mdd%':>7} {'hit%':>6} {'sharpe':>9}"
    print(header)
    print("-" * len(header))
    for r in results:
        if r.get("ok"):
            hit = (r["hit_rate"] or 0) * 100 if r.get("hit_rate") is not None else 0
            print(f"{r['strategy']:<28} {r['asset']:<10} {r['tf']:<5} "
                  f"{r['bars']:>8} {r['pnl_pct']:>+8.2f}% {r['trade_count']:>7} "
                  f"{r['mdd_pct']:>6.2f}% {hit:>5.1f}% {r['sharpe']:>+9.2f}")
        else:
            print(f"{r['strategy']:<28} {r['asset']:<10} {r['tf']:<5}  FAILED  {r.get('error', '?')[:60]}")


if __name__ == "__main__":
    strategies = [
        ("Hybrid V1 (RSI+Breakout)", lambda df: hybrid_signals(df, enable_short=False)),
        ("Hull Suite V2 (long+short)", lambda df: hull_signals(df, enable_short=True)),
        ("RSI Div V1 (com Hidden Bull)", lambda df: rsi_div_signals(df, use_hidden_bull=True)),
    ]

    print(f"\nMatriz: {len(strategies)} versoes originais x {len(ASSETS)} ativos x {len(TFS)} TFs = {len(strategies)*len(ASSETS)*len(TFS)} backtests\n")

    all_results = []
    for name, fn in strategies:
        for tf in TFS:
            for asset in ASSETS:
                r = run_one(name, fn, asset, tf)
                all_results.append(r)
                pnl = f"{r['pnl_pct']:+.2f}%" if r.get("ok") else r.get("error", "FAIL")[:30]
                print(f"  {name:<30} {asset} {tf}: {pnl} ({r['time']}s)")

    report_table(all_results, "Resultados consolidados -- versoes ORIGINAIS Pine em TFs menores")

    # Resumo
    print("\n" + "=" * 80)
    print(" Resumo por strategy x TF")
    print("=" * 80)
    print(f"{'strategy':<32} {'tf':<5} {'positivos':>10} {'pnl medio':>12} {'mdd medio':>11}")
    print("-" * 75)
    for name, _ in strategies:
        for tf in TFS:
            sub = [r for r in all_results if r.get("ok") and r.get("strategy") == name and r.get("tf") == tf]
            if not sub:
                continue
            positives = sum(1 for r in sub if r["pnl_pct"] > 0)
            avg_pnl = float(np.mean([r["pnl_pct"] for r in sub]))
            avg_mdd = float(np.mean([r["mdd_pct"] for r in sub]))
            print(f"{name:<32} {tf:<5} {positives}/{len(sub):<8} {avg_pnl:>+11.2f}% {avg_mdd:>10.2f}%")
