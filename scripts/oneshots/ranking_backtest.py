"""
Backtest de TODAS as estratégias disponíveis em múltiplos ativos e
ranqueamento por profit%. Período: 6 meses de candles 1h.
Rodar: python -m src.ranking_backtest
"""
import os
import sys
import io
import time
import json
import traceback
import importlib
import inspect
from pathlib import Path

import pandas as pd
from binance.client import Client

# UTF-8 no Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ---- Config ----
ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "JUPUSDT"]
INTERVAL = Client.KLINE_INTERVAL_1HOUR
HISTORY_START = "6 months ago UTC"
INITIAL_BALANCE = 1000.0

OUT_DIR = Path("src/logs/ranking")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---- Data fetcher (paginado via get_historical_klines) ----
def fetch_klines(client: Client, symbol: str, interval: str, start_str: str) -> pd.DataFrame:
    raw = client.get_historical_klines(symbol, interval, start_str)
    cols = [
        "open_time", "open_price", "high_price", "low_price", "close_price",
        "volume", "close_time", "qav", "trades", "tbbav", "tbqav", "_",
    ]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["open_price", "high_price", "low_price", "close_price", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    # Adiciona aliases esperados pelas estratégias extras (close, high, low, open, volume)
    df["open"] = df["open_price"]
    df["high"] = df["high_price"]
    df["low"] = df["low_price"]
    df["close"] = df["close_price"]
    return df[["open_time", "open_price", "high_price", "low_price", "close_price",
               "volume", "open", "high", "low", "close"]]


# ---- Carrega estratégias ----
def load_strategies():
    """Retorna lista de (name, callable, kwargs_default, needs_instance)."""
    strats = []

    # Main strategies
    from src.strategies.moving_average import getMovingAverageTradeStrategy
    from src.strategies.moving_average_antecipation import getMovingAverageAntecipationTradeStrategy
    from src.strategies.rsi_strategy import getRsiTradeStrategy
    from src.strategies.vortex_strategy import getVortexTradeStrategy
    from src.strategies.ma_rsi_volume_strategy import getMovingAverageRSIVolumeStrategy
    from src.strategies.chilo import getChiloStrategy
    from src.strategies.ut_bot_alerts import utBotAlerts
    import src.strategies.chilo_rsi as chilo_rsi_mod
    chilo_rsi_mod.USE_IN_MEMORY_ONLY = True

    strats.append(("MovingAverage", getMovingAverageTradeStrategy, {}, False))
    strats.append(("MovingAverageAntecipation", getMovingAverageAntecipationTradeStrategy, {}, False))
    strats.append(("RSI", getRsiTradeStrategy, {}, False))
    strats.append(("Vortex", getVortexTradeStrategy, {}, False))
    strats.append(("MA_RSI_Volume", getMovingAverageRSIVolumeStrategy, {}, False))
    strats.append(("ChiloOriginal", getChiloStrategy, {}, False))
    strats.append(("UTBotAlerts", utBotAlerts, {}, False))
    strats.append(("ChiloRSI", chilo_rsi_mod.ChiloRSIStrategy, {}, True))

    # Extras — descoberta dinâmica
    extras_dir = Path("src/strategies/extras")
    for py in sorted(extras_dir.glob("*.py")):
        if py.name in ("__init__.py", "create_strategies.py"):
            continue
        mod_name = f"src.strategies.extras.{py.stem}"
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            print(f"  [skip import] {py.name}: {e}")
            continue
        for name, obj in inspect.getmembers(mod, inspect.isfunction):
            if name.startswith("get") and name.endswith("TradeStrategy"):
                short = name[3:-len("TradeStrategy")] or name
                strats.append((f"X_{short}", obj, {}, False))
                break
    return strats


# ---- Backtest runner simplificado (isolado, sem I/O) ----
def run_backtest(stock_data: pd.DataFrame, strategy_fn, needs_instance, periods: int, initial_balance: float, **kwargs):
    """Retorna (profit_pct, trades, error_or_None). Silencia stdout da estratégia."""
    import src.strategies.chilo_rsi as chilo_rsi_mod

    # Fake trader para estratégias que precisam de self
    class FakeTrader:
        def __init__(self):
            self.operation_code = "BACKTEST_ASSET"
            self.actual_trade_position = False
        def get_asset_state(self, a):
            return chilo_rsi_mod.get_asset_state(a)
        def update_asset_state(self, a, **kw):
            return chilo_rsi_mod.update_asset_state(a, **kw)
    fake = FakeTrader() if needs_instance else None

    min_required = kwargs.get("slow_window", 60) + 20
    data = stock_data[-max(periods, min_required):].copy().reset_index(drop=True)
    data.dropna(subset=["close_price"], inplace=True)

    balance = initial_balance
    position = 0
    entry_price = 0.0
    last_signal = None
    trades = 0

    LOOKBACK = 500  # janela deslizante; 99% das estratégias olham últimos 50-200 candles
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        for i in range(1, len(data)):
            start = max(0, i + 1 - LOOKBACK)
            current = data.iloc[start : i + 1]
            close_price = data.iloc[i]["close_price"]
            try:
                if needs_instance:
                    signal = strategy_fn(fake, current, verbose=False, **kwargs)
                else:
                    signal = strategy_fn(current, verbose=False, **kwargs)
            except Exception as e:
                return None, 0, f"runtime: {e.__class__.__name__}: {e}"

            if signal is None:
                continue
            if signal and position == 0 and last_signal != "buy":
                position = 1
                entry_price = close_price
                last_signal = "buy"
                trades += 1
                if fake is not None:
                    fake.actual_trade_position = True
            elif (not signal) and position == 1 and last_signal != "sell":
                position = 0
                profit = ((close_price - entry_price) / entry_price) * balance
                balance += profit
                last_signal = "sell"
                trades += 1
                if fake is not None:
                    fake.actual_trade_position = False

        # Fecha posição final se aberta
        if position == 1:
            final_price = data.iloc[-1]["close_price"]
            balance += ((final_price - entry_price) / entry_price) * balance
    finally:
        sys.stdout = old_stdout

    profit_pct = ((balance - initial_balance) / initial_balance) * 100
    return profit_pct, trades, None


# ---- MAIN ----
def main():
    api = os.getenv("BINANCE_API_KEY", "")
    sec = os.getenv("BINANCE_SECRET_KEY", "")
    # Endpoints públicos (klines) não precisam de auth; Client aceita strings vazias
    client = Client(api if "Cole" not in api else "", sec if "Cole" not in sec else "")

    print(f"📥 Baixando 6 meses de candles 1h para {len(ASSETS)} ativos...")
    data_by_asset = {}
    for sym in ASSETS:
        t0 = time.time()
        try:
            df = fetch_klines(client, sym, INTERVAL, HISTORY_START)
            data_by_asset[sym] = df
            print(f"  ✅ {sym}: {len(df)} candles  ({time.time()-t0:.1f}s)")
        except Exception as e:
            print(f"  ❌ {sym}: {e}")

    print(f"\n🧩 Carregando estratégias...")
    strategies = load_strategies()
    print(f"  Total: {len(strategies)} estratégias")

    results = []  # list of dicts
    total_runs = len(ASSETS) * len(strategies)
    run = 0
    t_total = time.time()
    for sym, df in data_by_asset.items():
        periods = len(df)
        for (name, fn, kwargs, needs_inst) in strategies:
            run += 1
            t0 = time.time()
            profit, trades, err = run_backtest(df, fn, needs_inst, periods, INITIAL_BALANCE, **kwargs)
            dt = time.time() - t0
            if err:
                print(f"  [{run:03d}/{total_runs}] {sym} / {name}: ERRO ({err})  {dt:.1f}s")
                results.append({"asset": sym, "strategy": name, "profit_pct": None,
                                "trades": 0, "error": err, "seconds": dt})
            else:
                print(f"  [{run:03d}/{total_runs}] {sym} / {name}: {profit:+.2f}%  ({trades} trades, {dt:.1f}s)")
                results.append({"asset": sym, "strategy": name, "profit_pct": profit,
                                "trades": trades, "error": None, "seconds": dt})

    print(f"\n⏱️ Tempo total: {(time.time()-t_total)/60:.1f} min")

    # Salva JSON + CSV
    with open(OUT_DIR / "results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    df_res = pd.DataFrame(results)
    df_res.to_csv(OUT_DIR / "results.csv", index=False, encoding="utf-8")

    # ---- RANKING ----
    valid = df_res[df_res["error"].isna()].copy()

    print("\n" + "=" * 80)
    print("🏆 RANKING POR ATIVO (top 10 por profit%)")
    print("=" * 80)
    for sym in ASSETS:
        sub = valid[valid["asset"] == sym].sort_values("profit_pct", ascending=False).head(10)
        print(f"\n📌 {sym}")
        print(sub[["strategy", "profit_pct", "trades"]].to_string(index=False))

    print("\n" + "=" * 80)
    print("🏆 RANKING GLOBAL (média de profit% por estratégia, todos os ativos)")
    print("=" * 80)
    agg = valid.groupby("strategy").agg(
        avg_profit=("profit_pct", "mean"),
        median_profit=("profit_pct", "median"),
        assets_run=("asset", "count"),
        avg_trades=("trades", "mean"),
    ).sort_values("avg_profit", ascending=False)
    print(agg.to_string())

    # Salva ranking global
    agg.to_csv(OUT_DIR / "ranking_global.csv", encoding="utf-8")

    # Erros
    errs = df_res[df_res["error"].notna()]
    if len(errs):
        print(f"\n⚠️ {len(errs)} execuções com erro (ver results.csv)")


if __name__ == "__main__":
    main()
