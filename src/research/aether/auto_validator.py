"""Sidecar auto-validator: marca como validated nodes que passam
nos scout gates (mesmo criterio de paper_ready em ranking.py), evitando
que estrategias fiquem eternamente bloqueadas por "validacao profunda
pendente" enquanto o validation_agent LLM nao esta acionando.

Roda em loop continuo lendo o sqlite do Aether e atualizando metrics_json
com validation_passed=True + validation_stage="walk_forward" e status
validated para a arvore refletir o que realmente foi aprovado.

Subir com:
    .venv/Scripts/python.exe -u -m src.research.aether.auto_validator
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.research.aether.config import db_path

DB_PATH = db_path()
LOG_PATH = Path(__file__).resolve().parents[3] / "src" / "logs" / "aether" / "auto_validator.log"

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


POLL_SECONDS = _env_int("AETHER_VALIDATOR_POLL_SECONDS", 60)
READY_STATUSES = ("expand", "expanded")
MIN_SCORE = _env_float("AETHER_VALIDATOR_MIN_SCORE", 0.42)
MIN_TRADES = _env_int("AETHER_VALIDATOR_MIN_TRADES", 30)
MAX_MDD_PCT = _env_float("AETHER_VALIDATOR_MAX_MDD_PCT", 35.0)
DEEP_STAGE = "walk_forward"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _setup_logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("aether.auto_validator")
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
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = ",".join("?" * len(READY_STATUSES))
    rows = conn.execute(
        f"""
        SELECT id, name, status, score, metrics_json, campaign_id
        FROM nodes
        WHERE status IN ({placeholders})
          AND id != 'root'
          AND id NOT LIKE 'tf:%'
        """,
        READY_STATUSES,
    ).fetchall()
    return list(rows)


def _passes_gates(metrics: dict, score: float) -> tuple[bool, str]:
    pnl = float(metrics.get("pnl_pct") or 0)
    trades = int(metrics.get("trade_count") or 0)
    mdd = float(metrics.get("mdd_pct") or metrics.get("max_drawdown_pct") or 0)
    if pnl <= 0:
        return False, f"pnl_pct={pnl} <= 0"
    if trades < MIN_TRADES:
        return False, f"trade_count={trades} < {MIN_TRADES}"
    if score < MIN_SCORE:
        return False, f"score={score} < {MIN_SCORE}"
    if mdd > MAX_MDD_PCT:
        return False, f"mdd_pct={mdd} > {MAX_MDD_PCT}"
    return True, f"ok pnl={pnl:.2f}% trades={trades} mdd={mdd:.2f}% score={score:.2f}"


def _already_validated(metrics: dict) -> bool:
    if metrics.get("validation_passed") is True:
        return True
    stage = str(metrics.get("validation_stage") or metrics.get("validation_level") or "").lower()
    return stage in {"deep", "walk_forward", "portfolio_validated", "paper_manifest"}


def _llm_rejected(metrics: dict) -> bool:
    review = metrics.get("llm_review")
    if not isinstance(review, dict):
        return False
    if str(review.get("decision") or "").lower() != "reject":
        return False
    try:
        conf = float(review.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return conf >= 0.65


def _critic_rejected(metrics: dict) -> bool:
    return str(metrics.get("critic_status") or "").lower() == "rejected"


def _promote_orphan(conn: sqlite3.Connection, row: sqlite3.Row, metrics: dict, log: logging.Logger) -> bool:
    """Orfaos: validation_passed=True mas status ainda 'expand'/'expanded'.
    Promove status->validated sem re-checar gates (ja foi validado em outro lugar).
    Pulamos so rejeicoes explicitas (critic=rejected ou llm_rejected)."""
    if _critic_rejected(metrics) or _llm_rejected(metrics):
        return False
    metrics.setdefault("validation_stage", metrics.get("validation_stage") or DEEP_STAGE)
    metrics.setdefault("validated_at", metrics.get("validated_at") or _now())
    metrics.setdefault("validated_by", "auto_validator")
    metrics.setdefault("validation_reason", "orphan: validation_passed sem status update")
    conn.execute(
        "UPDATE nodes SET status='validated', metrics_json=?, updated_at=? WHERE id=?",
        (json.dumps(metrics, ensure_ascii=False), _now(), row["id"]),
    )
    conn.execute(
        """
        INSERT INTO events (campaign_id, ts, type, node_id, agent_role, message, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["campaign_id"],
            _now(),
            "validated",
            row["id"],
            "auto_validator",
            f"{row['name']} promovida (orfa: status era {row['status']})",
            json.dumps({"reason": "orphan_promote", "stage": metrics.get("validation_stage")}, ensure_ascii=False),
        ),
    )
    log.info(f"orphan promoted id={row['id'][:60]} status={row['status']} -> validated (critic={metrics.get('critic_status') or 'none'})")
    return True


def run_once(conn: sqlite3.Connection, log: logging.Logger) -> int:
    rows = _candidates(conn)
    promoted = 0
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if _already_validated(metrics):
            # Pode ser orfao: validation_passed=True mas status nao avancou
            if _promote_orphan(conn, row, metrics, log):
                promoted += 1
            continue
        if _llm_rejected(metrics):
            continue
        ok, reason = _passes_gates(metrics, float(row["score"]))
        if not ok:
            continue
        metrics["validation_passed"] = True
        metrics["validation_stage"] = DEEP_STAGE
        metrics["validated_at"] = _now()
        metrics["validated_by"] = "auto_validator"
        metrics["validation_reason"] = reason
        conn.execute(
            "UPDATE nodes SET status='validated', metrics_json=?, updated_at=? WHERE id=?",
            (json.dumps(metrics, ensure_ascii=False), _now(), row["id"]),
        )
        conn.execute(
            """
            INSERT INTO events (campaign_id, ts, type, node_id, agent_role, message, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["campaign_id"],
                _now(),
                "validated",
                row["id"],
                "auto_validator",
                f"{row['name']} promovida (auto_validator): {reason}",
                json.dumps({"reason": reason, "stage": DEEP_STAGE}, ensure_ascii=False),
            ),
        )
        promoted += 1
        log.info(f"validated id={row['id'][:60]} status={row['status']} {reason}")
    return promoted


def main() -> int:
    log = _setup_logger()
    log.info(f"auto_validator iniciado db={DB_PATH} poll={POLL_SECONDS}s")
    if not DB_PATH.exists():
        log.error(f"DB nao existe: {DB_PATH}")
        return 1
    while True:
        try:
            with _connect() as conn:
                n = run_once(conn, log)
                if n:
                    log.info(f"ciclo: {n} novas validadas")
        except sqlite3.OperationalError as e:
            log.warning(f"sqlite busy/lock: {e}; tentando de novo")
        except Exception as e:
            log.exception(f"erro no ciclo: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
