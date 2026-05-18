"""Throwaway backtest: compare exit-overlay risk configs against real executed trades.

Re-simulates the USD exit overlay (SL + trailing + ladder) bar-by-bar over 1m klines
for each recorded round-trip, holding the strategy's own SELL signal constant (as a cap
= the actual recorded exit time). Does NOT touch production files.
"""
from __future__ import annotations

import csv
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TRADES_CSV = ROOT / "src" / "logs" / "paper_balance" / "trades.csv"
CACHE_DIR = ROOT / "scripts" / "_bt_cache"
CACHE_DIR.mkdir(exist_ok=True)

ROUND_TRIP_FEE = 0.0020  # 0.20% round trip (already baked into recorded pnl)
SLIP_BUFFER_PCT = 0.25   # 0.25% of notional subtracted from SL limit
ATR_K = 5.0
GIVEBACK_MIN = 15.0
GIVEBACK_MAX = 40.0
GIVEBACK_FALLBACK = 25.0
MAX_HOLD_CANDLES = 96
CANDLE_MIN = 15  # bot runs on 15m candles

# ---------------------------------------------------------------------------
# Parse trades.csv -> round trips
# ---------------------------------------------------------------------------

def parse_trades():
    rows = list(csv.reader(TRADES_CSV.open(encoding="utf-8")))
    header = rows[0]
    rts = []
    for r in rows[1:]:
        if len(r) < 9 or r[3] != "SELL":
            continue
        ts_exit = datetime.fromisoformat(r[0]).replace(tzinfo=timezone.utc)
        asset = r[1]
        strat = r[2]
        exit_price = float(r[4])
        qty = float(r[5])
        pnl_usd = float(r[6].replace("+", ""))
        pnl_pct = float(r[7].replace("+", ""))
        open_ts = datetime.fromisoformat(r[9]).replace(tzinfo=timezone.utc)
        # entry price: recorded pnl_pct relative to entry. pnl_pct ~= (exit/entry - 1 - fee)*100?
        # Better: derive entry from qty + we know pnl_usd = qty*(exit-entry) - fees. But fee model
        # uncertain. We'll instead pull the matching BUY row for exact entry price.
        rts.append({
            "asset": asset, "strat": strat, "qty": qty,
            "entry_ts": open_ts, "exit_ts": ts_exit,
            "exit_price": exit_price, "rec_pnl_usd": pnl_usd, "rec_pnl_pct": pnl_pct,
        })
    # match BUY rows for entry price
    buys = []
    for r in rows[1:]:
        if len(r) < 6 or r[3] != "BUY":
            continue
        buys.append((datetime.fromisoformat(r[0]).replace(tzinfo=timezone.utc), r[1], float(r[4]), float(r[5])))
    for rt in rts:
        # find BUY with same asset and timestamp == entry_ts (closest)
        cand = [b for b in buys if b[1] == rt["asset"] and abs((b[0] - rt["entry_ts"]).total_seconds()) < 5]
        if not cand:
            cand = [b for b in buys if b[1] == rt["asset"] and b[0] <= rt["entry_ts"]]
            cand.sort(key=lambda b: b[0])
        if cand:
            rt["entry_price"] = cand[-1][2] if cand[-1][1] == rt["asset"] else None
            # prefer exact match
            exact = [b for b in cand if abs((b[0] - rt["entry_ts"]).total_seconds()) < 5]
            if exact:
                rt["entry_price"] = exact[0][2]
        else:
            # fall back: derive from rec_pnl_pct (gross approx)
            rt["entry_price"] = rt["exit_price"] / (1 + rt["rec_pnl_pct"] / 100.0)
        rt["notional"] = rt["qty"] * rt["entry_price"]
    return rts


# ---------------------------------------------------------------------------
# Klines
# ---------------------------------------------------------------------------

def fetch_1m(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    cache = CACHE_DIR / f"{symbol}_{int(start.timestamp())}_{int(end.timestamp())}.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    from binance.client import Client
    import os
    client = Client(api_key=os.environ.get("BINANCE_API_KEY", ""), api_secret=os.environ.get("BINANCE_API_SECRET", ""))
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time", "qav", "trades", "tbbav", "tbqav", "_"]
    out = []
    cur = start_ms
    while cur < end_ms:
        raw = client.get_klines(symbol=symbol, interval="1m", startTime=cur, endTime=end_ms, limit=1000)
        if not raw:
            break
        out.extend(raw)
        last = int(raw[-1][0])
        if last <= cur:
            break
        cur = last + 60_000
        if len(raw) < 1000:
            break
        time.sleep(0.15)
    if not out:
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close"])
    df = pd.DataFrame(out, columns=cols).drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df[["open_time", "open", "high", "low", "close"]].dropna().reset_index(drop=True)
    df.to_pickle(cache)
    return df


def atr_pct_series_15m(df1m: pd.DataFrame) -> pd.Series:
    """Resample 1m -> 15m, compute ATR(14)% per 15m bar, return series indexed by 15m open_time."""
    if df1m.empty:
        return pd.Series(dtype=float)
    d = df1m.set_index("open_time")
    agg = d.resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    if len(agg) < 15:
        return pd.Series(dtype=float)
    prev_close = agg["close"].shift(1)
    tr = pd.concat([agg["high"] - agg["low"], (agg["high"] - prev_close).abs(), (agg["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=14).mean()
    return (atr / agg["close"] * 100.0)


# ---------------------------------------------------------------------------
# Configs
# ---------------------------------------------------------------------------

BASE_LADDER = [(5.0, 3.0), (7.0, 5.0), (10.0, 8.0), (15.0, 13.0), (20.0, 17.0), (30.0, 27.0), (50.0, 45.0), (75.0, 70.0)]
AGGR_LADDER = [(2.0, 1.5), (3.0, 2.5), (5.0, 4.0), (7.0, 6.0), (10.0, 8.5), (15.0, 13.0), (20.0, 17.0), (30.0, 27.0), (50.0, 45.0), (75.0, 70.0)]


@dataclass
class Cfg:
    name: str
    sl_pct: float           # SL as fraction of N (e.g. 0.02)
    trail_act_pct: float    # trail activation as fraction of N
    trail_floor_pct: float  # trail floor as fraction of N
    ladder: list = field(default_factory=lambda: BASE_LADDER)


CONFIGS = [
    Cfg("A baseline",            0.020, 0.010, 0.010, BASE_LADDER),
    Cfg("B wider-trail",         0.020, 0.015, 0.010, BASE_LADDER),
    Cfg("C wider-trail-more",    0.020, 0.020, 0.012, BASE_LADDER),
    Cfg("D tighter-SL",          0.015, 0.010, 0.010, BASE_LADDER),
    Cfg("E tightSL+wide-trail",  0.015, 0.0175, 0.012, BASE_LADDER),
    Cfg("F looser-SL",           0.025, 0.015, 0.012, BASE_LADDER),
    Cfg("G aggr-ladder",         0.020, 0.0125, 0.010, AGGR_LADDER),
    # extras
    Cfg("H aggr-ladder+tightSL", 0.015, 0.0125, 0.010, AGGR_LADDER),
    Cfg("I wide-trail+aggr-lad", 0.020, 0.0175, 0.012, AGGR_LADDER),
    Cfg("J looserSL+wide-trail3",0.025, 0.020, 0.012, BASE_LADDER),
    Cfg("K looserSL3%+wide-tr",  0.030, 0.020, 0.015, BASE_LADDER),
    Cfg("L looserSL+aggr-lad",   0.025, 0.015, 0.012, AGGR_LADDER),
    Cfg("Z no-overlay (ref)",    99.0,  99.0,  0.0,   BASE_LADDER),  # SL/trail never trigger
]


def ladder_floor_usd(peak_usd: float, notional: float, ladder) -> float:
    if notional <= 0 or peak_usd <= 0:
        return 0.0
    peak_pct = peak_usd / notional * 100.0
    locked = 0.0
    for thr, lock in ladder:
        if peak_pct >= thr:
            locked = lock
        else:
            break
    return notional * locked / 100.0


# ---------------------------------------------------------------------------
# Simulate one round trip under one config
# ---------------------------------------------------------------------------

def simulate(rt, df1m, atr15, cfg: Cfg, mode: str = "close15"):
    """Returns (exit_price, exit_reason).

    mode='close15': faithful to the bot -- it evaluates SL/trail on the CLOSE of each
      15m candle (the bot's loop sees only the latest closed 15m candle). Peak tracked
      on 15m closes too (bot uses pnl = q*(close - P)).
    mode='lowhigh': conservative intra-bar -- SL/floor vs 1m low, peak vs 1m high.
    Both cap at recorded exit time (strategy SELL signal) and max_hold_candles.
    """
    P = rt["entry_price"]
    q = rt["qty"]
    N = rt["notional"]
    if P is None or P <= 0 or N <= 0:
        return rt["exit_price"], "no_entry_price"
    sl_usd = cfg.sl_pct * N
    trail_act_usd = cfg.trail_act_pct * N
    trail_floor_usd = cfg.trail_floor_pct * N
    slip_buf = N * SLIP_BUFFER_PCT / 100.0
    sl_threshold = -(max(0.0, sl_usd - slip_buf))

    entry_ts = rt["entry_ts"]
    exit_ts_cap = rt["exit_ts"]
    max_hold_ts = entry_ts + timedelta(minutes=CANDLE_MIN * MAX_HOLD_CANDLES)
    atr15 = atr15.sort_index() if len(atr15) else atr15

    if mode == "close15":
        d = df1m.set_index("open_time")
        bars = d.resample("15min").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        bars = bars[(bars.index > entry_ts) & (bars.index <= exit_ts_cap + timedelta(minutes=1))]
        if bars.empty:
            return rt["exit_price"], "no_candles"
        peak_pnl = 0.0
        trail_active = False
        for ts, c in bars.iterrows():
            if ts > max_hold_ts:
                return float(c["close"]), "max_hold"
            cl = float(c["close"])
            pnl = q * (cl - P)
            if pnl > peak_pnl:
                peak_pnl = pnl
            if (not trail_active) and trail_act_usd > 0 and peak_pnl >= trail_act_usd:
                trail_active = True
            if not trail_active:
                if pnl <= sl_threshold:
                    return cl, "stop_loss"
            else:
                idx = atr15.index.searchsorted(ts, side="right") - 1 if len(atr15) else -1
                atr_pct = float(atr15.iloc[idx]) if (len(atr15) and 0 <= idx < len(atr15) and pd.notna(atr15.iloc[idx]) and atr15.iloc[idx] > 0) else None
                eff_give = max(min(atr_pct * ATR_K, GIVEBACK_MAX), GIVEBACK_MIN) if atr_pct is not None else GIVEBACK_FALLBACK
                giveback_floor = peak_pnl * (1.0 - eff_give / 100.0)
                lad_floor = ladder_floor_usd(peak_pnl, N, cfg.ladder)
                dyn_floor = max(trail_floor_usd, giveback_floor, lad_floor)
                if pnl <= dyn_floor:
                    return cl, "trail"
                if pnl <= sl_threshold:
                    return cl, "stop_loss_post"
        return rt["exit_price"], "strategy_sell"

    # ---- conservative intra-bar mode ----
    win = df1m[(df1m["open_time"] > entry_ts) & (df1m["open_time"] <= exit_ts_cap + timedelta(minutes=2))]
    if win.empty:
        return rt["exit_price"], "no_candles"
    peak_pnl = 0.0
    trail_active = False
    for _, c in win.iterrows():
        ts = c["open_time"]
        if ts > max_hold_ts:
            return float(c["open"]), "max_hold"
        hi, lo = float(c["high"]), float(c["low"])
        bar_peak_pnl = q * (hi - P)
        if bar_peak_pnl > peak_pnl:
            peak_pnl = bar_peak_pnl
        pnl_low = q * (lo - P)
        if (not trail_active) and peak_pnl >= trail_act_usd and trail_act_usd > 0:
            trail_active = True
        if not trail_active:
            if pnl_low <= sl_threshold:
                return P + sl_threshold / q, "stop_loss"
        else:
            bucket = ts.floor("15min")
            idx = atr15.index.searchsorted(bucket, side="right") - 1 if len(atr15) else -1
            atr_pct = float(atr15.iloc[idx]) if (len(atr15) and 0 <= idx < len(atr15) and pd.notna(atr15.iloc[idx]) and atr15.iloc[idx] > 0) else None
            eff_give = max(min(atr_pct * ATR_K, GIVEBACK_MAX), GIVEBACK_MIN) if atr_pct is not None else GIVEBACK_FALLBACK
            giveback_floor = peak_pnl * (1.0 - eff_give / 100.0)
            lad_floor = ladder_floor_usd(peak_pnl, N, cfg.ladder)
            dyn_floor = max(trail_floor_usd, giveback_floor, lad_floor)
            if pnl_low <= dyn_floor:
                exit_px = min(max(P + dyn_floor / q, lo), hi)
                return exit_px, "trail"
            if pnl_low <= sl_threshold:
                return P + sl_threshold / q, "stop_loss_post"
    return rt["exit_price"], "strategy_sell"


def pnl_after_fees(entry_price, exit_price, notional):
    # gross pnl on notional, minus round-trip fee on notional (approx, fee on entry+exit ~ 2*0.1%*N)
    gross_pct = (exit_price - entry_price) / entry_price
    return notional * gross_pct - notional * ROUND_TRIP_FEE


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def aggregate(results):
    pnls = [r["pnl"] for r in results]
    total = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    wr = len(wins) / n * 100 if n else 0
    aw = sum(wins) / len(wins) if wins else 0
    al = sum(losses) / len(losses) if losses else 0
    pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
    # total notional ~ for % we use sum of notionals as denominator (capital deployed)?
    # Better: relative to ~$1000 bank like the report. Report says -0.83% over net -$8 -> ~$1000 bank.
    pct = total / 1000.0 * 100.0
    return {"total": total, "pct": pct, "wr": wr, "n": n, "aw": aw, "al": al, "pf": pf,
            "maxloss": min(pnls) if pnls else 0, "nwin": len(wins), "nloss": len(losses)}


def main():
    print("Parsing trades.csv ...")
    rts = parse_trades()
    print(f"  {len(rts)} closed round-trips")
    missing_entry = [r for r in rts if not r.get("entry_price")]
    if missing_entry:
        print(f"  WARNING: {len(missing_entry)} without entry price (derived from pnl%)")

    # fetch klines per round trip (group by asset to reduce calls -> fetch one big window per asset)
    assets = {}
    for r in rts:
        a = r["asset"]
        s, e = r["entry_ts"], r["exit_ts"]
        if a not in assets:
            assets[a] = [s, e]
        else:
            assets[a][0] = min(assets[a][0], s)
            assets[a][1] = max(assets[a][1], e)
    klines = {}
    atr_cache = {}
    for a, (s, e) in assets.items():
        s2 = s - timedelta(minutes=CANDLE_MIN * 20)  # extra for ATR warmup
        e2 = e + timedelta(minutes=CANDLE_MIN * MAX_HOLD_CANDLES + 30)
        print(f"  fetching 1m {a} {s2.date()}..{e2.date()} ...")
        df = fetch_1m(a, s2, e2)
        klines[a] = df
        atr_cache[a] = atr_pct_series_15m(df)
        print(f"    {len(df)} candles, atr15 pts={len(atr_cache[a])}")

    # sanity: recorded aggregate
    rec_total = sum(r["rec_pnl_usd"] for r in rts)
    rec_wins = [r["rec_pnl_usd"] for r in rts if r["rec_pnl_usd"] > 0]
    rec_losses = [r["rec_pnl_usd"] for r in rts if r["rec_pnl_usd"] < 0]
    rec_pf = sum(rec_wins) / abs(sum(rec_losses)) if rec_losses else float("inf")
    print(f"\nRECORDED actual: total ${rec_total:+.2f}  wr {len(rec_wins)/len(rts)*100:.1f}%  "
          f"PF {rec_pf:.2f}  n={len(rts)}  avgW ${sum(rec_wins)/len(rec_wins):.2f}  avgL ${sum(rec_losses)/len(rec_losses):.2f}")

    # simulate -- two modes
    def run_mode(mode):
        all_agg, per_asset, reasons = {}, {}, {}
        for cfg in CONFIGS:
            results, rsn, pa = [], {}, {}
            for r in rts:
                df = klines[r["asset"]]
                atr15 = atr_cache[r["asset"]]
                ex_px, reason = simulate(r, df, atr15, cfg, mode=mode)
                pnl = pnl_after_fees(r["entry_price"], ex_px, r["notional"]) if r.get("entry_price") else r["rec_pnl_usd"]
                results.append({"pnl": pnl, "asset": r["asset"], "reason": reason})
                rsn[reason] = rsn.get(reason, 0) + 1
                pa.setdefault(r["asset"], []).append(pnl)
            all_agg[cfg.name] = aggregate(results)
            reasons[cfg.name] = rsn
            per_asset[cfg.name] = {a: sum(v) for a, v in pa.items()}
        return all_agg, per_asset, reasons

    def print_table(all_agg, title):
        print("\n" + "=" * 112)
        print(title)
        print(f"{'Config':<22}{'TotalPnL$':>11}{'PnL%':>8}{'WinRate':>9}{'N':>4}{'#W':>4}{'#L':>4}{'AvgWin$':>9}{'AvgLoss$':>10}{'PF':>7}{'MaxLoss$':>10}")
        print("-" * 112)
        for cfg in CONFIGS:
            a = all_agg[cfg.name]
            print(f"{cfg.name:<22}{a['total']:>+11.2f}{a['pct']:>+8.2f}{a['wr']:>8.1f}%{a['n']:>4}{a['nwin']:>4}{a['nloss']:>4}"
                  f"{a['aw']:>+9.2f}{a['al']:>+10.2f}{a['pf']:>7.2f}{a['maxloss']:>+10.2f}")
        print("=" * 112)

    agg_c, pa_c, rsn_c = run_mode("close15")
    agg_l, pa_l, rsn_l = run_mode("lowhigh")

    print_table(agg_c, "PRIMARY -- 15m-close evaluation (faithful to bot loop behavior)")
    print("\nExit-reason breakdown (close15):")
    for cfg in CONFIGS:
        print(f"  {cfg.name:<22} {rsn_c[cfg.name]}")

    print_table(agg_l, "CONSERVATIVE -- intra-bar (1m) SL-vs-low / peak-vs-high (worst-case bound)")

    print("\nPer-asset PnL$ (close15 mode):")
    assets_sorted = sorted(assets.keys())
    print(f"{'asset':<10}" + "".join(f"{c.name.split()[0]:>9}" for c in CONFIGS))
    for a in assets_sorted:
        print(f"{a:<10}" + "".join(f"{pa_c[c.name].get(a,0):>+9.2f}" for c in CONFIGS))

    # validation
    a_c = agg_c["A baseline"]["total"]; a_l = agg_l["A baseline"]["total"]
    print(f"\nSANITY CHECK (config A re-sim vs recorded ${rec_total:+.2f}, PF {rec_pf:.2f}):")
    print(f"  close15 mode : ${a_c:+.2f} (diff ${a_c-rec_total:+.2f}), PF {agg_c['A baseline']['pf']:.2f}  -- this is the apples-to-apples check")
    print(f"  intra-bar    : ${a_l:+.2f} (diff ${a_l-rec_total:+.2f}), PF {agg_l['A baseline']['pf']:.2f}  -- intentionally pessimistic (every wick triggers SL)")

    # recommend on close15
    best = max(CONFIGS, key=lambda c: agg_c[c.name]["total"])
    print(f"\nBEST BY TOTAL PnL (close15): {best.name}  -> ${agg_c[best.name]['total']:+.2f}, PF {agg_c[best.name]['pf']:.2f}")
    for label, c in [("A baseline", CONFIGS[0]), (best.name, best)]:
        print(f"  {label}: T1 sl={c.sl_pct*300:.2f} act={c.trail_act_pct*300:.2f} floor={c.trail_floor_pct*300:.2f} | "
              f"T2 sl={c.sl_pct*225:.2f} act={c.trail_act_pct*225:.2f} floor={c.trail_floor_pct*225:.2f} | "
              f"T3 sl={c.sl_pct*150:.2f} act={c.trail_act_pct*150:.2f} floor={c.trail_floor_pct*150:.2f}; ladder={c.ladder[:3]}...")


if __name__ == "__main__":
    main()
