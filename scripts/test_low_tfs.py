"""Testa as 3 estrategias TV (engines integrados) em TFs menores: 1m e 5m.

Hipotese: TFs curtos sao catastroficos por ruido + fee acumulada.
Confirmacao via numeros.
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

from src.research.aether.compute import _build_signals, _run_local_backtest, load_dataset_fast


ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
TFS = ["15m", "5m", "1m"]
ENGINES = [
    ("hybrid_breakout", {}),
    ("hull_suite", {}),
    ("rsi_divergence", {}),
]


def run(engine, params, asset, tf):
    t0 = time.time()
    try:
        df = load_dataset_fast(f"binance:{asset}:{tf}")
        if len(df) < 250:
            return {"ok": False, "error": f"few candles ({len(df)})", "time": 0}
        sig = _build_signals(engine, df, params)
        r = _run_local_backtest(df, sig, capital=1000.0, fee_bps=10.0,
                                slippage_bps=2.0, timeframe=tf)
        r["time"] = round(time.time() - t0, 1)
        r["bars"] = len(df)
        return r
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:80], "time": round(time.time() - t0, 1)}


def report_table(results, title):
    print(f"\n{'=' * 100}")
    print(f" {title}")
    print(f"{'=' * 100}")
    header = f"{'engine':<18} {'asset':<10} {'tf':<5} {'bars':>8} {'pnl%':>9} {'trades':>7} {'mdd%':>7} {'hit%':>6} {'sharpe':>7} {'time':>6}"
    print(header)
    print("-" * len(header))
    for r in results:
        if r.get("ok"):
            hit = (r["hit_rate"] or 0) * 100 if r.get("hit_rate") is not None else 0
            print(f"{r['engine']:<18} {r['asset']:<10} {r['tf']:<5} "
                  f"{r['bars']:>8} {r['pnl_pct']:>+8.2f}% {r['trade_count']:>7} "
                  f"{r['mdd_pct']:>6.2f}% {hit:>5.1f}% {r['sharpe']:>+7.2f} "
                  f"{r['time']:>5.1f}s")
        else:
            print(f"{r['engine']:<18} {r['asset']:<10} {r['tf']:<5}  FAILED  {r.get('error', '?')[:60]}")


if __name__ == "__main__":
    all_results = []
    print(f"\nMatriz TFs menores: {len(ENGINES)} engines x {len(ASSETS)} ativos x {len(TFS)} TFs = {len(ENGINES)*len(ASSETS)*len(TFS)} backtests")
    print(f"Engines: {[e[0] for e in ENGINES]}")
    print(f"TFs: {TFS}")
    print(f"Capital $1000, fee 10bps, slip 2bps, long-only")
    for engine, params in ENGINES:
        for tf in TFS:
            for asset in ASSETS:
                r = run(engine, params, asset, tf)
                r["engine"] = engine
                r["asset"] = asset
                r["tf"] = tf
                all_results.append(r)
                ok = "ok" if r.get("ok") else "FAIL"
                pnl = f"{r['pnl_pct']:+.2f}%" if r.get("ok") else "-"
                print(f"  {engine:<18} {asset} {tf}: {pnl} ({r['time']}s) {ok}")

    report_table(all_results, "Resultados consolidados -- TFs menores")

    # Resumo por engine x tf
    print("\n" + "=" * 100)
    print(" Resumo por engine x TF")
    print("=" * 100)
    print(f"{'engine':<18} {'tf':<5} {'positivos':>10} {'pnl medio':>12} {'mdd medio':>11}")
    print("-" * 60)
    for engine, _ in ENGINES:
        for tf in TFS:
            sub = [r for r in all_results if r.get("ok") and r.get("engine") == engine and r.get("tf") == tf]
            if not sub:
                continue
            positives = sum(1 for r in sub if r["pnl_pct"] > 0)
            avg_pnl = float(np.mean([r["pnl_pct"] for r in sub]))
            avg_mdd = float(np.mean([r["mdd_pct"] for r in sub]))
            print(f"{engine:<18} {tf:<5} {positives}/{len(sub):<8} {avg_pnl:>+11.2f}% {avg_mdd:>10.2f}%")
