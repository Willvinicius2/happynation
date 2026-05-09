"""Sniper & Strategy by CSBender_ -- backtest no motor do Aether.

Logica REAL (resto do Pine eh visualizacao):
  Piriod = '720'  // 720 minutos = 12h
  ch1 = security(tickerid, '720', open)
  ch2 = security(tickerid, '720', close)
  longCondition  = crossover(ch2, ch1)   // close 12h cruza acima de open 12h
  shortCondition = crossunder(ch2, ch1)

Spot Binance long-only: V1 sai quando short signal (=0); V2 long+short.
TFs testados: TODOS (1m, 5m, 15m, 1h, 4h, 1d) por orientacao do operador.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.research.aether.compute import _run_local_backtest, load_dataset_fast


# Em minutos
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


def sniper_signals(
    df: pd.DataFrame,
    *,
    tf_label: str,
    period_minutes: int = 720,
    enable_short: bool = False,
) -> pd.Series:
    """Gera sinais Sniper baseado em cruzamentos open/close em candles de 720min.

    Para TFs menores que 720min: resample pra 720min, calcula cruzamento, ffill.
    Para TFs >= 720min (1d=1440): usa cruzamento do proprio candle do TF.
    """
    n = len(df)
    base_min = TF_MINUTES.get(tf_label, 60)

    if base_min < period_minutes:
        # Resample para 720min
        df_idx = df.copy()
        # open_time pode ser datetime (timezone aware ou naive) ou int (ms)
        if "open_time" in df_idx.columns:
            ot = df_idx["open_time"]
            if not pd.api.types.is_datetime64_any_dtype(ot):
                ot = pd.to_datetime(ot, utc=True)
            df_idx = df_idx.assign(_t=ot).set_index("_t").sort_index()
        else:
            df_idx = df_idx.copy()
            df_idx.index = pd.date_range("2020-01-01", periods=n, freq=f"{base_min}min")

        rule = f"{period_minutes}min"
        # closed='right', label='right': cada bucket eh fechado/identificado pelo
        # FIM do periodo (ex: bucket 12h fechado em 12:00 corresponde ao label 12:00).
        # Isso garante que o sinal calculado no fim do periodo so eh aplicado
        # a candles APOS o fim (ffill prospectivo, sem look-ahead).
        agg = df_idx.resample(rule, label="right", closed="right").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
        }).dropna()

        o = agg["open"]
        c = agg["close"]
        long_cond = (c > o) & (c.shift(1) <= o.shift(1))
        short_cond = (c < o) & (c.shift(1) >= o.shift(1))

        pos_arr = np.zeros(len(agg), dtype=np.int8)
        cur = 0
        long_arr = long_cond.fillna(False).values
        short_arr = short_cond.fillna(False).values
        for i in range(len(agg)):
            if long_arr[i]:
                cur = 1
            elif short_arr[i]:
                cur = -1 if enable_short else 0
            pos_arr[i] = cur
        pos_series = pd.Series(pos_arr, index=agg.index)

        # Shift +1 periodo: o sinal so vale a partir do periodo SEGUINTE (sem look-ahead).
        # Reindex para o df original via ffill -- agora propaga so pra frente.
        pos_full = pos_series.reindex(df_idx.index, method="ffill").shift(1).fillna(0).astype(int)
        return pd.Series(pos_full.values.astype(np.int8), index=df.index)
    else:
        # TF >= 12h: usa o proprio candle
        o = df["open"].astype(float).reset_index(drop=True)
        c = df["close"].astype(float).reset_index(drop=True)
        long_cond = ((c > o) & (c.shift(1) <= o.shift(1))).fillna(False).values
        short_cond = ((c < o) & (c.shift(1) >= o.shift(1))).fillna(False).values
        pos_arr = np.zeros(n, dtype=np.int8)
        cur = 0
        for i in range(n):
            if long_cond[i]:
                cur = 1
            elif short_cond[i]:
                cur = -1 if enable_short else 0
            pos_arr[i] = cur
        return pd.Series(pos_arr, index=df.index)


# =========================================================================
# Matriz
# =========================================================================
ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]


def run_matrix(*, enable_short: bool = False, label: str = "") -> list[dict]:
    rows: list[dict] = []
    print(f"Rodando matriz Sniper CSBender ({label})...\n")
    for asset in ASSETS:
        for tf in TIMEFRAMES:
            t0 = time.time()
            try:
                df = load_dataset_fast(f"binance:{asset}:{tf}")
                if len(df) < 250:
                    rows.append({"asset": asset, "tf": tf, "ok": False,
                                 "error": f"few candles ({len(df)})"})
                    continue
                sig = sniper_signals(df, tf_label=tf, enable_short=enable_short)
                r = _run_local_backtest(df, sig, capital=1000.0, fee_bps=10.0,
                                        slippage_bps=2.0, timeframe=tf)
                r["asset"] = asset; r["tf"] = tf
                r["elapsed_s"] = round(time.time() - t0, 2)
                rows.append(r)
                pnl = f"{r.get('pnl_pct', 0):+.2f}%" if r.get("ok") else "FAIL"
                print(f"  {asset} {tf}: {pnl} ({r.get('elapsed_s', '?')}s)")
            except Exception as exc:
                rows.append({"asset": asset, "tf": tf, "ok": False,
                             "error": f"{exc.__class__.__name__}: {str(exc)[:100]}",
                             "elapsed_s": round(time.time() - t0, 2)})
                print(f"  {asset} {tf}: ERROR {str(exc)[:60]}")
    return rows


def report(rows: list[dict], title: str) -> None:
    print(f"\n{'='*100}\n {title}\n{'='*100}")
    header = f"{'asset':<10} {'tf':<5} {'bars':>8} {'pnl%':>9} {'trades':>7} {'mdd%':>7} {'hit%':>6} {'sharpe':>8}"
    print(header); print("-" * len(header))
    ok_rows = []
    for r in rows:
        if r.get("ok"):
            hit = (r["hit_rate"] or 0) * 100 if r.get("hit_rate") is not None else 0
            print(f"{r['asset']:<10} {r['tf']:<5} {r['bars']:>8} "
                  f"{r['pnl_pct']:>+8.2f}% {r['trade_count']:>7} "
                  f"{r['mdd_pct']:>6.2f}% {hit:>5.1f}% {r['sharpe']:>+8.2f}")
            ok_rows.append(r)
        else:
            print(f"{r['asset']:<10} {r['tf']:<5}  FAILED  {r.get('error', '?')[:60]}")
    if ok_rows:
        avg_pnl = float(np.mean([r["pnl_pct"] for r in ok_rows]))
        positives = sum(1 for r in ok_rows if r["pnl_pct"] > 0)
        avg_mdd = float(np.mean([r["mdd_pct"] for r in ok_rows]))
        print("-" * len(header))
        print(f"resumo: {len(ok_rows)} OK | positivos {positives}/{len(ok_rows)} | "
              f"pnl medio {avg_pnl:+.2f}% | mdd medio {avg_mdd:.2f}%")


if __name__ == "__main__":
    print("\n" + "#" * 100)
    print("# SNIPER CSBENDER -- V1 LONG ONLY (sai quando short signal, spot Binance)")
    print("#" * 100)
    rows_long = run_matrix(enable_short=False, label="long-only")
    report(rows_long, "Sniper CSBender LONG-only -- 720min cross open/close")

    print("\n" + "#" * 100)
    print("# SNIPER CSBENDER -- V2 LONG+SHORT (replica TV; futures-only na real)")
    print("#" * 100)
    rows_ls = run_matrix(enable_short=True, label="long+short")
    report(rows_ls, "Sniper CSBender LONG+SHORT")
