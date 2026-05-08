"""
Monta um portfolio focado em win rate a partir de top10_12m_detailed.csv.

Regras:
- exclui ativos problemáticos definidos em EXCLUDE_ASSETS;
- exige lucro positivo;
- exige quantidade mínima de trades para evitar amostra pequena;
- escolhe, por ativo, a estratégia com maior win rate.
"""
import sys
from pathlib import Path

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

OUT = Path("src/logs/ranking")
SOURCE = OUT / "top10_12m_detailed.csv"
TARGET = OUT / "portfolio_winrate_focused.csv"

EXCLUDE_ASSETS = {"JUPUSDT"}
INITIAL_PER_ASSET = 1000.0
MIN_TRADES = 30
MIN_PROFIT_PCT = 0.0


def main():
    df = pd.read_csv(SOURCE)

    candidates = df[
        (~df["asset"].isin(EXCLUDE_ASSETS))
        & (df["profit_pct"] > MIN_PROFIT_PCT)
        & (df["total_trades"] >= MIN_TRADES)
    ].copy()

    if candidates.empty:
        raise SystemExit("Nenhuma estratégia passou nos filtros de win rate.")

    rows = []
    for asset in sorted(candidates["asset"].unique()):
        best = candidates[candidates["asset"] == asset].sort_values(
            ["winrate_pct", "profit_pct", "max_drawdown_pct"],
            ascending=[False, False, False],
        ).iloc[0]
        rows.append(best)

    port = pd.DataFrame(rows)
    port = port[
        [
            "asset",
            "strategy",
            "profit_pct",
            "profit_usd",
            "total_trades",
            "winrate_pct",
            "max_drawdown_pct",
            "final_balance",
        ]
    ].reset_index(drop=True)

    total_invested = INITIAL_PER_ASSET * len(port)
    total_final = port["final_balance"].sum()
    total_profit = total_final - total_invested
    total_profit_pct = total_profit / total_invested * 100

    print("=" * 100)
    print("PORTFOLIO WIN-RATE FOCUSED - 12 MESES - Sem alavancagem")
    print(f"Banca inicial: ${INITIAL_PER_ASSET:.0f} x {len(port)} ativos = ${total_invested:,.0f}")
    print(f"Filtros: profit_pct > {MIN_PROFIT_PCT}, total_trades >= {MIN_TRADES}")
    print("=" * 100)
    print(port.to_string(index=False))
    print()
    print("=" * 100)
    print("TOTAIS")
    print("=" * 100)
    print(f"  Investido:      ${total_invested:>12,.2f}")
    print(f"  Banca final:    ${total_final:>12,.2f}")
    print(f"  Profit ($):     ${total_profit:>+12,.2f}")
    print(f"  Profit (%):     {total_profit_pct:>+12.2f}%")
    print(f"  Total trades:   {int(port['total_trades'].sum()):>12,}")
    print(f"  Win rate medio: {port['winrate_pct'].mean():>12.2f}%")
    print(f"  Drawdown medio: {port['max_drawdown_pct'].mean():>12.2f}%")
    print(f"  Ativos +:       {(port['profit_pct'] > 0).sum()}/{len(port)}")

    port.to_csv(TARGET, index=False, encoding="utf-8")
    print(f"\nSalvo em {TARGET}")


if __name__ == "__main__":
    main()
