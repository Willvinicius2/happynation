"""Walk-forward / out-of-sample para ChampionsStrategy por ativo.

Divide o historico em janelas de IS (in-sample) + OOS (out-of-sample).
Para cada janela:
  1. otimiza o grid em IS e escolhe o melhor combo que passa Juiz
  2. roda o melhor combo na janela OOS imediatamente seguinte
  3. agrega metricas OOS (degradacao vs IS = sinal de overfit)

Saida:
  src/logs/agents/walk_forward_champions.json

Uso:
  python -u scripts/walk_forward_champions.py
  python -u scripts/walk_forward_champions.py --total 17520 --is-window 1500 --oos-window 500 --step 500
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest_champions_strategy import prepare  # noqa: E402
from src.optimize_champions_by_asset import fast_run_backtest  # noqa: E402

# Reusa fetch_paginated, profit_factor, passes_juiz, score do otimizador 3000:
from scripts.optimize_champions_3000 import (  # noqa: E402
    fetch_paginated,
    profit_factor,
    passes_juiz,
    score,
)

import pandas as pd  # noqa: E402
from binance.client import Client  # noqa: E402


ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"]

PARAM_GRID = {
    "take_profit_pct": [0.6, 0.8, 1.0, 1.2, 1.5],
    "stop_loss_pct": [1.0, 1.5, 2.0, 2.5],
    "rsi_min": [38.0, 42.0, 46.0],
    "rsi_max": [60.0, 64.0, 68.0, 72.0],
    "pullback_pct": [0.5, 0.8, 1.2],
    "max_hold_bars": [12, 18, 24, 36],
}


_BACKTEST_CACHE: dict[tuple, dict] = {}


def _cached_backtest(df: pd.DataFrame, df_id: int, params: dict) -> dict:
    """Cache por (id(df), tupla de params). df_id eh id() do DataFrame; reutiliza
    resultado quando a mesma janela aparece em multiplas iteracoes."""
    key = (df_id, tuple(sorted(params.items())))
    if key in _BACKTEST_CACHE:
        return _BACKTEST_CACHE[key]
    result = fast_run_backtest(df, **params)
    _BACKTEST_CACHE[key] = result
    return result


def best_in_sample(df_is: pd.DataFrame):
    """Roda grid em IS, retorna (params, result) que passa Juiz com maior score.
    Se nenhum passa, retorna o melhor overall.

    Usa cache: muitos params resultam no mesmo backtest no mesmo df.
    """
    keys = list(PARAM_GRID.keys())
    combos = list(itertools.product(*(PARAM_GRID[k] for k in keys)))
    best_pass = None
    best_pass_s = -1e9
    best_overall = None
    best_overall_s = -1e9
    df_id = id(df_is)

    for values in combos:
        params = dict(zip(keys, values))
        result = _cached_backtest(df_is, df_id, params)
        s = score(result)
        if s > best_overall_s:
            best_overall_s = s
            best_overall = (params, result)
        ok, _ = passes_juiz(result)
        if ok and s > best_pass_s:
            best_pass_s = s
            best_pass = (params, result)

    return best_pass or best_overall, best_pass is not None


def metrics_summary(result: dict) -> dict:
    return {
        "trades": int(result.get("total_trades", 0) or 0),
        "winrate_pct": round(float(result.get("winrate_pct", 0.0) or 0.0), 2),
        "profit_pct": round(float(result.get("profit_pct", 0.0) or 0.0), 2),
        "max_dd_pct": round(abs(float(result.get("max_drawdown_pct", 0.0) or 0.0)), 2),
        "profit_factor": round(profit_factor(result), 3),
    }


def walk_forward(df: pd.DataFrame, is_window: int, oos_window: int, step: int) -> list[dict]:
    """Gera janelas IS+OOS deslizantes. Cada item retorna metricas IS e OOS."""
    windows = []
    n = len(df)
    start = 0
    while start + is_window + oos_window <= n:
        df_is = df.iloc[start : start + is_window].reset_index(drop=True)
        df_oos = df.iloc[start + is_window : start + is_window + oos_window].reset_index(drop=True)
        (params, is_result), passed = best_in_sample(df_is)
        oos_result = fast_run_backtest(df_oos, **params)

        windows.append({
            "is_start_index": start,
            "is_end_index": start + is_window,
            "oos_end_index": start + is_window + oos_window,
            "is_passed_juiz": passed,
            "params": params,
            "in_sample": metrics_summary(is_result),
            "out_of_sample": metrics_summary(oos_result),
            "oos_passes_juiz": passes_juiz(oos_result)[0],
        })
        start += step
    return windows


def aggregate(windows: list[dict]) -> dict:
    if not windows:
        return {"windows": 0}
    is_pf = [w["in_sample"]["profit_factor"] for w in windows]
    oos_pf = [w["out_of_sample"]["profit_factor"] for w in windows]
    is_profit = [w["in_sample"]["profit_pct"] for w in windows]
    oos_profit = [w["out_of_sample"]["profit_pct"] for w in windows]
    oos_pass = sum(1 for w in windows if w["oos_passes_juiz"])

    def _avg(xs):
        finite = [x for x in xs if x not in (float("inf"), float("-inf"))]
        return round(sum(finite) / len(finite), 3) if finite else None

    return {
        "windows": len(windows),
        "oos_pass_rate_pct": round(oos_pass / len(windows) * 100, 1),
        "avg_is_profit_pct": _avg(is_profit),
        "avg_oos_profit_pct": _avg(oos_profit),
        "avg_is_pf": _avg(is_pf),
        "avg_oos_pf": _avg(oos_pf),
        "degradation_pf_pct": (
            round((1 - (_avg(oos_pf) / _avg(is_pf))) * 100, 1)
            if _avg(is_pf) and _avg(is_pf) > 0 and _avg(oos_pf) is not None
            else None
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--total", type=int, default=17520, help="Total de candles 1h (default 17520 ~= 2 anos)")
    ap.add_argument("--is-window", type=int, default=1500, dest="is_window", help="Tamanho da janela IS")
    ap.add_argument("--oos-window", type=int, default=500, dest="oos_window", help="Tamanho da janela OOS")
    ap.add_argument("--step", type=int, default=500, help="Passo entre janelas")
    args = ap.parse_args()

    api_key = (os.getenv("BINANCE_API_KEY", "") or "").strip('"')
    api_secret = (os.getenv("BINANCE_SECRET_KEY", "") or "").strip('"')
    client = Client(api_key, api_secret)

    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": {
            "total": args.total,
            "is_window": args.is_window,
            "oos_window": args.oos_window,
            "step": args.step,
        },
        "assets": {},
    }

    for asset in ASSETS:
        print(f"\n>>> {asset}", flush=True)
        df = prepare(fetch_paginated(client, asset, total=args.total))
        if len(df) < args.is_window + args.oos_window:
            print(f"   candles insuficientes: {len(df)}", flush=True)
            report["assets"][asset] = {"error": "candles insuficientes", "candles": len(df)}
            continue
        print(f"   {len(df)} candles, walk-forward IS={args.is_window} OOS={args.oos_window} step={args.step}", flush=True)
        windows = walk_forward(df, args.is_window, args.oos_window, args.step)
        agg = aggregate(windows)
        print(
            f"   janelas={agg['windows']} | OOS pass={agg['oos_pass_rate_pct']}% | "
            f"avg IS profit={agg['avg_is_profit_pct']}% | avg OOS profit={agg['avg_oos_profit_pct']}% | "
            f"degrad PF={agg['degradation_pf_pct']}%",
            flush=True,
        )
        report["assets"][asset] = {"aggregate": agg, "windows": windows}

    # === Veredito formal por ativo (criterio OOS, nao IS) ===
    # Param e considerado APROVADO se sobrevive nas janelas nao vistas:
    #   - OOS pass rate >= OOS_PASS_THRESHOLD_PCT (% janelas em que o melhor
    #     param da IS passou Juiz tambem na OOS subsequente)
    #   - degradacao IS->OOS PF tolerada ate MAX_DEGRADATION_PCT
    OOS_PASS_THRESHOLD_PCT = float(os.getenv("WF_VERDICT_OOS_PASS_PCT", "30"))
    MAX_DEGRADATION_PCT = float(os.getenv("WF_VERDICT_MAX_DEGRAD_PCT", "40"))
    verdicts: dict[str, dict] = {}
    for asset, data in report["assets"].items():
        if "error" in data:
            verdicts[asset] = {"approved": False, "reason": data["error"]}
            continue
        a = data["aggregate"]
        oos_pass = float(a["oos_pass_rate_pct"] or 0)
        degrad = a["degradation_pf_pct"]
        reasons: list[str] = []
        if oos_pass < OOS_PASS_THRESHOLD_PCT:
            reasons.append(f"OOS pass {oos_pass}% < {OOS_PASS_THRESHOLD_PCT}%")
        if degrad is not None and degrad > MAX_DEGRADATION_PCT:
            reasons.append(f"degrad PF {degrad}% > {MAX_DEGRADATION_PCT}%")
        verdicts[asset] = {
            "approved": not reasons,
            "oos_pass_rate_pct": oos_pass,
            "degradation_pf_pct": degrad,
            "reason": "; ".join(reasons) if reasons else "passou criterio OOS",
        }
    report["verdict_criteria"] = {
        "oos_pass_threshold_pct": OOS_PASS_THRESHOLD_PCT,
        "max_degradation_pct": MAX_DEGRADATION_PCT,
    }
    report["verdict"] = verdicts

    out = ROOT / "src/logs/agents/walk_forward_champions.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print(
        f"VEREDITO WALK-FORWARD (criterio OOS pass>={OOS_PASS_THRESHOLD_PCT}%, "
        f"degrad PF<={MAX_DEGRADATION_PCT}%)"
    )
    approved = [a for a, v in verdicts.items() if v["approved"]]
    rejected = [a for a, v in verdicts.items() if not v["approved"]]
    for asset, v in verdicts.items():
        flag = "APROVADO" if v["approved"] else "REPROVADO"
        print(f"  [{flag:9}] {asset}: {v['reason']}")
    print(f"\n  -> {len(approved)} aprovados, {len(rejected)} reprovados")
    if rejected:
        print(f"  -> reprovados: {', '.join(rejected)}")
    print(f"\nrelatorio: {out}")


if __name__ == "__main__":
    main()
