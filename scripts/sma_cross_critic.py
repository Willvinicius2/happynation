"""Critic v4 manual nas top candidatas SMA Cross com Scaled Exits."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.test_sma_cross_exits import sma_cross_signals
from src.research.aether.compute import _run_local_backtest, load_dataset_fast


# (variant_label, asset, tf, kwargs)
CANDIDATES = [
    ("V2 TP+4%/SL-2%", "SOLUSDT", "4h", {"tp_pct": 4.0, "sl_pct": 2.0}),
    ("V2 TP+4%/SL-2%", "BTCUSDT", "1d", {"tp_pct": 4.0, "sl_pct": 2.0}),
    ("V1 TP+2.5%/SL-2%", "SOLUSDT", "4h", {"tp_pct": 2.5, "sl_pct": 2.0}),
    ("V4 sem TP/SL", "BTCUSDT", "1d", {"tp_pct": None, "sl_pct": None}),
    ("V4 sem TP/SL", "SOLUSDT", "1d", {"tp_pct": None, "sl_pct": None}),
]
ALT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]


def bt(df, sig, tf):
    return _run_local_backtest(df, sig, capital=1000.0, fee_bps=10.0, slippage_bps=2.0, timeframe=tf)


def critic_run(label, asset, tf, kwargs):
    print(f"\n{'='*80}\n CRITIC v4 -- {label}  {asset} {tf}\n{'='*80}")
    df = load_dataset_fast(f"binance:{asset}:{tf}")
    sig = sma_cross_signals(df, **kwargs)
    base = bt(df, sig, tf)
    print(f"\n[BASE] pnl={base['pnl_pct']:+.2f}% trades={base['trade_count']} "
          f"mdd={base['mdd_pct']:.2f}% hit={(base['hit_rate'] or 0)*100:.1f}% sharpe={base['sharpe']:+.2f}")

    # Gate 1 WF
    n = len(df); fs = n // 4; pnls = []
    for k in range(4):
        sub = df.iloc[k*fs : (k+1)*fs if k<3 else n].reset_index(drop=True)
        if len(sub) < 60: continue
        s = sma_cross_signals(sub, **kwargs)
        r = bt(sub, s, tf)
        if r.get("ok"): pnls.append(r["pnl_pct"])
    passes = sum(1 for p in pnls if p >= -3.0)
    wf_ok = passes >= 3
    print(f"\nGate 1 WF: {[round(p,2) for p in pnls]}  ({passes}/3) -> {'PASS' if wf_ok else 'FAIL'}")

    # Gate 2 CS
    alts = [a for a in ALT_SYMBOLS if a != asset]
    cs_pnl = {}
    for alt in alts:
        df_a = load_dataset_fast(f"binance:{alt}:{tf}")
        s = sma_cross_signals(df_a, **kwargs)
        r = bt(df_a, s, tf)
        if r.get("ok"): cs_pnl[alt] = round(r["pnl_pct"], 2)
    cs_passes = sum(1 for v in cs_pnl.values() if v >= -3.0)
    cs_ok = cs_passes >= 1
    print(f"Gate 2 CS: {cs_pnl}  ({cs_passes}/1) -> {'PASS' if cs_ok else 'FAIL'}")

    # Gate 3 Perturb (short_len, long_len, tp, sl)
    rng = np.random.default_rng(42)
    pt_pnls = []
    base_short = kwargs.get("short_len", 14)
    base_long = kwargs.get("long_len", 28)
    for i in range(5):
        sl_len = int(np.clip(rng.normal(base_short, 2), 10, 18))
        ll_len = int(np.clip(rng.normal(base_long, 4), 22, 35))
        tp_p = kwargs.get("tp_pct")
        if tp_p is not None:
            tp_p = float(np.clip(rng.normal(tp_p, tp_p * 0.15), tp_p * 0.7, tp_p * 1.3))
        s = sma_cross_signals(df, short_len=sl_len, long_len=ll_len,
                               tp_pct=tp_p, sl_pct=kwargs.get("sl_pct"))
        r = bt(df, s, tf)
        if r.get("ok"): pt_pnls.append((sl_len, ll_len, tp_p, round(r["pnl_pct"], 2)))
    pnl_vals = [p[-1] for p in pt_pnls]
    median = float(np.median(pnl_vals)) if pnl_vals else 0
    pos = sum(1 for v in pnl_vals if v > 0) / len(pnl_vals) if pnl_vals else 0
    pt_ok = median > 0 and pos >= 0.5
    print(f"Gate 3 PT: {pt_pnls}  median={median:+.2f}% pos={pos:.2f} -> {'PASS' if pt_ok else 'FAIL'}")

    # Gate 4 Bootstrap
    sa = sig.shift(1).fillna(0).astype(int).values
    rets = pd.Series(df["close"].astype(float).values).pct_change().fillna(0).values
    in_pos = (sa * rets)[sa != 0]
    bs_ok = False
    if len(in_pos) >= 30:
        rng2 = np.random.default_rng(123)
        sharpes = []
        bars_per_year = {"4h": 6 * 365, "1d": 365}.get(tf, 365)
        for _ in range(500):
            sample = rng2.choice(in_pos, size=len(in_pos), replace=True)
            if sample.std() > 0:
                sharpes.append(sample.mean() / sample.std() * np.sqrt(bars_per_year))
        p5 = float(np.percentile(sharpes, 5)); p50 = float(np.percentile(sharpes, 50))
        bs_ok = p5 >= -0.15
        print(f"Gate 4 BS: P5={p5:.3f} P50={p50:.3f} -> {'PASS' if bs_ok else 'FAIL'}")
    else:
        print(f"Gate 4 BS: poucos in_pos ({len(in_pos)}) -> FAIL")

    all_ok = wf_ok and cs_ok and pt_ok and bs_ok
    print(f"\n[VEREDICTO]: {'APROVADA' if all_ok else 'REPROVADA'}")
    return {"label": label, "asset": asset, "tf": tf, "base": base,
            "wf_ok": wf_ok, "cs_ok": cs_ok, "pt_ok": pt_ok, "bs_ok": bs_ok,
            "approved": all_ok}


if __name__ == "__main__":
    results = []
    for label, asset, tf, kw in CANDIDATES:
        results.append(critic_run(label, asset, tf, kw))

    print("\n" + "=" * 80)
    print(" RESUMO FINAL -- SMA Cross com Scaled Exits")
    print("=" * 80)
    print(f"{'variant':<22} {'asset':<10} {'tf':<5} {'pnl%':>10} {'mdd%':>8} {'WF':>3} {'CS':>3} {'PT':>3} {'BS':>3}  {'verdict':>10}")
    print("-" * 90)
    for r in results:
        marks = ["P" if g else "F" for g in (r["wf_ok"], r["cs_ok"], r["pt_ok"], r["bs_ok"])]
        v = "APROVADA" if r["approved"] else "REPROVADA"
        print(f"{r['label']:<22} {r['asset']:<10} {r['tf']:<5} "
              f"{r['base']['pnl_pct']:>+9.2f}% {r['base']['mdd_pct']:>7.2f}% "
              f"{marks[0]:>3} {marks[1]:>3} {marks[2]:>3} {marks[3]:>3}  {v:>10}")
    aprovadas = [r for r in results if r["approved"]]
    print(f"\naprovadas: {len(aprovadas)}/{len(results)}")
