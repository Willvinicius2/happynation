"""Simula as estrategias atuais sem abrir ordens reais nem paper.

Usa os mesmos parametros Champions configurados em src/main.py, baixa candles
publicos da Binance e roda um backtest simples candle a candle.
"""

from __future__ import annotations

import os
import sys

import pandas as pd
from binance.client import Client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.backtest_champions_strategy import prepare  # noqa: E402


COLS = [
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

INITIAL_BALANCE = 1000.0
INTERVAL = Client.KLINE_INTERVAL_1HOUR
TOTAL_CANDLES = 3000
ROUNDTRIP_FEE_PCT = 0.20

PARAMS_BY_SYMBOL = {
    "BTCUSDT": {
        "take_profit_pct": 0.6,
        "stop_loss_pct": 2.0,
        "rsi_min": 46.0,
        "rsi_max": 72.0,
        "pullback_pct": 0.8,
        "max_hold_bars": 18,
    },
    "ETHUSDT": {
        "take_profit_pct": 0.8,
        "stop_loss_pct": 2.0,
        "rsi_min": 46.0,
        "rsi_max": 60.0,
        "pullback_pct": 0.5,
        "max_hold_bars": 12,
    },
    "SOLUSDT": {
        "take_profit_pct": 0.6,
        "stop_loss_pct": 1.5,
        "rsi_min": 38.0,
        "rsi_max": 64.0,
        "pullback_pct": 0.5,
        "max_hold_bars": 18,
    },
    "XRPUSDT": {
        "take_profit_pct": 0.9,
        "stop_loss_pct": 1.5,
        "rsi_min": 40.0,
        "rsi_max": 64.0,
        "pullback_pct": 0.7,
        "max_hold_bars": 18,
    },
    "ADAUSDT": {
        "take_profit_pct": 0.8,
        "stop_loss_pct": 1.0,
        "rsi_min": 46.0,
        "rsi_max": 60.0,
        "pullback_pct": 0.5,
        "max_hold_bars": 18,
    },
}


def fetch_klines(client: Client, symbol: str, total: int = TOTAL_CANDLES) -> pd.DataFrame:
    chunks = []
    end_time = None
    remaining = total

    while remaining > 0:
        limit = min(1000, remaining)
        params = {"symbol": symbol, "interval": INTERVAL, "limit": limit}
        if end_time is not None:
            params["endTime"] = end_time

        raw = client.get_klines(**params)
        if not raw:
            break

        frame = pd.DataFrame(raw, columns=COLS)
        chunks.append(frame)
        end_time = int(frame["open_time"].iloc[0]) - 1
        remaining -= len(frame)

        if len(frame) < limit:
            break

    if not chunks:
        return pd.DataFrame(columns=COLS)

    data = (
        pd.concat(chunks, ignore_index=True)
        .drop_duplicates("open_time")
        .sort_values("open_time")
        .reset_index(drop=True)
    )
    for column in ["open_price", "high_price", "low_price", "close_price", "volume"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data["open_time"] = pd.to_datetime(data["open_time"], unit="ms", utc=True)
    return data


def simulate(df: pd.DataFrame, params: dict[str, float], roundtrip_fee_pct: float) -> dict[str, object]:
    balance = INITIAL_BALANCE
    position = False
    entry_price = 0.0
    entry_index = 0
    trades: list[float] = []
    last_trades: list[dict[str, object]] = []
    equity = []

    for index in range(220, len(df)):
        row = df.iloc[index]
        prev = df.iloc[index - 1]
        price = float(row["close_price"])

        if position:
            reason = "SIGNAL"
            if float(row["low_price"]) <= entry_price * (1 - params["stop_loss_pct"] / 100):
                pnl_pct = -params["stop_loss_pct"]
                reason = "SL"
            elif float(row["high_price"]) >= entry_price * (1 + params["take_profit_pct"] / 100):
                pnl_pct = params["take_profit_pct"]
                reason = "TP"
            else:
                pnl_pct = (price - entry_price) / entry_price * 100
                sell_signal = (
                    (index - entry_index >= params["max_hold_bars"] and pnl_pct > 0)
                    or (pnl_pct > 0.20 and price < float(row["ema20"]) and float(row["rsi"]) < 48)
                    or (pnl_pct < 0 and price < float(row["ema50"]) and float(row["rsi"]) < 43)
                )
                if not sell_signal:
                    equity.append(balance * (price / entry_price))
                    continue

            net_pnl_pct = pnl_pct - roundtrip_fee_pct
            balance *= 1 + net_pnl_pct / 100
            trades.append(net_pnl_pct)
            last_trades.append(
                {
                    "exit_time": str(row["open_time"])[:16],
                    "reason": reason,
                    "net_pct": round(net_pnl_pct, 3),
                    "bars": index - entry_index,
                }
            )
            position = False
        else:
            recent = df.iloc[index - 5 : index + 1]
            trend_ok = price > row["ema200"] and row["ema20"] > row["ema50"] and row["ema50"] > row["ema200"]
            champion_ok = row["tema7"] > row["tema21"] and row["tema21_slope"] > 0
            pullback_ok = (
                bool((recent["low_price"] <= recent["ema20"] * (1 + params["pullback_pct"] / 100)).any())
                and price > row["ema8"]
            )
            momentum_ok = (
                row["rsi"] > prev["rsi"]
                and params["rsi_min"] <= row["rsi"] <= params["rsi_max"]
                and price > prev["close_price"]
                and row["ret3_pct"] > -2.5
            )
            volatility_ok = 0.10 <= row["atr_pct"] <= 7.0
            volume_ok = row["volume"] >= row["volume_ma24"] * 0.30

            if trend_ok and champion_ok and pullback_ok and momentum_ok and volatility_ok and volume_ok:
                position = True
                entry_price = price
                entry_index = index

        equity.append(balance if not position else balance * (price / entry_price))

    wins = sum(1 for trade in trades if trade > 0)
    losses = len(trades) - wins
    gross_profit = sum(trade for trade in trades if trade > 0)
    gross_loss = abs(sum(trade for trade in trades if trade <= 0))
    equity_series = pd.Series(equity or [INITIAL_BALANCE])
    drawdown_pct = ((equity_series - equity_series.cummax()) / equity_series.cummax() * 100).min()

    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "winrate_pct": round(wins / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else float("inf"),
        "profit_pct": round((balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100, 2),
        "final_balance": round(balance, 2),
        "max_drawdown_pct": round(abs(drawdown_pct), 2),
        "last_trades": last_trades[-3:],
    }


def verdict(result: dict[str, object]) -> str:
    if result["trades"] >= 20 and result["profit_factor"] >= 1.15 and result["max_drawdown_pct"] <= 25:
        return "OK_SIMULAR"
    return "NAO_ENTRAR"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    client = Client("", "")
    print(f"SIMULACAO CHAMPIONS | {TOTAL_CANDLES} candles 1h | capital ${INITIAL_BALANCE:.0f} por ativo")
    print(f"Taxa estimada: {ROUNDTRIP_FEE_PCT:.2f}% por trade completo")
    print()

    for symbol, params in PARAMS_BY_SYMBOL.items():
        data = prepare(fetch_klines(client, symbol))
        result = simulate(data, params, roundtrip_fee_pct=ROUNDTRIP_FEE_PCT)
        print(
            f"{symbol:8} profit={result['profit_pct']:+6.2f}% "
            f"final=${result['final_balance']:7.2f} trades={result['trades']:3} "
            f"WR={result['winrate_pct']:5.1f}% PF={result['profit_factor']:5.2f} "
            f"DD={result['max_drawdown_pct']:5.2f}% -> {verdict(result)}"
        )
        for trade in result["last_trades"]:
            print(
                f"         trade {trade['exit_time']} {trade['reason']:6} "
                f"net={trade['net_pct']:+.3f}% bars={trade['bars']}"
            )
        print()


if __name__ == "__main__":
    main()
