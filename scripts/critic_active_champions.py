"""Critic v4 nos Champions args ATIVOS (que estao em src/main.py).

Aplica 6 gates de robustez sobre cada (ativo, params) que o bot usa em
producao. Nao depende do DB do Aether — opera direto sobre fast_run_backtest.

Gates (cada um devolve pass/fail + metrica):
  G1 cross_symbol    : roda mesmos params nos outros 4 ativos
                       (>= 2/4 com profit > -3%)
  G2 perturbation    : +/-10% nos params float; mediana(profit) > 0
  G3 walk_forward    : 6 folds contiguos no historico 1h;
                       >= 4/6 com profit > -3%
  G4 grid_dense      : 5x5 (take_profit_pct x stop_loss_pct) ao redor
                       dos params; >= 50% combos positivos
  G5 multi_timeframe : mesmos params em 5m,15m,4h,1d;
                       >= 2/4 com profit > -3%
  G6 bootstrap_mc    : 1000 reamostragens dos trades;
                       sharpe percentil 5 >= -0.3

Saida:
  src/logs/agents/critic_active_validation.json

Uso:
  python -u scripts/critic_active_champions.py
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from statistics import median, stdev

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from binance.client import Client  # noqa: E402

from src.backtest_champions_strategy import prepare  # noqa: E402
from src.optimize_champions_by_asset import fast_run_backtest  # noqa: E402
from scripts.optimize_champions_3000 import fetch_paginated  # noqa: E402

# Champions args ATIVOS (cole espelhando src/main.py).
# Se main.py mudar, atualize aqui.
ACTIVE_CHAMPIONS = {
    "BTCUSDT": {
        "take_profit_pct": 99.0, "stop_loss_pct": 2.0,
        "rsi_min": 46.0, "rsi_max": 72.0,
        "pullback_pct": 0.8, "max_hold_bars": 48,
    },
    "ETHUSDT": {
        "take_profit_pct": 99.0, "stop_loss_pct": 2.0,
        "rsi_min": 46.0, "rsi_max": 60.0,
        "pullback_pct": 0.5, "max_hold_bars": 48,
    },
    "SOLUSDT": {
        "take_profit_pct": 99.0, "stop_loss_pct": 1.5,
        "rsi_min": 38.0, "rsi_max": 64.0,
        "pullback_pct": 0.5, "max_hold_bars": 48,
    },
    "XRPUSDT": {
        "take_profit_pct": 99.0, "stop_loss_pct": 1.5,
        "rsi_min": 40.0, "rsi_max": 64.0,
        "pullback_pct": 0.7, "max_hold_bars": 48,
    },
    "ADAUSDT": {
        "take_profit_pct": 99.0, "stop_loss_pct": 1.0,
        "rsi_min": 46.0, "rsi_max": 60.0,
        "pullback_pct": 0.5, "max_hold_bars": 48,
    },
}

# Thresholds dos gates
G1_CROSS_MIN_PASS = 2          # de 4 alts
G1_CROSS_MIN_PROFIT = -3.0
G2_PERTURB_PCT = 0.10
G3_WF_FOLDS = 6
G3_WF_MIN_PASS = 4
G3_WF_MIN_PROFIT = -3.0
G4_GRID_SIZE = 5
G4_GRID_RANGE_PCT = 0.20
G4_GRID_MIN_POSITIVE_FRAC = 0.50
G5_MTF_TIMEFRAMES = ("5m", "15m", "4h", "1d")
G5_MTF_BARS = {"5m": 8640, "15m": 17280, "4h": 4380, "1d": 1095}
G5_MTF_MIN_PASS = 2
G5_MTF_MIN_PROFIT = -3.0
G6_BOOT_SAMPLES = 1000
G6_BOOT_MIN_SHARPE_P5 = -0.3

# Total minimo de gates pra aprovar
APPROVAL_MIN_GATES = 4

# ---------- helpers ----------
def _profit_pct(result: dict) -> float:
    return float(result.get("profit_pct") or result.get("profit_usd", 0.0) / 10.0)


def _trades(result: dict) -> list[float]:
    return list(result.get("trades", []) or [])


def _safe_backtest(df: pd.DataFrame, args: dict) -> dict:
    try:
        return fast_run_backtest(df, **args)
    except Exception as e:
        return {"profit_pct": -100.0, "trades": [], "error": str(e)}


# ---------- G1: cross-symbol ----------
def gate_cross_symbol(asset: str, args: dict, candles_1h: dict[str, pd.DataFrame]) -> dict:
    alts = [a for a in ACTIVE_CHAMPIONS if a != asset]
    results = {}
    for alt in alts:
        df_alt = candles_1h.get(alt)
        if df_alt is None or len(df_alt) < 250:
            results[alt] = None
            continue
        r = _safe_backtest(df_alt, args)
        results[alt] = round(_profit_pct(r), 2)
    passed = sum(
        1 for v in results.values()
        if v is not None and v >= G1_CROSS_MIN_PROFIT
    )
    return {
        "results": results,
        "passed": passed,
        "min_pass": G1_CROSS_MIN_PASS,
        "min_profit_pct": G1_CROSS_MIN_PROFIT,
        "ok": passed >= G1_CROSS_MIN_PASS,
    }


# ---------- G2: perturbation ----------
def _perturb(args: dict) -> list[dict]:
    out = []
    float_keys = ["take_profit_pct", "stop_loss_pct", "rsi_min", "rsi_max", "pullback_pct"]
    for k in float_keys:
        for delta in (-G2_PERTURB_PCT, +G2_PERTURB_PCT):
            new = dict(args)
            new[k] = round(args[k] * (1 + delta), 4)
            out.append(new)
    return out


def gate_perturbation(asset: str, args: dict, df: pd.DataFrame) -> dict:
    perturbed = _perturb(args)
    profits = []
    for pp in perturbed:
        r = _safe_backtest(df, pp)
        profits.append(_profit_pct(r))
    med = round(median(profits), 2) if profits else 0.0
    return {
        "n_perturbed": len(profits),
        "median_profit_pct": med,
        "min_profit_pct": round(min(profits), 2) if profits else 0.0,
        "max_profit_pct": round(max(profits), 2) if profits else 0.0,
        "ok": med > 0,
    }


# ---------- G3: walk-forward K-fold ----------
def gate_walk_forward(asset: str, args: dict, df: pd.DataFrame) -> dict:
    n = len(df)
    fold_size = n // G3_WF_FOLDS
    fold_profits: list[float] = []
    for k in range(G3_WF_FOLDS):
        start = k * fold_size
        end = (k + 1) * fold_size if k < G3_WF_FOLDS - 1 else n
        sub = df.iloc[start:end].reset_index(drop=True)
        if len(sub) < 250:
            fold_profits.append(-100.0)
            continue
        r = _safe_backtest(sub, args)
        fold_profits.append(round(_profit_pct(r), 2))
    passed = sum(1 for p in fold_profits if p >= G3_WF_MIN_PROFIT)
    return {
        "fold_profits_pct": fold_profits,
        "passed": passed,
        "min_pass": G3_WF_MIN_PASS,
        "min_profit_pct": G3_WF_MIN_PROFIT,
        "ok": passed >= G3_WF_MIN_PASS,
    }


# ---------- G4: grid denso (TP x SL) ----------
def gate_grid_dense(asset: str, args: dict, df: pd.DataFrame) -> dict:
    tp_center = args["take_profit_pct"]
    sl_center = args["stop_loss_pct"]
    tp_grid = np.linspace(
        tp_center * (1 - G4_GRID_RANGE_PCT),
        tp_center * (1 + G4_GRID_RANGE_PCT),
        G4_GRID_SIZE,
    )
    sl_grid = np.linspace(
        sl_center * (1 - G4_GRID_RANGE_PCT),
        sl_center * (1 + G4_GRID_RANGE_PCT),
        G4_GRID_SIZE,
    )
    profits = []
    for tp, sl in itertools.product(tp_grid, sl_grid):
        new = dict(args)
        new["take_profit_pct"] = round(float(tp), 3)
        new["stop_loss_pct"] = round(float(sl), 3)
        r = _safe_backtest(df, new)
        profits.append(_profit_pct(r))
    pos_frac = sum(1 for p in profits if p > 0) / len(profits) if profits else 0.0
    return {
        "n_combos": len(profits),
        "positive_frac": round(pos_frac, 3),
        "p25_profit_pct": round(float(np.percentile(profits, 25)), 2) if profits else 0.0,
        "median_profit_pct": round(float(np.median(profits)), 2) if profits else 0.0,
        "min_positive_frac": G4_GRID_MIN_POSITIVE_FRAC,
        "ok": pos_frac >= G4_GRID_MIN_POSITIVE_FRAC,
    }


# ---------- G5: multi-timeframe ----------
def gate_multi_timeframe(asset: str, args: dict, client: Client) -> dict:
    interval_map = {
        "5m": Client.KLINE_INTERVAL_5MINUTE,
        "15m": Client.KLINE_INTERVAL_15MINUTE,
        "4h": Client.KLINE_INTERVAL_4HOUR,
        "1d": Client.KLINE_INTERVAL_1DAY,
    }
    profits = {}
    for tf in G5_MTF_TIMEFRAMES:
        total = G5_MTF_BARS[tf]
        try:
            raw = fetch_paginated(client, asset, total=total)
            # fetch_paginated usa interval=KLINE_INTERVAL_1HOUR fixo; reimplementamos minimo.
            raw = _fetch_tf(client, asset, total, interval_map[tf])
            df = prepare(raw)
            if len(df) < 250:
                profits[tf] = None
                continue
            r = _safe_backtest(df, args)
            profits[tf] = round(_profit_pct(r), 2)
        except Exception as e:
            profits[tf] = None
    passed = sum(1 for v in profits.values() if v is not None and v >= G5_MTF_MIN_PROFIT)
    return {
        "profits_pct_by_tf": profits,
        "passed": passed,
        "min_pass": G5_MTF_MIN_PASS,
        "min_profit_pct": G5_MTF_MIN_PROFIT,
        "ok": passed >= G5_MTF_MIN_PASS,
    }


def _fetch_tf(client: Client, symbol: str, total: int, interval) -> pd.DataFrame:
    cols = ["open_time", "open_price", "high_price", "low_price", "close_price", "volume",
            "close_time", "qav", "trades", "tbbav", "tbqav", "_"]
    chunks = []
    end_time = None
    remaining = total
    while remaining > 0:
        limit = min(1000, remaining)
        params = {"symbol": symbol, "interval": interval, "limit": limit}
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


# ---------- G6: bootstrap MC ----------
def gate_bootstrap(asset: str, args: dict, df: pd.DataFrame) -> dict:
    r = _safe_backtest(df, args)
    trades = _trades(r)
    if len(trades) < 10:
        return {
            "n_trades": len(trades),
            "sharpe_p5": None,
            "min_sharpe_p5": G6_BOOT_MIN_SHARPE_P5,
            "ok": False,
            "reason": "trades<10",
        }
    arr = np.array(trades, dtype=float)
    rng = np.random.default_rng(42)
    sharpes = []
    for _ in range(G6_BOOT_SAMPLES):
        sample = rng.choice(arr, size=len(arr), replace=True)
        mu = float(sample.mean())
        sd = float(sample.std(ddof=1)) if len(sample) > 1 else 0.0
        if sd == 0:
            sharpes.append(0.0)
        else:
            sharpes.append(mu / sd)
    sharpes = np.array(sharpes)
    p5 = float(np.percentile(sharpes, 5))
    pos_frac = float((sharpes > 0).mean())
    return {
        "n_trades": len(trades),
        "n_samples": G6_BOOT_SAMPLES,
        "sharpe_p5": round(p5, 3),
        "sharpe_median": round(float(np.median(sharpes)), 3),
        "sharpe_positive_frac": round(pos_frac, 3),
        "min_sharpe_p5": G6_BOOT_MIN_SHARPE_P5,
        "ok": p5 >= G6_BOOT_MIN_SHARPE_P5,
    }


# ---------- main ----------
def main() -> int:
    api_key = (os.getenv("BINANCE_API_KEY", "") or "").strip('"')
    api_secret = (os.getenv("BINANCE_SECRET_KEY", "") or os.getenv("BINANCE_API_SECRET", "") or "").strip('"')
    client = Client(api_key, api_secret)

    print("[critic] carregando candles 1h dos 5 ativos...", flush=True)
    candles_1h: dict[str, pd.DataFrame] = {}
    for asset in ACTIVE_CHAMPIONS:
        raw = fetch_paginated(client, asset, total=17520)
        df = prepare(raw)
        candles_1h[asset] = df
        print(f"  {asset}: {len(df)} candles", flush=True)

    report: dict = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "approval_min_gates": APPROVAL_MIN_GATES,
        "active_params": ACTIVE_CHAMPIONS,
        "assets": {},
    }

    for asset, args in ACTIVE_CHAMPIONS.items():
        df = candles_1h[asset]
        print(f"\n>>> {asset}", flush=True)

        gates = {}
        for name, fn in [
            ("G1_cross_symbol", lambda: gate_cross_symbol(asset, args, candles_1h)),
            ("G2_perturbation", lambda: gate_perturbation(asset, args, df)),
            ("G3_walk_forward", lambda: gate_walk_forward(asset, args, df)),
            ("G4_grid_dense", lambda: gate_grid_dense(asset, args, df)),
            ("G5_multi_timeframe", lambda: gate_multi_timeframe(asset, args, client)),
            ("G6_bootstrap_mc", lambda: gate_bootstrap(asset, args, df)),
        ]:
            try:
                gates[name] = fn()
            except Exception as e:
                gates[name] = {"ok": False, "error": str(e)}
            flag = "PASS" if gates[name].get("ok") else "FAIL"
            print(f"   [{flag}] {name}", flush=True)

        passed = sum(1 for g in gates.values() if g.get("ok"))
        approved = passed >= APPROVAL_MIN_GATES
        report["assets"][asset] = {
            "gates": gates,
            "passed": passed,
            "total": 6,
            "approved": approved,
        }
        print(f"   -> {passed}/6 gates | {'APROVADO' if approved else 'REPROVADO'}", flush=True)

    out = ROOT / "src/logs/agents/critic_active_validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"VEREDITO CRITIC v4 (min {APPROVAL_MIN_GATES}/6 gates pra aprovar)")
    for asset, data in report["assets"].items():
        flag = "APROVADO" if data["approved"] else "REPROVADO"
        gates_str = " ".join(
            ("+" if g.get("ok") else "-") + g_name.split("_", 1)[0]
            for g_name, g in data["gates"].items()
        )
        print(f"  [{flag:9}] {asset}: {data['passed']}/6 | {gates_str}")
    print(f"\nrelatorio: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
