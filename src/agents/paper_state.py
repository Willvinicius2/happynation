from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_PAPER_STATE_PATH = Path("src/logs/paper_balance/state.json")


def load_paper_state(path: Path | str = DEFAULT_PAPER_STATE_PATH) -> dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def asset_metrics(asset: str, state: dict[str, Any], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    balance = float(state.get("balance", 0.0) or 0.0)
    initial = float(state.get("initial_balance", balance) or balance or 0.0)
    cash_balance = float(state.get("cash_balance", 0.0) or 0.0)
    allocated_usd = float(state.get("allocated_usd", 0.0) or 0.0)
    if "market_value" in state:
        market_value = float(state.get("market_value", 0.0) or 0.0)
    else:
        market_value = balance
    total_trades = int(state.get("total_trades", 0) or 0)
    wins = int(state.get("wins", 0) or 0)
    losses = int(state.get("losses", 0) or 0)
    pnl_usd = balance - initial
    pnl_pct = (pnl_usd / initial * 100) if initial else 0.0
    winrate_pct = (wins / total_trades * 100) if total_trades else 0.0

    risk = state.get("risk", {}) or {}
    day_start = float(risk.get("day_start_balance", balance) or balance or 0.0)
    daily_realized_pnl = float(risk.get("daily_realized_pnl", 0.0) or 0.0)
    daily_pnl_pct = (daily_realized_pnl / day_start * 100) if day_start else 0.0

    # DD a nivel de portfolio: per-asset `balance` reflete apenas o caixa
    # naquele bucket (e nao valor do asset), entao usar (initial - balance) por
    # asset gera drawdowns falsos de 95-100%. Quando ha _meta com equity
    # consolidada, calculamos o DD real do portfolio e replicamos no asset.
    if meta:
        bankroll = float(meta.get("total_bankroll") or 0.0)
        equity = meta.get("equity")
        if bankroll > 0 and equity is not None:
            drawdown_pct = max((bankroll - float(equity)) / bankroll * 100, 0.0)
        else:
            drawdown_pct = 0.0
    else:
        drawdown_pct = max(((initial - balance) / initial * 100) if initial else 0.0, 0.0)

    return {
        "asset": asset,
        "strategy": state.get("strategy", "-"),
        "balance": balance,
        "cash_balance": cash_balance,
        "allocated_usd": allocated_usd,
        "market_value": market_value,
        "initial_balance": initial,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "winrate_pct": winrate_pct,
        "drawdown_pct": drawdown_pct,
        "daily_pnl_pct": daily_pnl_pct,
        "consecutive_losses": int(risk.get("consecutive_losses", 0) or 0),
        "last_block_reason": risk.get("last_block_reason", ""),
        "position_open": state.get("position") is not None,
    }
