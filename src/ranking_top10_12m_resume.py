"""Retoma o backtest top10/12m a partir de ADA/PSAR + JUP completo.
Combina com os 46 runs já finalizados (parseados do run.log)."""
import os
import sys
import io
import time
import re
import json
import importlib
import inspect
from pathlib import Path

import pandas as pd
from binance.client import Client

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Importa definições do script original
from src.ranking_top10_12m import (
    ASSETS, INTERVAL, HISTORY_START, INITIAL_BALANCE, TOP10,
    fetch_klines, load_top10_strategies, run_detailed_backtest,
)

OUT_DIR = Path("src/logs/ranking")
PREV_LOG = OUT_DIR / "top10_12m_run.log"

# Runs remanescentes
REMAINING = [
    ("ADAUSDT", "X_PSAR"),
    ("ADAUSDT", "X_FisherTransform"),
    ("ADAUSDT", "X_ChaikinOscillator"),
    ("ADAUSDT", "X_Cmf"),
] + [("JUPUSDT", s) for s in TOP10]


def parse_prev_log():
    """Extrai métricas já completadas do log anterior."""
    results = []
    pat = re.compile(
        r"\[(\d+)/60\]\s+([A-Z]+USDT)/(\w+):\s+([+-][0-9.]+)%\s+\(\$([+-][0-9.]+)\)\s+\|\s+(\d+)\s+trades\s+\|\s+WR\s+([0-9.]+)%\s+\|\s+DD\s+([+-]?[0-9.]+)%"
    )
    with open(PREV_LOG, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = pat.search(line)
            if not m:
                continue
            idx, asset, strat, ppct, pusd, trades, wr, dd = m.groups()
            results.append({
                "asset": asset,
                "strategy": strat,
                "profit_pct": float(ppct),
                "profit_usd": float(pusd),
                "total_trades": int(trades),
                "winrate_pct": float(wr),
                "max_drawdown_pct": float(dd),
                "final_balance": round(INITIAL_BALANCE + float(pusd), 2),
                "from_previous_run": True,
            })
    return results


def main():
    prev = parse_prev_log()
    print(f"📂 Runs anteriores recuperados do log: {len(prev)}")

    api = os.getenv("BINANCE_API_KEY", "")
    sec = os.getenv("BINANCE_SECRET_KEY", "")
    client = Client(api if "Cole" not in api else "", sec if "Cole" not in sec else "")

    needed_assets = list({a for a, _ in REMAINING})
    print(f"📥 Baixando 12m candles 1h para {needed_assets}")
    data = {}
    for sym in needed_assets:
        t0 = time.time()
        df = fetch_klines(client, sym, INTERVAL, HISTORY_START)
        data[sym] = df
        print(f"  ✅ {sym}: {len(df)} candles  ({time.time()-t0:.1f}s)")

    strategies = load_top10_strategies()
    strat_map = {name: fn for name, fn in strategies}

    new_results = []
    t_total = time.time()
    for i, (asset, strat_name) in enumerate(REMAINING, 1):
        if strat_name not in strat_map:
            print(f"  [SKIP] {asset}/{strat_name} (estratégia não encontrada)")
            continue
        fn = strat_map[strat_name]
        t0 = time.time()
        res = run_detailed_backtest(data[asset], fn, INITIAL_BALANCE)
        dt = time.time() - t0
        res.update({"asset": asset, "strategy": strat_name, "seconds": round(dt, 1)})
        new_results.append(res)
        if "error" in res:
            print(f"  [{i:02d}/{len(REMAINING)}] {asset}/{strat_name}: ERRO {res['error']}  {dt:.1f}s")
        else:
            print(f"  [{i:02d}/{len(REMAINING)}] {asset}/{strat_name}: "
                  f"{res['profit_pct']:+.2f}% (${res['profit_usd']:+.2f}) "
                  f"| {res['total_trades']} trades "
                  f"| WR {res['winrate_pct']:.1f}% "
                  f"| DD {res['max_drawdown_pct']:.1f}%  "
                  f"{dt:.1f}s")

    print(f"\n⏱️ Tempo retomada: {(time.time()-t_total)/60:.1f} min")

    all_results = prev + new_results
    print(f"📊 Total consolidado: {len(all_results)} runs")

    df_res = pd.DataFrame(all_results)
    df_res.to_csv(OUT_DIR / "top10_12m_detailed.csv", index=False, encoding="utf-8")
    with open(OUT_DIR / "top10_12m_detailed.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)

    ok = df_res[df_res.get("error", pd.Series([None]*len(df_res))).isna()].copy() if "error" in df_res else df_res.copy()

    agg = ok.groupby("strategy").agg(
        total_profit_usd=("profit_usd", "sum"),
        avg_profit_pct=("profit_pct", "mean"),
        median_profit_pct=("profit_pct", "median"),
        total_trades=("total_trades", "sum"),
        avg_winrate=("winrate_pct", "mean"),
        avg_drawdown=("max_drawdown_pct", "mean"),
        assets_positive=("profit_pct", lambda s: (s > 0).sum()),
    ).round(2).sort_values("avg_profit_pct", ascending=False)

    print("\n" + "=" * 110)
    print("🏆 RANKING TOP 10 — 12 MESES (banca $1000 por asset)")
    print("=" * 110)
    print(agg.to_string())
    agg.to_csv(OUT_DIR / "top10_12m_ranking.csv", encoding="utf-8")

    print("\n" + "=" * 110)
    print("📋 DETALHE ATIVO × ESTRATÉGIA (profit %)")
    print("=" * 110)
    try:
        pivot_profit = ok.pivot(index="strategy", columns="asset", values="profit_pct").round(2)
        print(pivot_profit.to_string())
    except Exception as e:
        print(f"(pivot falhou: {e})")

    print("\n💾 Arquivos salvos em src/logs/ranking/:")
    print("   - top10_12m_detailed.csv / .json")
    print("   - top10_12m_ranking.csv")


if __name__ == "__main__":
    main()
