import itertools
import os
import sys
import warnings
from pathlib import Path

import pandas as pd
from binance.client import Client

from src.backtest_champions_strategy import fetch_klines, prepare

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

warnings.filterwarnings("ignore", category=FutureWarning)

ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"]
OUT = Path("src/logs/ranking")
OUT.mkdir(parents=True, exist_ok=True)

PARAM_GRID = {
    "take_profit_pct": [0.7, 0.9, 1.1],
    "stop_loss_pct": [1.5, 2.0, 2.5],
    "rsi_min": [40.0, 44.0],
    "rsi_max": [62.0, 64.0, 68.0],
    "pullback_pct": [0.7, 1.0, 1.3],
    "max_hold_bars": [18, 24, 36],
}

MIN_TRADES = 8
TARGET_WINRATE = 70.0


def score_result(result):
    if result["total_trades"] < MIN_TRADES:
        return None

    profit = result["profit_pct"]
    winrate = result["winrate_pct"]
    trades = result["total_trades"]
    drawdown = abs(result["max_drawdown_pct"])

    winrate_bonus = max(0.0, winrate - TARGET_WINRATE) * 0.30
    trade_bonus = min(trades, 40) * 0.08
    drawdown_penalty = drawdown * 0.25
    return profit + winrate_bonus + trade_bonus - drawdown_penalty


# Custos de execucao alinhados com o paper trading (PaperBalanceLogger):
# round-trip taker fee 0.20% (Binance spot) + slippage realista 0.05% por trade.
# Configuravel via env pra rodar walk-forward com varios cenarios.
ROUNDTRIP_FEE_PCT = float(os.getenv("BACKTEST_ROUNDTRIP_FEE_PCT", "0.20"))
SLIPPAGE_PCT_PER_TRADE = float(os.getenv("BACKTEST_SLIPPAGE_PCT", "0.05"))
TRADE_COST_PCT = ROUNDTRIP_FEE_PCT + SLIPPAGE_PCT_PER_TRADE


def fast_run_backtest(
    df,
    take_profit_pct,
    stop_loss_pct,
    rsi_min,
    rsi_max,
    pullback_pct,
    max_hold_bars,
):
    close = df["close_price"].to_numpy()
    high = df["high_price"].to_numpy()
    low = df["low_price"].to_numpy()
    ema8 = df["ema8"].to_numpy()
    ema20 = df["ema20"].to_numpy()
    ema50 = df["ema50"].to_numpy()
    ema200 = df["ema200"].to_numpy()
    tema7 = df["tema7"].to_numpy()
    tema21 = df["tema21"].to_numpy()
    tema21_slope = df["tema21_slope"].to_numpy()
    rsi = df["rsi"].to_numpy()
    atr_pct = df["atr_pct"].to_numpy()
    volume = df["volume"].to_numpy()
    volume_ma24 = df["volume_ma24"].to_numpy()
    ret3_pct = df["ret3_pct"].to_numpy()

    balance = 1000.0
    position = False
    entry_price = 0.0
    entry_index = 0
    trades = []
    equity = []

    for i in range(220, len(df)):
        price = float(close[i])

        if position:
            if float(low[i]) <= entry_price * (1 - stop_loss_pct / 100):
                pnl_pct = -stop_loss_pct
            elif float(high[i]) >= entry_price * (1 + take_profit_pct / 100):
                pnl_pct = take_profit_pct
            else:
                pnl_pct = (price - entry_price) / entry_price * 100
                sell = (
                    (i - entry_index >= max_hold_bars and pnl_pct > 0)
                    or (pnl_pct > 0.20 and price < float(ema20[i]) and float(rsi[i]) < 48)
                    or (pnl_pct < 0 and price < float(ema50[i]) and float(rsi[i]) < 43)
                )
                if not sell:
                    equity.append(balance * (price / entry_price))
                    continue

            # Aplica custo round-trip (fee + slippage) no PnL realizado.
            pnl_pct_net = pnl_pct - TRADE_COST_PCT
            balance *= 1 + pnl_pct_net / 100
            trades.append(pnl_pct_net)
            position = False
        else:
            recent_low = low[i - 5 : i + 1]
            recent_ema20 = ema20[i - 5 : i + 1]
            trend_ok = price > ema200[i] and ema20[i] > ema50[i] and ema50[i] > ema200[i]
            champion_ok = tema7[i] > tema21[i] and tema21_slope[i] > 0
            pullback_ok = (recent_low <= recent_ema20 * (1 + pullback_pct / 100)).any() and price > ema8[i]
            momentum_ok = (
                rsi[i] > rsi[i - 1]
                and rsi_min <= rsi[i] <= rsi_max
                and price > close[i - 1]
                and ret3_pct[i] > -2.5
            )
            volatility_ok = 0.10 <= atr_pct[i] <= 7.0
            volume_ok = volume[i] >= volume_ma24[i] * 0.30

            if trend_ok and champion_ok and pullback_ok and momentum_ok and volatility_ok and volume_ok:
                position = True
                entry_price = price
                entry_index = i

        equity.append(balance if not position else balance * (price / entry_price))

    if position:
        pnl_pct = (float(close[-1]) - entry_price) / entry_price * 100
        pnl_pct_net = pnl_pct - TRADE_COST_PCT
        balance *= 1 + pnl_pct_net / 100
        trades.append(pnl_pct_net)

    total = len(trades)
    wins = sum(1 for trade in trades if trade > 0)
    losses = total - wins
    eq = pd.Series(equity or [1000.0])
    drawdown = ((eq - eq.cummax()) / eq.cummax() * 100).min()

    return {
        "final_balance": round(balance, 2),
        "profit_usd": round(balance - 1000.0, 2),
        "profit_pct": round((balance - 1000.0) / 1000.0 * 100, 2),
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(wins / total * 100, 2) if total else 0.0,
        "avg_win_pct": round(sum(t for t in trades if t > 0) / wins, 2) if wins else 0.0,
        "avg_loss_pct": round(sum(t for t in trades if t <= 0) / losses, 2) if losses else 0.0,
        "max_drawdown_pct": round(drawdown, 2),
    }


def optimize_asset(df):
    keys = list(PARAM_GRID.keys())
    rows = []

    for values in itertools.product(*(PARAM_GRID[key] for key in keys)):
        params = dict(zip(keys, values))
        result = fast_run_backtest(df, **params)
        score = score_result(result)
        if score is None:
            continue
        rows.append({**params, **result, "score": round(score, 4)})

    ranked = pd.DataFrame(rows)
    if ranked.empty:
        return ranked, None

    ranked = ranked.sort_values(
        ["score", "profit_pct", "winrate_pct", "total_trades"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)
    return ranked, ranked.iloc[0].to_dict()


def main():
    client = Client(os.getenv("BINANCE_API_KEY", ""), os.getenv("BINANCE_SECRET_KEY", ""))
    best_rows = []
    all_rows = []

    for asset in ASSETS:
        print(f"\nOtimizando {asset}...", flush=True)
        df = prepare(fetch_klines(client, asset))
        ranked, best = optimize_asset(df)
        if ranked.empty or best is None:
            print(f"  Nenhum candidato com pelo menos {MIN_TRADES} trades.")
            continue

        ranked.insert(0, "asset", asset)
        all_rows.append(ranked)
        best["asset"] = asset
        best_rows.append(best)

        print(
            f"  Melhor: profit {best['profit_pct']:+.2f}% | WR {best['winrate_pct']:.2f}% "
            f"| trades {int(best['total_trades'])} | DD {best['max_drawdown_pct']:.2f}%"
            ,
            flush=True,
        )
        print(
            "  Params: "
            f"TP {best['take_profit_pct']} | SL {best['stop_loss_pct']} | "
            f"RSI {best['rsi_min']}-{best['rsi_max']} | pullback {best['pullback_pct']} | "
            f"hold {int(best['max_hold_bars'])}"
            ,
            flush=True,
        )

    best_report = pd.DataFrame(best_rows)
    best_report.to_csv(OUT / "champions_by_asset_best.csv", index=False, encoding="utf-8")

    if all_rows:
        pd.concat(all_rows, ignore_index=True).to_csv(
            OUT / "champions_by_asset_all.csv",
            index=False,
            encoding="utf-8",
        )

    total_trades = int(best_report["total_trades"].sum())
    total_wins = int(best_report["wins"].sum())
    total_profit = float(best_report["profit_usd"].sum())

    print("\nTOTAIS BEST BY ASSET")
    print(f"Profit: ${total_profit:+.2f} ({total_profit / (1000 * len(best_report)) * 100:+.2f}%)")
    print(f"Win rate ponderado: {total_wins / total_trades * 100:.2f}%")
    print(f"Trades: {total_trades}")
    print(f"Drawdown medio: {best_report['max_drawdown_pct'].mean():.2f}%")
    print(f"\nSalvo em {OUT / 'champions_by_asset_best.csv'}")


if __name__ == "__main__":
    main()
