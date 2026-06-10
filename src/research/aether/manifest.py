from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import artifact_dir
from .db import add_event, connect, loads
from .repository import add_artifact
from .timeutils import now_iso


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _family_manifest_count(conn, campaign_id: str | None, engine: str) -> int:
    """Conta manifests `manifest_draft` ja emitidos para a familia (engine)
    dentro da mesma campanha. Usado para impedir flood de manifests da mesma
    familia."""
    if not engine:
        return 0
    rows = conn.execute(
        """
        SELECT a.node_id, n.metrics_json, n.tags_json
        FROM artifacts a
        LEFT JOIN nodes n ON n.id = a.node_id
        WHERE a.kind = 'manifest_draft'
          AND (? IS NULL OR a.campaign_id = ?)
        """,
        (campaign_id, campaign_id),
    ).fetchall()
    target = engine.lower()
    count = 0
    for row in rows:
        node_engine = ""
        try:
            metrics = loads(row["metrics_json"], {}) if row["metrics_json"] else {}
        except Exception:
            metrics = {}
        best = metrics.get("best_probe") if isinstance(metrics, dict) else None
        if isinstance(best, dict):
            node_engine = str(best.get("engine") or "").lower()
        if not node_engine:
            try:
                tags = json.loads(row["tags_json"] or "[]") if row["tags_json"] else []
            except Exception:
                tags = []
            for tag in tags:
                if isinstance(tag, str) and tag.startswith("engine:"):
                    node_engine = tag.split(":", 1)[1].lower()
                    break
        if node_engine == target:
            count += 1
    return count


PSEUDOCODE_BY_ENGINE = {
    "ma_crossover": [
        "on bar t:",
        "  closes = close[0:t]  # never include current unfinished bar",
        "  short_now = mean(last short_window closes)",
        "  long_now = mean(last long_window closes)",
        "  short_prev = mean(previous short_window closes)",
        "  long_prev = mean(previous long_window closes)",
        "  if short_now > long_now and short_prev <= long_prev: signal ENTER_LONG",
        "  if short_now < long_now and short_prev >= long_prev:",
        "      signal EXIT when long_only else ENTER_SHORT",
        "  execute signal at open[t+1]",
    ],
    "bollinger": [
        "on bar t:",
        "  closes = close[0:t]  # ignores current unfinished bar",
        "  ma = mean(last window closes)",
        "  sigma = pstdev(last window closes)",
        "  lower = ma - num_std * sigma",
        "  upper = ma + num_std * sigma",
        "  enter long on strict cross below lower band",
        "  enter short on strict cross above upper band when bidirectional",
        "  exit long on strict cross back to mean",
        "  execute signal at open[t+1]",
    ],
    "rsi": [
        "on bar t:",
        "  closes = close[0:t]",
        "  compute SMA RSI over period",
        "  enter long when RSI crosses below oversold",
        "  enter short when RSI crosses above overbought when bidirectional",
        "  exit long when RSI crosses back above 50",
        "  execute signal at open[t+1]",
    ],
    "donchian": [
        "on bar t:",
        "  highs/lows = OHLC[0:t]",
        "  bullish breakout when high[t-1] > max(previous entry_window highs)",
        "  bearish breakout when low[t-1] < min(previous exit_window lows)",
        "  map bearish to EXIT when long_only else ENTER_SHORT",
        "  execute signal at open[t+1]",
    ],
    "supertrend": [
        "on bar t:",
        "  compute ATR and supertrend bands only from completed bars",
        "  emit directional signal on confirmed trend flip",
        "  execute signal at open[t+1]",
    ],
    "path_simulator": [
        "on bar t:",
        "  returns = log returns from trailing lookback closed candles only",
        "  bootstrap paths x horizon future paths using deterministic seed",
        "  estimate probability up, expected net return, stop-hit probability and 5pct CVaR",
        "  enter long only when all probability, expectancy and tail-risk gates pass",
        "  execute signal at open[t+1]; never inspect candles after t",
    ],
    "macd": [
        "on bar t:",
        "  closes = close[0:t]",
        "  macd = EMA(fast) - EMA(slow)",
        "  signal_line = EMA(macd, signal)",
        "  signal LONG when macd > signal_line, SHORT otherwise (or EXIT if long_only)",
        "  execute signal at open[t+1]",
    ],
    "stochastic": [
        "on bar t:",
        "  k = 100 * (close - min(low, period)) / (max(high, period) - min(low, period))",
        "  k_smoothed = SMA(k, smooth_k); d = SMA(k_smoothed, smooth_d)",
        "  enter LONG when k < oversold and k crosses above d",
        "  enter SHORT when k > overbought and k crosses below d",
        "  execute signal at open[t+1]",
    ],
    "adx": [
        "on bar t:",
        "  compute +DI, -DI, ADX over period",
        "  signal LONG if ADX >= threshold and +DI > -DI",
        "  signal SHORT if ADX >= threshold and -DI > +DI",
        "  no position when trend strength below threshold",
        "  execute signal at open[t+1]",
    ],
    "keltner": [
        "on bar t:",
        "  typical = (high+low+close)/3",
        "  ema = EMA(typical, window); atr = EMA(TR, window)",
        "  upper = ema + atr_mult*atr; lower = ema - atr_mult*atr",
        "  enter LONG when close < lower (mean reversion)",
        "  enter SHORT when close > upper",
        "  execute signal at open[t+1]",
    ],
    "vwap_dev": [
        "on bar t:",
        "  vwap = sum(typical*volume, window) / sum(volume, window)",
        "  z = (close - vwap) / std(close - vwap, window)",
        "  enter LONG when z < -z_threshold; SHORT when z > z_threshold",
        "  execute signal at open[t+1]",
    ],
    "roc": [
        "on bar t:",
        "  roc = (close[t] / close[t-period] - 1) * 100",
        "  signal LONG when roc > upper, SHORT when roc < lower",
        "  execute signal at open[t+1]",
    ],
    "williams_r": [
        "on bar t:",
        "  hh = max(high, period); ll = min(low, period)",
        "  wr = -100 * (hh - close) / (hh - ll)",
        "  enter LONG when wr < oversold (e.g. -80)",
        "  enter SHORT when wr > overbought (e.g. -20)",
        "  execute signal at open[t+1]",
    ],
    "cci": [
        "on bar t:",
        "  typical = (high+low+close)/3",
        "  cci = (typical - SMA(typical, period)) / (0.015 * mean_abs_dev)",
        "  enter LONG when cci < -threshold; SHORT when cci > threshold",
        "  execute signal at open[t+1]",
    ],
    "mfi": [
        "on bar t:",
        "  typical*volume direction by sign(typical_diff)",
        "  mfi = 100 - 100/(1 + pos_flow_sum / neg_flow_sum) over period",
        "  enter LONG when mfi < oversold, SHORT when mfi > overbought",
        "  execute signal at open[t+1]",
    ],
    "obv_trend": [
        "on bar t:",
        "  obv = cumsum(sign(close_diff) * volume)",
        "  signal LONG when obv > SMA(obv, window), SHORT otherwise",
        "  execute signal at open[t+1]",
    ],
    "psar": [
        "on bar t:",
        "  classic Parabolic SAR with af_step and af_max",
        "  signal LONG while price above SAR, SHORT when SAR flips above price",
        "  execute signal at open[t+1]",
    ],
    "atr_breakout": [
        "on bar t:",
        "  atr = SMA(TR, atr_period)",
        "  enter LONG when close > close[t-1] + mult*atr",
        "  enter SHORT when close < close[t-1] - mult*atr",
        "  execute signal at open[t+1]",
    ],
    "zscore": [
        "on bar t:",
        "  z = (close - SMA(close, window)) / std(close, window)",
        "  enter LONG when z < -threshold (mean reversion)",
        "  enter SHORT when z > threshold",
        "  execute signal at open[t+1]",
    ],
}


def generate_manifest(node_id: str, *, force: bool = False) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not row:
            raise KeyError(node_id)
        node = dict(row)
        metrics = loads(node.get("metrics_json"), {})
        best = metrics.get("best_probe") or {}
        engine = best.get("engine", "unknown")
        params = best.get("params") or {}
        cap = _env_int("AETHER_MANIFEST_PER_FAMILY_CAP", 5)
        if cap > 0 and engine and engine != "unknown" and not force:
            family_count = _family_manifest_count(conn, node.get("campaign_id"), engine)
            if family_count >= cap:
                add_event(
                    conn,
                    type="manifest_skipped_family_cap",
                    node_id=node_id,
                    agent_role="manifest_agent",
                    message=(
                        f"Manifest nao gerado: familia '{engine}' ja tem {family_count}"
                        f" manifests (cap={cap})."
                    ),
                    payload={"family": engine, "count": family_count, "cap": cap},
                )
                return {
                    "skipped": "family_cap",
                    "family": engine,
                    "count": family_count,
                    "cap": cap,
                    "node_id": node_id,
                }
        manifest = {
            "manifest_version": "aether-mvp-v1",
            "generated_at": now_iso(),
            "paper_activation": "manual_only",
            "node": {
                "id": node["id"],
                "code": node["code"],
                "name": node["name"],
                "hypothesis": node["hypothesis"],
                "status": node["status"],
                "score": node["score"],
                "timeframe": node["timeframe"],
                "agent_role": node["agent_role"],
            },
            "engine": {
                "family": engine,
                "params": params,
            },
            "dataset": {
                "dataset_id": best.get("dataset_id"),
                "symbol": best.get("symbol"),
                "timeframe": best.get("timeframe") or node.get("timeframe"),
            },
            "costs": {
                "taker_fee_bps": best.get("fee_bps", 10.0),
                "slippage_bps_per_unit_notional": best.get("slippage_bps", 2.0),
                "spread_bps": best.get("spread_bps", 0.0),
            },
            "sizing": {
                "mode": "fixed_notional",
                "capital": best.get("capital", 10000.0),
                "fraction": best.get("fraction", 0.1),
                "leverage_max": best.get("leverage", 2.0),
            },
            "runtime_invariants": {
                "entry_fill": "market_at_open_next_bar",
                "exit_fill": "market_at_open_next_bar",
                "stop_loss": "disabled",
                "signal_arbitration": "exit_wins_on_tie",
                "paper_or_live_activation": "forbidden_without_human_command",
            },
            "metrics": metrics,
            "pseudocode": PSEUDOCODE_BY_ENGINE.get(engine, ["See linked strategy implementation."]),
            "limitations": [
                "Manifest gerado por MVP; exige revisao humana antes de paper.",
                "Scout nao substitui validacao profunda cross-era, bootstrap e portfolio integration.",
                "Nenhuma ativacao paper/live foi executada.",
            ],
            "paper_checklist": [
                "Validar causalidade e lookahead.",
                "Rodar fee/slippage/spread stress.",
                "Rodar cross-era e sensitivity local.",
                "Confirmar adapter do bot para engine e parametros.",
                "Confirmar sizing e runtime invariants no paper runtime.",
            ],
        }
        out_dir = artifact_dir() / "manifests"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_id = node_id.replace(":", "_").replace("/", "_")
        path = out_dir / f"{safe_id}_{now_iso().replace(':', '').replace('-', '')}.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        add_artifact(conn, node_id=node_id, kind="manifest_draft", path=str(path), title=path.name, payload={})
        add_event(
            conn,
            type="manifest_generated",
            node_id=node_id,
            agent_role="manifest_agent",
            message=f"Manifest detalhado gerado: {path.name}",
            payload={"path": str(path)},
        )
        return {"path": str(path), "manifest": manifest}

