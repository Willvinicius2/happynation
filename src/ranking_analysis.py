"""Análise mais rica dos resultados do ranking: profit, trades, eficiência, consistência."""
import pandas as pd
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

RESULTS = Path("src/logs/ranking/results.csv")
OUT = Path("src/logs/ranking/ranking_detalhado.csv")

df = pd.read_csv(RESULTS)
valid = df[df["error"].isna()].copy()

# Profit por trade (evitando div 0)
valid["profit_per_trade"] = valid.apply(
    lambda r: (r["profit_pct"] / r["trades"]) if r["trades"] > 0 else 0.0, axis=1
)

agg = valid.groupby("strategy").agg(
    avg_profit=("profit_pct", "mean"),
    median_profit=("profit_pct", "median"),
    min_profit=("profit_pct", "min"),
    max_profit=("profit_pct", "max"),
    total_trades=("trades", "sum"),
    avg_trades=("trades", "mean"),
    avg_profit_per_trade=("profit_per_trade", "mean"),
    positive_assets=("profit_pct", lambda s: (s > 0).sum()),
    assets=("asset", "count"),
).reset_index()

# Win rate (ativos positivos / total)
agg["win_rate_pct"] = (agg["positive_assets"] / agg["assets"] * 100).round(1)

# Arredonda colunas numéricas
for c in ["avg_profit", "median_profit", "min_profit", "max_profit",
          "avg_trades", "avg_profit_per_trade"]:
    agg[c] = agg[c].round(3)

# Ordena por avg_profit desc
agg_sorted = agg.sort_values("avg_profit", ascending=False).reset_index(drop=True)
agg_sorted.insert(0, "rank", range(1, len(agg_sorted) + 1))

# Filtra estratégias sem trades (que ficaram em 0.00% só por não entrar)
agg_with_trades = agg_sorted[agg_sorted["total_trades"] > 0].copy()

print("=" * 110)
print("🏆 RANKING DETALHADO — ordenado por avg_profit")
print("=" * 110)
print(agg_sorted[["rank", "strategy", "avg_profit", "median_profit",
                  "total_trades", "avg_trades", "avg_profit_per_trade",
                  "win_rate_pct", "min_profit", "max_profit"]].to_string(index=False))

# Top 10 por profit_per_trade (eficiência)
print("\n" + "=" * 110)
print("⚡ TOP 10 por PROFIT POR TRADE (só estratégias com trades > 0)")
print("=" * 110)
eff = agg_with_trades.sort_values("avg_profit_per_trade", ascending=False).head(10)
print(eff[["strategy", "avg_profit_per_trade", "total_trades", "avg_profit", "win_rate_pct"]].to_string(index=False))

# Top 10 por win_rate
print("\n" + "=" * 110)
print("🎯 TOP 10 por WIN RATE (quantos ativos deram lucro)")
print("=" * 110)
wr = agg_with_trades.sort_values(["win_rate_pct", "avg_profit"], ascending=[False, False]).head(10)
print(wr[["strategy", "win_rate_pct", "avg_profit", "total_trades", "avg_profit_per_trade"]].to_string(index=False))

# Salva CSV
agg_sorted.to_csv(OUT, index=False, encoding="utf-8")
print(f"\n💾 Salvo em: {OUT}")
