"""Sidecar validation_agent LLM: revisa qualitativamente as estrategias
auto-validadas, dando veredito (approve/reject) com base em sharpe, mdd,
hit_rate, trade_count, robustez (probes ok vs total) e perfil de risco.

Atualiza metrics_json com:
    llm_review = {
        decision: "approve" | "reject",
        confidence: 0..1,
        key_risks: [str],
        reasoning: str,
        provider, model, reviewed_at, reviewed_by
    }

Se decision=reject e confidence >= LLM_REJECT_CONFIDENCE, rebaixa:
    validation_passed -> False
    status            -> "rejected"

Roda em loop, com cap por ciclo para nao gastar tokens. Prioriza nodes
com validation_passed=True e sem llm_review ainda; ordena por score desc.

Subir com:
    .venv/Scripts/python.exe -u -m src.research.aether.llm_validator
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.research.aether.config import db_path
from src.research.aether.llm_runtime import call_llm_agent

LOG_PATH = Path(__file__).resolve().parents[3] / "src" / "logs" / "aether" / "llm_validator.log"

POLL_SECONDS = int(os.environ.get("AETHER_LLM_VALIDATOR_POLL_SECONDS", "900"))
MAX_PER_CYCLE = int(os.environ.get("AETHER_LLM_VALIDATOR_MAX_PER_CYCLE", "1"))
MAX_TOKENS = int(os.environ.get("AETHER_LLM_VALIDATOR_MAX_TOKENS", "550"))
LLM_REJECT_CONFIDENCE = 0.65  # acima disso, rebaixa o node


SYSTEM_PROMPT = """Voce eh o Validation Agent do Aether. Revise uma estrategia que ja passou gates quantitativos. Foque robustez, risco, overfitting e causalidade. Responda APENAS JSON:
{
  "decision": "approve" | "reject",
  "confidence": 0.0..1.0,
  "key_risks": ["risco 1", "risco 2"],
  "reasoning": "texto curto"
}"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _setup_logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("aether.llm_validator")
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
    conn = sqlite3.connect(str(db_path()), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _candidates(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT id, name, hypothesis, causal_mechanism, status, score, metrics_json, campaign_id
        FROM nodes
        WHERE status IN ('validated', 'expand', 'expanded')
          AND id != 'root'
          AND id NOT LIKE 'tf:%'
        ORDER BY score DESC, updated_at ASC
        LIMIT ?
        """,
        (limit * 5,),
    ).fetchall()
    out = []
    for row in rows:
        try:
            m = json.loads(row["metrics_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if m.get("validation_passed") is not True:
            continue
        if isinstance(m.get("llm_review"), dict):
            continue
        out.append((row, m))
        if len(out) >= limit:
            break
    return out


def _build_prompt(row: sqlite3.Row, metrics: dict) -> str:
    best = metrics.get("best_probe") or {}
    payload = {
        "name": row["name"],
        "hypothesis": (row["hypothesis"] or "")[:260],
        "causal_mechanism": (row["causal_mechanism"] or "")[:260],
        "score": float(row["score"]),
        "metrics": {
            "pnl_pct": metrics.get("pnl_pct"),
            "sharpe": metrics.get("sharpe"),
            "mdd_pct": metrics.get("mdd_pct"),
            "trade_count": metrics.get("trade_count"),
            "ok_count": metrics.get("ok_count"),
            "probe_count": metrics.get("probe_count"),
            "pass_count": metrics.get("pass_count"),
        },
        "best_probe": {
            "engine": best.get("engine"),
            "params": best.get("params"),
            "symbol": best.get("symbol"),
            "timeframe": best.get("timeframe"),
            "hit_rate": best.get("hit_rate"),
            "leverage": best.get("leverage"),
            "fee_bps": best.get("fee_bps"),
            "slippage_bps": best.get("slippage_bps"),
        },
    }
    return "Avalie e responda somente JSON:\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


_JSON_RE = re.compile(r"\{[\s\S]*\}")


def _parse_response(text: str) -> dict | None:
    if not text:
        return None
    match = _JSON_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    decision = str(data.get("decision") or "").lower()
    if decision not in ("approve", "reject"):
        return None
    try:
        confidence = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    key_risks = data.get("key_risks") or []
    if not isinstance(key_risks, list):
        key_risks = [str(key_risks)]
    return {
        "decision": decision,
        "confidence": confidence,
        "key_risks": [str(x)[:200] for x in key_risks][:6],
        "reasoning": str(data.get("reasoning") or "")[:600],
    }


def _apply_review(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    metrics: dict,
    review: dict,
    provider: str | None,
    model: str | None,
    log: logging.Logger,
) -> None:
    review_full = {
        **review,
        "provider": provider,
        "model": model,
        "reviewed_at": _now(),
        "reviewed_by": "validation_agent",
    }
    metrics["llm_review"] = review_full
    new_status = row["status"]
    if review["decision"] == "reject" and review["confidence"] >= LLM_REJECT_CONFIDENCE:
        metrics["validation_passed"] = False
        metrics["validation_stage"] = "llm_rejected"
        new_status = "rejected"
        log.info(
            f"REJECT id={row['id'][:60]} score={row['score']:.2f} "
            f"conf={review['confidence']:.2f} | {review['reasoning'][:120]}"
        )
    elif review["decision"] == "approve":
        metrics["validation_stage"] = "deep_llm"
        log.info(
            f"APPROVE id={row['id'][:60]} score={row['score']:.2f} "
            f"conf={review['confidence']:.2f} | {review['reasoning'][:120]}"
        )
    else:
        log.info(
            f"REJECT-LOW-CONF id={row['id'][:60]} score={row['score']:.2f} "
            f"conf={review['confidence']:.2f} | {review['reasoning'][:120]}"
        )
    conn.execute(
        "UPDATE nodes SET metrics_json=?, status=?, updated_at=? WHERE id=?",
        (json.dumps(metrics, ensure_ascii=False), new_status, _now(), row["id"]),
    )
    conn.execute(
        """
        INSERT INTO events (campaign_id, ts, type, node_id, agent_role, message, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["campaign_id"],
            _now(),
            "llm_review",
            row["id"],
            "validation_agent",
            f"{row['name']}: {review['decision']} (conf={review['confidence']:.2f})",
            json.dumps(review_full, ensure_ascii=False),
        ),
    )


def run_once(conn: sqlite3.Connection, log: logging.Logger) -> dict:
    pending = _candidates(conn, MAX_PER_CYCLE)
    if not pending:
        return {"reviewed": 0, "approved": 0, "rejected": 0, "errors": 0}
    approved = rejected = errors = 0
    for row, metrics in pending:
        prompt = _build_prompt(row, metrics)
        response = call_llm_agent(
            "validation_agent",
            system=SYSTEM_PROMPT,
            prompt=prompt,
            max_tokens=MAX_TOKENS,
            timeout=90.0,
        )
        if not response.get("ok"):
            errors += 1
            log.warning(
                f"LLM erro id={row['id'][:60]} provider={response.get('provider')} "
                f"err={str(response.get('error') or '')[:200]}"
            )
            continue
        review = _parse_response(str(response.get("text") or ""))
        if review is None:
            errors += 1
            log.warning(
                f"parse-fail id={row['id'][:60]} text_preview={(response.get('text') or '')[:200]!r}"
            )
            continue
        _apply_review(
            conn, row, metrics, review,
            provider=response.get("provider"),
            model=response.get("model"),
            log=log,
        )
        if review["decision"] == "approve":
            approved += 1
        else:
            rejected += 1
    return {"reviewed": len(pending), "approved": approved, "rejected": rejected, "errors": errors}


def main() -> int:
    log = _setup_logger()
    log.info(f"llm_validator iniciado db={db_path()} poll={POLL_SECONDS}s max_per_cycle={MAX_PER_CYCLE}")
    while True:
        try:
            with _connect() as conn:
                stats = run_once(conn, log)
            if stats["reviewed"]:
                log.info(
                    f"ciclo: reviewed={stats['reviewed']} approved={stats['approved']} "
                    f"rejected={stats['rejected']} errors={stats['errors']}"
                )
        except Exception:
            log.exception("erro no ciclo do llm_validator")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
