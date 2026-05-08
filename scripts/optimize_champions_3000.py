"""Otimiza ChampionsStrategy por ativo usando 3000 candles (4 meses) e
escolhe a melhor combinacao que passa nos criterios do Juiz.

Saida:
  - imprime tabela com melhor combo passando Juiz por ativo
  - escreve src/logs/agents/optimize_3000.json
"""

from __future__ import annotations

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

import pandas as pd  # noqa: E402
from binance.client import Client  # noqa: E402


ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"]

# Grid expandido para encontrar combos robustos com 3000 candles
PARAM_GRID = {
    "take_profit_pct": [0.6, 0.8, 1.0, 1.2, 1.5],
    "stop_loss_pct": [1.0, 1.5, 2.0, 2.5],
    "rsi_min": [38.0, 42.0, 46.0],
    "rsi_max": [60.0, 64.0, 68.0, 72.0],
    "pullback_pct": [0.5, 0.8, 1.2],
    "max_hold_bars": [12, 18, 24, 36],
}

MIN_TRADES = 20
MIN_PF = 1.15
MAX_DD_PCT = 25.0


def fetch_paginated(client, symbol, total=3000):
    cols = ["open_time", "open_price", "high_price", "low_price", "close_price", "volume",
            "close_time", "qav", "trades", "tbbav", "tbqav", "_"]
    chunks = []
    end_time = None
    remaining = total
    while remaining > 0:
        limit = min(1000, remaining)
        params = {"symbol": symbol, "interval": Client.KLINE_INTERVAL_1HOUR, "limit": limit}
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


def profit_factor(result):
    avg_win = float(result.get("avg_win_pct", 0.0) or 0.0)
    avg_loss = abs(float(result.get("avg_loss_pct", 0.0) or 0.0))
    wins = int(result.get("wins", 0) or 0)
    losses = int(result.get("losses", 0) or 0)
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    gross_profit = avg_win * wins
    gross_loss = avg_loss * losses
    if gross_loss <= 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def passes_juiz(result):
    if result["total_trades"] < MIN_TRADES:
        return False, "trades insuficientes"
    pf = profit_factor(result)
    if pf < MIN_PF:
        return False, f"PF {pf:.2f} < {MIN_PF}"
    if abs(result["max_drawdown_pct"]) > MAX_DD_PCT:
        return False, f"DD {abs(result['max_drawdown_pct']):.1f}% > {MAX_DD_PCT}%"
    return True, "OK"


def score(result):
    """Score final para escolher melhor combo entre os que passam o Juiz."""
    pf = profit_factor(result)
    pf_capped = min(pf, 5.0)
    return (
        pf_capped * 25  # premia PF até 5x
        + result["winrate_pct"] * 0.5
        + min(result["total_trades"], 80) * 0.2
        - abs(result["max_drawdown_pct"]) * 1.0
    )


def optimize(df, asset):
    keys = list(PARAM_GRID.keys())
    combos = list(itertools.product(*(PARAM_GRID[k] for k in keys)))
    best_passing = None
    best_passing_score = -1e9
    best_overall = None
    best_overall_score = -1e9

    for values in combos:
        params = dict(zip(keys, values))
        result = fast_run_backtest(df, **params)
        s = score(result)
        if s > best_overall_score:
            best_overall_score = s
            best_overall = (params, result)

        ok, reason = passes_juiz(result)
        if ok and s > best_passing_score:
            best_passing_score = s
            best_passing = (params, result)

    return best_passing, best_overall


def main():
    api_key = os.getenv("BINANCE_API_KEY", "") or ""
    api_secret = os.getenv("BINANCE_SECRET_KEY", "") or ""
    client = Client(api_key.strip('"'), api_secret.strip('"'))

    rows = []
    for asset in ASSETS:
        print(f"\n>>> {asset}", flush=True)
        df = prepare(fetch_paginated(client, asset, total=3000))
        print(f"   {len(df)} candles, testando {len(list(itertools.product(*PARAM_GRID.values())))} combos...", flush=True)
        passing, overall = optimize(df, asset)

        passing_params, passing_result = passing if passing else (None, None)
        overall_params, overall_result = overall

        if passing_result:
            pf = profit_factor(passing_result)
            print(
                f"   PASSA JUIZ: trades={passing_result['total_trades']} "
                f"WR={passing_result['winrate_pct']:.1f}% PF={pf:.2f} "
                f"DD={abs(passing_result['max_drawdown_pct']):.1f}% "
                f"profit={passing_result['profit_pct']:+.1f}%",
                flush=True,
            )
            print(f"   params: {passing_params}", flush=True)
        else:
            pf = profit_factor(overall_result)
            print(
                f"   NENHUM combo passa Juiz. Melhor overall: trades={overall_result['total_trades']} "
                f"WR={overall_result['winrate_pct']:.1f}% PF={pf:.2f} "
                f"DD={abs(overall_result['max_drawdown_pct']):.1f}%",
                flush=True,
            )

        rows.append({
            "asset": asset,
            "passes_juiz": passing is not None,
            "passing_params": passing_params,
            "passing_metrics": {
                **(passing_result or {}),
                "profit_factor": round(profit_factor(passing_result), 3) if passing_result else None,
            },
            "overall_best_params": overall_params,
            "overall_best_metrics": {
                **overall_result,
                "profit_factor": round(profit_factor(overall_result), 3),
            },
        })

    out = ROOT / "src/logs/agents/optimize_3000.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "criteria": {"min_trades": MIN_TRADES, "min_pf": MIN_PF, "max_dd_pct": MAX_DD_PCT},
        "results": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=" * 60)
    print("RESUMO")
    for r in rows:
        flag = "✓" if r["passes_juiz"] else "✗"
        print(f"  {flag} {r['asset']}")
    print(f"\nrelatorio: {out}")


if __name__ == "__main__":
    main()
