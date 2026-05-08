"""Teste de paridade: roda a CLASSE REAL do bot (getMovingAverageTradeStrategy)
no historico ETHUSDT com simulacao simples e compara com o resultado do sweep
numpy (ma_crossover). Se as metricas baterem, a troca em main.py e segura.
Se divergirem, sweep nao representa o que vai rodar live.
"""

from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from binance.client import Client  # noqa: E402
from src.strategies.moving_average import getMovingAverageTradeStrategy  # noqa: E402


def fetch_paginated(client, symbol, total=3000):
    cols = ["open_time","open_price","high_price","low_price","close_price","volume",
            "close_time","qav","trades","tbbav","tbqav","_"]
    chunks = []
    end_time = None
    remaining = total
    while remaining > 0:
        limit = min(1000, remaining)
        params = {"symbol": symbol, "interval": Client.KLINE_INTERVAL_1HOUR, "limit": limit}
        if end_time is not None:
            params["endTime"] = end_time
        raw = client.get_klines(**params)
        if not raw:
            break
        df = pd.DataFrame(raw, columns=cols)
        chunks.append(df)
        end_time = int(df["open_time"].iloc[0]) - 1
        remaining -= len(df)
        if len(df) < limit:
            break
    df_all = pd.concat(chunks).drop_duplicates(subset=["open_time"]).sort_values("open_time")
    for c in ["open_price","high_price","low_price","close_price","volume"]:
        df_all[c] = pd.to_numeric(df_all[c], errors="coerce")
    return df_all.reset_index(drop=True)


def simulate_real_class(df, fast_window=12, slow_window=21, take_profit_pct=0.8,
                        stop_loss_pct=2.0, max_hold_bars=24):
    """Simula como o bot real usaria getMovingAverageTradeStrategy:
    a cada candle, chama a funcao com o slice ate aquele momento, e segue
    o sinal True (comprar) / False (vender). TP/SL aplicados intrabar."""
    balance = 1000.0
    position = False
    entry_price = 0.0
    entry_index = 0
    trades = []

    close = df["close_price"].to_numpy()
    high = df["high_price"].to_numpy()
    low = df["low_price"].to_numpy()
    n = len(df)

    last_signal = None
    for i in range(slow_window + 5, n):
        slice_df = df.iloc[: i + 1]
        signal = getMovingAverageTradeStrategy(
            slice_df, fast_window=fast_window, slow_window=slow_window, verbose=False,
        )
        price = close[i]

        if position:
            stop = entry_price * (1 - stop_loss_pct / 100)
            tp = entry_price * (1 + take_profit_pct / 100)
            if low[i] <= stop:
                pnl = -stop_loss_pct
                exit_now = True
            elif high[i] >= tp:
                pnl = take_profit_pct
                exit_now = True
            else:
                pnl = (price - entry_price) / entry_price * 100
                exit_now = (i - entry_index) >= max_hold_bars or signal is False
            if exit_now:
                balance *= 1 + pnl / 100
                trades.append(pnl)
                position = False
        else:
            # entra no cruzamento (era False/None, virou True)
            if signal is True and last_signal is not True:
                position = True
                entry_price = price
                entry_index = i
        last_signal = signal

    if position:
        pnl = (close[-1] - entry_price) / entry_price * 100
        balance *= 1 + pnl / 100
        trades.append(pnl)

    if not trades:
        return None
    arr = np.array(trades)
    wins = int((arr > 0).sum())
    losses = len(arr) - wins
    avg_win = arr[arr > 0].mean() if wins else 0
    avg_loss = abs(arr[arr <= 0].mean()) if losses else 0
    pf = (avg_win * wins) / (avg_loss * losses) if losses and avg_loss else (float("inf") if wins else 0)
    eq = np.cumprod(1 + arr / 100)
    dd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq) * 100).min() if len(eq) > 1 else 0
    return {
        "trades": len(arr), "wins": wins, "losses": losses,
        "winrate": wins / len(arr) * 100,
        "profit_factor": pf,
        "max_drawdown_pct": dd,
        "profit_pct": (balance - 1000) / 10,
    }


def main():
    api_key = os.getenv("BINANCE_API_KEY", "") or ""
    api_secret = os.getenv("BINANCE_SECRET_KEY", "") or ""
    client = Client(api_key.strip('"'), api_secret.strip('"'))

    # Sweep numpy ETH ma_crossover melhor: fast=12, slow=21, TP=0.8, SL=2.0, hold=24, PF 4.23
    cases = [
        ("ETHUSDT", 12, 21),
        ("SOLUSDT", 5, 50),
        ("XRPUSDT", 9, 50),
    ]
    for asset, fast, slow in cases:
        print(f"\n>>> {asset} (fast={fast} slow={slow})")
        df = fetch_paginated(client, asset, total=3000)
        result = simulate_real_class(df, fast_window=fast, slow_window=slow,
                                     take_profit_pct=0.8, stop_loss_pct=2.0,
                                     max_hold_bars=24)
        if result is None:
            print("  sem trades")
            continue
        print(f"  classe REAL (SMA, sem cruzamento explicito):")
        print(f"    trades={result['trades']}  WR={result['winrate']:.1f}%  PF={result['profit_factor']:.2f}  DD={abs(result['max_drawdown_pct']):.1f}%  profit={result['profit_pct']:+.1f}%")


if __name__ == "__main__":
    main()
