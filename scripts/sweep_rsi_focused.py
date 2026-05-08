"""Sweep cirúrgico de RSI + gestão de risco em todos os 5 pares do paper.

12 meses de dados, varredura fina de high/low, com e sem TP/SL/max_hold.
Reporta top 30 globais + melhor por ativo, salva CSV.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from binance.client import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.modules.StrategyResearchEngine import (
    ResearchRules,
    StrategySpec,
    validate_strategy,
)
from src.strategies.rsi_strategy import getRsiTradeStrategy
from src.strategy_research import fetch_klines


ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "XRPUSDT"]
HISTORY = "12 months ago UTC"
INTERVAL = Client.KLINE_INTERVAL_1HOUR

LOW_GRID = [18, 20, 22, 25]
HIGH_GRID = [62, 65, 68, 72]
TP_GRID = [None, 3.0, 5.0]
SL_GRID = [None, 2.5]
HOLD_GRID = [None, 96]


def build_specs() -> list[StrategySpec]:
    specs: list[StrategySpec] = []
    for low in LOW_GRID:
        for high in HIGH_GRID:
            if low >= high:
                continue
            for tp in TP_GRID:
                for sl in SL_GRID:
                    for hold in HOLD_GRID:
                        specs.append(
                            StrategySpec(
                                name="RSI",
                                function=getRsiTradeStrategy,
                                params={"low": low, "high": high},
                                take_profit_pct=tp,
                                stop_loss_pct=sl,
                                max_hold_candles=hold,
                            )
                        )
    return specs


def main() -> None:
    out_dir = Path("src/logs/research")
    out_dir.mkdir(parents=True, exist_ok=True)

    client = Client()
    print(f"Baixando {HISTORY} de {len(ASSETS)} ativos...", flush=True)
    data_by_asset: dict[str, pd.DataFrame] = {}
    for asset in ASSETS:
        data = fetch_klines(client, asset, INTERVAL, HISTORY)
        data_by_asset[asset] = data
        print(f"  {asset}: {len(data)} candles", flush=True)

    specs = build_specs()
    rules = ResearchRules(
        window_days=45,
        min_window_profit_pct=2.0,
        min_window_trades=3,
        min_total_trades=10,
        min_winrate_pct=50.0,
        max_drawdown_pct=25.0,
        min_profit_factor=1.20,
        max_loss_streak=5,
        min_consistency_score=40.0,
        initial_balance=200.0,
        lookback_candles=500,
        trade_fee_pct=0.1,
    )

    jobs = [(asset, data, spec) for asset, data in data_by_asset.items() for spec in specs]
    print(f"Rodando {len(jobs)} testes RSI em paralelo (5 workers)...", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(validate_strategy, asset, data, spec, rules): (asset, spec) for asset, data, spec in jobs}
        done = 0
        for fut in as_completed(futs):
            results.append(fut.result())
            done += 1
            if done % 250 == 0:
                print(f"  {done}/{len(jobs)} concluidos", flush=True)

    rows = []
    for r in results:
        rows.append({
            "asset": r.asset,
            "low": r.params.get("low"),
            "high": r.params.get("high"),
            "tp": r.take_profit_pct,
            "sl": r.stop_loss_pct,
            "hold": r.max_hold_candles,
            "passed": r.passed,
            "total_profit_pct": r.total_profit_pct,
            "total_trades": r.total_trades,
            "winrate_pct": r.winrate_pct,
            "max_drawdown_pct": r.max_drawdown_pct,
            "profit_factor": r.profit_factor,
            "max_loss_streak": r.max_loss_streak,
            "consistency_score": r.consistency_score,
            "windows_passed": r.windows_passed,
            "windows_total": r.windows_total,
            "avg_window_profit_pct": r.avg_window_profit_pct,
            "min_window_profit_pct": r.min_window_profit_pct,
            "score": r.score,
            "error": r.error,
        })

    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "sweep_rsi_focused.csv", index=False)
    print(f"\nSalvo {len(df)} linhas em src/logs/research/sweep_rsi_focused.csv", flush=True)

    # Top global
    print("\n=== TOP 30 GLOBAL (por score) ===", flush=True)
    top = df.sort_values("score", ascending=False).head(30)
    cols = ["asset","low","high","tp","sl","hold","total_profit_pct","total_trades","winrate_pct","max_drawdown_pct","profit_factor","windows_passed","windows_total","passed","score"]
    print(top[cols].to_string(), flush=True)

    # Melhor por ativo
    print("\n=== MELHOR POR ATIVO (filtrado: trades>=15, profit>0, PF>1.2) ===", flush=True)
    filt = df[(df["total_trades"] >= 15) & (df["total_profit_pct"] > 0) & (df["profit_factor"] > 1.2)]
    for asset in ASSETS:
        sub = filt[filt["asset"] == asset].sort_values("total_profit_pct", ascending=False).head(5)
        print(f"\n--- {asset} ---", flush=True)
        if sub.empty:
            print("  (nenhuma config lucrativa com >=15 trades e PF>1.2)", flush=True)
        else:
            print(sub[cols].to_string(), flush=True)

    # Aprovadas
    aprovadas = df[df["passed"]].sort_values("score", ascending=False)
    print(f"\n=== APROVADAS PELO FILTRO (passed=True): {len(aprovadas)} ===", flush=True)
    if not aprovadas.empty:
        print(aprovadas[cols].head(20).to_string(), flush=True)


if __name__ == "__main__":
    main()
