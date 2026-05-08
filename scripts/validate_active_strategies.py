"""Roda o backtest Champions com os args por ativo usados em src/main.py
e aplica os criterios do Juiz para decidir quais ativos seguem ou saem
do portfolio ativo.

Saida:
  - imprime tabela
  - escreve src/logs/agents/active_validation.json com decisao por ativo
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest_champions_strategy import (  # noqa: E402
    prepare,
    run_backtest,
)
import pandas as pd  # noqa: E402
from binance.client import Client  # noqa: E402


def fetch_klines_paginated(client, symbol, total=17520, interval=Client.KLINE_INTERVAL_1HOUR):
    """Binance limita 1000 candles por request; paginar via end_time."""
    cols = [
        "open_time", "open_price", "high_price", "low_price", "close_price", "volume",
        "close_time", "qav", "trades", "tbbav", "tbqav", "_",
    ]
    chunks: list[pd.DataFrame] = []
    end_time = None
    remaining = total
    while remaining > 0:
        limit = min(1000, remaining)
        params = {"symbol": symbol, "interval": interval, "limit": limit}
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
    if not chunks:
        return pd.DataFrame(columns=cols)
    df_all = pd.concat(chunks, ignore_index=True).drop_duplicates(subset=["open_time"]).sort_values("open_time")
    for col in ["open_price", "high_price", "low_price", "close_price", "volume"]:
        df_all[col] = pd.to_numeric(df_all[col], errors="coerce")
    df_all["open_time"] = pd.to_datetime(df_all["open_time"], unit="ms", utc=True)
    return df_all.reset_index(drop=True)


CHAMPIONS_ARGS_BTC = {"take_profit_pct": 1.1, "stop_loss_pct": 1.5, "rsi_min": 40.0, "rsi_max": 62.0, "pullback_pct": 0.7, "max_hold_bars": 18}
CHAMPIONS_ARGS_ETH = {"take_profit_pct": 0.7, "stop_loss_pct": 1.5, "rsi_min": 40.0, "rsi_max": 64.0, "pullback_pct": 0.7, "max_hold_bars": 18}
CHAMPIONS_ARGS_SOL = {"take_profit_pct": 0.7, "stop_loss_pct": 1.5, "rsi_min": 40.0, "rsi_max": 64.0, "pullback_pct": 0.7, "max_hold_bars": 18}
CHAMPIONS_ARGS_XRP = {"take_profit_pct": 0.9, "stop_loss_pct": 1.5, "rsi_min": 40.0, "rsi_max": 64.0, "pullback_pct": 0.7, "max_hold_bars": 18}
CHAMPIONS_ARGS_ADA = {"take_profit_pct": 0.7, "stop_loss_pct": 2.0, "rsi_min": 40.0, "rsi_max": 68.0, "pullback_pct": 0.7, "max_hold_bars": 18}

PORTFOLIO = [
    ("BTCUSDT", CHAMPIONS_ARGS_BTC),
    ("ETHUSDT", CHAMPIONS_ARGS_ETH),
    ("SOLUSDT", CHAMPIONS_ARGS_SOL),
    ("XRPUSDT", CHAMPIONS_ARGS_XRP),
    ("ADAUSDT", CHAMPIONS_ARGS_ADA),
]

# Criterios do Juiz (reduzidos para ChampionsStrategy: usamos PF derivado dos
# avg_win/avg_loss porque o backtest atual nao expoe profit_factor direto)
MIN_TRADES = 20
MIN_PF = 1.15
MAX_DD_PCT = 25.0


def profit_factor(result: dict) -> float:
    avg_win = result.get("avg_win_pct", 0.0)
    avg_loss = abs(result.get("avg_loss_pct", 0.0))
    wins = result.get("wins", 0)
    losses = result.get("losses", 0)
    if losses == 0 or avg_loss == 0:
        return float("inf") if wins > 0 else 0.0
    gross_profit = avg_win * wins
    gross_loss = avg_loss * losses
    return round(gross_profit / gross_loss, 3) if gross_loss else 0.0


def evaluate(asset: str, result: dict) -> dict:
    pf = profit_factor(result)
    dd = abs(float(result.get("max_drawdown_pct", 0.0)))
    trades = int(result.get("total_trades", 0))
    failed: list[str] = []
    if trades < MIN_TRADES:
        failed.append(f"trades={trades} (min {MIN_TRADES})")
    if pf < MIN_PF:
        failed.append(f"PF={pf} (min {MIN_PF})")
    if dd > MAX_DD_PCT:
        failed.append(f"DD={dd}% (max {MAX_DD_PCT}%)")
    decision = "manter" if not failed else "remover"
    return {
        "asset": asset,
        "decision": decision,
        "trades": trades,
        "winrate_pct": result.get("winrate_pct"),
        "profit_pct": result.get("profit_pct"),
        "profit_factor": pf,
        "max_drawdown_pct": dd,
        "fail_reasons": failed,
    }


def main() -> None:
    api_key = os.getenv("BINANCE_API_KEY", "") or ""
    api_secret = os.getenv("BINANCE_SECRET_KEY", "") or ""
    client = Client(api_key.strip('"'), api_secret.strip('"'))
    rows = []
    for asset, args in PORTFOLIO:
        raw = fetch_klines_paginated(client, asset, total=17520)
        df = prepare(raw)
        print(f"  ({asset}: {len(df)} candles)", file=sys.stderr)
        result = run_backtest(df, **args)
        verdict = evaluate(asset, result)
        rows.append(verdict)
        flag = "✓" if verdict["decision"] == "manter" else "✗"
        reasons = ", ".join(verdict["fail_reasons"]) or "passou"
        print(
            f"{flag} {asset:8} trades={verdict['trades']:3} "
            f"WR={verdict['winrate_pct']:5}% PF={verdict['profit_factor']:5.2f} "
            f"DD={verdict['max_drawdown_pct']:5.1f}%  -> {verdict['decision']:8} ({reasons})"
        )

    out = ROOT / "src/logs/agents/active_validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "criteria": {"min_trades": MIN_TRADES, "min_pf": MIN_PF, "max_dd_pct": MAX_DD_PCT},
                "results": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    keep = [r["asset"] for r in rows if r["decision"] == "manter"]
    drop = [r["asset"] for r in rows if r["decision"] == "remover"]
    print()
    print(f"manter: {keep}")
    print(f"remover: {drop}")
    print(f"relatorio: {out}")


if __name__ == "__main__":
    main()
