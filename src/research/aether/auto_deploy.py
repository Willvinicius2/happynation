"""Sidecar auto-deploy: pega nodes validated no DB do Aether e injeta em
`src/logs/research/active_strategies.json` via `src.research.active`.

O `StrategyRunner` do bot ja consulta o manifest ativo antes de cair
para a estrategia hardcoded (Champions), entao basta popular o arquivo
para o bot comecar a usar as estrategias validadas pelo Aether.

Subir com:
    .venv/Scripts/python.exe -u -m src.research.aether.auto_deploy
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from src.research.aether.config import db_path, sqlite_busy_retry_seconds, sqlite_busy_timeout_ms
from src.research.active import deploy_record, list_active, remove

LOG_PATH = Path(__file__).resolve().parents[3] / "src" / "logs" / "aether" / "auto_deploy.log"
BLOCKLIST_PATH = Path(
    os.environ.get(
        "AETHER_DEPLOY_BLOCKLIST_PATH",
        str(Path(__file__).resolve().parents[3] / "src" / "logs" / "research" / "strategy_blocklist.json"),
    )
)

POLL_SECONDS = int(os.environ.get("AETHER_DEPLOY_POLL_SECONDS", "60"))
BUSY_RETRY_SECONDS = sqlite_busy_retry_seconds()


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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}


def _env_csv_set(name: str, default: str) -> set[str]:
    raw = os.environ.get(name, default)
    return {part.strip().upper() for part in raw.split(",") if part.strip()}


def _env_csv_list(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default)
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


# Gates configuraveis via env. MDD <= 0 desliga o bloqueio por drawdown:
# o deploy passa a decidir por lucro/trades/score/Critic/caps.
MIN_SCORE = _env_float("AETHER_DEPLOY_MIN_SCORE", 0.42)
MIN_TRADES = _env_int("AETHER_DEPLOY_MIN_TRADES", 12)
MAX_MDD_PCT = _env_float("AETHER_DEPLOY_MAX_MDD_PCT", 0.0)
MIN_PNL_PCT = _env_float("AETHER_DEPLOY_MIN_PNL_PCT", 0.5)
# 'approved' = exige Critic aprovado; 'any' = aceita pending/sem critic.
CRITIC_GATE = os.environ.get("AETHER_DEPLOY_CRITIC_GATE", "approved").strip().lower()

# Cap por familia (engine) para diversificar.
# Valores frouxos: o score composto + Critic ja protegem contra overfitting,
# entao o cap aqui e apenas para evitar concentracao patologica.
MAX_PER_FAMILY = _env_int("AETHER_DEPLOY_MAX_PER_FAMILY", 30)
MAX_PER_ASSET = _env_int("AETHER_DEPLOY_MAX_PER_ASSET", 12)
# Limita repeticao no mesmo par/timeframe,
# mesmo par/timeframe sem derrubar demais o numero de votos.
MAX_PER_ASSET_FAMILY_TF = _env_int("AETHER_DEPLOY_MAX_PER_ASSET_FAMILY_TF", 5)
REQUIRE_COMBO_FOR_MULTI = _env_bool("AETHER_DEPLOY_REQUIRE_COMBO_FOR_MULTI", True)
ALLOWED_ASSETS = _env_csv_set("AETHER_DEPLOY_ALLOWED_ASSETS", "BTCUSDT,ETHUSDT,SOLUSDT,ADAUSDT")
ALLOWED_TIMEFRAMES = _env_csv_set("AETHER_DEPLOY_ALLOWED_TIMEFRAMES", "1H")

# Maximo total deployado.
DEPLOY_TOP_N = _env_int("AETHER_DEPLOY_TOP_N", 250)
MANAGED_SOURCE_PATTERNS = _env_csv_list("AETHER_DEPLOY_MANAGED_SOURCES", "aether_auto_deploy,aether_happynation")
RESPECT_UNMANAGED_CAPS = _env_bool("AETHER_DEPLOY_RESPECT_UNMANAGED_CAPS", True)
TRADE_COUNT_WEIGHT = _env_float("AETHER_DEPLOY_TRADE_COUNT_WEIGHT", 0.35)
PNL_WEIGHT = _env_float("AETHER_DEPLOY_PNL_WEIGHT", 0.08)
SHARPE_WEIGHT = _env_float("AETHER_DEPLOY_SHARPE_WEIGHT", 0.08)
MDD_PENALTY = _env_float("AETHER_DEPLOY_MDD_PENALTY", 0.10)
FAST_TF_BONUS = _env_float("AETHER_DEPLOY_FAST_TF_BONUS", 0.08)
MIN_HIT_RATE = _env_float("AETHER_DEPLOY_MIN_HIT_RATE", 0.0)
HIT_RATE_WEIGHT = _env_float("AETHER_DEPLOY_HIT_RATE_WEIGHT", 0.0)
MAX_EST_TRADES_PER_DAY = _env_float("AETHER_DEPLOY_MAX_EST_TRADES_PER_DAY", 0.0)
RESPECT_RANKING_RESET = _env_bool("AETHER_DEPLOY_RESPECT_RANKING_RESET", True)
AETHER_DEPLOY_CAMPAIGN_ID = os.environ.get("AETHER_DEPLOY_CAMPAIGN_ID", "").strip()

# Default risk se nao houver no node.
# TP 10% deixa o trail decidir a saida na maioria dos casos: ele reage ao
# topo real de cada trade enquanto TP fixo so age na cota. SL 2% e maxhold 96
# permanecem como rede de seguranca.
DEFAULT_RISK = {
    "take_profit_pct": 10.0,
    "stop_loss_pct": 2.0,
    "max_hold_candles": 96,
}


def _active_id(node_id: str, engine: str, params: dict, symbol: str, timeframe: str) -> str:
    payload = json.dumps(
        {
            "node_id": node_id,
            "engine": engine,
            "params": params,
            "symbol": symbol,
            "timeframe": timeframe,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return "aether-" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:14]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _setup_logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("aether.auto_deploy")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def _connect() -> sqlite3.Connection:
    busy_timeout = sqlite_busy_timeout_ms()
    conn = sqlite3.connect(str(db_path()), timeout=busy_timeout / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={busy_timeout}")
    return conn


def _load_blocklist() -> dict:
    if not BLOCKLIST_PATH.exists():
        return {"active_ids": {}, "source_node_ids": {}}
    try:
        data = json.loads(BLOCKLIST_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"active_ids": {}, "source_node_ids": {}}
    active_ids = data.get("active_ids") or {}
    source_node_ids = data.get("source_node_ids") or {}
    if isinstance(active_ids, list):
        active_ids = {str(item): {"reason": "blocked"} for item in active_ids}
    if isinstance(source_node_ids, list):
        source_node_ids = {str(item): {"reason": "blocked"} for item in source_node_ids}
    return {
        "active_ids": active_ids if isinstance(active_ids, dict) else {},
        "source_node_ids": source_node_ids if isinstance(source_node_ids, dict) else {},
    }


def _block_reason(blocklist: dict, active_id: str, source_node_id: str) -> str:
    for bucket, key in (("active_ids", active_id), ("source_node_ids", source_node_id)):
        if not key:
            continue
        entry = (blocklist.get(bucket) or {}).get(str(key))
        if entry:
            if isinstance(entry, dict):
                return str(entry.get("reason") or "blocked")
            return str(entry)
    return ""


def _active_campaign_id(conn: sqlite3.Connection) -> str | None:
    if AETHER_DEPLOY_CAMPAIGN_ID:
        return AETHER_DEPLOY_CAMPAIGN_ID
    try:
        row = conn.execute(
            """
            SELECT id
            FROM campaigns
            WHERE kind='current' AND status='active'
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.Error:
        return None
    return str(row["id"]) if row and row["id"] else None


def _ranking_reset_filter(conn: sqlite3.Connection, campaign_id: str | None) -> tuple[str | None, set[str]]:
    if not RESPECT_RANKING_RESET or not campaign_id:
        return None, set()
    try:
        row = conn.execute(
            "SELECT value_json FROM settings WHERE key=?",
            (f"ranking_hidden_node_ids:{campaign_id}",),
        ).fetchone()
    except sqlite3.Error:
        return None, set()
    if not row:
        return None, set()
    try:
        payload = json.loads(row["value_json"] or "{}")
    except json.JSONDecodeError:
        return None, set()
    if not isinstance(payload, dict):
        return None, set()
    hidden_ids = {str(node_id) for node_id in (payload.get("hidden_node_ids") or []) if node_id}
    reset_at = str(payload.get("reset_at") or "") or None
    return reset_at, hidden_ids


def _validated_nodes(conn: sqlite3.Connection) -> tuple[list[tuple[sqlite3.Row, dict]], dict]:
    campaign_id = _active_campaign_id(conn)
    reset_at, hidden_ids = _ranking_reset_filter(conn, campaign_id)
    params: list[object] = []
    campaign_clause = ""
    reset_clause = ""
    if campaign_id:
        campaign_clause = "AND campaign_id=?"
        params.append(campaign_id)
    if reset_at:
        reset_clause = "AND created_at > ?"
        params.append(reset_at)
    rows = conn.execute(
        """
        SELECT id, name, score, metrics_json, created_at
        FROM nodes
        WHERE status='validated'
          AND id != 'root'
          AND id NOT LIKE 'tf:%'
          {campaign_clause}
          {reset_clause}
        """.format(campaign_clause=campaign_clause, reset_clause=reset_clause),
        params,
    ).fetchall()
    out: list[tuple[sqlite3.Row, dict]] = []
    hidden_filtered = 0
    for row in rows:
        if str(row["id"] or "") in hidden_ids:
            hidden_filtered += 1
            continue
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except json.JSONDecodeError:
            continue
        stage = str(metrics.get("validation_stage") or metrics.get("validation_level") or "").lower()
        if metrics.get("validation_passed") is not True and stage not in {
            "deep",
            "walk_forward",
            "portfolio_validated",
            "paper_manifest",
        }:
            continue
        out.append((row, metrics))
    return out, {
        "campaign_id": campaign_id,
        "reset_at": reset_at,
        "hidden_ids": len(hidden_ids),
        "hidden_filtered": hidden_filtered,
    }


def _pick_metric(metrics: dict, *keys: str):
    for key in keys:
        if metrics.get(key) is not None:
            return metrics.get(key)
    for container in (metrics.get("last_scout"), metrics.get("best_probe")):
        if not isinstance(container, dict):
            continue
        for key in keys:
            if container.get(key) is not None:
                return container.get(key)
    return None


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _deploy_gate_block_reason(metrics: dict, score: float) -> str:
    if _as_float(_pick_metric(metrics, "pnl_pct", "oos_pnl_pct", "return_pct")) < MIN_PNL_PCT:
        return "pnl_below_min"
    if _as_int(_pick_metric(metrics, "trade_count", "trades", "oos_trades")) < MIN_TRADES:
        return "trades_below_min"
    if score < MIN_SCORE:
        return "score_below_min"
    if MAX_MDD_PCT > 0 and _as_float(_pick_metric(metrics, "mdd_pct", "oos_mdd_pct")) > MAX_MDD_PCT:
        return "mdd_above_max"
    # Critic gate configuravel: 'approved' exige aprovacao explicita;
    # 'any' aceita pending/sem critic mas ainda bloqueia rejected.
    critic_status = metrics.get("critic_status")
    if critic_status == "rejected":
        return "critic_rejected"
    if CRITIC_GATE == "approved" and critic_status != "approved":
        return "critic_not_approved"
    min_hit_pct = _normalize_hit_rate(MIN_HIT_RATE)
    if min_hit_pct > 0:
        hit_pct = _normalize_hit_rate(_pick_metric(metrics, "hit_rate", "win_rate", "winrate"))
        if hit_pct < min_hit_pct:
            return "hit_rate_below_min"
    return ""


def _passes_deploy_gates(metrics: dict, score: float) -> bool:
    return _deploy_gate_block_reason(metrics, score) == ""


def _record_for_with_reason(row: sqlite3.Row, metrics: dict) -> tuple[dict | None, str]:
    best = metrics.get("best_probe") or {}
    engine = best.get("engine")
    if not engine:
        return None, "missing_engine"
    params = best.get("params")
    if not isinstance(params, dict) or not params:
        return None, "missing_params"
    probe_specs = metrics.get("probe_specs") if isinstance(metrics.get("probe_specs"), list) else []
    clean_specs = [
        {"engine": str(spec.get("engine")), "params": dict(spec.get("params") or {})}
        for spec in probe_specs
        if isinstance(spec, dict) and spec.get("engine")
    ]
    if len(clean_specs) > 1:
        if REQUIRE_COMBO_FOR_MULTI and str(engine) != "combo":
            return None, "multi_probe_requires_combo"
        if str(engine) == "combo":
            params = {**params, "mode": params.get("mode") or "consensus", "specs": params.get("specs") or clean_specs}
    symbol = (best.get("symbol") or "").upper()
    if not symbol:
        return None, "missing_symbol"
    if not _asset_allowed(symbol):
        return None, "asset_not_allowed"
    score = float(row["score"])
    timeframe = str(best.get("timeframe") or "")
    if not _timeframe_allowed(timeframe):
        return None, "timeframe_not_allowed"
    rec_id = _active_id(row["id"], str(engine), params, symbol, timeframe)
    return {
        "id": rec_id,
        "engine": str(engine),
        "params": params,
        "probe_specs": clean_specs,
        "asset": symbol,
        "timeframe": timeframe,
        "score": score,
        "risk": dict(DEFAULT_RISK),
        "summary": {
            "name": row["name"],
            "pnl_pct": _pick_metric(metrics, "pnl_pct", "oos_pnl_pct", "return_pct"),
            "sharpe": _pick_metric(metrics, "sharpe", "oos_sharpe"),
            "mdd_pct": _pick_metric(metrics, "mdd_pct", "oos_mdd_pct"),
            "trade_count": _pick_metric(metrics, "trade_count", "trades", "oos_trades"),
            "hit_rate": best.get("hit_rate"),
            "validation_passed": bool(metrics.get("validation_passed")),
            "validation_stage": metrics.get("validation_stage"),
            "validated_at": metrics.get("validated_at"),
            "validated_by": metrics.get("validated_by"),
            "critic_status": metrics.get("critic_status"),
            "critic_reason": metrics.get("critic_reason"),
            "combo_count": metrics.get("combo_count"),
        },
        "source_node_id": row["id"],
        "source": "aether_auto_deploy",
    }, ""


def _record_for(row: sqlite3.Row, metrics: dict) -> dict | None:
    rec, _reason = _record_for_with_reason(row, metrics)
    return rec


def _asset_allowed(symbol: str) -> bool:
    if not ALLOWED_ASSETS or "*" in ALLOWED_ASSETS:
        return True
    return symbol.upper() in ALLOWED_ASSETS


def _timeframe_allowed(timeframe: str) -> bool:
    if not ALLOWED_TIMEFRAMES or "*" in ALLOWED_TIMEFRAMES:
        return True
    return timeframe.upper() in ALLOWED_TIMEFRAMES


def _family_for(rec: dict) -> str:
    return str(rec.get("engine") or rec.get("template") or "?")


def _asset_for(rec: dict) -> str:
    return str(rec.get("asset") or rec.get("symbol") or "?")


def _asset_family_tf_key(rec: dict) -> str:
    return "|".join([_asset_for(rec), _family_for(rec), str(rec.get("timeframe") or "")])


def _source_matches_pattern(source: str, pattern: str) -> bool:
    pattern = str(pattern or "").strip().lower()
    if not pattern:
        return False
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return source.startswith(pattern[:-1])
    return source == pattern


def _is_managed_record(rec: dict) -> bool:
    """Records managed by this deploy loop may be rotated out.

    Legacy Aether deploys such as ``aether_happynation`` are rotatable, while
    manual ranking deploys remain reserved slots unless the operator opts in
    through AETHER_DEPLOY_MANAGED_SOURCES.
    """
    source = str(rec.get("source") or rec.get("origin") or "").strip().lower()
    return any(_source_matches_pattern(source, pattern) for pattern in MANAGED_SOURCE_PATTERNS)


def _seed_cap_counts(records: list[dict]) -> tuple[dict[str, int], dict[str, int], dict[str, int], dict[tuple[str, str], int]]:
    family_counts: dict[str, int] = {}
    asset_counts: dict[str, int] = {}
    asset_family_tf_counts: dict[str, int] = {}
    hyp_asset_counts: dict[tuple[str, str], int] = {}
    for rec in records:
        family = _family_for(rec)
        asset = _asset_for(rec)
        asset_family_tf = _asset_family_tf_key(rec)
        hyp_key = (_hypothesis_base_name(rec), asset)
        family_counts[family] = family_counts.get(family, 0) + 1
        asset_counts[asset] = asset_counts.get(asset, 0) + 1
        asset_family_tf_counts[asset_family_tf] = asset_family_tf_counts.get(asset_family_tf, 0) + 1
        hyp_asset_counts[hyp_key] = hyp_asset_counts.get(hyp_key, 0) + 1
    return family_counts, asset_counts, asset_family_tf_counts, hyp_asset_counts


def _functional_fingerprint(rec: dict) -> tuple:
    """Identidade pelo *resultado* do backtest, nao pelos params.

    Granularidade ajustada pra valor inteiro de pnl/mdd (1 casa decimal):
    pnl=23.6 e pnl=23.3 antes geravam fingerprints distintos e ambos passavam.
    Agora 23.6 e 23.7 colidem — mantem o de maior score e descarta o irmao
    que difere por ruido de backtest, nao por edge real.
    """
    summary = rec.get("summary") or {}
    return (
        rec.get("asset"),
        rec.get("timeframe"),
        round(float(summary.get("pnl_pct") or 0.0), 1),
        int(summary.get("trade_count") or 0),
        round(float(summary.get("mdd_pct") or 0.0), 1),
        round(float(summary.get("hit_rate") or 0.0), 3),
    )


def _record_manifest_view(rec: dict) -> str:
    payload = dict(rec)
    payload.pop("deployed_at", None)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _summary_float(rec: dict, key: str, default: float = 0.0) -> float:
    summary = rec.get("summary") if isinstance(rec.get("summary"), dict) else {}
    return _as_float(summary.get(key), default)


def _normalize_hit_rate(value) -> float:
    hit = _as_float(value, 0.0)
    if hit <= 0:
        return 0.0
    return hit * 100.0 if hit <= 1.0 else hit


def _timeframe_minutes(timeframe: str) -> int | None:
    tf = str(timeframe or "").strip().lower()
    table = {
        "1m": 1,
        "3m": 3,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60,
        "2h": 120,
        "4h": 240,
        "6h": 360,
        "8h": 480,
        "12h": 720,
        "1d": 1440,
    }
    return table.get(tf)


def _estimated_trades_per_day(rec: dict) -> float:
    trades = _summary_float(rec, "trade_count")
    if trades <= 0:
        return 0.0
    tf = str(rec.get("timeframe") or "").strip().lower()
    # These divisors mirror the current Aether validation windows used by the
    # frequency reports: short intraday candidates have a shorter available
    # window than 1h/4h/1d candidates.
    days_by_tf = {
        "15m": 180.0,
        "1h": 730.0,
        "4h": 730.0,
        "1d": 1095.0,
    }
    days = days_by_tf.get(tf, 730.0)
    return trades / days if days > 0 else 0.0


def _fast_timeframe_bonus(timeframe: str) -> float:
    minutes = _timeframe_minutes(timeframe)
    if minutes is None or minutes <= 0:
        return 0.0
    if minutes <= 15:
        return FAST_TF_BONUS
    if minutes <= 60:
        return FAST_TF_BONUS * 0.65
    if minutes <= 240:
        return FAST_TF_BONUS * 0.25
    return 0.0


def _deploy_rank_score(base_score: float, rec: dict) -> float:
    """Ranking operacional: robustez primeiro, mas com vies de mais amostras.

    O score original do Aether continua sendo gate e fica no manifest. Este
    ranking so decide quais candidatos entram quando ha mais paper-ready que
    slots disponiveis.
    """
    trades = _summary_float(rec, "trade_count")
    pnl = _summary_float(rec, "pnl_pct")
    sharpe = _summary_float(rec, "sharpe")
    mdd = _summary_float(rec, "mdd_pct")
    hit_pct = _normalize_hit_rate(_summary_float(rec, "hit_rate"))
    trade_bonus = min(max(trades, 0.0), 180.0) / 180.0 * TRADE_COUNT_WEIGHT
    pnl_bonus = min(max(pnl, 0.0), 200.0) / 200.0 * PNL_WEIGHT
    sharpe_bonus = min(max(sharpe, 0.0), 3.0) / 3.0 * SHARPE_WEIGHT
    hit_bonus = min(max(hit_pct, 0.0), 100.0) / 100.0 * HIT_RATE_WEIGHT
    mdd_drag = min(max(mdd, 0.0), 50.0) / 50.0 * MDD_PENALTY
    return base_score + trade_bonus + pnl_bonus + sharpe_bonus + hit_bonus + _fast_timeframe_bonus(str(rec.get("timeframe") or "")) - mdd_drag


def _hypothesis_base_name(rec: dict) -> str:
    """Extrai o nome base da hipotese descartando sufixos de timeframe.

    Nomes vem como "Mutacao de Zscore mean-revert em regime low-vol / 1h / 15m".
    Cortar antes do primeiro " / " agrupa todas as variacoes de TF da MESMA
    hipotese. Usado pra impor um cap de variacoes por hipotese+ativo.
    """
    summary = rec.get("summary") or {}
    name = str(summary.get("name") or "")
    base = name.split(" / ")[0].strip().lower()
    return base or "?"


# Maximo de variacoes (timeframes) da MESMA hipotese-base no MESMO ativo.
# Default 0 = sem cap por hipotese+ativo. O ranking/critic ja filtram robustez;
# manter este cap baixo fazia o manifest travar em poucas dezenas mesmo com
# muito paper_ready disponivel.
MAX_PER_HYPOTHESIS_ASSET = _env_int("AETHER_DEPLOY_MAX_PER_HYPOTHESIS_ASSET", 0)


def _format_reason_counts(counts: Counter[str], limit: int = 8) -> str:
    if not counts:
        return ""
    parts = [f"{key}:{value}" for key, value in counts.most_common(limit)]
    extra = sum(counts.values()) - sum(value for _key, value in counts.most_common(limit))
    if extra > 0:
        parts.append(f"other:{extra}")
    return ",".join(parts)


def _cap_detail_key(kind: str, *parts: str) -> str:
    clean_parts = []
    for part in parts:
        token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(part or "?")).strip("_")
        clean_parts.append((token or "?")[:48])
    return f"{kind}:{'|'.join(clean_parts)}"


def _reason_bucket(reason: str) -> str:
    text = str(reason or "blocked").strip().lower()
    if text.startswith("manifest top validation rejected:"):
        detail = text.split(":", 1)[1]
        if "cross-symbol" in detail:
            return "manifest_top_validation_cross_symbol"
        if "trade sample" in detail:
            return "manifest_top_validation_trade_sample"
        if "walk-forward" in detail:
            return "manifest_top_validation_walk_forward"
        return "manifest_top_validation_rejected"
    if text.startswith("heavy validation"):
        if "cut_or_revalidate" in text:
            return "heavy_validation_cut_or_revalidate"
        if "cut_or_pause" in text:
            return "heavy_validation_cut_or_pause"
        return "heavy_validation"
    key = re.sub(r"[^a-z0-9_]+", "_", text).strip("_")
    return key[:80] or "blocked"


def _select_deploy_records(
    ranked: list[tuple[float, dict]],
    reserved_records: list[dict] | None = None,
    cap_reasons: Counter[str] | None = None,
    cap_details: Counter[str] | None = None,
) -> list[tuple[float, dict]]:
    selected: list[tuple[float, dict]] = []
    family_counts, asset_counts, asset_family_tf_counts, hyp_asset_counts = _seed_cap_counts(reserved_records or [])
    selected_est_tpd = sum(_estimated_trades_per_day(rec) for rec in (reserved_records or []))
    for idx, (score, rec) in enumerate(ranked):
        family = _family_for(rec)
        asset = _asset_for(rec)
        asset_family_tf = _asset_family_tf_key(rec)
        hyp_key = (_hypothesis_base_name(rec), asset)
        if MAX_PER_FAMILY > 0 and family_counts.get(family, 0) >= MAX_PER_FAMILY:
            if cap_reasons is not None:
                cap_reasons["family_cap"] += 1
            if cap_details is not None:
                cap_details[_cap_detail_key("family", family)] += 1
            continue
        if MAX_PER_ASSET > 0 and asset_counts.get(asset, 0) >= MAX_PER_ASSET:
            if cap_reasons is not None:
                cap_reasons["asset_cap"] += 1
            if cap_details is not None:
                cap_details[_cap_detail_key("asset", asset)] += 1
            continue
        if MAX_PER_ASSET_FAMILY_TF > 0 and asset_family_tf_counts.get(asset_family_tf, 0) >= MAX_PER_ASSET_FAMILY_TF:
            if cap_reasons is not None:
                cap_reasons["asset_family_timeframe_cap"] += 1
            if cap_details is not None:
                cap_details[_cap_detail_key("asset_family_timeframe", asset, family, str(rec.get("timeframe") or "?"))] += 1
            continue
        # Cap de variacoes da mesma hipotese-base no mesmo ativo.
        if MAX_PER_HYPOTHESIS_ASSET > 0 and hyp_asset_counts.get(hyp_key, 0) >= MAX_PER_HYPOTHESIS_ASSET:
            if cap_reasons is not None:
                cap_reasons["hypothesis_asset_cap"] += 1
            if cap_details is not None:
                cap_details[_cap_detail_key("hypothesis_asset", asset, hyp_key[0])] += 1
            continue
        rec_est_tpd = _estimated_trades_per_day(rec)
        if MAX_EST_TRADES_PER_DAY > 0 and rec_est_tpd > 0 and selected_est_tpd + rec_est_tpd > MAX_EST_TRADES_PER_DAY:
            if cap_reasons is not None:
                cap_reasons["estimated_trades_per_day_cap"] += 1
            if cap_details is not None:
                cap_details[_cap_detail_key("estimated_tpd", asset, str(rec.get("timeframe") or "?"))] += 1
            continue
        selected.append((score, rec))
        selected_est_tpd += rec_est_tpd
        family_counts[family] = family_counts.get(family, 0) + 1
        asset_counts[asset] = asset_counts.get(asset, 0) + 1
        asset_family_tf_counts[asset_family_tf] = asset_family_tf_counts.get(asset_family_tf, 0) + 1
        hyp_asset_counts[hyp_key] = hyp_asset_counts.get(hyp_key, 0) + 1
        if len(selected) >= DEPLOY_TOP_N:
            if cap_reasons is not None:
                cap_reasons["top_n_cap"] += max(0, len(ranked) - idx - 1)
            if cap_details is not None:
                cap_details["top_n"] += max(0, len(ranked) - idx - 1)
            break
    return selected


def run_once(log: logging.Logger) -> dict:
    blocklist = _load_blocklist()
    with _connect() as conn:
        nodes, reset_info = _validated_nodes(conn)
    candidates: list[tuple[float, dict]] = []
    gate_reasons: Counter[str] = Counter()
    record_reasons: Counter[str] = Counter()
    blocklist_reasons: Counter[str] = Counter()
    cap_reasons: Counter[str] = Counter()
    cap_details: Counter[str] = Counter()
    blocked = 0
    for row, metrics in nodes:
        gate_reason = _deploy_gate_block_reason(metrics, float(row["score"]))
        if gate_reason:
            gate_reasons[gate_reason] += 1
            continue
        rec, record_reason = _record_for_with_reason(row, metrics)
        if rec is None:
            record_reasons[record_reason or "record_unavailable"] += 1
            continue
        block_reason = _block_reason(blocklist, str(rec.get("id") or ""), str(row["id"] or ""))
        if block_reason:
            blocklist_reasons[_reason_bucket(block_reason)] += 1
            blocked += 1
            continue
        base_score = float(row["score"])
        candidates.append((_deploy_rank_score(base_score, rec), rec))
    # Dedupe estrutural por (engine, params, asset, risk) — preserva o de maior score.
    by_key: dict[str, tuple[float, dict]] = {}
    for score, rec in candidates:
        key = json.dumps(
            {"e": rec["engine"], "p": rec["params"], "a": rec["asset"], "r": rec["risk"]},
            sort_keys=True,
        )
        existing = by_key.get(key)
        if existing is None or score > existing[0]:
            by_key[key] = (score, rec)
    after_struct = len(by_key)
    # Dedupe funcional por resultado: clones com params triviais diferentes
    # mas (asset, timeframe, pnl, trades, mdd, hit) identicos colapsam.
    by_func: dict[tuple, tuple[float, dict]] = {}
    for score, rec in by_key.values():
        fp = _functional_fingerprint(rec)
        existing = by_func.get(fp)
        if existing is None or score > existing[0]:
            by_func[fp] = (score, rec)
    func_dropped = after_struct - len(by_func)
    ranked = sorted(by_func.values(), key=lambda x: -x[0])
    active_records = list_active()
    reserved_records = [
        rec for rec in active_records
        if RESPECT_UNMANAGED_CAPS and not _is_managed_record(rec)
    ]
    deduped = _select_deploy_records(
        ranked,
        reserved_records=reserved_records,
        cap_reasons=cap_reasons,
        cap_details=cap_details,
    )

    # Identifica o que precisa sair: ids do manifest atual nao mais elegiveis.
    current_records = {
        str(s.get("id")): s
        for s in active_records
        if _is_managed_record(s) and s.get("id")
    }
    current_ids = set(current_records)
    keep_ids = {rec["id"] for _score, rec in deduped}
    removed = 0
    for old_id in current_ids - keep_ids:
        if remove(old_id):
            removed += 1

    deployed = 0
    skipped_unchanged = 0
    for _score, rec in deduped:
        # O cap de familia ja foi aplicado em _select_deploy_records().
        # Reaplicar dentro de deploy_record podaria tambem estrategias de
        # outras fontes do manifest, como lotes manuais/HAPPYNATION.
        current = current_records.get(rec["id"])
        if current is not None and _record_manifest_view(current) == _record_manifest_view(rec):
            skipped_unchanged += 1
            continue
        deploy_record(rec, max_per_family=0)
        deployed += 1

    log.info(
        f"deploy: candidates={len(candidates)} struct_dedupe={after_struct} "
        f"func_dropped={func_dropped} after_dedupe={len(deduped)} "
        f"deployed={deployed} skipped_unchanged={skipped_unchanged} "
        f"removed_stale={removed} blocked={blocked}"
    )
    details = {
        "gate": _format_reason_counts(gate_reasons),
        "record": _format_reason_counts(record_reasons),
        "blocklist": _format_reason_counts(blocklist_reasons),
        "caps": _format_reason_counts(cap_reasons),
        "cap_detail": _format_reason_counts(cap_details),
    }
    detail_msg = " ".join(f"{name}={value}" for name, value in details.items() if value)
    if reset_info.get("reset_at"):
        reset_msg = (
            f"ranking_reset=campaign:{reset_info.get('campaign_id') or '-'},"
            f"reset_at:{reset_info.get('reset_at')},"
            f"hidden:{reset_info.get('hidden_ids')},"
            f"filtered_hidden:{reset_info.get('hidden_filtered')}"
        )
        detail_msg = f"{detail_msg} {reset_msg}".strip()
    if detail_msg:
        log.info("deploy detail: %s", detail_msg)
    return {
        "deployed": deployed,
        "removed": removed,
        "candidates": len(candidates),
        "blocked": blocked,
        "gate_reasons": dict(gate_reasons),
        "record_reasons": dict(record_reasons),
        "blocklist_reasons": dict(blocklist_reasons),
        "cap_reasons": dict(cap_reasons),
        "cap_details": dict(cap_details),
        "ranking_reset": reset_info,
    }


def main() -> int:
    log = _setup_logger()
    log.info(f"auto_deploy iniciado db={db_path()} poll={POLL_SECONDS}s")
    log.info(
        "config: min_score=%s min_pnl=%s min_trades=%s max_mdd=%s min_hit=%s hit_weight=%s max_est_tpd=%s top_n=%s assets=%s timeframes=%s max_family=%s max_asset=%s max_asset_family_tf=%s max_hyp_asset=%s managed_sources=%s respect_unmanaged_caps=%s",
        MIN_SCORE,
        MIN_PNL_PCT,
        MIN_TRADES,
        "off" if MAX_MDD_PCT <= 0 else MAX_MDD_PCT,
        "off" if MIN_HIT_RATE <= 0 else MIN_HIT_RATE,
        HIT_RATE_WEIGHT,
        "off" if MAX_EST_TRADES_PER_DAY <= 0 else MAX_EST_TRADES_PER_DAY,
        DEPLOY_TOP_N,
        ",".join(sorted(ALLOWED_ASSETS)) or "*",
        ",".join(sorted(ALLOWED_TIMEFRAMES)) or "*",
        MAX_PER_FAMILY,
        MAX_PER_ASSET,
        MAX_PER_ASSET_FAMILY_TF,
        MAX_PER_HYPOTHESIS_ASSET,
        ",".join(MANAGED_SOURCE_PATTERNS) or "(none)",
        RESPECT_UNMANAGED_CAPS,
    )
    log.info(
        "ranking_reset_filter: respect=%s campaign=%s",
        RESPECT_RANKING_RESET,
        AETHER_DEPLOY_CAMPAIGN_ID or "active",
    )
    log.info("blocklist=%s", BLOCKLIST_PATH)
    while True:
        sleep_for = POLL_SECONDS
        try:
            run_once(log)
        except sqlite3.OperationalError as exc:
            log.warning("sqlite busy/lock: %s", exc)
            sleep_for = BUSY_RETRY_SECONDS
        except Exception:
            log.exception("erro no ciclo de deploy")
        time.sleep(sleep_for)


if __name__ == "__main__":
    sys.exit(main())
