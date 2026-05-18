from __future__ import annotations

import sqlite3
import json
import os
import re
from typing import Any

from .campaign_constants import LEGACY_CAMPAIGN_ID
from .campaigns import campaign_label_map, get_active_campaign_id
from .db import loads


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


READY_STATUSES = {"validated"}
PAPER_READY_MIN_SCORE = _env_float("AETHER_PAPER_READY_MIN_SCORE", 0.42)
PAPER_READY_MIN_TRADES = _env_int("AETHER_PAPER_READY_MIN_TRADES", 30)
PAPER_READY_MAX_MDD_PCT = _env_float("AETHER_PAPER_READY_MAX_MDD_PCT", 35.0)
PAPER_READY_REQUIRE_CRITIC = _env_bool("AETHER_PAPER_READY_REQUIRE_CRITIC", True)
PAPER_READY_REQUIRE_COMBO_FOR_MULTI = _env_bool("AETHER_PAPER_READY_REQUIRE_COMBO_FOR_MULTI", True)
REVIEW_MDD_PCT = 45.0
DEEP_VALIDATION_MARKS = {"deep", "walk_forward", "portfolio_validated", "paper_manifest"}
READINESS_RANK = {"paper_ready": 0, "needs_review": 1, "archive": 2}


def _pick_metric(metrics: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in metrics and metrics[key] is not None:
            return metrics[key]
    last = metrics.get("last_scout")
    if isinstance(last, dict):
        for key in keys:
            if key in last and last[key] is not None:
                return last[key]
    best = metrics.get("best_probe")
    if isinstance(best, dict):
        for key in keys:
            if key in best and best[key] is not None:
                return best[key]
    return None


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _has_deep_validation(metrics: dict[str, Any]) -> bool:
    if metrics.get("validation_passed") is True:
        return True
    stage = str(metrics.get("validation_stage") or metrics.get("validation_level") or "").lower()
    return stage in DEEP_VALIDATION_MARKS


def _passes_scout_gates(score: float, pnl_pct: float, trade_count: int, mdd_pct: float) -> bool:
    return (
        pnl_pct > 0
        and trade_count >= PAPER_READY_MIN_TRADES
        and score >= PAPER_READY_MIN_SCORE
        and mdd_pct <= PAPER_READY_MAX_MDD_PCT
    )


def _critic_approved(metrics: dict[str, Any]) -> bool:
    if not PAPER_READY_REQUIRE_CRITIC:
        return True
    return metrics.get("critic_status") == "approved"


def _combo_ready(metrics: dict[str, Any]) -> bool:
    if not PAPER_READY_REQUIRE_COMBO_FOR_MULTI:
        return True
    specs = metrics.get("probe_specs")
    if not isinstance(specs, list) or len(specs) <= 1:
        return True
    best = metrics.get("best_probe") if isinstance(metrics.get("best_probe"), dict) else {}
    return best.get("engine") == "combo"


def _readiness(status: str, score: float, pnl_pct: float, trade_count: int, mdd_pct: float, metrics: dict[str, Any]) -> str:
    if (
        status in READY_STATUSES
        and _has_deep_validation(metrics)
        and _critic_approved(metrics)
        and _combo_ready(metrics)
        and _passes_scout_gates(score, pnl_pct, trade_count, mdd_pct)
    ):
        return "paper_ready"
    if status in READY_STATUSES:
        return "needs_review"
    return "archive"


def _blockers(readiness: str, pnl_pct: float, trade_count: int, mdd_pct: float, metrics: dict[str, Any] | None = None) -> list[str]:
    if readiness == "paper_ready":
        return []
    metrics = metrics or {}
    blockers = []
    if not _critic_approved(metrics):
        blockers.append("Critic pendente")
    if not _combo_ready(metrics):
        blockers.append("combo multi-engine pendente")
    if pnl_pct <= 0:
        blockers.append("PnL <= 0")
    if trade_count < PAPER_READY_MIN_TRADES:
        blockers.append("trade_count baixo")
    if mdd_pct >= REVIEW_MDD_PCT:
        blockers.append("MDD alto")
    return blockers or ["revisao manual"]


def _compact_name(name: str) -> str:
    parts = [part.strip() for part in str(name).split("/") if part.strip()]
    if len(parts) <= 2:
        return str(name)
    compacted = []
    seen = set()
    for part in parts:
        key = part.lower()
        if key in seen:
            continue
        compacted.append(part)
        seen.add(key)
    return " / ".join(compacted)


def _dedupe_key(row: sqlite3.Row, metrics: dict[str, Any]) -> str:
    best_probe = metrics.get("best_probe") if isinstance(metrics.get("best_probe"), dict) else {}
    campaign_id = row["campaign_id"] or ""
    if best_probe:
        params = best_probe.get("params") or {}
        if not isinstance(params, str):
            params = json.dumps(params, sort_keys=True, ensure_ascii=False)
        return "|".join(
            [
                "probe",
                campaign_id,
                str(row["timeframe"] or best_probe.get("timeframe") or ""),
                str(best_probe.get("symbol") or ""),
                str(best_probe.get("dataset_id") or ""),
                str(best_probe.get("engine") or ""),
                str(params),
            ]
        )
    if row["source_path"]:
        return f"source|{campaign_id}|{row['source_type'] or ''}|{row['source_path']}"
    name_key = re.sub(r"\s+", " ", _compact_name(row["name"]).lower()).strip()
    return f"name|{campaign_id}|{row['timeframe'] or ''}|{name_key}"


def _source_bucket(node_id: str, campaign_id: str | None, source_type: str | None) -> str:
    if campaign_id == LEGACY_CAMPAIGN_ID or source_type in {"approved", "staging", "diag", "legacy_root"}:
        return "legacy"
    if source_type == "web_research" or node_id.startswith("webhyp:") or ":webhyp:" in node_id:
        return "web"
    return "aether"


def _deployed_node_ids() -> set[str]:
    """Retorna node_ids ja ativos no bot (active_strategies.json)."""
    try:
        from src.research.active import list_active

        out: set[str] = set()
        for rec in list_active():
            for key in ("source_node_id", "node_id"):
                nid = rec.get(key)
                if nid:
                    out.add(str(nid))
        return out
    except Exception:
        return set()


def _deployed_records() -> list[dict[str, Any]]:
    try:
        from src.research.active import list_active

        return list(list_active())
    except Exception:
        return []


def active_manifest_summary() -> dict[str, Any]:
    """Resumo do manifesto que o bot realmente esta usando."""
    records = _deployed_records()
    source_ids: set[str] = set()
    manual = 0
    aether = 0
    for rec in records:
        source = str(rec.get("source") or "").lower()
        identifiers = [
            str(rec.get("id") or ""),
            str(rec.get("source_node_id") or ""),
            str(rec.get("node_id") or ""),
        ]
        if "manual" in source or any(value.startswith(("manual:", "manual-")) for value in identifiers):
            manual += 1
        else:
            aether += 1
        for key in ("source_node_id", "node_id"):
            node_id = rec.get(key)
            if node_id:
                source_ids.add(str(node_id))
    return {
        "active_manifest_total": len(records),
        "active_manifest_sources": len(source_ids),
        "active_manifest_manual": manual,
        "active_manifest_aether": aether,
    }


_DEPLOY_ALLOWED_ASSETS = {
    a.strip().upper()
    for a in os.environ.get("AETHER_DEPLOY_ALLOWED_ASSETS", "BTCUSDT,ETHUSDT,SOLUSDT,ADAUSDT").split(",")
    if a.strip()
}
_DEPLOY_MAX_PER_FAMILY = _env_int("AETHER_DEPLOY_MAX_PER_FAMILY", 8)
_DEPLOY_MAX_PER_ASSET = _env_int("AETHER_DEPLOY_MAX_PER_ASSET", 5)
_DEPLOY_MAX_PER_ASSET_FAMILY_TF = _env_int("AETHER_DEPLOY_MAX_PER_ASSET_FAMILY_TF", 1)


def _deploy_cap_state(deployed_recs: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    family: dict[str, int] = {}
    asset: dict[str, int] = {}
    aft: dict[str, int] = {}
    for rec in deployed_recs:
        fam = str(rec.get("engine") or rec.get("template") or "?")
        sym = str(rec.get("asset") or rec.get("symbol") or "?").upper()
        tf = str(rec.get("timeframe") or "")
        family[fam] = family.get(fam, 0) + 1
        asset[sym] = asset.get(sym, 0) + 1
        aft["|".join([sym, fam, tf])] = aft.get("|".join([sym, fam, tf]), 0) + 1
    return {"family": family, "asset": asset, "aft": aft}


def _deploy_blocker(
    item: dict[str, Any],
    metrics: dict[str, Any],
    cap_state: dict[str, dict[str, int]],
) -> str | None:
    best = metrics.get("best_probe") if isinstance(metrics.get("best_probe"), dict) else {}
    sym = str(best.get("symbol") or "").upper()
    eng = str(best.get("engine") or "?")
    tf = str(best.get("timeframe") or item.get("timeframe") or "")
    if _DEPLOY_ALLOWED_ASSETS and "*" not in _DEPLOY_ALLOWED_ASSETS and sym not in _DEPLOY_ALLOWED_ASSETS:
        return f"Asset {sym or '?'} fora da allowlist do bot ({','.join(sorted(_DEPLOY_ALLOWED_ASSETS))})"
    if _DEPLOY_MAX_PER_ASSET_FAMILY_TF > 0 and cap_state["aft"].get("|".join([sym, eng, tf]), 0) >= _DEPLOY_MAX_PER_ASSET_FAMILY_TF:
        return f"Cap saturado: ja existe {eng}/{sym}/{tf} no bot (max {_DEPLOY_MAX_PER_ASSET_FAMILY_TF})"
    if _DEPLOY_MAX_PER_FAMILY > 0 and cap_state["family"].get(eng, 0) >= _DEPLOY_MAX_PER_FAMILY:
        return f"Cap saturado: familia {eng} ja tem {_DEPLOY_MAX_PER_FAMILY} no bot"
    if _DEPLOY_MAX_PER_ASSET > 0 and cap_state["asset"].get(sym, 0) >= _DEPLOY_MAX_PER_ASSET:
        return f"Cap saturado: asset {sym} ja tem {_DEPLOY_MAX_PER_ASSET} no bot"
    return "Aguardando proximo ciclo do auto_deploy (~60s)"


def build_strategy_ranking(
    conn: sqlite3.Connection,
    *,
    starting_capital: float = 1000.0,
    limit: int = 80,
    campaign_id: str | None = None,
    created_after: str | None = None,
    dedupe: bool = True,
    include_deployed: bool = True,
    hide_blocked: bool = False,
) -> list[dict[str, Any]]:
    active_campaign = get_active_campaign_id(conn)
    campaign_filter = campaign_id or active_campaign
    labels = campaign_label_map(conn)
    deployed_ids = _deployed_node_ids()
    deployed_recs = _deployed_records()
    cap_state = _deploy_cap_state(deployed_recs)
    hidden_ids = set() if include_deployed else deployed_ids
    params: list[Any] = []
    campaign_where = ""
    if campaign_filter != "all":
        campaign_where = "AND campaign_id=?"
        params.append(campaign_filter)
    created_where = ""
    if created_after:
        created_where = "AND created_at > ?"
        params.append(created_after)
    params.append(max(limit * 30, 600))
    rows = conn.execute(
        f"""
        SELECT id, campaign_id, code, name, status, score, priority, timeframe, agent_role,
               result_summary, prune_reason, metrics_json, source_type, source_path, created_at, updated_at
        FROM nodes
        WHERE status='validated'
          AND id != 'root'
        {campaign_where}
        {created_where}
        ORDER BY score DESC, priority DESC, updated_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    ranking = []
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        if hidden_ids and row["id"] in hidden_ids:
            continue
        metrics = loads(row["metrics_json"], {})
        pnl_pct = _as_float(_pick_metric(metrics, "pnl_pct", "oos_pnl_pct", "return_pct"))
        best_probe = metrics.get("best_probe") if isinstance(metrics.get("best_probe"), dict) else {}
        if best_probe:
            probe_capital = _as_float(best_probe.get("capital"), 0.0)
            final_equity = _as_float(best_probe.get("final_equity"), 0.0)
            if probe_capital > 0 and final_equity > 0:
                pnl_pct = (final_equity / probe_capital - 1.0) * 100.0

        ending_capital = starting_capital * (1.0 + pnl_pct / 100.0)
        trade_count = int(_as_float(_pick_metric(metrics, "trade_count", "trades", "oos_trades"), 0.0))
        mdd_pct = _as_float(_pick_metric(metrics, "mdd_pct", "oos_mdd_pct"))
        sharpe = _as_float(_pick_metric(metrics, "sharpe", "oos_sharpe"))
        bars = int(_as_float(_pick_metric(metrics, "bars", "oos_bars") or (best_probe or {}).get("bars"), 0.0))
        from .scoring import _format_period as _fmt_period
        period = _fmt_period(bars, row["timeframe"]) if bars and row["timeframe"] else ""
        score = _as_float(row["score"])
        readiness = _readiness(row["status"], score, pnl_pct, trade_count, mdd_pct, metrics)
        if readiness != "paper_ready":
            continue

        is_deployed = row["id"] in deployed_ids
        deploy_blocker_msg = None if is_deployed else _deploy_blocker(
            {"timeframe": row["timeframe"]}, metrics, cap_state
        )
        if (
            hide_blocked
            and not is_deployed
            and deploy_blocker_msg
            and not deploy_blocker_msg.startswith("Aguardando")
        ):
            continue
        item = {
            "node_id": row["id"],
            "campaign_id": row["campaign_id"],
            "campaign_label": labels.get(row["campaign_id"], row["campaign_id"] or "sem campanha"),
            "source_bucket": _source_bucket(row["id"], row["campaign_id"], row["source_type"]),
            "code": row["code"],
            "name": _compact_name(row["name"]),
            "status": row["status"],
            "readiness": readiness,
            "score": round(score, 4),
            "timeframe": row["timeframe"],
            "agent_role": row["agent_role"],
            "starting_capital": round(starting_capital, 2),
            "ending_capital": round(ending_capital, 2),
            "pnl_abs": round(ending_capital - starting_capital, 2),
            "pnl_pct": round(pnl_pct, 4),
            "sharpe": round(sharpe, 4),
            "mdd_pct": round(mdd_pct, 4),
            "trade_count": trade_count,
            "bars": bars,
            "period": period,
            "blockers": _blockers(readiness, pnl_pct, trade_count, mdd_pct, metrics),
            "summary": row["result_summary"] or row["prune_reason"] or "",
            "source_type": row["source_type"],
            "source_path": row["source_path"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "duplicate_count": 1,
            "deployed": is_deployed,
            "deploy_blocker": deploy_blocker_msg,
        }
        key = _dedupe_key(row, metrics)
        if dedupe and key in seen:
            current = seen[key]
            current["duplicate_count"] += 1
            current_rank = READINESS_RANK.get(current["readiness"], 9)
            item_rank = READINESS_RANK.get(item["readiness"], 9)
            current_sort = (current_rank, -current["score"], -current["ending_capital"])
            item_sort = (item_rank, -item["score"], -item["ending_capital"])
            if item_sort < current_sort:
                item["duplicate_count"] = current["duplicate_count"]
                seen[key] = item
            continue
        seen[key] = item
        ranking.append(item)

    if dedupe:
        ranking = list(seen.values())

    ranking.sort(
        key=lambda item: (
            READINESS_RANK.get(item["readiness"], 9),
            -item["pnl_pct"],
            -item["sharpe"],
            item["mdd_pct"],
            -item["score"],
        )
    )
    return ranking[:limit]


def ranking_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = {
        "paper_ready": 0,
        "needs_review": 0,
        "legacy": 0,
        "web": 0,
        "aether": 0,
        "duplicates_hidden": 0,
        "deployed": 0,
        "not_deployed": 0,
    }
    for row in rows:
        out[row.get("readiness") or "archive"] = out.get(row.get("readiness") or "archive", 0) + 1
        bucket = row.get("source_bucket") or "aether"
        out[bucket] = out.get(bucket, 0) + 1
        out["duplicates_hidden"] += max(0, int(row.get("duplicate_count") or 1) - 1)
        if row.get("deployed"):
            out["deployed"] += 1
        else:
            out["not_deployed"] += 1
    return out
