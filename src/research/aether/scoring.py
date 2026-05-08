from __future__ import annotations

from typing import Any


MIN_SCOUT_TRADES = 6
MIN_SCOUT_SHARPE = 0.05
MIN_SCOUT_PNL_PCT = -3.0
MAX_SCOUT_MDD_PCT = 35.0
MIN_SURVIVAL_SCORE = 0.12

# Periodo coberto por candle, em horas. Usado pra estimar o periodo testado
# no resumo da estrategia (bars * tf_h).
_TF_HOURS = {
    "1m": 1 / 60, "3m": 3 / 60, "5m": 5 / 60, "10m": 10 / 60, "15m": 15 / 60,
    "30m": 30 / 60, "1h": 1.0, "2h": 2.0, "4h": 4.0, "6h": 6.0, "8h": 8.0,
    "12h": 12.0, "1d": 24.0, "3d": 72.0, "1w": 168.0,
}


def _format_period(bars: int, timeframe: str) -> str:
    """Converte bars*timeframe em string humana (dias / meses / anos)."""
    if not bars or not timeframe:
        return ""
    h = _TF_HOURS.get(str(timeframe).lower())
    if not h:
        return ""
    days = (bars * h) / 24.0
    if days >= 365:
        return f"{days / 365:.1f}a"
    if days >= 30:
        return f"{days / 30:.1f}m"
    if days >= 1:
        return f"{days:.1f}d"
    return f"{days * 24:.1f}h"


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def score_from_metrics(metrics: dict[str, Any]) -> tuple[float, str, bool]:
    """Return (score, reason, should_prune).

    Aggressive pruning is intentional: the scout stage exists to discard weak
    branches before expensive validation.
    """
    if not metrics:
        return 0.15, "sem metricas", True
    if metrics.get("ok") is False:
        return 0.0, str(metrics.get("error") or "probe falhou"), True

    sharpe = float(metrics.get("sharpe") or 0.0)
    trades = int(metrics.get("trade_count") or metrics.get("trades") or 0)
    mdd_pct = float(metrics.get("mdd_pct") or 0.0)
    pnl_pct = float(metrics.get("pnl_pct") or 0.0)
    cost_bps = float(metrics.get("fee_bps") or 10.0)

    if trades < MIN_SCOUT_TRADES:
        return 0.03, f"poda: trade_count baixo ({trades})", True
    if pnl_pct <= MIN_SCOUT_PNL_PCT:
        return 0.04, f"poda: PnL scout muito negativo ({pnl_pct:.2f}%)", True
    if sharpe < MIN_SCOUT_SHARPE:
        return 0.05, f"poda: Sharpe scout fraco ({sharpe:.2f})", True
    if mdd_pct > MAX_SCOUT_MDD_PCT:
        return 0.08, f"poda: MDD acima de {MAX_SCOUT_MDD_PCT:.0f}% ({mdd_pct:.2f}%)", True

    sharpe_score = clamp(sharpe / 3.0)
    trade_score = clamp(trades / 120.0)
    drawdown_score = clamp(1.0 - (mdd_pct / MAX_SCOUT_MDD_PCT))
    pnl_score = clamp(pnl_pct / 25.0)
    cost_score = clamp(cost_bps / 10.0, 0.5, 1.2)

    score = (
        0.38 * sharpe_score
        + 0.22 * drawdown_score
        + 0.18 * trade_score
        + 0.17 * pnl_score
        + 0.05 * clamp(cost_score)
    )
    should_prune = score < MIN_SURVIVAL_SCORE
    bars = int(metrics.get("bars") or 0)
    timeframe = str(metrics.get("timeframe") or "")
    period = _format_period(bars, timeframe)
    period_part = f" periodo={period}" if period else ""
    reason = (
        f"Sh={sharpe:.2f} trades={trades} MDD={mdd_pct:.2f}% "
        f"PnL={pnl_pct:.2f}% score={score:.2f}{period_part}"
    )
    return round(clamp(score), 4), reason, should_prune


def aggregate_node_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in results if r.get("ok")]
    if not ok:
        return {"ok": False, "error": "todos os probes falharam", "probe_count": len(results)}
    combo_ok = [r for r in ok if r.get("engine") == "combo"]
    score_pool = combo_ok or ok
    best = max(score_pool, key=lambda r: float(r.get("sharpe") or -999))
    pass_count = sum(1 for r in score_pool if not score_from_metrics(r)[2])
    return {
        "ok": True,
        "probe_count": len(results),
        "ok_count": len(ok),
        "combo_count": len(combo_ok),
        "failed_count": len(results) - len(ok),
        "pass_count": pass_count,
        "best_probe": best,
        "sharpe": best.get("sharpe"),
        "trade_count": best.get("trade_count"),
        "trades": best.get("trade_count"),
        "mdd_pct": best.get("mdd_pct"),
        "pnl_pct": best.get("pnl_pct"),
        "fee_bps": best.get("fee_bps", 10.0),
        "bars": best.get("bars"),
        "timeframe": best.get("timeframe"),
    }
