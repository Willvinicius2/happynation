"""
Backtest alavancado 10x das TOP 10 estratégias.
Simula: liquidação a -9.5%, fee 0.04% por side (0.08% roundtrip no notional).
Banca inicial $1000 por ativo.
"""
import os
import sys
import io
import time
import json
from pathlib import Path

import pandas as pd
from binance.client import Client

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.ranking_top10_12m import (
    ASSETS, INTERVAL, HISTORY_START, INITIAL_BALANCE, TOP10, LOOKBACK,
    fetch_klines, load_top10_strategies,
)

LEVERAGE = 10.0
FEE_PCT_PER_SIDE = 0.04          # taker Binance futures
LIQ_THRESHOLD_PCT = 9.5           # -9.5% = liquidação com 10x (margem quase zerada)

OUT_DIR = Path("src/logs/ranking")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def run_leveraged_backtest(stock_data, strategy_fn, initial_balance=1000.0,
                            leverage=10.0, fee_pct=0.04, liq_thr_pct=9.5):
    data = stock_data.reset_index(drop=True).copy()

    balance = initial_balance
    position = 0
    entry_price = 0.0
    entry_idx = None
    notional_at_entry = 0.0
    last_signal = None
    trades = []
    equity = [balance]
    liquidated = False
    blown_up = False

    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        for i in range(1, len(data)):
            # Liquidation check PRIMEIRO (antes de ler novo sinal)
            if position == 1:
                low = data.iloc[i]["low_price"]
                liq_price = entry_price * (1 - liq_thr_pct / 100)
                if low <= liq_price:
                    # Perda = liq_thr sobre notional → consome balance inteira
                    pnl_pct_raw = -liq_thr_pct
                    pnl_usd = notional_at_entry * pnl_pct_raw / 100
                    fee_total = 2 * notional_at_entry * fee_pct / 100
                    balance = balance + pnl_usd - fee_total
                    if balance < 0:
                        balance = 0
                    trades.append({
                        "entry_price": entry_price, "exit_price": liq_price,
                        "pnl_pct_raw": pnl_pct_raw,
                        "pnl_pct_leveraged": pnl_pct_raw * leverage,
                        "pnl_usd": round(pnl_usd - fee_total, 2),
                        "fees_usd": round(fee_total, 2),
                        "liquidated": True, "bars_held": i - entry_idx,
                    })
                    position = 0
                    last_signal = "sell"
                    liquidated = True
                    if balance <= 1.0:
                        blown_up = True
                        balance = 0
                        break

            if balance <= 1.0:
                blown_up = True
                break

            start = max(0, i + 1 - LOOKBACK)
            current = data.iloc[start: i + 1]
            close_price = data.iloc[i]["close_price"]

            try:
                signal = strategy_fn(current, verbose=False)
            except Exception as e:
                return {"error": f"{type(e).__name__}: {e}"}

            if signal is None:
                equity.append(balance)
                continue

            if signal and position == 0 and last_signal != "buy":
                entry_price = close_price
                entry_idx = i
                notional_at_entry = balance * leverage
                position = 1
                last_signal = "buy"
            elif (not signal) and position == 1 and last_signal != "sell":
                pnl_pct_raw = (close_price - entry_price) / entry_price * 100
                pnl_usd_gross = notional_at_entry * pnl_pct_raw / 100
                fee_total = 2 * notional_at_entry * fee_pct / 100
                net = pnl_usd_gross - fee_total
                balance = balance + net
                if balance < 0:
                    balance = 0
                trades.append({
                    "entry_price": entry_price, "exit_price": close_price,
                    "pnl_pct_raw": round(pnl_pct_raw, 4),
                    "pnl_pct_leveraged": round(pnl_pct_raw * leverage, 4),
                    "pnl_usd": round(net, 2),
                    "fees_usd": round(fee_total, 2),
                    "liquidated": False, "bars_held": i - entry_idx,
                })
                position = 0
                last_signal = "sell"
                if balance <= 1.0:
                    blown_up = True
                    balance = 0
                    break

            # Equity mark-to-market
            if position == 1:
                mtm_pnl = notional_at_entry * ((close_price - entry_price) / entry_price)
                equity.append(balance + mtm_pnl)
            else:
                equity.append(balance)
    finally:
        sys.stdout = old_stdout

    # Fecha posição aberta ao final (sem fee extra; usa close price)
    if position == 1 and not blown_up:
        close_price = data.iloc[-1]["close_price"]
        pnl_pct_raw = (close_price - entry_price) / entry_price * 100
        pnl_usd_gross = notional_at_entry * pnl_pct_raw / 100
        fee_total = 2 * notional_at_entry * fee_pct / 100
        net = pnl_usd_gross - fee_total
        balance = max(0, balance + net)
        trades.append({
            "entry_price": entry_price, "exit_price": close_price,
            "pnl_pct_raw": round(pnl_pct_raw, 4),
            "pnl_pct_leveraged": round(pnl_pct_raw * leverage, 4),
            "pnl_usd": round(net, 2), "fees_usd": round(fee_total, 2),
            "liquidated": False, "bars_held": len(data)-1 - entry_idx, "forced_close": True,
        })

    total = len(trades)
    wins = sum(1 for t in trades if t["pnl_usd"] > 0)
    losses = total - wins
    winrate = (wins / total * 100) if total else 0.0
    liquidations = sum(1 for t in trades if t.get("liquidated"))
    total_fees = sum(t["fees_usd"] for t in trades)

    eq = pd.Series(equity)
    peak = eq.cummax().replace(0, 1)
    dd_pct = ((eq - peak) / peak * 100).min() if len(eq) else 0.0

    profit_usd = balance - initial_balance
    profit_pct = profit_usd / initial_balance * 100

    return {
        "final_balance": round(balance, 2),
        "profit_usd": round(profit_usd, 2),
        "profit_pct": round(profit_pct, 2),
        "total_trades": total,
        "wins": wins, "losses": losses,
        "winrate_pct": round(winrate, 2),
        "liquidations": liquidations,
        "blown_up": blown_up or balance <= 1.0,
        "total_fees_usd": round(total_fees, 2),
        "max_drawdown_pct": round(dd_pct, 2),
    }


def main():
    api = os.getenv("BINANCE_API_KEY", "")
    sec = os.getenv("BINANCE_SECRET_KEY", "")
    client = Client(api if "Cole" not in api else "", sec if "Cole" not in sec else "")

    print(f"⚡ Leverage: {LEVERAGE}x | Liquidação: -{LIQ_THRESHOLD_PCT}% | Fee: {FEE_PCT_PER_SIDE}%/side")
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
    print(f"\n🧩 Top 10 carregadas: {[n for n,_ in strategies]}")

    results = []
    total = len(ASSETS) * len(strategies)
    run = 0
    t_total = time.time()
    for sym, df in data.items():
        for (name, fn) in strategies:
            run += 1
            t0 = time.time()
            res = run_leveraged_backtest(df, fn, INITIAL_BALANCE, LEVERAGE,
                                         FEE_PCT_PER_SIDE, LIQ_THRESHOLD_PCT)
            dt = time.time() - t0
            res.update({"asset": sym, "strategy": name, "seconds": round(dt, 1)})
            results.append(res)
            if "error" in res:
                print(f"  [{run:03d}/{total}] {sym}/{name}: ERRO {res['error']}  {dt:.1f}s")
            else:
                status = "💥REKT" if res["blown_up"] else "OK"
                print(f"  [{run:03d}/{total}] {sym}/{name}: {status} "
                      f"{res['profit_pct']:+.2f}% (${res['profit_usd']:+.2f}) "
                      f"| {res['total_trades']} trades "
                      f"| WR {res['winrate_pct']:.1f}% "
                      f"| Liq {res['liquidations']} "
                      f"| Fees ${res['total_fees_usd']:.0f} "
                      f"| DD {res['max_drawdown_pct']:.1f}%  "
                      f"{dt:.1f}s")

    print(f"\n⏱️ Tempo total: {(time.time()-t_total)/60:.1f} min")

    df_res = pd.DataFrame(results)
    df_res.to_csv(OUT_DIR / "top10_12m_10x_detailed.csv", index=False, encoding="utf-8")
    with open(OUT_DIR / "top10_12m_10x_detailed.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    ok = df_res[~df_res.get("error", pd.Series([None]*len(df_res))).notna()].copy() if "error" in df_res else df_res.copy()

    agg = ok.groupby("strategy").agg(
        total_profit_usd=("profit_usd", "sum"),
        avg_profit_pct=("profit_pct", "mean"),
        total_trades=("total_trades", "sum"),
        avg_winrate=("winrate_pct", "mean"),
        total_liquidations=("liquidations", "sum"),
        blown_assets=("blown_up", "sum"),
        total_fees=("total_fees_usd", "sum"),
        avg_drawdown=("max_drawdown_pct", "mean"),
        assets_positive=("profit_pct", lambda s: (s > 0).sum()),
    ).round(2).sort_values("avg_profit_pct", ascending=False)

    print("\n" + "=" * 120)
    print(f"🏆 RANKING TOP 10 — 12 MESES — LEVERAGE {LEVERAGE}x")
    print("=" * 120)
    print(agg.to_string())
    agg.to_csv(OUT_DIR / "top10_12m_10x_ranking.csv", encoding="utf-8")

    print("\n" + "=" * 120)
    print("📋 DETALHE ATIVO × ESTRATÉGIA (profit %)")
    print("=" * 120)
    try:
        pivot = ok.pivot(index="strategy", columns="asset", values="profit_pct").round(2)
        print(pivot.to_string())
    except Exception as e:
        print(f"(pivot falhou: {e})")

    print("\n💾 Salvo em src/logs/ranking/:")
    print("   - top10_12m_10x_detailed.csv / .json")
    print("   - top10_12m_10x_ranking.csv")


if __name__ == "__main__":
    main()
