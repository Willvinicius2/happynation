"""V4 (V3 + 3 melhorias de MDD) em ETH 4h e SOL 1d, depois roda Critic v4
manualmente nos sinais que sobrevivem.

Critic v4 implementado in-line (sem usar critic_agent que precisa de DB):
  1. Cross-symbol: re-roda em 2 alts (BTC, BNB)
  2. Walk-forward 4-fold (PnL por fold)
  3. Perturb params +/-10% (5 variantes)
  4. Bootstrap MC: re-amostra trades 500x

So aprovado se passar todos os 4 gates.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.test_hybrid_tv import hybrid_v3_signals, hybrid_v4_signals
from src.research.aether.compute import _run_local_backtest, load_dataset_fast


# Alvos: (asset, tf)
TARGETS = [("ETHUSDT", "4h"), ("SOLUSDT", "1d")]
ALT_SYMBOLS = ["BTCUSDT", "BNBUSDT"]
CAPITAL = 1000.0
FEE = 10.0
SLIP = 2.0


def backtest(df, sig, tf):
    return _run_local_backtest(df, sig, capital=CAPITAL, fee_bps=FEE, slippage_bps=SLIP, timeframe=tf)


# =========================================================================
# CRITIC v4 GATES
# =========================================================================
def gate_walk_forward(df, sig_fn, tf, *, folds=4, min_pnl=-3.0):
    """Divide o df em folds e roda em cada um. Aprovado se >= folds-1 deram pnl >= min_pnl."""
    n = len(df)
    fold_size = n // folds
    results = []
    for k in range(folds):
        start = k * fold_size
        end = start + fold_size if k < folds - 1 else n
        sub = df.iloc[start:end].reset_index(drop=True)
        if len(sub) < 100:
            continue
        sig = sig_fn(sub)
        r = backtest(sub, sig, tf)
        if not r.get("ok"):
            continue
        results.append(r["pnl_pct"])
    passes = sum(1 for p in results if p >= min_pnl)
    return {
        "fold_pnls": [round(p, 2) for p in results],
        "passes": passes,
        "needs": folds - 1,
        "ok": passes >= folds - 1,
    }


def gate_cross_symbol(asset, tf, sig_fn, *, alts=ALT_SYMBOLS, min_pnl=-3.0, min_pass=1):
    """Roda a estrategia em N ativos alternativos. Pelo menos 1 deve ter pnl >= min_pnl."""
    pnls = {}
    for alt in alts:
        try:
            df = load_dataset_fast(f"binance:{alt}:{tf}")
            sig = sig_fn(df)
            r = backtest(df, sig, tf)
            if r.get("ok"):
                pnls[alt] = r["pnl_pct"]
        except Exception as e:
            pnls[alt] = None
    passes = sum(1 for p in pnls.values() if p is not None and p >= min_pnl)
    return {
        "alts_pnl": {k: round(v, 2) if v is not None else None for k, v in pnls.items()},
        "passes": passes,
        "needs": min_pass,
        "ok": passes >= min_pass,
    }


def gate_perturb(df, tf, *, n_variants=5, min_positive_frac=0.5):
    """Gera N variantes com params perturbados +/-10%. Mediana do PnL > 0."""
    rng = np.random.default_rng(42)
    pnls = []
    for i in range(n_variants):
        # perturba 4 params chave
        atr_mult_trail = float(np.clip(rng.normal(2.0, 0.2), 1.5, 2.5))
        atr_mult_stop = float(np.clip(rng.normal(1.0, 0.1), 0.7, 1.3))
        breakout_len = int(np.clip(rng.normal(20, 2), 16, 24))
        vol_mult = float(np.clip(rng.normal(1.3, 0.15), 1.05, 1.6))
        sig = hybrid_v4_signals(
            df,
            atr_mult_trail=atr_mult_trail,
            atr_mult_stop=atr_mult_stop,
            breakout_len=breakout_len,
            vol_mult=vol_mult,
        )
        r = backtest(df, sig, tf)
        if r.get("ok"):
            pnls.append(r["pnl_pct"])
    if not pnls:
        return {"variants_pnl": [], "ok": False, "median": None}
    median_pnl = float(np.median(pnls))
    positive_frac = sum(1 for p in pnls if p > 0) / len(pnls)
    return {
        "variants_pnl": [round(p, 2) for p in pnls],
        "median": round(median_pnl, 2),
        "positive_frac": round(positive_frac, 2),
        "ok": median_pnl > 0 and positive_frac >= min_positive_frac,
    }


def gate_bootstrap_mc(df, sig, *, n_iter=500, p5_threshold=-0.15):
    """Re-amostra retornos por trade aleatoriamente. P5 do sharpe >= threshold."""
    sig_arr = sig.shift(1).fillna(0).astype(int).values
    close = df["close"].astype(float).values
    rets = pd.Series(close).pct_change().fillna(0).values
    strategy_rets = sig_arr * rets
    in_pos_rets = strategy_rets[sig_arr != 0]
    if len(in_pos_rets) < 30:
        return {"ok": False, "reason": "too few in-pos returns", "n": int(len(in_pos_rets))}
    rng = np.random.default_rng(123)
    sharpes = []
    for _ in range(n_iter):
        sample = rng.choice(in_pos_rets, size=len(in_pos_rets), replace=True)
        if sample.std() > 0:
            sharpes.append(sample.mean() / sample.std() * np.sqrt(252))
    p5 = float(np.percentile(sharpes, 5))
    p50 = float(np.percentile(sharpes, 50))
    return {
        "p5_sharpe": round(p5, 3),
        "p50_sharpe": round(p50, 3),
        "ok": p5 >= p5_threshold,
    }


# =========================================================================
# RUN
# =========================================================================
def run_critic_v4(asset, tf):
    print(f"\n{'='*80}\n CRITIC v4 -- {asset} {tf} (V4 strategy)\n{'='*80}")
    df = load_dataset_fast(f"binance:{asset}:{tf}")
    sig = hybrid_v4_signals(df)
    base = backtest(df, sig, tf)
    print(f"\n[BASE] pnl={base['pnl_pct']:+.2f}% trades={base['trade_count']} "
          f"mdd={base['mdd_pct']:.2f}% hit={(base['hit_rate'] or 0)*100:.1f}% sharpe={base['sharpe']:+.2f}")

    print("\n[GATE 1: Walk-forward 4-fold]")
    wf = gate_walk_forward(df, hybrid_v4_signals, tf)
    status = "PASS" if wf["ok"] else "FAIL"
    print(f"  fold pnls: {wf['fold_pnls']}  ({wf['passes']}/{wf['needs']} pass) -> {status}")

    print("\n[GATE 2: Cross-symbol em " + ", ".join(ALT_SYMBOLS) + "]")
    cs = gate_cross_symbol(asset, tf, hybrid_v4_signals)
    status = "PASS" if cs["ok"] else "FAIL"
    print(f"  alts: {cs['alts_pnl']}  ({cs['passes']}/{cs['needs']} pass) -> {status}")

    print("\n[GATE 3: Perturb params +/-10% (5 variantes)]")
    pt = gate_perturb(df, tf)
    status = "PASS" if pt["ok"] else "FAIL"
    print(f"  variants pnl: {pt['variants_pnl']}  median={pt.get('median')}%  positive_frac={pt.get('positive_frac')} -> {status}")

    print("\n[GATE 4: Bootstrap MC sharpe (P5)]")
    bs = gate_bootstrap_mc(df, sig)
    status = "PASS" if bs["ok"] else "FAIL"
    print(f"  P5 sharpe: {bs.get('p5_sharpe')}  P50 sharpe: {bs.get('p50_sharpe')} -> {status}")

    all_pass = wf["ok"] and cs["ok"] and pt["ok"] and bs["ok"]
    print(f"\n[VEREDICTO {asset} {tf}]: {'APROVADA POR TODOS OS GATES' if all_pass else 'REPROVADA'}")
    return {
        "asset": asset, "tf": tf, "base": base,
        "wf": wf, "cs": cs, "pt": pt, "bs": bs,
        "approved": all_pass,
    }


def compare_v3_v4(asset, tf):
    df = load_dataset_fast(f"binance:{asset}:{tf}")
    r3 = backtest(df, hybrid_v3_signals(df), tf)
    r4 = backtest(df, hybrid_v4_signals(df), tf)
    print(f"\n{asset} {tf}:")
    print(f"  V3: pnl {r3['pnl_pct']:+.2f}%, mdd {r3['mdd_pct']:.2f}%, trades {r3['trade_count']}, sharpe {r3['sharpe']:+.2f}")
    print(f"  V4: pnl {r4['pnl_pct']:+.2f}%, mdd {r4['mdd_pct']:.2f}%, trades {r4['trade_count']}, sharpe {r4['sharpe']:+.2f}")
    print(f"  delta MDD: {r4['mdd_pct'] - r3['mdd_pct']:+.2f}pp  delta PnL: {r4['pnl_pct'] - r3['pnl_pct']:+.2f}pp")


def run_critic_v3(asset, tf):
    """Mesmos gates do v4, mas usando hybrid_v3_signals (versao melhor)."""
    print(f"\n{'='*80}\n CRITIC v4 -- {asset} {tf} (V3 strategy)\n{'='*80}")
    df = load_dataset_fast(f"binance:{asset}:{tf}")
    sig = hybrid_v3_signals(df)
    base = backtest(df, sig, tf)
    print(f"\n[BASE] pnl={base['pnl_pct']:+.2f}% trades={base['trade_count']} "
          f"mdd={base['mdd_pct']:.2f}% hit={(base['hit_rate'] or 0)*100:.1f}% sharpe={base['sharpe']:+.2f}")

    print("\n[GATE 1: Walk-forward 4-fold]")
    wf = gate_walk_forward(df, hybrid_v3_signals, tf)
    status = "PASS" if wf["ok"] else "FAIL"
    print(f"  fold pnls: {wf['fold_pnls']}  ({wf['passes']}/{wf['needs']} pass) -> {status}")

    print("\n[GATE 2: Cross-symbol em " + ", ".join(ALT_SYMBOLS) + "]")
    cs = gate_cross_symbol(asset, tf, hybrid_v3_signals)
    status = "PASS" if cs["ok"] else "FAIL"
    print(f"  alts: {cs['alts_pnl']}  ({cs['passes']}/{cs['needs']} pass) -> {status}")

    print("\n[GATE 3: Perturb params +/-10% (5 variantes V3)]")
    rng = np.random.default_rng(42)
    pnls = []
    for i in range(5):
        atr_mult_trail = float(np.clip(rng.normal(2.0, 0.2), 1.5, 2.5))
        atr_mult_stop = float(np.clip(rng.normal(1.5, 0.15), 1.2, 1.8))
        breakout_len = int(np.clip(rng.normal(20, 2), 16, 24))
        vol_mult = float(np.clip(rng.normal(1.3, 0.15), 1.05, 1.6))
        sig_v = hybrid_v3_signals(df, atr_mult_trail=atr_mult_trail, atr_mult_stop=atr_mult_stop,
                                   breakout_len=breakout_len, vol_mult=vol_mult)
        r = backtest(df, sig_v, tf)
        if r.get("ok"): pnls.append(r["pnl_pct"])
    median_pnl = float(np.median(pnls)) if pnls else 0
    positive_frac = (sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else 0
    pt_ok = median_pnl > 0 and positive_frac >= 0.5
    status = "PASS" if pt_ok else "FAIL"
    print(f"  variants pnl: {[round(p,2) for p in pnls]}  median={round(median_pnl,2)}%  positive_frac={round(positive_frac,2)} -> {status}")

    print("\n[GATE 4: Bootstrap MC sharpe (P5)]")
    bs = gate_bootstrap_mc(df, sig)
    status = "PASS" if bs["ok"] else "FAIL"
    print(f"  P5 sharpe: {bs.get('p5_sharpe')}  P50 sharpe: {bs.get('p50_sharpe')} -> {status}")

    all_pass = wf["ok"] and cs["ok"] and pt_ok and bs["ok"]
    print(f"\n[VEREDICTO {asset} {tf} V3]: {'APROVADA' if all_pass else 'REPROVADA'}")
    return {"asset": asset, "tf": tf, "base": base,
            "wf": wf, "cs": cs, "pt_ok": pt_ok, "bs": bs, "approved": all_pass}


if __name__ == "__main__":
    import sys as _sys
    use_v3 = "v3" in _sys.argv
    print("=" * 80)
    print(f" CRITIC v4 em {'V3' if use_v3 else 'V4'}")
    print("=" * 80)
    results = []
    for asset, tf in TARGETS:
        if use_v3:
            results.append(run_critic_v3(asset, tf))
        else:
            results.append(run_critic_v4(asset, tf))
    print("\n" + "=" * 80)
    print(" RESUMO FINAL")
    print("=" * 80)
    for r in results:
        status = "APROVADA" if r["approved"] else "REPROVADA"
        print(f"  {r['asset']:8s} {r['tf']:4s}: {status}  "
              f"(pnl {r['base']['pnl_pct']:+.2f}%, mdd {r['base']['mdd_pct']:.2f}%, sharpe {r['base']['sharpe']:+.2f})")
