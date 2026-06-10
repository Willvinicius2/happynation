"""Backtest the BTCUSDT 15m path_simulator paper candidates.

This is a report generator only. It does not place orders and does not edit the
active strategy manifest.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.research.aether.compute import _build_signals, _run_local_backtest, load_dataset_fast  # noqa: E402


DATASET_ID = "binance:BTCUSDT:15m"
SYMBOL = "BTCUSDT"
TIMEFRAME = "15m"
REPORT_PATH = ROOT / "src" / "logs" / "agents" / "btc_path_simulator_report.json"

PATH_SIMULATOR_PARAMS: dict[str, Any] = {
    "lookback": 512,
    "horizon": 16,
    "paths": 2000,
    "block_size": 4,
    "min_p_up": 0.58,
    "min_expected_pct": 0.25,
    "max_stop_hit_prob": 0.35,
    "max_cvar_loss_pct": 1.20,
    "roundtrip_cost_pct": 0.24,
    "long_only": True,
}

REGIME_FILTER_PARAMS: dict[str, Any] = {
    "method": "trend_align",
    "ma_window": 200,
    "slope_window": 12,
    "vol_window": 96,
    "vol_percentile_max": 0.7,
    "long_only": True,
}

STRATEGIES: list[dict[str, Any]] = [
    {
        "name": "BTC path simulator 15m",
        "engine": "path_simulator",
        "params": PATH_SIMULATOR_PARAMS,
        "probe_specs": [{"engine": "path_simulator", "params": PATH_SIMULATOR_PARAMS}],
    },
    {
        "name": "BTC path simulator + regime filter 15m",
        "engine": "combo",
        "params": {
            "mode": "consensus",
            "specs": [
                {"engine": "path_simulator", "params": PATH_SIMULATOR_PARAMS},
                {"engine": "regime_filter", "params": REGIME_FILTER_PARAMS},
            ],
        },
        "probe_specs": [
            {"engine": "path_simulator", "params": PATH_SIMULATOR_PARAMS},
            {"engine": "regime_filter", "params": REGIME_FILTER_PARAMS},
        ],
    },
]

CRITERIA = {
    "min_trade_count": 30,
    "min_profit_factor": 1.15,
    "max_mdd_pct": 25.0,
    "min_sharpe": 0.5,
    "min_pnl_pct": 0.0,
}


def _passes(result: dict[str, Any]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    trade_count = int(result.get("trade_count") or 0)
    profit_factor = float(result.get("profit_factor") or 0.0)
    mdd_pct = float(result.get("mdd_pct") or 0.0)
    sharpe = float(result.get("sharpe") or 0.0)
    pnl_pct = float(result.get("pnl_pct") or 0.0)
    if trade_count < CRITERIA["min_trade_count"]:
        failures.append(f"trade_count={trade_count} < {CRITERIA['min_trade_count']}")
    if profit_factor < CRITERIA["min_profit_factor"]:
        failures.append(f"profit_factor={profit_factor} < {CRITERIA['min_profit_factor']}")
    if mdd_pct > CRITERIA["max_mdd_pct"]:
        failures.append(f"mdd_pct={mdd_pct} > {CRITERIA['max_mdd_pct']}")
    if sharpe < CRITERIA["min_sharpe"]:
        failures.append(f"sharpe={sharpe} < {CRITERIA['min_sharpe']}")
    if pnl_pct <= CRITERIA["min_pnl_pct"]:
        failures.append(f"pnl_pct={pnl_pct} <= {CRITERIA['min_pnl_pct']}")
    return not failures, failures


def _last_signal_time(df, signal) -> str | None:
    idx = signal[signal > 0].index
    if len(idx) == 0 or "open_time" not in df.columns:
        return None
    return str(df.loc[idx[-1], "open_time"])


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    df = load_dataset_fast(DATASET_ID)
    rows: list[dict[str, Any]] = []
    for strategy in STRATEGIES:
        signal = _build_signals(strategy["engine"], df, dict(strategy["params"]))
        result = _run_local_backtest(
            df,
            signal,
            capital=10000.0,
            fee_bps=10.0,
            slippage_bps=2.0,
            timeframe=TIMEFRAME,
        )
        passed, failures = _passes(result)
        rows.append(
            {
                "name": strategy["name"],
                "engine": strategy["engine"],
                "params": strategy["params"],
                "probe_specs": strategy["probe_specs"],
                "signal_count": int((signal > 0).sum()),
                "last_signal_time": _last_signal_time(df, signal),
                "result": result,
                "passed": passed,
                "failures": failures,
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataset_id": DATASET_ID,
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "bars": int(len(df)),
        "costs": {"fee_bps": 10.0, "slippage_bps": 2.0, "roundtrip_cost_pct": 0.24},
        "criteria": CRITERIA,
        "strategies": rows,
        "all_passed": all(row["passed"] for row in rows),
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"report={REPORT_PATH}")
    for row in rows:
        result = row["result"]
        verdict = "PASS" if row["passed"] else "FAIL"
        print(
            f"{verdict} {row['name']}: pnl={result.get('pnl_pct')}% "
            f"trades={result.get('trade_count')} pf={result.get('profit_factor')} "
            f"mdd={result.get('mdd_pct')}% sharpe={result.get('sharpe')}"
        )
        if row["failures"]:
            print("  failures: " + "; ".join(row["failures"]))


if __name__ == "__main__":
    main()
