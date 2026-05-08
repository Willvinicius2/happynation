"""Testa o portfolio atualizado em 30d, 90d, 180d para validar robustez."""
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
from src.strategies.extras.accelerator_oscillator_strategy import getAcceleratorOscillatorTradeStrategy
from src.strategies.extras.hull_moving_average_strategy import getHullMovingAverageTradeStrategy

# Portfolio atual (após passo 1: SOL→HMA, XRP→Accelerator)
PORTFOLIO = [
    ("BTCUSDT", "X_ATR",                   getATRTradeStrategy),
    ("ETHUSDT", "X_TEMA",                  getTEMATradeStrategy),
    ("SOLUSDT", "X_HullMovingAverage",     getHullMovingAverageTradeStrategy),
    ("XRPUSDT", "X_AcceleratorOscillator", getAcceleratorOscillatorTradeStrategy),
    ("ADAUSDT", "X_AcceleratorOscillator", getAcceleratorOscillatorTradeStrategy),
]
INITIAL = 1000.0
PERIODS = ["30 days ago UTC", "90 days ago UTC", "180 days ago UTC"]
LABELS  = ["30d", "90d", "180d"]
INTERVAL = Client.KLINE_INTERVAL_1HOUR
OUT = Path("src/logs/ranking")


def main():
    api = os.getenv("BINANCE_API_KEY", "")
    sec = os.getenv("BINANCE_SECRET_KEY", "")
    client = Client(api if "Cole" not in api else "", sec if "Cole" not in sec else "")

    # Baixa dados uma vez por ativo no maior período (180d), depois corta
    assets = [p[0] for p in PORTFOLIO]
    unique_assets = list(dict.fromkeys(assets))
    print(f"📥 Baixando 180d de candles 1h para {len(unique_assets)} ativos...")
    data_180 = {}
    for a in unique_assets:
        t0 = time.time()
        df = fetch_klines(client, a, INTERVAL, "180 days ago UTC")
        for src, dst in [("close_price","close"),("high_price","high"),("low_price","low"),("open_price","open")]:
            if dst not in df.columns:
                df[dst] = df[src]
        data_180[a] = df
        print(f"  ✅ {a}: {len(df)} candles ({time.time()-t0:.1f}s)")

    # Candles aproximados por período (1h)
    sizes = {"30d": 30*24, "90d": 90*24, "180d": 180*24}

    all_results = {}
    for lbl in LABELS:
        print(f"\n{'='*100}\n🔎 Período: {lbl}\n{'='*100}")
        n = sizes[lbl]
        period_results = []
        for asset, strat_name, fn in PORTFOLIO:
            df_full = data_180[asset]
            df = df_full.iloc[-n:].reset_index(drop=True).copy() if n < len(df_full) else df_full.copy()
            t0 = time.time()
            res = run_detailed_backtest(df, fn, INITIAL)
            dt = time.time() - t0
            res.update({"asset": asset, "strategy": strat_name, "period": lbl, "candles": len(df)})
            period_results.append(res)
            if "error" in res:
                print(f"  ❌ {asset}/{strat_name}: {res['error']}")
                continue
            print(f"  {asset}/{strat_name}: {res['profit_pct']:+.2f}% (${res['profit_usd']:+.2f}) "
                  f"| {res['total_trades']} trades | WR {res['winrate_pct']:.1f}% "
                  f"| DD {res['max_drawdown_pct']:.1f}% ({dt:.1f}s)")

        all_results[lbl] = period_results
        df_r = pd.DataFrame(period_results)
        inv = INITIAL * len(df_r)
        final = df_r["final_balance"].sum()
        print(f"\n  📊 {lbl}: Investido ${inv:,.0f} → Final ${final:,.2f} → Profit ${final-inv:+,.2f} ({(final-inv)/inv*100:+.2f}%) | +ativos: {(df_r['profit_pct']>0).sum()}/{len(df_r)}")

    # Consolida e salva
    rows = [r for lst in all_results.values() for r in lst]
    df_all = pd.DataFrame(rows)
    df_all.to_csv(OUT / "portfolio_multi_period.csv", index=False)

    print("\n" + "=" * 100)
    print("🎯 RESUMO ROBUSTEZ — comparação entre períodos")
    print("=" * 100)
    summary_rows = []
    for lbl in LABELS:
        sub = df_all[df_all["period"]==lbl]
        inv = INITIAL * len(sub)
        final = sub["final_balance"].sum()
        summary_rows.append({
            "periodo": lbl, "investido": inv, "final": round(final,2),
            "profit_usd": round(final-inv,2), "profit_pct": round((final-inv)/inv*100,2),
            "trades": int(sub["total_trades"].sum()),
            "wr_medio": round(sub["winrate_pct"].mean(),2),
            "dd_medio": round(sub["max_drawdown_pct"].mean(),2),
            "ativos_positivos": f"{(sub['profit_pct']>0).sum()}/{len(sub)}",
        })
    summary = pd.DataFrame(summary_rows)
    print(summary.to_string(index=False))

    # Profit % por ativo × período
    print("\n" + "=" * 100)
    print("📋 PROFIT % POR ATIVO × PERÍODO")
    print("=" * 100)
    pivot = df_all.pivot(index="asset", columns="period", values="profit_pct").round(2)
    pivot = pivot[LABELS]  # reordena
    print(pivot.to_string())

    print(f"\n💾 Salvo em {OUT / 'portfolio_multi_period.csv'}")


if __name__ == "__main__":
    main()
