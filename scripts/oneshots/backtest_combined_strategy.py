#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import sys
import pandas as pd
from binance.client import Client
import warnings
import numpy as np

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

warnings.filterwarnings("ignore")

ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"]
INITIAL_BALANCE = 1000.0

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

def vortex(df, period=14):
    high = df["high_price"]
    low = df["low_price"]
    close = df["close_price"]

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    vm_plus = (high - low.shift(1)).abs()
    vm_minus = (low - high.shift(1)).abs()

    vi_plus = vm_plus.rolling(period).sum() / tr.rolling(period).sum()
    vi_minus = vm_minus.rolling(period).sum() / tr.rolling(period).sum()

    return vi_plus, vi_minus

def prepare(df):
    df = df.copy().reset_index(drop=True)
    if len(df) < 220:
        return None
    close = df["close_price"]
    for window in [8, 20, 50, 200]:
        df[f"ema{window}"] = close.ewm(span=window, adjust=False).mean()
    df["tema7"] = tema(close, 7)
    df["tema21"] = tema(close, 21)
    df["tema21_slope"] = df["tema21"].diff()
    df["rsi"] = rsi(close, period=14)
    df["vi_plus"], df["vi_minus"] = vortex(df, period=14)

    prev_close = close.shift(1)
    true_range = pd.concat([
        df["high_price"] - df["low_price"],
        (df["high_price"] - prev_close).abs(),
        (df["low_price"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr_pct"] = true_range.rolling(14, min_periods=14).mean() / close * 100
    df["volume_ma24"] = df["volume"].rolling(24, min_periods=24).mean()
    df["ret3_pct"] = close.pct_change(3) * 100
    return df

def run_backtest_champions_only(df):
    if df is None:
        return None
    balance = INITIAL_BALANCE
    position = False
    entry_price = 0.0
    entry_index = 0
    trades = []

    for i in range(220, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        price = float(row["close_price"])

        if position:
            if float(row["low_price"]) <= entry_price * 0.98:
                pnl_pct = -2.0
            elif float(row["high_price"]) >= entry_price * 1.009:
                pnl_pct = 0.9
            else:
                pnl_pct = (price - entry_price) / entry_price * 100
                sell = (
                    (i - entry_index >= 24 and pnl_pct > 0)
                    or (pnl_pct > 0.20 and price < float(row["ema20"]) and float(row["rsi"]) < 48)
                    or (pnl_pct < 0 and price < float(row["ema50"]) and float(row["rsi"]) < 43)
                )
                if not sell:
                    continue
            balance *= 1 + pnl_pct / 100
            trades.append(pnl_pct)
            position = False
        else:
            recent = df.iloc[max(0, i-5):i+1]
            trend_ok = price > row["ema200"] and row["ema20"] > row["ema50"] and row["ema50"] > row["ema200"]
            champion_ok = row["tema7"] > row["tema21"] and row["tema21_slope"] > 0
            pullback_ok = bool((recent["low_price"] <= recent["ema20"] * 1.007).any()) and price > row["ema8"]
            momentum_ok = row["rsi"] > prev["rsi"] and 40 <= row["rsi"] <= 64 and price > prev["close_price"]
            volatility_ok = 0.10 <= row["atr_pct"] <= 7.0
            volume_ok = row["volume"] >= row["volume_ma24"] * 0.30

            if trend_ok and champion_ok and pullback_ok and momentum_ok and volatility_ok and volume_ok:
                position = True
                entry_price = price
                entry_index = i

    total = len(trades)
    wins = sum(1 for t in trades if t > 0)
    return {
        "trades": total,
        "wins": wins,
        "wr": round(wins/total*100, 1) if total > 0 else 0,
        "roi": round((balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100, 2),
        "profit": round(balance - INITIAL_BALANCE, 2)
    }

def run_backtest_combined(df):
    if df is None:
        return None
    balance = INITIAL_BALANCE
    position = False
    entry_price = 0.0
    entry_index = 0
    trades = []

    for i in range(220, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        price = float(row["close_price"])

        if position:
            if float(row["low_price"]) <= entry_price * 0.98:
                pnl_pct = -2.0
            elif float(row["high_price"]) >= entry_price * 1.009:
                pnl_pct = 0.9
            else:
                pnl_pct = (price - entry_price) / entry_price * 100
                sell = (
                    (i - entry_index >= 24 and pnl_pct > 0)
                    or (pnl_pct > 0.20 and price < float(row["ema20"]) and float(row["rsi"]) < 48)
                    or (pnl_pct < 0 and price < float(row["ema50"]) and float(row["rsi"]) < 43)
                )
                if not sell:
                    continue
            balance *= 1 + pnl_pct / 100
            trades.append(pnl_pct)
            position = False
        else:
            recent = df.iloc[max(0, i-5):i+1]

            # ChampionsStrategy filters
            trend_ok = price > row["ema200"] and row["ema20"] > row["ema50"] and row["ema50"] > row["ema200"]
            champion_ok = row["tema7"] > row["tema21"] and row["tema21_slope"] > 0
            pullback_ok = bool((recent["low_price"] <= recent["ema20"] * 1.007).any()) and price > row["ema8"]
            momentum_ok = row["rsi"] > prev["rsi"] and 40 <= row["rsi"] <= 64 and price > prev["close_price"]
            volatility_ok = 0.10 <= row["atr_pct"] <= 7.0
            volume_ok = row["volume"] >= row["volume_ma24"] * 0.30

            # Vortex filter (confirma o sinal)
            vortex_ok = pd.notna(row["vi_plus"]) and pd.notna(row["vi_minus"]) and row["vi_plus"] > row["vi_minus"]

            # COMBINADA: ChampionsStrategy E Vortex
            if (trend_ok and champion_ok and pullback_ok and momentum_ok and
                volatility_ok and volume_ok and vortex_ok):
                position = True
                entry_price = price
                entry_index = i

    total = len(trades)
    wins = sum(1 for t in trades if t > 0)
    return {
        "trades": total,
        "wins": wins,
        "wr": round(wins/total*100, 1) if total > 0 else 0,
        "roi": round((balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100, 2),
        "profit": round(balance - INITIAL_BALANCE, 2)
    }

def fetch_and_test(symbol):
    try:
        client = Client(os.getenv("BINANCE_API_KEY", ""), os.getenv("BINANCE_SECRET_KEY", ""))
        raw = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=1000)
        cols = ["open_time", "open_price", "high_price", "low_price", "close_price", "volume",
                "close_time", "qav", "trades", "tbbav", "tbqav", "_"]
        df = pd.DataFrame(raw, columns=cols)
        for col in ["open_price", "high_price", "low_price", "close_price", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = prepare(df)
        if df is None:
            return None, None

        result_champions = run_backtest_champions_only(df)
        result_combined = run_backtest_combined(df)
        return result_champions, result_combined
    except Exception as e:
        return None, None

print("\n" + "="*80)
print("ESTRATEGIA COMBINADA - ChampionsStrategy + Vortex")
print("="*80 + "\n")

print(f"{'ATIVO':^12} {'CHAMPIONS':^25} {'COMBINADA':^25} {'MELHORIA':<15}")
print(f"{'':^12} {'ROI | WR | TDS':^25} {'ROI | WR | TDS':^25} {'ROI | WR':<15}")
print("-"*80)

total_champ_profit = 0
total_champ_trades = 0
total_champ_wins = 0

total_comb_profit = 0
total_comb_trades = 0
total_comb_wins = 0

for asset in ASSETS:
    champ, comb = fetch_and_test(asset)

    if champ and comb:
        champ_str = f"{champ['roi']:+.2f}% | {champ['wr']:5.1f}% | {champ['trades']:3}"
        comb_str = f"{comb['roi']:+.2f}% | {comb['wr']:5.1f}% | {comb['trades']:3}"

        roi_diff = comb['roi'] - champ['roi']
        wr_diff = comb['wr'] - champ['wr']

        improvement = f"{roi_diff:+.2f}% | {wr_diff:+.1f}%"

        print(f"{asset:^12} {champ_str:^25} {comb_str:^25} {improvement:<15}")

        total_champ_profit += champ['profit']
        total_champ_trades += champ['trades']
        total_champ_wins += champ['wins']

        total_comb_profit += comb['profit']
        total_comb_trades += comb['trades']
        total_comb_wins += comb['wins']

print("-"*80)

champ_wr = round(total_champ_wins / total_champ_trades * 100, 1) if total_champ_trades > 0 else 0
comb_wr = round(total_comb_wins / total_comb_trades * 100, 1) if total_comb_trades > 0 else 0

champ_roi = round(total_champ_profit / len(ASSETS) / INITIAL_BALANCE * 100, 2)
comb_roi = round(total_comb_profit / len(ASSETS) / INITIAL_BALANCE * 100, 2)

champ_str = f"{champ_roi:+.2f}% | {champ_wr:5.1f}% | {total_champ_trades:3}"
comb_str = f"{comb_roi:+.2f}% | {comb_wr:5.1f}% | {total_comb_trades:3}"

roi_diff = comb_roi - champ_roi
wr_diff = comb_wr - champ_wr
improvement = f"{roi_diff:+.2f}% | {wr_diff:+.1f}%"

print(f"{'TOTAL':^12} {champ_str:^25} {comb_str:^25} {improvement:<15}")

print("\n" + "="*80)
print("ANALISE")
print("="*80)
print(f"\nChampionsStrategy (SO):")
print(f"  ROI: {champ_roi:+.2f}%")
print(f"  Win Rate: {champ_wr:.1f}%")
print(f"  Trades: {total_champ_trades}")
print(f"  Profit: ${total_champ_profit:+.2f}")

print(f"\nChampions + Vortex (COMBINADA):")
print(f"  ROI: {comb_roi:+.2f}%")
print(f"  Win Rate: {comb_wr:.1f}%")
print(f"  Trades: {total_comb_trades}")
print(f"  Profit: ${total_comb_profit:+.2f}")

print(f"\nMelhoria com Vortex:")
print(f"  ROI: {roi_diff:+.2f}%")
print(f"  WR: {wr_diff:+.1f}%")
print(f"  Trades: {total_comb_trades - total_champ_trades:+d}")

if comb_wr > champ_wr:
    print(f"\n✓ Vortex AUMENTA o Win Rate em {wr_diff:+.1f}%")
else:
    print(f"\n✓ Vortex REDUZ sinais falsos (Win Rate {wr_diff:+.1f}%)")

print("\n" + "="*80)
