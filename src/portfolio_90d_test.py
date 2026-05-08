"""
Teste de 90 dias do portfolio best-of-each:
- BTC/ETH/SOL/XRP/ADA (sem JUP)
- Cada ativo com sua melhor estratégia do 12m
- Banca inicial $1000 por ativo
"""
import os
import sys
import io
import time
import json
from pathlib import Path

import pandas as pd
from binance.client import Client

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.ranking_top10_12m import fetch_klines, LOOKBACK
from src.ranking_top10_12m import run_detailed_backtest
from src.strategies.adapters import (
    ATR_STRATEGY, TEMA_STRATEGY, DPO_STRATEGY, ACCELERATOR_STRATEGY,
)
from src.strategies.extras.atr_strategy import getATRTradeStrategy
from src.strategies.extras.tema_strategy import getTEMATradeStrategy
from src.strategies.extras.detrended_price_oscillator_strategy import getDetrendedPriceOscillatorTradeStrategy
from src.strategies.extras.accelerator_oscillator_strategy import getAcceleratorOscillatorTradeStrategy

# Portfolio config (mesmo do main.py)
PORTFOLIO = [
    ("BTCUSDT", "X_ATR",                      getATRTradeStrategy),
    ("ETHUSDT", "X_TEMA",                     getTEMATradeStrategy),
    ("SOLUSDT", "X_ATR",                      getATRTradeStrategy),
    ("XRPUSDT", "X_DetrendedPriceOscillator", getDetrendedPriceOscillatorTradeStrategy),
    ("ADAUSDT", "X_AcceleratorOscillator",    getAcceleratorOscillatorTradeStrategy),
]
INITIAL = 1000.0
HISTORY = "90 days ago UTC"
INTERVAL = Client.KLINE_INTERVAL_1HOUR

OUT = Path("src/logs/ranking")
OUT.mkdir(parents=True, exist_ok=True)


def main():
    api = os.getenv("BINANCE_API_KEY", "")
    sec = os.getenv("BINANCE_SECRET_KEY", "")
    client = Client(api if "Cole" not in api else "", sec if "Cole" not in sec else "")

    print(f"📥 Baixando 90 dias de candles 1h para {len(PORTFOLIO)} ativos...")
    results = []
    t_total = time.time()

    for asset, strat_name, raw_fn in PORTFOLIO:
        t0 = time.time()
        df = fetch_klines(client, asset, INTERVAL, HISTORY)
        # Adapta colunas (raw_fn espera 'close'/'high'/etc)
        df2 = df.copy()
        for src, dst in [("close_price","close"),("high_price","high"),("low_price","low"),("open_price","open")]:
            if dst not in df2.columns:
                df2[dst] = df2[src]
        dt_fetch = time.time() - t0
        t1 = time.time()
        res = run_detailed_backtest(df2, raw_fn, INITIAL)
        dt_run = time.time() - t1
        res.update({"asset": asset, "strategy": strat_name,
                    "candles": len(df), "fetch_s": round(dt_fetch,1), "run_s": round(dt_run,1)})
        results.append(res)
        if "error" in res:
            print(f"  ❌ {asset}/{strat_name}: ERRO {res['error']}  {dt_fetch+dt_run:.1f}s")
            continue
        print(f"  ✅ {asset}/{strat_name}: {res['profit_pct']:+.2f}% "
              f"(${res['profit_usd']:+.2f}) | {res['total_trades']} trades "
              f"| WR {res['winrate_pct']:.1f}% | DD {res['max_drawdown_pct']:.1f}% "
              f"({dt_fetch+dt_run:.1f}s)")

    total_time = time.time() - t_total
    print(f"\n⏱️ Tempo total: {total_time:.1f}s")

    df_res = pd.DataFrame(results)
    ok = df_res[df_res.get("error", pd.Series([None]*len(df_res))).isna()].copy() if "error" in df_res.columns else df_res.copy()

    invested = INITIAL * len(ok)
    final = ok["final_balance"].sum()
    profit = final - invested

    print("\n" + "=" * 100)
    print(f"🏆 PORTFOLIO BEST-OF-EACH — 90 DIAS — Sem alavancagem")
    print("=" * 100)
    cols = ["asset","strategy","profit_pct","profit_usd","total_trades","winrate_pct","max_drawdown_pct","final_balance"]
    print(ok[cols].to_string(index=False))

    print("\n" + "=" * 100)
    print(f"📊 TOTAIS — 90 dias")
    print("=" * 100)
    print(f"  Investido:      ${invested:>12,.2f}")
    print(f"  Banca final:    ${final:>12,.2f}")
    print(f"  Profit ($):     ${profit:>+12,.2f}")
    print(f"  Profit (%):     {profit/invested*100:>+12.2f}%")
    print(f"  Total trades:   {int(ok['total_trades'].sum()):>12,}")
    print(f"  Win rate médio: {ok['winrate_pct'].mean():>12.2f}%")
    print(f"  Ativos +:       {(ok['profit_pct']>0).sum()}/{len(ok)}")

    df_res.to_csv(OUT / "portfolio_90d.csv", index=False, encoding="utf-8")
    print(f"\n💾 Salvo em {OUT / 'portfolio_90d.csv'}")


if __name__ == "__main__":
    main()
