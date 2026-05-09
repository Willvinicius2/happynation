"""RSI Divergence Strategy -- backtest no motor do Aether.

Implementa a logica de divergencias do TV (Regular Bull / Hidden Bull entry,
Regular Bear / RSI overbought exit). Long-only para spot Binance.

Pivots: confirma um pivot low/high quando ha 'lbR' candles depois sem quebrar
o extremo do candle 'central'. Retorna o valor do pivot defasado em lbR.
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


# =========================================================================
# Indicadores
# =========================================================================
def rsi_wilder(close: pd.Series, length: int = 9) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def find_pivots(series: pd.Series, lbL: int, lbR: int, kind: str = "low") -> np.ndarray:
    """Retorna array com o valor do pivot DETECTADO em cada index (offset por lbR).

    Pivot low: index i -> True se series[i-lbR] == min(series[i-lbR-lbL : i+1]).
    Em palavras: o candle em (i-lbR) eh o minimo da janela [center-lbL, center+lbR].
    Retorna nan onde nao ha pivot.
    """
    n = len(series)
    arr = series.values.astype(float)
    out = np.full(n, np.nan)
    for i in range(lbL + lbR, n):
        center = i - lbR
        win_start = max(0, center - lbL)
        win_end = i + 1  # inclui ate i (i = center + lbR)
        window = arr[win_start:win_end]
        if not np.isfinite(arr[center]):
            continue
        if kind == "low":
            if arr[center] == window.min():
                out[i] = arr[center]
        else:  # high
            if arr[center] == window.max():
                out[i] = arr[center]
    return out


def valuewhen_n(cond: np.ndarray, source: np.ndarray, n: int) -> np.ndarray:
    """Replica Pine valuewhen: retorna o valor de source[i] no n-esimo candle anterior
    (zero-based) onde cond eh true. n=0 = mais recente true; n=1 = anterior; etc.
    """
    sz = len(source)
    out = np.full(sz, np.nan)
    indices: list[int] = []
    for i in range(sz):
        if cond[i]:
            indices.append(i)
        if len(indices) > n:
            out[i] = source[indices[-(n + 1)]]
    return out


# =========================================================================
# Sinais
# =========================================================================
def rsi_div_signals(
    df: pd.DataFrame,
    *,
    rsi_len: int = 9,
    lbL: int = 1,
    lbR: int = 3,
    range_lower: int = 5,
    range_upper: int = 60,
    take_profit_rsi: int = 80,
    use_hidden_bull: bool = True,
) -> pd.Series:
    close = df["close"].astype(float).reset_index(drop=True)
    high = df["high"].astype(float).reset_index(drop=True)
    low = df["low"].astype(float).reset_index(drop=True)
    osc = rsi_wilder(close, rsi_len)

    # Pivots no RSI
    pl_val = find_pivots(osc, lbL, lbR, kind="low")
    ph_val = find_pivots(osc, lbL, lbR, kind="high")
    pl_found = ~np.isnan(pl_val)
    ph_found = ~np.isnan(ph_val)

    # Para Pine: osc[lbR] eh o RSI no pivot center; valuewhen(plFound, osc[lbR], 1)
    # eh o RSI no pivot ANTERIOR. Como pl_val ja eh o RSI no center, usamos diretamente.
    osc_arr = osc.values
    high_arr = high.values
    low_arr = low.values
    n = len(close)

    # Valor do RSI/preco no center do pivot atual (defasado)
    osc_at_center = np.full(n, np.nan)
    low_at_center = np.full(n, np.nan)
    high_at_center = np.full(n, np.nan)
    for i in range(lbR, n):
        center = i - lbR
        if pl_found[i]:
            osc_at_center[i] = osc_arr[center]
            low_at_center[i] = low_arr[center]
        if ph_found[i]:
            osc_at_center[i] = osc_arr[center] if pl_found[i] else osc_at_center[i]
            high_at_center[i] = high_arr[center]

    # Para garantir separacao de pivots low e high, calculamos series separadas
    osc_at_pl_center = np.full(n, np.nan)
    low_at_pl_center = np.full(n, np.nan)
    osc_at_ph_center = np.full(n, np.nan)
    high_at_ph_center = np.full(n, np.nan)
    for i in range(lbR, n):
        center = i - lbR
        if pl_found[i]:
            osc_at_pl_center[i] = osc_arr[center]
            low_at_pl_center[i] = low_arr[center]
        if ph_found[i]:
            osc_at_ph_center[i] = osc_arr[center]
            high_at_ph_center[i] = high_arr[center]

    # valuewhen(plFound, osc[lbR], 1): RSI no pivot anterior (n=1 = anterior)
    osc_prev_pl = valuewhen_n(pl_found, osc_at_pl_center, 1)
    low_prev_pl = valuewhen_n(pl_found, low_at_pl_center, 1)
    osc_prev_ph = valuewhen_n(ph_found, osc_at_ph_center, 1)
    high_prev_ph = valuewhen_n(ph_found, high_at_ph_center, 1)

    # _inRange(plFound[1]): bars desde o ULTIMO pivot anterior >= rangeLower e <= rangeUpper
    # plFound[1] = condicao no candle anterior. Vamos calcular bars_since_pl_prev e ph_prev.
    def bars_since_prev_true(cond):
        sz = len(cond)
        out = np.full(sz, -1)
        last = -1  # ultimo index com cond=true
        for i in range(sz):
            if i > 0 and cond[i - 1]:  # cond[1] = anterior na semantica Pine
                last = i - 1
            if last < 0:
                out[i] = -1
            else:
                out[i] = i - last
        return out

    bars_pl_prev = bars_since_prev_true(pl_found)
    bars_ph_prev = bars_since_prev_true(ph_found)
    in_range_pl = (bars_pl_prev >= range_lower) & (bars_pl_prev <= range_upper)
    in_range_ph = (bars_ph_prev >= range_lower) & (bars_ph_prev <= range_upper)

    # Regular Bullish: osc HL + price LL
    osc_hl = (osc_at_pl_center > osc_prev_pl) & in_range_pl
    price_ll = low_at_pl_center < low_prev_pl
    bull_cond = pl_found & osc_hl & price_ll

    # Hidden Bullish: osc LL + price HL
    osc_ll = (osc_at_pl_center < osc_prev_pl) & in_range_pl
    price_hl = low_at_pl_center > low_prev_pl
    hidden_bull_cond = pl_found & osc_ll & price_hl

    # Regular Bearish: osc LH + price HH
    osc_lh = (osc_at_ph_center < osc_prev_ph) & in_range_ph
    price_hh = high_at_ph_center > high_prev_ph
    bear_cond = ph_found & osc_lh & price_hh

    # Take profit: RSI cruza acima de takeProfitRSILevel
    cross_above = (osc.values > take_profit_rsi) & (np.roll(osc.values, 1) <= take_profit_rsi)
    cross_above[0] = False

    long_entry = bull_cond | (hidden_bull_cond if use_hidden_bull else np.zeros(n, dtype=bool))
    long_exit = cross_above | bear_cond

    # State machine
    sig = np.zeros(n, dtype=np.int8)
    pos = 0
    for i in range(n):
        if pos == 0:
            if long_entry[i]:
                pos = 1
        else:  # pos == 1
            if long_exit[i]:
                pos = 0
        sig[i] = pos
    return pd.Series(sig, index=df.index)


# =========================================================================
# Matriz
# =========================================================================
ASSETS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
TIMEFRAMES = ["15m", "1h", "4h", "1d"]


def run_matrix(
    *,
    rsi_len: int = 9, take_profit_rsi: int = 80,
    use_hidden_bull: bool = True,
    label: str = "default",
) -> list[dict]:
    rows: list[dict] = []
    print(f"Rodando matriz RSI Divergence ({label})...\n")
    for asset in ASSETS:
        for tf in TIMEFRAMES:
            dataset_id = f"binance:{asset}:{tf}"
            t0 = time.time()
            try:
                df = load_dataset_fast(dataset_id)
                if len(df) < 250:
                    rows.append({"asset": asset, "tf": tf, "ok": False,
                                 "error": f"poucos candles ({len(df)})"})
                    continue
                sig = rsi_div_signals(df, rsi_len=rsi_len, take_profit_rsi=take_profit_rsi,
                                       use_hidden_bull=use_hidden_bull)
                r = _run_local_backtest(df, sig, capital=1000.0, fee_bps=10.0,
                                         slippage_bps=2.0, timeframe=tf)
                r["asset"] = asset; r["tf"] = tf
                r["elapsed_s"] = round(time.time() - t0, 2)
                rows.append(r)
            except Exception as exc:
                rows.append({"asset": asset, "tf": tf, "ok": False,
                             "error": f"{exc.__class__.__name__}: {str(exc)[:100]}",
                             "elapsed_s": round(time.time() - t0, 2)})
    return rows


def report(rows: list[dict], title: str) -> None:
    print("\n" + "=" * 100)
    print(f" {title}")
    print("=" * 100)
    header = f"{'asset':<10} {'tf':<5} {'bars':>6} {'pnl%':>9} {'trades':>7} {'mdd%':>7} {'hit%':>6} {'sharpe':>7}"
    print(header); print("-" * len(header))
    ok_rows = []
    for r in rows:
        if r.get("ok"):
            hit = (r["hit_rate"] or 0) * 100 if r.get("hit_rate") is not None else 0
            print(f"{r['asset']:<10} {r['tf']:<5} {r['bars']:>6} "
                  f"{r['pnl_pct']:>+8.2f}% {r['trade_count']:>7} "
                  f"{r['mdd_pct']:>6.2f}% {hit:>5.1f}% {r['sharpe']:>+7.2f}")
            ok_rows.append(r)
        else:
            print(f"{r['asset']:<10} {r['tf']:<5}  FAILED  {r.get('error', '?')[:60]}")
    if ok_rows:
        avg_pnl = float(np.mean([r["pnl_pct"] for r in ok_rows]))
        positives = sum(1 for r in ok_rows if r["pnl_pct"] > 0)
        avg_mdd = float(np.mean([r["mdd_pct"] for r in ok_rows]))
        avg_trades = float(np.mean([r["trade_count"] for r in ok_rows]))
        print("-" * len(header))
        print(f"resumo: {len(ok_rows)} OK | positivos {positives}/{len(ok_rows)} | "
              f"pnl medio {avg_pnl:+.2f}% | mdd medio {avg_mdd:.2f}% | trades medio {avg_trades:.0f}")


if __name__ == "__main__":
    print("\n" + "#" * 100)
    print("# RSI DIVERGENCE -- defaults (rsi=9, lbL=1, lbR=3, TP RSI>80, hidden_bull=ON)")
    print("#" * 100)
    rows = run_matrix(label="rsi=9 TP=80 hidden=on")
    report(rows, "RSI Divergence default")

    print("\n" + "#" * 100)
    print("# RSI DIVERGENCE -- so Regular Bull (sem Hidden)")
    print("#" * 100)
    rows2 = run_matrix(use_hidden_bull=False, label="hidden_bull=off")
    report(rows2, "RSI Divergence sem Hidden Bull")
