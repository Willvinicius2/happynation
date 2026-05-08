#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import sys
import pandas as pd
from binance.client import Client
import warnings

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

def run_backtest(df, take_profit_pct=0.9, stop_loss_pct=2.0):
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
            if float(row["low_price"]) <= entry_price * (1 - stop_loss_pct / 100):
                pnl_pct = -stop_loss_pct
            elif float(row["high_price"]) >= entry_price * (1 + take_profit_pct / 100):
                pnl_pct = take_profit_pct
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

def fetch_and_test(symbol, tp_value):
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
            return None
        return run_backtest(df, take_profit_pct=tp_value, stop_loss_pct=2.0)
    except Exception as e:
        return None

print("\n" + "="*80)
print("OTIMIZACAO DE TAKE PROFIT - ChampionsStrategy (1h)")
print("="*80 + "\n")

tp_values = [0.5, 0.7, 0.9, 1.2, 1.5, 2.0, 2.5, 3.0]

results_by_tp = {}

for tp in tp_values:
    print(f"\nTestando TP: {tp:.1f}% (SL: 2.0%)...")
    print("-"*60)

    total_profit = 0
    total_trades = 0
    total_wins = 0
    asset_results = []

    for asset in ASSETS:
        result = fetch_and_test(asset, tp)
        if result:
            print(f"  {asset:10} => ROI {result['roi']:+6.2f}% | {result['trades']:3} trades | WR {result['wr']:5.1f}%")
            total_profit += result['profit']
            total_trades += result['trades']
            total_wins += result['wins']
            asset_results.append(result)
        else:
            print(f"  {asset:10} => Erro")

    if asset_results:
        avg_roi = total_profit / len(asset_results) / INITIAL_BALANCE * 100
        avg_wr = total_wins / total_trades * 100 if total_trades > 0 else 0
        results_by_tp[tp] = {
            "profit": total_profit,
            "roi": avg_roi,
            "trades": total_trades,
            "wins": total_wins,
            "wr": avg_wr,
            "assets": len(asset_results)
        }
        print(f"\n  TOTAL: ${total_profit:+.2f} | ROI: {avg_roi:+.2f}% | WR: {avg_wr:.1f}%")

print("\n\n" + "="*80)
print("RANKING DE TAKE PROFIT")
print("="*80 + "\n")

sorted_results = sorted(results_by_tp.items(), key=lambda x: x[1]['roi'], reverse=True)

print(f"{'TP':>5} {'ROI':>8} {'WR':>6} {'TRADES':>8} {'PROFIT':>10} {'STATUS':<20}")
print("-"*65)

for tp, data in sorted_results:
    status = ""
    if tp == 0.9:
        status = "(ATUAL)"
    trades_per_asset = data['trades'] / data['assets']
    print(f"{tp:>5.1f}% {data['roi']:>7.2f}% {data['wr']:>5.1f}% {int(data['trades']):>8} ${data['profit']:>9.2f} {status:<20}")

print("\n" + "="*80)
if sorted_results:
    best_tp, best_data = sorted_results[0]
    print(f"RECOMENDACAO: TP = {best_tp:.1f}% => ROI +{best_data['roi']:.2f}%")
    print("="*80)

    # Comparar com TP atual (0.9%)
    current = results_by_tp.get(0.9, {})
    if current:
        improvement = best_data['roi'] - current['roi']
        print(f"\nMelhoria vs. TP 0.9%: {improvement:+.2f}% ({improvement/current['roi']*100:+.0f}%)")
        print(f"Novo ROI: {best_data['roi']:+.2f}% (vs. {current['roi']:+.2f}% atual)")
        print(f"Win Rate: {best_data['wr']:.1f}% (vs. {current['wr']:.1f}% atual)")
        print(f"Trades: {int(best_data['trades'])} (vs. {int(current['trades'])} atual)")
