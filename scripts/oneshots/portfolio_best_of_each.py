"""
Simula um portfolio best-of-each: cada ativo roda a sua melhor estratégia
(do backtest 12m) e JUPUSDT é excluído.
Usa os resultados já salvos em top10_12m_detailed.csv.
"""
import sys
import pandas as pd
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

OUT = Path("src/logs/ranking")
df = pd.read_csv(OUT / "top10_12m_detailed.csv")

EXCLUDE_ASSETS = {"JUPUSDT"}
INITIAL_PER_ASSET = 1000.0

# Para cada ativo (não-excluído), pega a linha com maior profit_pct
assets = [a for a in df["asset"].unique() if a not in EXCLUDE_ASSETS]

rows = []
for a in assets:
    best = df[df["asset"] == a].sort_values("profit_pct", ascending=False).iloc[0]
    rows.append(best)

port = pd.DataFrame(rows)
port = port[["asset", "strategy", "profit_pct", "profit_usd", "total_trades",
             "winrate_pct", "max_drawdown_pct", "final_balance"]].reset_index(drop=True)

total_invested = INITIAL_PER_ASSET * len(port)
total_final = port["final_balance"].sum()
total_profit = total_final - total_invested
total_profit_pct = total_profit / total_invested * 100

print("=" * 100)
print(f"🎯 PORTFOLIO BEST-OF-EACH (sem JUP) — 12 MESES — Sem alavancagem")
print(f"   Banca inicial: ${INITIAL_PER_ASSET:.0f} × {len(port)} ativos = ${total_invested:,.0f}")
print("=" * 100)
print(port.to_string(index=False))
print()
print("=" * 100)
print(f"📊 TOTAIS")
print("=" * 100)
print(f"  Investido:      ${total_invested:>12,.2f}")
print(f"  Banca final:    ${total_final:>12,.2f}")
print(f"  Profit ($):     ${total_profit:>+12,.2f}")
print(f"  Profit (%):     {total_profit_pct:>+12.2f}%")
print(f"  Total trades:   {int(port['total_trades'].sum()):>12,}")
print(f"  Win rate médio: {port['winrate_pct'].mean():>12.2f}%")
print(f"  Drawdown médio: {port['max_drawdown_pct'].mean():>12.2f}%")
print(f"  Ativos +:       {(port['profit_pct']>0).sum()}/{len(port)}")

# Comparação com melhor single-strategy
print()
print("=" * 100)
print("🔄 COMPARAÇÃO vs MELHOR ESTRATÉGIA ÚNICA (aplicada a todos os 5 ativos)")
print("=" * 100)
for strat in ["X_DetrendedPriceOscillator", "X_ATR", "X_TEMA"]:
    s = df[(df["strategy"] == strat) & (~df["asset"].isin(EXCLUDE_ASSETS))]
    if len(s) == 0:
        continue
    si = INITIAL_PER_ASSET * len(s)
    sf = s["final_balance"].sum()
    sp = sf - si
    print(f"  {strat:28s}: final ${sf:,.2f} | profit ${sp:+,.2f} ({sp/si*100:+.2f}%)")

port.to_csv(OUT / "portfolio_best_of_each.csv", index=False, encoding="utf-8")
print("\n💾 Salvo em src/logs/ranking/portfolio_best_of_each.csv")
