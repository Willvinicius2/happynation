"""
Testa top 1 E top 2 estratégias por ativo em 90 dias.
Verifica se vale rodar 2 estratégias em paralelo no mesmo ativo.
"""
import os
import sys
import time
from pathlib import Path

import pandas as pd
from binance.client import Client

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.ranking_top10_12m import fetch_klines, run_detailed_backtest
from src.strategies.extras.atr_strategy import getATRTradeStrategy
from src.strategies.extras.tema_strategy import getTEMATradeStrategy
from src.strategies.extras.detrended_price_oscillator_strategy import getDetrendedPriceOscillatorTradeStrategy
from src.strategies.extras.accelerator_oscillator_strategy import getAcceleratorOscillatorTradeStrategy
from src.strategies.extras.ultimate_oscillator_strategy import getUltimateOscillatorTradeStrategy
from src.strategies.extras.hull_moving_average_strategy import getHullMovingAverageTradeStrategy

# Top 2 estratégias por ativo (baseado no pivot 12m)
# BTC: #1 X_ATR +26.34% | #2 X_UltimateOscillator +22.89%
# ETH: #1 X_TEMA +180.74% | #2 X_DetrendedPrice +171.86%
# SOL: #1 X_ATR +47.04% | #2 X_HullMovingAverage +15.45%
# XRP: #1 X_DetrendedPrice +76.25% | #2 X_AcceleratorOscillator +76.10%
# ADA: #1 X_AcceleratorOscillator +55.26% | #2 X_TEMA +21.60%

PAIRS = [
    ("BTCUSDT", "X_ATR",                      getATRTradeStrategy),
    ("BTCUSDT", "X_UltimateOscillator",       getUltimateOscillatorTradeStrategy),
    ("ETHUSDT", "X_TEMA",                     getTEMATradeStrategy),
    ("ETHUSDT", "X_DetrendedPriceOscillator", getDetrendedPriceOscillatorTradeStrategy),
    ("SOLUSDT", "X_ATR",                      getATRTradeStrategy),
    ("SOLUSDT", "X_HullMovingAverage",        getHullMovingAverageTradeStrategy),
    ("XRPUSDT", "X_DetrendedPriceOscillator", getDetrendedPriceOscillatorTradeStrategy),
    ("XRPUSDT", "X_AcceleratorOscillator",    getAcceleratorOscillatorTradeStrategy),
    ("ADAUSDT", "X_AcceleratorOscillator",    getAcceleratorOscillatorTradeStrategy),
    ("ADAUSDT", "X_TEMA",                     getTEMATradeStrategy),
]
INITIAL = 1000.0
HISTORY = "90 days ago UTC"
INTERVAL = Client.KLINE_INTERVAL_1HOUR
OUT = Path("src/logs/ranking")


def main():
    api = os.getenv("BINANCE_API_KEY", "")
    sec = os.getenv("BINANCE_SECRET_KEY", "")
    client = Client(api if "Cole" not in api else "", sec if "Cole" not in sec else "")

    # Baixa dados uma vez por ativo
    assets = list({p[0] for p in PAIRS})
    print(f"📥 Baixando 90d candles 1h para {len(assets)} ativos...")
    data = {}
    for a in assets:
        t0 = time.time()
        data[a] = fetch_klines(client, a, INTERVAL, HISTORY)
        # Adapta colunas pras estratégias extras
        for src, dst in [("close_price","close"),("high_price","high"),("low_price","low"),("open_price","open")]:
            if dst not in data[a].columns:
                data[a][dst] = data[a][src]
        print(f"  ✅ {a}: {len(data[a])} candles ({time.time()-t0:.1f}s)")

    print()
    results = []
    for asset, strat_name, fn in PAIRS:
        t0 = time.time()
        res = run_detailed_backtest(data[asset], fn, INITIAL)
        dt = time.time() - t0
        res.update({"asset": asset, "strategy": strat_name})
        results.append(res)
        if "error" in res:
            print(f"  ❌ {asset}/{strat_name}: {res['error']}")
        else:
            print(f"  {asset}/{strat_name}: {res['profit_pct']:+.2f}% "
                  f"(${res['profit_usd']:+.2f}) | {res['total_trades']} trades "
                  f"| WR {res['winrate_pct']:.1f}% | DD {res['max_drawdown_pct']:.1f}% "
                  f"({dt:.1f}s)")

    df = pd.DataFrame(results)
    df.to_csv(OUT / "portfolio_90d_combo.csv", index=False)

    # Análise: cenário A (só top1) vs B (soma top1+top2 independentes)
    print("\n" + "=" * 100)
    print("📊 CENÁRIOS DE PORTFOLIO — 90 dias")
    print("=" * 100)

    # Pega 1ª e 2ª de cada ativo
    for asset in ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","ADAUSDT"]:
        sub = df[df["asset"]==asset].reset_index(drop=True)
        top1 = sub.iloc[0]
        top2 = sub.iloc[1]
        print(f"\n{asset}:")
        print(f"  #1 {top1['strategy']:35s} {top1['profit_pct']:+6.2f}% (${top1['profit_usd']:+.2f}) | WR {top1['winrate_pct']:.1f}% | {top1['total_trades']} trades")
        print(f"  #2 {top2['strategy']:35s} {top2['profit_pct']:+6.2f}% (${top2['profit_usd']:+.2f}) | WR {top2['winrate_pct']:.1f}% | {top2['total_trades']} trades")

    print("\n" + "=" * 100)
    print("🎯 COMPARAÇÃO GLOBAL")
    print("=" * 100)

    # Cenário A: só top1 (5 × $1000 = $5000)
    sum_top1 = sum(df[df["asset"]==a].iloc[0]["profit_usd"] for a in assets)
    # Cenário B: top1 + top2 paralelo (10 × $1000 = $10000, independentes)
    sum_both = df["profit_usd"].sum()
    # Cenário C: top1 + top2, mas com $500 cada (5 × $1000 = $5000, split 50/50)
    # profit_pct do original × $500 = profit_pct * 5 em $
    sum_split = sum((df.iloc[i]["profit_pct"] / 100) * 500 for i in range(len(df)))

    print(f"  A) Só #1 ($5.000 investido): Profit ${sum_top1:+.2f} ({sum_top1/5000*100:+.2f}%)")
    print(f"  B) #1 + #2 paralelo ($10.000 investido): Profit ${sum_both:+.2f} ({sum_both/10000*100:+.2f}%)")
    print(f"  C) #1 + #2 split 50/50 ($5.000 investido): Profit ${sum_split:+.2f} ({sum_split/5000*100:+.2f}%)")


if __name__ == "__main__":
    main()
