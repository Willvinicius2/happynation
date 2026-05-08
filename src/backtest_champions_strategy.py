import os
import sys
import warnings
from pathlib import Path

import pandas as pd
from binance.client import Client

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

warnings.filterwarnings("ignore", category=FutureWarning)

ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"]
INITIAL_BALANCE = 1000.0
INTERVAL = Client.KLINE_INTERVAL_1HOUR
LIMIT = 1000
OUT = Path("src/logs/ranking")
OUT.mkdir(parents=True, exist_ok=True)


def fetch_klines(client, symbol):
    raw = client.get_klines(symbol=symbol, interval=INTERVAL, limit=LIMIT)
    cols = [
        "open_time",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "close_time",
        "qav",
        "trades",
        "tbbav",
        "tbqav",
        "_",
    ]
    df = pd.DataFrame(raw, columns=cols)
    for col in ["open_price", "high_price", "low_price", "close_price", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return (100 - (100 / (1 + rs))).fillna(50)


def tema(series, period):
    ema1 = series.ewm(span=period, adjust=False).mean()
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    ema3 = ema2.ewm(span=period, adjust=False).mean()
    return 3 * ema1 - 3 * ema2 + ema3


def prepare(df):
    df = df.copy().reset_index(drop=True)
    close = df["close_price"]
    for window in [8, 20, 50, 200]:
        df[f"ema{window}"] = close.ewm(span=window, adjust=False).mean()
    df["tema7"] = tema(close, 7)
    df["tema21"] = tema(close, 21)
    df["tema21_slope"] = df["tema21"].diff()
    df["rsi"] = rsi(close, period=14)
    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            df["high_price"] - df["low_price"],
            (df["high_price"] - prev_close).abs(),
            (df["low_price"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr_pct"] = true_range.rolling(14, min_periods=14).mean() / close * 100
    df["volume_ma24"] = df["volume"].rolling(24, min_periods=24).mean()
    df["ret3_pct"] = close.pct_change(3) * 100
    return df


def run_backtest(
    df,
    take_profit_pct=0.9,
    stop_loss_pct=2.0,
    rsi_min=40.0,
    rsi_max=64.0,
    pullback_pct=0.7,
    max_hold_bars=24,
):
    balance = INITIAL_BALANCE
    position = False
    entry_price = 0.0
    entry_index = 0
    trades = []
    equity = []

    for i in range(220, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        price = float(row["close_price"])

        if position:
            if float(row["low_price"]) <= entry_price * (1 - stop_loss_pct / 100):
                pnl_pct = -stop_loss_pct
            elif float(row["high_price"]) >= entry_price * (1 + take_profit_pct / 100):
                pnl_pct = take_profit_pct
            else:
                pnl_pct = (price - entry_price) / entry_price * 100
                sell = (
                    (i - entry_index >= max_hold_bars and pnl_pct > 0)
                    or (pnl_pct > 0.20 and price < float(row["ema20"]) and float(row["rsi"]) < 48)
                    or (pnl_pct < 0 and price < float(row["ema50"]) and float(row["rsi"]) < 43)
                )
                if not sell:
                    equity.append(balance * (price / entry_price))
                    continue

            balance *= 1 + pnl_pct / 100
            trades.append(pnl_pct)
            position = False
        else:
            recent = df.iloc[i - 5 : i + 1]
            trend_ok = price > row["ema200"] and row["ema20"] > row["ema50"] and row["ema50"] > row["ema200"]
            champion_ok = row["tema7"] > row["tema21"] and row["tema21_slope"] > 0
            pullback_ok = bool((recent["low_price"] <= recent["ema20"] * (1 + pullback_pct / 100)).any()) and price > row["ema8"]
            momentum_ok = (
                row["rsi"] > prev["rsi"]
                and rsi_min <= row["rsi"] <= rsi_max
                and price > prev["close_price"]
                and row["ret3_pct"] > -2.5
            )
            volatility_ok = 0.10 <= row["atr_pct"] <= 7.0
            volume_ok = row["volume"] >= row["volume_ma24"] * 0.30

            if trend_ok and champion_ok and pullback_ok and momentum_ok and volatility_ok and volume_ok:
                position = True
                entry_price = price
                entry_index = i

        equity.append(balance if not position else balance * (price / entry_price))

    if position:
        final_price = float(df.iloc[-1]["close_price"])
        pnl_pct = (final_price - entry_price) / entry_price * 100
        balance *= 1 + pnl_pct / 100
        trades.append(pnl_pct)

    total = len(trades)
    wins = sum(1 for trade in trades if trade > 0)
    losses = total - wins
    eq = pd.Series(equity or [INITIAL_BALANCE])
    drawdown = ((eq - eq.cummax()) / eq.cummax() * 100).min()

    return {
        "final_balance": round(balance, 2),
        "profit_usd": round(balance - INITIAL_BALANCE, 2),
        "profit_pct": round((balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100, 2),
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(wins / total * 100, 2) if total else 0.0,
        "avg_win_pct": round(sum(t for t in trades if t > 0) / wins, 2) if wins else 0.0,
        "avg_loss_pct": round(sum(t for t in trades if t <= 0) / losses, 2) if losses else 0.0,
        "max_drawdown_pct": round(drawdown, 2),
    }


def main():
    client = Client(os.getenv("BINANCE_API_KEY", ""), os.getenv("BINANCE_SECRET_KEY", ""))
    rows = []
    for asset in ASSETS:
        df = prepare(fetch_klines(client, asset))
        result = run_backtest(df)
        result.update({"asset": asset, "strategy": "ChampionsStrategy"})
        rows.append(result)
        print(
            f"{asset}: {result['profit_pct']:+.2f}% | WR {result['winrate_pct']:.1f}% "
            f"| trades {result['total_trades']} | DD {result['max_drawdown_pct']:.1f}%"
        )

    report = pd.DataFrame(rows)
    report.to_csv(OUT / "champions_strategy.csv", index=False, encoding="utf-8")

    total_final = report["final_balance"].sum()
    total_profit = total_final - INITIAL_BALANCE * len(report)
    total_wins = report["wins"].sum()
    total_trades = report["total_trades"].sum()

    print("\nTOTAIS")
    print(f"Profit: ${total_profit:+.2f} ({total_profit / (INITIAL_BALANCE * len(report)) * 100:+.2f}%)")
    print(f"Win rate ponderado: {total_wins / total_trades * 100:.2f}%")
    print(f"Win rate medio por ativo: {report['winrate_pct'].mean():.2f}%")
    print(f"Trades: {int(total_trades)}")
    print(f"Drawdown medio: {report['max_drawdown_pct'].mean():.2f}%")
    print(f"\nSalvo em {OUT / 'champions_strategy.csv'}")


if __name__ == "__main__":
    main()
