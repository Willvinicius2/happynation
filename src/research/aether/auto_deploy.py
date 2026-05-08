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
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.research.aether.config import db_path
from src.research.active import deploy_record, list_active, remove

LOG_PATH = Path(__file__).resolve().parents[3] / "src" / "logs" / "aether" / "auto_deploy.log"

POLL_SECONDS = int(os.environ.get("AETHER_DEPLOY_POLL_SECONDS", "60"))


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


# Gates configuraveis via env. Defaults alinhados com o ranking (paper_ready),
# nao com filtros conservadores extras. O usuario que estiver mais conservador
# sobe os limites via env (AETHER_DEPLOY_MIN_PNL_PCT etc).
MIN_SCORE = _env_float("AETHER_DEPLOY_MIN_SCORE", 0.42)
MIN_TRADES = _env_int("AETHER_DEPLOY_MIN_TRADES", 12)
MAX_MDD_PCT = _env_float("AETHER_DEPLOY_MAX_MDD_PCT", 35.0)
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
DEPLOY_TOP_N = _env_int("AETHER_DEPLOY_TOP_N", 80)

# Default risk se nao houver no node.
DEFAULT_RISK = {
    "take_profit_pct": 3.0,
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
    conn = sqlite3.connect(str(db_path()), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _validated_nodes(conn: sqlite3.Connection) -> list[tuple[sqlite3.Row, dict]]:
    rows = conn.execute(
        """
        SELECT id, name, score, metrics_json
        FROM nodes
        WHERE status='validated'
          AND id != 'root'
          AND id NOT LIKE 'tf:%'
        """
    ).fetchall()
    out: list[tuple[sqlite3.Row, dict]] = []
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if metrics.get("validation_passed") is not True:
            continue
        out.append((row, metrics))
    return out


def _passes_deploy_gates(metrics: dict, score: float) -> bool:
    if float(metrics.get("pnl_pct") or 0) < MIN_PNL_PCT:
        return False
    if int(metrics.get("trade_count") or 0) < MIN_TRADES:
        return False
    if score < MIN_SCORE:
        return False
    if float(metrics.get("mdd_pct") or 0) > MAX_MDD_PCT:
        return False
    # Critic gate configuravel: 'approved' exige aprovacao explicita;
    # 'any' aceita pending/sem critic mas ainda bloqueia rejected.
    critic_status = metrics.get("critic_status")
    if critic_status == "rejected":
        return False
    if CRITIC_GATE == "approved" and critic_status != "approved":
        return False
    return True


def _record_for(row: sqlite3.Row, metrics: dict) -> dict | None:
    best = metrics.get("best_probe") or {}
    engine = best.get("engine")
    if not engine:
        return None
    params = best.get("params")
    if not isinstance(params, dict) or not params:
        return None
    probe_specs = metrics.get("probe_specs") if isinstance(metrics.get("probe_specs"), list) else []
    clean_specs = [
        {"engine": str(spec.get("engine")), "params": dict(spec.get("params") or {})}
        for spec in probe_specs
        if isinstance(spec, dict) and spec.get("engine")
    ]
    if len(clean_specs) > 1:
        if REQUIRE_COMBO_FOR_MULTI and str(engine) != "combo":
            return None
        if str(engine) == "combo":
            params = {**params, "mode": params.get("mode") or "consensus", "specs": params.get("specs") or clean_specs}
    symbol = (best.get("symbol") or "").upper()
    if not symbol:
        return None
    if not _asset_allowed(symbol):
        return None
    score = float(row["score"])
    timeframe = str(best.get("timeframe") or "")
    if not _timeframe_allowed(timeframe):
        return None
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
            "pnl_pct": metrics.get("pnl_pct"),
            "sharpe": metrics.get("sharpe"),
            "mdd_pct": metrics.get("mdd_pct"),
            "trade_count": metrics.get("trade_count"),
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
    }


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


def _functional_fingerprint(rec: dict) -> tuple:
    """Identidade pelo *resultado* do backtest, nao pelos params.

    Dois records com params diferentes mas que produziram exatamente o mesmo
    pnl/trades/mdd/hit em (asset, timeframe) sao clones funcionais — params
    triviais que nao alteram o sinal. O dedupe por params byte-a-byte deixa
    passar; este funcional pega.
    """
    summary = rec.get("summary") or {}
    return (
        rec.get("asset"),
        rec.get("timeframe"),
        round(float(summary.get("pnl_pct") or 0.0), 2),
        int(summary.get("trade_count") or 0),
        round(float(summary.get("mdd_pct") or 0.0), 2),
        round(float(summary.get("hit_rate") or 0.0), 4),
    )


def _select_deploy_records(ranked: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
    selected: list[tuple[float, dict]] = []
    family_counts: dict[str, int] = {}
    asset_counts: dict[str, int] = {}
    asset_family_tf_counts: dict[str, int] = {}
    for score, rec in ranked:
        family = _family_for(rec)
        asset = _asset_for(rec)
        asset_family_tf = _asset_family_tf_key(rec)
        if MAX_PER_FAMILY > 0 and family_counts.get(family, 0) >= MAX_PER_FAMILY:
            continue
        if MAX_PER_ASSET > 0 and asset_counts.get(asset, 0) >= MAX_PER_ASSET:
            continue
        if MAX_PER_ASSET_FAMILY_TF > 0 and asset_family_tf_counts.get(asset_family_tf, 0) >= MAX_PER_ASSET_FAMILY_TF:
            continue
        selected.append((score, rec))
        family_counts[family] = family_counts.get(family, 0) + 1
        asset_counts[asset] = asset_counts.get(asset, 0) + 1
        asset_family_tf_counts[asset_family_tf] = asset_family_tf_counts.get(asset_family_tf, 0) + 1
        if len(selected) >= DEPLOY_TOP_N:
            break
    return selected


def run_once(log: logging.Logger) -> dict:
    with _connect() as conn:
        nodes = _validated_nodes(conn)
    candidates: list[tuple[float, dict]] = []
    for row, metrics in nodes:
        if not _passes_deploy_gates(metrics, float(row["score"])):
            continue
        rec = _record_for(row, metrics)
        if rec is None:
            continue
        candidates.append((float(row["score"]), rec))
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
    deduped = _select_deploy_records(ranked)

    # Identifica o que precisa sair: ids do manifest atual nao mais elegiveis.
    current_ids = {s.get("id") for s in list_active() if s.get("source") == "aether_auto_deploy"}
    keep_ids = {rec["id"] for _score, rec in deduped}
    removed = 0
    for old_id in current_ids - keep_ids:
        if remove(old_id):
            removed += 1

    deployed = 0
    for _score, rec in deduped:
        deploy_record(rec, max_per_family=MAX_PER_FAMILY)
        deployed += 1

    log.info(
        f"deploy: candidates={len(candidates)} struct_dedupe={after_struct} "
        f"func_dropped={func_dropped} after_dedupe={len(deduped)} "
        f"deployed={deployed} removed_stale={removed}"
    )
    return {"deployed": deployed, "removed": removed, "candidates": len(candidates)}


def main() -> int:
    log = _setup_logger()
    log.info(f"auto_deploy iniciado db={db_path()} poll={POLL_SECONDS}s")
    log.info(
        "config: min_trades=%s top_n=%s assets=%s timeframes=%s max_family=%s max_asset=%s max_asset_family_tf=%s",
        MIN_TRADES,
        DEPLOY_TOP_N,
        ",".join(sorted(ALLOWED_ASSETS)) or "*",
        ",".join(sorted(ALLOWED_TIMEFRAMES)) or "*",
        MAX_PER_FAMILY,
        MAX_PER_ASSET,
        MAX_PER_ASSET_FAMILY_TF,
    )
    while True:
        try:
            run_once(log)
        except Exception:
            log.exception("erro no ciclo de deploy")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
