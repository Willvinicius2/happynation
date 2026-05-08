"""Gera ranking final do backtest 10x a partir do CSV salvo."""
import sys
import pandas as pd
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

OUT_DIR = Path("src/logs/ranking")
CSV = OUT_DIR / "top10_12m_10x_detailed.csv"

df = pd.read_csv(CSV)
ok = df[df.get("error", pd.Series([pd.NA]*len(df))).isna()].copy() if "error" in df.columns else df.copy()

# Garante tipos
ok["blown_up"] = ok["blown_up"].astype(bool)

agg = ok.groupby("strategy").agg(
    total_profit_usd=("profit_usd", "sum"),
    avg_profit_pct=("profit_pct", "mean"),
    median_profit_pct=("profit_pct", "median"),
    total_trades=("total_trades", "sum"),
    avg_winrate=("winrate_pct", "mean"),
    total_liquidations=("liquidations", "sum"),
    blown_assets=("blown_up", "sum"),
    total_fees=("total_fees_usd", "sum"),
    avg_drawdown=("max_drawdown_pct", "mean"),
    assets_positive=("profit_pct", lambda s: (s > 0).sum()),
).round(2).sort_values("avg_profit_pct", ascending=False)

print("=" * 130)
print("🏆 RANKING TOP 10 — 12 MESES — LEVERAGE 10x — Banca $1000/ativo ($6000 total)")
print("=" * 130)
print(agg.to_string())

agg.to_csv(OUT_DIR / "top10_12m_10x_ranking.csv", encoding="utf-8")

print("\n" + "=" * 130)
print("📋 DETALHE ATIVO × ESTRATÉGIA — PROFIT %")
print("=" * 130)
pivot = ok.pivot(index="strategy", columns="asset", values="profit_pct").round(2)
print(pivot.to_string())

print("\n" + "=" * 130)
print("💥 DETALHE ATIVO × ESTRATÉGIA — CONTAS BLOWN UP (REKT)")
print("=" * 130)
pivot_rekt = ok.pivot(index="strategy", columns="asset", values="blown_up")
print(pivot_rekt.to_string())

# Resumo bruto
total_rekt = ok["blown_up"].sum()
total_runs = len(ok)
print("\n" + "=" * 130)
print(f"📊 RESUMO GLOBAL")
print("=" * 130)
print(f"  Total runs:        {total_runs}")
print(f"  Contas zeradas:    {total_rekt} ({total_rekt/total_runs*100:.1f}%)")
print(f"  Liquidações totais: {ok['liquidations'].sum()}")
print(f"  Fees totais:       ${ok['total_fees_usd'].sum():,.0f}")
print(f"  Profit total $:    ${ok['profit_usd'].sum():+,.2f}")
