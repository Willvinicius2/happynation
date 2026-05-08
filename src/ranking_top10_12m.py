"""
Backtest detalhado das TOP 10 estratégias do ranking anterior,
em 12 meses de candles 1h, banca inicial $1000.
Métricas por (asset, strategy): trades, wins, losses, win rate,
profit em % e em $, melhor/pior trade, drawdown máximo.
"""
import os
import sys
import io
import time
import json
import importlib
import inspect
from pathlib import Path

import pandas as pd
from binance.client import Client

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ---- Config ----
ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "JUPUSDT"]
INTERVAL = Client.KLINE_INTERVAL_1HOUR
HISTORY_START = "12 months ago UTC"
INITIAL_BALANCE = 1000.0
LOOKBACK = 500

TOP10 = [
    "X_ATR",
    "X_HullMovingAverage",
    "X_DetrendedPriceOscillator",
    "X_AcceleratorOscillator",
    "X_UltimateOscillator",
    "X_TEMA",
    "X_PSAR",
    "X_FisherTransform",
    "X_ChaikinOscillator",
    "X_Cmf",
]

OUT_DIR = Path("src/logs/ranking")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def fetch_klines(client, symbol, interval, start_str):
    raw = client.get_historical_klines(symbol, interval, start_str)
    cols = ["open_time", "open_price", "high_price", "low_price", "close_price",
            "volume", "close_time", "qav", "trades", "tbbav", "tbqav", "_"]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["open_price", "high_price", "low_price", "close_price", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["open"] = df["open_price"]
    df["high"] = df["high_price"]
    df["low"] = df["low_price"]
    df["close"] = df["close_price"]
    return df[["open_time", "open_price", "high_price", "low_price", "close_price",
               "volume", "open", "high", "low", "close"]]


def load_top10_strategies():
    """Mapeia nomes TOP10 → função. Todas são do módulo extras."""
    import src.strategies.chilo_rsi as chilo_rsi_mod
    chilo_rsi_mod.USE_IN_MEMORY_ONLY = True

    name_map = {}
    extras_dir = Path("src/strategies/extras")
    for py in sorted(extras_dir.glob("*.py")):
        if py.name in ("__init__.py", "create_strategies.py"):
            continue
        mod_name = f"src.strategies.extras.{py.stem}"
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        for fn_name, obj in inspect.getmembers(mod, inspect.isfunction):
            if fn_name.startswith("get") and fn_name.endswith("TradeStrategy"):
                short = "X_" + fn_name[3:-len("TradeStrategy")]
                if short in TOP10:
                    name_map[short] = obj
                break
    return [(n, name_map[n]) for n in TOP10 if n in name_map]


def run_detailed_backtest(stock_data, strategy_fn, initial_balance=1000.0):
    """Retorna dict com métricas detalhadas (trades individuais)."""
    data = stock_data.reset_index(drop=True).copy()

    balance = initial_balance
    equity_curve = [balance]
    position = 0
    entry_price = 0.0
    last_signal = None
    trades = []  # lista de dicts {entry, exit, pnl_pct, pnl_usd, bars}
    entry_bar = None

    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        for i in range(1, len(data)):
            start = max(0, i + 1 - LOOKBACK)
            current = data.iloc[start: i + 1]
            close_price = data.iloc[i]["close_price"]

            try:
                signal = strategy_fn(current, verbose=False)
            except Exception as e:
                return {"error": f"{type(e).__name__}: {e}"}

            if signal is None:
                equity_curve.append(balance if position == 0 else
                                    balance * (close_price / entry_price))
                continue

            if signal and position == 0 and last_signal != "buy":
                position = 1
                entry_price = close_price
                entry_bar = i
                last_signal = "buy"
            elif (not signal) and position == 1 and last_signal != "sell":
                pnl_pct = (close_price - entry_price) / entry_price * 100
                pnl_usd = balance * (pnl_pct / 100)
                trades.append({
                    "entry_price": entry_price,
                    "exit_price": close_price,
                    "pnl_pct": pnl_pct,
                    "pnl_usd": pnl_usd,
                    "bars_held": i - entry_bar,
                })
                balance += pnl_usd
                position = 0
                last_signal = "sell"

            # Equity curve: marked-to-market se comprado
            if position == 1:
                equity_curve.append(balance * (close_price / entry_price))
            else:
                equity_curve.append(balance)
    finally:
        sys.stdout = old_stdout

    # Fecha posição final se aberta (mark-to-market)
    if position == 1:
        close_price = data.iloc[-1]["close_price"]
        pnl_pct = (close_price - entry_price) / entry_price * 100
        pnl_usd = balance * (pnl_pct / 100)
        trades.append({
            "entry_price": entry_price, "exit_price": close_price,
            "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
            "bars_held": len(data) - 1 - entry_bar, "forced_close": True,
        })
        balance += pnl_usd

    # Métricas
    total = len(trades)
    wins = sum(1 for t in trades if t["pnl_pct"] > 0)
    losses = total - wins
    winrate = (wins / total * 100) if total else 0.0
    avg_win = (sum(t["pnl_pct"] for t in trades if t["pnl_pct"] > 0) / wins) if wins else 0.0
    avg_loss = (sum(t["pnl_pct"] for t in trades if t["pnl_pct"] <= 0) / losses) if losses else 0.0
    best = max((t["pnl_pct"] for t in trades), default=0.0)
    worst = min((t["pnl_pct"] for t in trades), default=0.0)

    # Drawdown
    eq = pd.Series(equity_curve)
    peak = eq.cummax()
    dd_pct = ((eq - peak) / peak * 100).min() if len(eq) else 0.0

    profit_usd = balance - initial_balance
    profit_pct = profit_usd / initial_balance * 100

    return {
        "final_balance": round(balance, 2),
        "profit_usd": round(profit_usd, 2),
        "profit_pct": round(profit_pct, 2),
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(winrate, 2),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "best_trade_pct": round(best, 2),
        "worst_trade_pct": round(worst, 2),
        "max_drawdown_pct": round(dd_pct, 2),
    }


def main():
    api = os.getenv("BINANCE_API_KEY", "")
    sec = os.getenv("BINANCE_SECRET_KEY", "")
    client = Client(api if "Cole" not in api else "", sec if "Cole" not in sec else "")

    print(f"📥 Baixando 12 meses de candles 1h para {len(ASSETS)} ativos...")
    data = {}
    for sym in ASSETS:
        t0 = time.time()
        try:
            df = fetch_klines(client, sym, INTERVAL, HISTORY_START)
            data[sym] = df
            print(f"  ✅ {sym}: {len(df)} candles  ({time.time()-t0:.1f}s)")
        except Exception as e:
            print(f"  ❌ {sym}: {e}")

    strategies = load_top10_strategies()
    print(f"\n🧩 Top 10 estratégias carregadas: {[n for n,_ in strategies]}")

    results = []
    total = len(ASSETS) * len(strategies)
    run = 0
    t_total = time.time()
    for sym, df in data.items():
        for (name, fn) in strategies:
            run += 1
            t0 = time.time()
            res = run_detailed_backtest(df, fn, INITIAL_BALANCE)
            dt = time.time() - t0
            res.update({"asset": sym, "strategy": name, "seconds": round(dt, 1)})
            results.append(res)
            if "error" in res:
                print(f"  [{run:03d}/{total}] {sym}/{name}: ERRO ({res['error']})  {dt:.1f}s")
            else:
                print(f"  [{run:03d}/{total}] {sym}/{name}: "
                      f"{res['profit_pct']:+.2f}% (${res['profit_usd']:+.2f}) "
                      f"| {res['total_trades']} trades "
                      f"| WR {res['winrate_pct']:.1f}% "
                      f"| DD {res['max_drawdown_pct']:.1f}%  "
                      f"{dt:.1f}s")

    print(f"\n⏱️ Tempo total: {(time.time()-t_total)/60:.1f} min")

    # Salva
    df_res = pd.DataFrame(results)
    df_res.to_csv(OUT_DIR / "top10_12m_detailed.csv", index=False, encoding="utf-8")
    with open(OUT_DIR / "top10_12m_detailed.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    # Tabela ranking por estratégia (agregado 6 ativos)
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
    print("🏆 RANKING TOP 10 — 12 MESES (banca $1000 por asset, 6 assets = $6000 alocados)")
    print("=" * 110)
    print(agg.to_string())
    agg.to_csv(OUT_DIR / "top10_12m_ranking.csv", encoding="utf-8")

    print("\n" + "=" * 110)
    print("📋 DETALHE POR ATIVO × ESTRATÉGIA")
    print("=" * 110)
    pivot_profit = ok.pivot(index="strategy", columns="asset", values="profit_pct")
    print(pivot_profit.round(2).to_string())

    print("\n💾 Arquivos salvos em src/logs/ranking/:")
    print("   - top10_12m_detailed.csv (todos os runs)")
    print("   - top10_12m_ranking.csv (ranking agregado)")


if __name__ == "__main__":
    main()
