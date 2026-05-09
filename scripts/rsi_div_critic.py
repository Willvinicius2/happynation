"""Critic v4 manual em RSI Divergence V2 (sem Hidden Bull) -- candidatos 4h."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.test_rsi_divergence import rsi_div_signals
from src.research.aether.compute import _run_local_backtest, load_dataset_fast


CANDIDATES = [("BTCUSDT", "4h"), ("ETHUSDT", "4h"), ("SOLUSDT", "4h"), ("BNBUSDT", "1d")]
CAPITAL, FEE, SLIP = 1000.0, 10.0, 2.0


def bt(df, sig, tf):
    return _run_local_backtest(df, sig, capital=CAPITAL, fee_bps=FEE, slippage_bps=SLIP, timeframe=tf)


def sig_fn(df, **overrides):
    params = dict(rsi_len=9, lbL=1, lbR=3, take_profit_rsi=80, use_hidden_bull=False)
    params.update(overrides)
    return rsi_div_signals(df, **params)


def gate_walk_forward(df, tf, *, folds=4, min_pnl=-3.0):
    n = len(df)
    fold_size = n // folds
    pnls = []
    for k in range(folds):
        start = k * fold_size
        end = start + fold_size if k < folds - 1 else n
        sub = df.iloc[start:end].reset_index(drop=True)
        if len(sub) < 100:
            continue
        s = sig_fn(sub)
        r = bt(sub, s, tf)
        if r.get("ok"):
            pnls.append(r["pnl_pct"])
    passes = sum(1 for p in pnls if p >= min_pnl)
    return {"fold_pnls": [round(p, 2) for p in pnls],
            "passes": passes, "needs": folds - 1, "ok": passes >= folds - 1}


def gate_cross_symbol(asset, tf, *, alts=None, min_pnl=-3.0, min_pass=1):
    if alts is None:
        alts = [a for a, _ in CANDIDATES if a != asset]
    pnls = {}
    for alt in alts:
        df_alt = load_dataset_fast(f"binance:{alt}:{tf}")
        s = sig_fn(df_alt)
        r = bt(df_alt, s, tf)
        if r.get("ok"):
            pnls[alt] = round(r["pnl_pct"], 2)
    passes = sum(1 for v in pnls.values() if v >= min_pnl)
    return {"alts_pnl": pnls, "passes": passes, "needs": min_pass, "ok": passes >= min_pass}


def gate_perturb(df, tf):
    rng = np.random.default_rng(42)
    pnls = []
    for i in range(5):
        rsi_len = int(np.clip(rng.normal(9, 1.5), 7, 12))
        tp_rsi = int(np.clip(rng.normal(80, 4), 70, 88))
        lbL = int(np.clip(rng.normal(1, 0.5), 1, 2))
        lbR = int(np.clip(rng.normal(3, 1), 2, 5))
        s = sig_fn(df, rsi_len=rsi_len, take_profit_rsi=tp_rsi, lbL=lbL, lbR=lbR)
        r = bt(df, s, tf)
        if r.get("ok"):
            pnls.append((rsi_len, tp_rsi, lbL, lbR, round(r["pnl_pct"], 2)))
    pnl_vals = [p[-1] for p in pnls]
    median = float(np.median(pnl_vals)) if pnl_vals else 0.0
    pos_frac = (sum(1 for p in pnl_vals if p > 0) / len(pnl_vals)) if pnl_vals else 0.0
    return {"variants": pnls, "median": round(median, 2),
            "positive_frac": round(pos_frac, 2),
            "ok": median > 0 and pos_frac >= 0.5}


def gate_bootstrap_mc(df, sig):
    sig_arr = sig.shift(1).fillna(0).astype(int).values
    rets = pd.Series(df["close"].astype(float).values).pct_change().fillna(0).values
    strat_rets = sig_arr * rets
    in_pos = strat_rets[sig_arr != 0]
    if len(in_pos) < 30:
        return {"ok": False, "reason": "few in-pos", "n": int(len(in_pos))}
    rng = np.random.default_rng(123)
    sharpes = []
    for _ in range(500):
        sample = rng.choice(in_pos, size=len(in_pos), replace=True)
        if sample.std() > 0:
            sharpes.append(sample.mean() / sample.std() * np.sqrt(2190))  # 4h ~= 2190 bars/year
    p5 = float(np.percentile(sharpes, 5))
    p50 = float(np.percentile(sharpes, 50))
    return {"p5_sharpe": round(p5, 3), "p50_sharpe": round(p50, 3), "ok": p5 >= -0.15}


def run_critic(asset, tf):
    print(f"\n{'='*80}\n CRITIC v4 -- RSI Divergence V2 {asset} {tf}\n{'='*80}")
    df = load_dataset_fast(f"binance:{asset}:{tf}")
    sig = sig_fn(df)
    base = bt(df, sig, tf)
    print(f"\n[BASE] pnl={base['pnl_pct']:+.2f}% trades={base['trade_count']} "
          f"mdd={base['mdd_pct']:.2f}% hit={(base['hit_rate'] or 0)*100:.1f}% sharpe={base['sharpe']:+.2f}")

    print("\n[GATE 1: Walk-forward 4-fold]")
    wf = gate_walk_forward(df, tf)
    print(f"  fold pnls: {wf['fold_pnls']}  ({wf['passes']}/{wf['needs']}) -> {'PASS' if wf['ok'] else 'FAIL'}")

    print("\n[GATE 2: Cross-symbol]")
    cs = gate_cross_symbol(asset, tf)
    print(f"  alts: {cs['alts_pnl']}  ({cs['passes']}/{cs['needs']}) -> {'PASS' if cs['ok'] else 'FAIL'}")

    print("\n[GATE 3: Perturb (rsi_len, tp, lbL, lbR)]")
    pt = gate_perturb(df, tf)
    print(f"  variants: {pt['variants']}")
    print(f"  median={pt['median']}%  pos_frac={pt['positive_frac']} -> {'PASS' if pt['ok'] else 'FAIL'}")

    print("\n[GATE 4: Bootstrap MC]")
    bs = gate_bootstrap_mc(df, sig)
    print(f"  P5={bs.get('p5_sharpe')} P50={bs.get('p50_sharpe')} -> {'PASS' if bs.get('ok') else 'FAIL'}")

    all_ok = wf["ok"] and cs["ok"] and pt["ok"] and bs.get("ok", False)
    print(f"\n[VEREDICTO {asset} {tf}]: {'APROVADA' if all_ok else 'REPROVADA'}")
    return {"asset": asset, "tf": tf, "base": base,
            "wf": wf, "cs": cs, "pt": pt, "bs": bs, "approved": all_ok}


if __name__ == "__main__":
    results = []
    for asset, tf in CANDIDATES:
        results.append(run_critic(asset, tf))

    print("\n" + "=" * 80)
    print(" RESUMO FINAL -- RSI Divergence V2 (sem Hidden Bull)")
    print("=" * 80)
    print(f"{'asset':<10} {'tf':<5} {'pnl%':>10} {'mdd%':>8} {'sharpe':>8} {'WF':>4} {'CS':>4} {'PT':>4} {'BS':>4} {'verdict':>10}")
    print("-" * 80)
    for r in results:
        marks = ["P" if g else "F" for g in (r["wf"]["ok"], r["cs"]["ok"], r["pt"]["ok"], r["bs"].get("ok", False))]
        v = "APROVADA" if r["approved"] else "REPROVADA"
        print(f"{r['asset']:<10} {r['tf']:<5} {r['base']['pnl_pct']:>+9.2f}% "
              f"{r['base']['mdd_pct']:>7.2f}% {r['base']['sharpe']:>+7.2f} "
              f"{marks[0]:>4} {marks[1]:>4} {marks[2]:>4} {marks[3]:>4} {v:>10}")
    aprovadas = [r for r in results if r["approved"]]
    print(f"\naprovadas: {len(aprovadas)}/{len(results)}")
