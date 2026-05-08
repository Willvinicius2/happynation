#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import sys
import pandas as pd
from binance.client import Client
import warnings
from itertools import product

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

def prepare(df, tema7_period=7, tema21_period=21):
    df = df.copy().reset_index(drop=True)
    if len(df) < 220:
        return None
    close = df["close_price"]
    for window in [8, 20, 50, 200]:
        df[f"ema{window}"] = close.ewm(span=window, adjust=False).mean()
    df["tema7"] = tema(close, tema7_period)
    df["tema21"] = tema(close, tema21_period)
    df["tema21_slope"] = df["tema21"].diff()
    df["rsi"] = rsi(close, period=14)
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

def run_backtest(df, rsi_min=40, rsi_max=64, pullback_pct=0.7):
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
            pullback_pct_adj = pullback_pct / 100
            pullback_ok = bool((recent["low_price"] <= recent["ema20"] * (1 + pullback_pct_adj)).any()) and price > row["ema8"]
            momentum_ok = row["rsi"] > prev["rsi"] and rsi_min <= row["rsi"] <= rsi_max and price > prev["close_price"]
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

def fetch_and_test(symbol, params):
    try:
        client = Client(os.getenv("BINANCE_API_KEY", ""), os.getenv("BINANCE_SECRET_KEY", ""))
        raw = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=1000)
        cols = ["open_time", "open_price", "high_price", "low_price", "close_price", "volume",
                "close_time", "qav", "trades", "tbbav", "tbqav", "_"]
        df = pd.DataFrame(raw, columns=cols)
        for col in ["open_price", "high_price", "low_price", "close_price", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = prepare(df, params.get('tema7', 7), params.get('tema21', 21))
        if df is None:
            return None
        return run_backtest(df, params['rsi_min'], params['rsi_max'], params['pullback_pct'])
    except Exception as e:
        return None

print("\n" + "="*80)
print("OTIMIZACAO DE PARAMETROS - ChampionsStrategy")
print("="*80 + "\n")

# Testar combinacoes de parametros
rsi_min_options = [35, 40, 45]
rsi_max_options = [60, 64, 70]
pullback_options = [0.5, 0.7, 1.0]

combinations = list(product(rsi_min_options, rsi_max_options, pullback_options))

print(f"Testando {len(combinations)} combinacoes de parametros...\n")

best_results = []

for idx, (rsi_min, rsi_max, pullback) in enumerate(combinations):
    print(f"\r[{idx+1}/{len(combinations)}] RSI {rsi_min}-{rsi_max}, PB {pullback:.1f}%", end="", flush=True)

    params = {
        'rsi_min': rsi_min,
        'rsi_max': rsi_max,
        'pullback_pct': pullback,
        'tema7': 7,
        'tema21': 21
    }

    total_profit = 0
    total_trades = 0
    total_wins = 0
    count = 0

    for asset in ASSETS:
        result = fetch_and_test(asset, params)
        if result:
            total_profit += result['profit']
            total_trades += result['trades']
            total_wins += result['wins']
            count += 1

    if count > 0:
        avg_roi = total_profit / count / INITIAL_BALANCE * 100
        avg_wr = total_wins / total_trades * 100 if total_trades > 0 else 0

        best_results.append({
            'rsi_min': rsi_min,
            'rsi_max': rsi_max,
            'pullback': pullback,
            'roi': avg_roi,
            'wr': avg_wr,
            'trades': total_trades,
            'profit': total_profit
        })

print("\n\n" + "="*80)
print("TOP 10 COMBINACOES OTIMIZADAS")
print("="*80 + "\n")

best_results_sorted = sorted(best_results, key=lambda x: x['roi'], reverse=True)[:10]

print(f"{'RSI':^12} {'PB%':^6} {'ROI':>8} {'WR':>6} {'TRADES':>8} {'PROFIT':>10}")
print("-"*65)

current = best_results_sorted[0]
for r in best_results_sorted:
    status = " (MELHOR)" if r == current else ""
    print(f"{r['rsi_min']}-{r['rsi_max']:2d}    {r['pullback']:>5.1f}  {r['roi']:>7.2f}% {r['wr']:>5.1f}% {int(r['trades']):>8} ${r['profit']:>9.2f}{status}")

print("\n" + "="*80)
print(f"MELHOR: RSI {current['rsi_min']}-{current['rsi_max']} | PB {current['pullback']:.1f}% => ROI +{current['roi']:.2f}%")
print("="*80)

# Comparar com config padrao (40-64, 0.7)
default = next((r for r in best_results if r['rsi_min'] == 40 and r['rsi_max'] == 64 and r['pullback'] == 0.7), None)
if default:
    improvement = current['roi'] - default['roi']
    print(f"\nMelhoria vs. padrao (RSI 40-64, PB 0.7%): {improvement:+.2f}%")
    print(f"Novo ROI: {current['roi']:.2f}% vs. {default['roi']:.2f}% (padrao)")
