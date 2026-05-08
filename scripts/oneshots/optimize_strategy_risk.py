from __future__ import annotations

import argparse
import json
import os
import warnings
from pathlib import Path

import pandas as pd
from binance.client import Client

from src.modules.StrategyResearchEngine import (
    ResearchRules,
    StrategyResearchResult,
    StrategySpec,
    discover_extra_strategy_specs,
    get_default_strategy_specs,
    get_strategy_risk_grid,
    research_strategies_parallel,
    results_to_dataframe,
)
from src.strategy_research import fetch_klines


OUT_DIR = Path("src/logs/research")
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Otimiza parametros + take profit + stop loss por estrategia e valida consistencia."
    )
    parser.add_argument("--assets", default="BTCUSDT,ETHUSDT,SOLUSDT,JUPUSDT")
    parser.add_argument("--interval", default=Client.KLINE_INTERVAL_1HOUR)
    parser.add_argument("--history", default="8 months ago UTC")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--include-extras", action="store_true")
    parser.add_argument("--max-param-sets", type=int, default=40, help="Limite de parametros base por estrategia.")
    parser.add_argument("--max-risk-profiles", type=int, default=12, help="Limite de TP/SL por combinacao de parametros.")
    parser.add_argument("--window-days", type=int, default=60)
    parser.add_argument("--min-window-profit", type=float, default=50.0)
    parser.add_argument("--min-window-trades", type=int, default=6)
    parser.add_argument("--min-total-trades", type=int, default=20)
    parser.add_argument("--min-winrate", type=float, default=50.0)
    parser.add_argument("--max-drawdown", type=float, default=25.0)
    parser.add_argument("--min-profit-factor", type=float, default=1.15)
    parser.add_argument("--max-loss-streak", type=int, default=4)
    parser.add_argument("--min-consistency-score", type=float, default=55.0)
    parser.add_argument("--fee", type=float, default=0.1)
    return parser.parse_args()


def limit_specs_per_strategy(specs: list[StrategySpec], max_param_sets: int) -> list[StrategySpec]:
    grouped: dict[str, list[StrategySpec]] = {}
    for spec in specs:
        grouped.setdefault(spec.name, []).append(spec)

    limited: list[StrategySpec] = []
    for strategy_name, strategy_specs in grouped.items():
        if len(strategy_specs) <= max_param_sets:
            limited.extend(strategy_specs)
            continue

        step = max(1, len(strategy_specs) // max_param_sets)
        sampled = strategy_specs[::step][:max_param_sets]
        limited.extend(sampled)
        print(f"  {strategy_name}: usando {len(sampled)} de {len(strategy_specs)} combinacoes de parametros")

    return limited


def expand_specs_with_limited_risk_profiles(strategies: list[StrategySpec], max_risk_profiles: int) -> list[StrategySpec]:
    expanded: list[StrategySpec] = []
    for strategy in strategies:
        risk_profiles = get_strategy_risk_grid(strategy.name)[:max_risk_profiles]
        for risk in risk_profiles:
            expanded.append(
                StrategySpec(
                    name=strategy.name,
                    function=strategy.function,
                    params=dict(strategy.params),
                    needs_instance=strategy.needs_instance,
                    take_profit_pct=risk["take_profit_pct"],
                    stop_loss_pct=risk["stop_loss_pct"],
                    max_hold_candles=risk["max_hold_candles"],
                )
            )
    return expanded


def add_score(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe

    df = dataframe.copy()
    df["score"] = (
        df["score"].fillna(0) * 1.0
        + df["consistency_score"].fillna(0) * 1.6
        + df["total_profit_pct"].fillna(0) * 0.6
        + df["min_window_profit_pct"].fillna(0) * 0.9
        + df["winrate_pct"].fillna(0) * 0.3
        + df["profit_factor"].fillna(0).clip(upper=5) * 5.0
        + df["total_trades"].fillna(0).clip(upper=200) * 0.08
        - df["max_drawdown_pct"].fillna(0) * 1.8
        - df["max_loss_streak"].fillna(0) * 3.0
    )
    df.loc[df["passed"] == True, "score"] += 1000
    return df


def best_by_strategy_asset(results: list[StrategyResearchResult]) -> pd.DataFrame:
    df = add_score(results_to_dataframe(results))
    if df.empty:
        return df

    df = df.sort_values(
        ["asset", "strategy", "passed", "score", "total_profit_pct", "winrate_pct"],
        ascending=[True, True, False, False, False, False],
    )
    return df.groupby(["asset", "strategy"], as_index=False).head(1).reset_index(drop=True)


def main() -> None:
    args = parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    assets = [asset.strip().upper() for asset in args.assets.split(",") if asset.strip()]
    client = Client(os.getenv("BINANCE_API_KEY", ""), os.getenv("BINANCE_SECRET_KEY", ""))

    print(f"Baixando candles de {len(assets)} ativos desde {args.history}...")
    data_by_asset = {}
    for asset in assets:
        data_by_asset[asset] = fetch_klines(client, asset, args.interval, args.history)
        print(f"  {asset}: {len(data_by_asset[asset])} candles")

    base_specs = get_default_strategy_specs()
    if args.include_extras:
        base_specs.extend(discover_extra_strategy_specs())

    base_specs = limit_specs_per_strategy(base_specs, args.max_param_sets)
    strategies = expand_specs_with_limited_risk_profiles(base_specs, args.max_risk_profiles)

    rules = ResearchRules(
        window_days=args.window_days,
        min_window_profit_pct=args.min_window_profit,
        min_window_trades=args.min_window_trades,
        min_total_trades=args.min_total_trades,
        min_winrate_pct=args.min_winrate,
        max_drawdown_pct=args.max_drawdown,
        min_profit_factor=args.min_profit_factor,
        max_loss_streak=args.max_loss_streak,
        min_consistency_score=args.min_consistency_score,
        trade_fee_pct=args.fee,
    )

    print(f"Rodando {len(strategies) * len(data_by_asset)} backtests com TP/SL em paralelo...")
    results = research_strategies_parallel(
        data_by_asset=data_by_asset,
        strategies=strategies,
        rules=rules,
        max_workers=args.workers,
    )

    all_results = add_score(results_to_dataframe(results))
    best_results = best_by_strategy_asset(results)
    approved = all_results[all_results["passed"] == True].copy()

    all_results.to_csv(OUT_DIR / "all_strategy_risk_optimization.csv", index=False, encoding="utf-8")
    best_results.to_csv(OUT_DIR / "best_risk_by_strategy_asset.csv", index=False, encoding="utf-8")
    approved.to_csv(OUT_DIR / "approved_risk_strategies.csv", index=False, encoding="utf-8")

    with open(OUT_DIR / "all_strategy_risk_optimization.json", "w", encoding="utf-8") as file:
        json.dump([result.to_dict() for result in results], file, ensure_ascii=False, indent=2)

    print("\nMelhor TP/SL por estrategia e ativo:")
    columns = [
        "asset",
        "strategy",
        "take_profit_pct",
        "stop_loss_pct",
        "max_hold_candles",
        "params",
        "passed",
        "total_profit_pct",
        "total_trades",
        "winrate_pct",
        "max_drawdown_pct",
        "profit_factor",
        "max_loss_streak",
        "consistency_score",
        "score",
        "min_window_profit_pct",
    ]
    print(best_results[columns].head(50).to_string(index=False))

    print("\nAprovadas pelo filtro completo:")
    if approved.empty:
        print("  Nenhuma passou em todos os criterios. Veja best_risk_by_strategy_asset.csv para as melhores candidatas.")
    else:
        print(approved[columns].head(30).to_string(index=False))

    print(f"\nArquivos salvos em: {OUT_DIR}")


if __name__ == "__main__":
    main()
