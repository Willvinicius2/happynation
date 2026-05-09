"""Critic v4 manual em Hull Suite 1d (BTC, ETH, SOL, BNB).

4 gates:
  1. Walk-forward 4-fold (>= 3/4 com pnl >= -3%)
  2. Cross-symbol (>= 1 dos outros 3 ativos com pnl >= -3%)
  3. Perturb length 50,53,55,57,60 (mediana > 0, >= 50% positivos)
  4. Bootstrap MC sharpe (P5 >= -0.15)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.test_hull_suite import hull_signals
from src.research.aether.compute import _run_local_backtest, load_dataset_fast


CANDIDATES = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
TF = "1d"
CAPITAL = 1000.0
FEE = 10.0
SLIP = 2.0


def bt(df, sig, tf=TF):
    return _run_local_backtest(df, sig, capital=CAPITAL, fee_bps=FEE, slippage_bps=SLIP, timeframe=tf)


def gate_walk_forward(df, *, length=55, mode="Hma", folds=4, min_pnl=-3.0):
    n = len(df)
    fold_size = n // folds
    pnls = []
    for k in range(folds):
        start = k * fold_size
        end = start + fold_size if k < folds - 1 else n
        sub = df.iloc[start:end].reset_index(drop=True)
        if len(sub) < 60:
            continue
        sig = hull_signals(sub, length=length, mode=mode)
        r = bt(sub, sig)
        if r.get("ok"):
            pnls.append(r["pnl_pct"])
    passes = sum(1 for p in pnls if p >= min_pnl)
    return {"fold_pnls": [round(p, 2) for p in pnls],
            "passes": passes, "needs": folds - 1, "ok": passes >= folds - 1}


def gate_cross_symbol(asset, *, length=55, mode="Hma", min_pnl=-3.0, min_pass=1):
    alts = [a for a in CANDIDATES if a != asset]
    pnls = {}
    for alt in alts:
        try:
            df_alt = load_dataset_fast(f"binance:{alt}:{TF}")
            sig = hull_signals(df_alt, length=length, mode=mode)
            r = bt(df_alt, sig)
            if r.get("ok"):
                pnls[alt] = r["pnl_pct"]
        except Exception:
            pnls[alt] = None
    passes = sum(1 for v in pnls.values() if v is not None and v >= min_pnl)
    return {"alts_pnl": {k: round(v, 2) if v is not None else None for k, v in pnls.items()},
            "passes": passes, "needs": min_pass, "ok": passes >= min_pass}


def gate_perturb(df):
    pnls = []
    for length in [45, 50, 55, 60, 65]:
        sig = hull_signals(df, length=length, mode="Hma")
        r = bt(df, sig)
        if r.get("ok"):
            pnls.append((length, r["pnl_pct"]))
    pnl_vals = [p for _, p in pnls]
    median = float(np.median(pnl_vals)) if pnl_vals else 0.0
    pos_frac = (sum(1 for p in pnl_vals if p > 0) / len(pnl_vals)) if pnl_vals else 0.0
    return {"variants": [(l, round(p, 2)) for l, p in pnls],
            "median": round(median, 2),
            "positive_frac": round(pos_frac, 2),
            "ok": median > 0 and pos_frac >= 0.5}


def gate_bootstrap_mc(df, sig, *, n_iter=500, p5_thr=-0.15):
    sig_arr = sig.shift(1).fillna(0).astype(int).values
    rets = pd.Series(df["close"].astype(float).values).pct_change().fillna(0).values
    strat_rets = sig_arr * rets
    in_pos = strat_rets[sig_arr != 0]
    if len(in_pos) < 30:
        return {"ok": False, "reason": "too few in-pos returns", "n": int(len(in_pos))}
    rng = np.random.default_rng(123)
    sharpes = []
    for _ in range(n_iter):
        sample = rng.choice(in_pos, size=len(in_pos), replace=True)
        if sample.std() > 0:
            sharpes.append(sample.mean() / sample.std() * np.sqrt(365))
    p5 = float(np.percentile(sharpes, 5))
    p50 = float(np.percentile(sharpes, 50))
    return {"p5_sharpe": round(p5, 3), "p50_sharpe": round(p50, 3), "ok": p5 >= p5_thr}


def run_critic(asset):
    print(f"\n{'='*80}\n CRITIC v4 -- Hull Suite {asset} {TF}\n{'='*80}")
    df = load_dataset_fast(f"binance:{asset}:{TF}")
    sig = hull_signals(df)
    base = bt(df, sig)
    print(f"\n[BASE] pnl={base['pnl_pct']:+.2f}% trades={base['trade_count']} "
          f"mdd={base['mdd_pct']:.2f}% hit={(base['hit_rate'] or 0)*100:.1f}% sharpe={base['sharpe']:+.2f}")

    print("\n[GATE 1: Walk-forward 4-fold]")
    wf = gate_walk_forward(df)
    print(f"  fold pnls: {wf['fold_pnls']}  ({wf['passes']}/{wf['needs']} pass) -> {'PASS' if wf['ok'] else 'FAIL'}")

    print("\n[GATE 2: Cross-symbol]")
    cs = gate_cross_symbol(asset)
    print(f"  alts: {cs['alts_pnl']}  ({cs['passes']}/{cs['needs']} pass) -> {'PASS' if cs['ok'] else 'FAIL'}")

    print("\n[GATE 3: Perturb length 45/50/55/60/65]")
    pt = gate_perturb(df)
    print(f"  variants: {pt['variants']}  median={pt['median']}%  positive_frac={pt['positive_frac']} -> {'PASS' if pt['ok'] else 'FAIL'}")

    print("\n[GATE 4: Bootstrap MC sharpe]")
    bs = gate_bootstrap_mc(df, sig)
    print(f"  P5 sharpe: {bs.get('p5_sharpe')}  P50: {bs.get('p50_sharpe')} -> {'PASS' if bs['ok'] else 'FAIL'}")

    all_pass = wf["ok"] and cs["ok"] and pt["ok"] and bs["ok"]
    print(f"\n[VEREDICTO {asset} {TF}]: {'APROVADA' if all_pass else 'REPROVADA'}")
    return {"asset": asset, "base": base, "wf": wf, "cs": cs, "pt": pt, "bs": bs,
            "approved": all_pass}


if __name__ == "__main__":
    results = []
    for asset in CANDIDATES:
        results.append(run_critic(asset))

    print("\n" + "=" * 80)
    print(" RESUMO FINAL -- Hull Suite 1d (length 55, mode Hma, long-only)")
    print("=" * 80)
    print(f"{'asset':<10} {'pnl%':>10} {'mdd%':>8} {'sharpe':>8} {'WF':>4} {'CS':>4} {'PT':>4} {'BS':>4} {'verdict':>10}")
    print("-" * 80)
    for r in results:
        marks = ["P" if g else "F" for g in (r["wf"]["ok"], r["cs"]["ok"], r["pt"]["ok"], r["bs"]["ok"])]
        v = "APROVADA" if r["approved"] else "REPROVADA"
        print(f"{r['asset']:<10} {r['base']['pnl_pct']:>+9.2f}% {r['base']['mdd_pct']:>7.2f}% "
              f"{r['base']['sharpe']:>+7.2f} "
              f"{marks[0]:>4} {marks[1]:>4} {marks[2]:>4} {marks[3]:>4} {v:>10}")
    aprovadas = [r for r in results if r["approved"]]
    print(f"\naprovadas: {len(aprovadas)}/{len(results)}")
