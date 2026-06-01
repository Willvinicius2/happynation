"""Hyper Agent: meta-controlador que ajusta automaticamente os
parametros de exploracao do Aether com base na saude da arvore.

Le periodicamente:
  - distribuicao de familias (engines) ativas -> entropia Shannon
  - idade do recorde global (validated com maior score)
  - taxa de aprovacao do Critic

Decide e escreve em settings (chave 'hyper_controls') um dict que o
orchestrator le no inicio de cada ciclo:
  {
    "force_exploration_every": int,   # com qual cadencia forcar
    "verdict":   "stuck"|"converging"|"balanced"|"exploring",
    "diversity": float,
    "best_age_min": float,
    "critic_reject_rate": float,
    "updated_at": iso8601,
  }

Regras (curtas):
  - veredito 'stuck'      -> every=2  (forcar quase sempre)
  - veredito 'converging' -> every=3
  - veredito 'balanced'   -> every=4  (default)
  - veredito 'exploring'  -> every=6  (relaxar)

Subir com:
    .venv/Scripts/python.exe -u -m src.research.aether.hyper_agent
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.research.aether.config import db_path, sqlite_busy_timeout_ms
from src.research.aether.repository import ensure_agents, set_agent_status

DB_PATH = db_path()
LOG_PATH = Path(__file__).resolve().parents[3] / "src" / "logs" / "aether" / "hyper_agent.log"

POLL_SECONDS = 120
SETTING_KEY = "hyper_controls"
DEFAULT_EVERY = 4
EVERY_MIN = 2
EVERY_MAX = 12
# Mudancas bruscas no `every` causam comportamento on/off no orchestrator.
# Limita a delta por ciclo para evitar oscilacao agressiva.
MAX_DELTA_PER_CYCLE = 2
HEARTBEAT_EVENT_MINUTES = max(1, int(os.getenv("AETHER_HYPER_HEARTBEAT_EVENT_MINUTES", "10")))

VERDICT_TO_EVERY = {
    "stuck": 2,
    "converging": 3,
    "balanced": 4,
    "exploring": 6,
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _setup_logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("aether.hyper_agent")
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
    conn = sqlite3.connect(str(DB_PATH), timeout=busy_timeout / 1000.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout}")
    return conn


def _engine_of(metrics: dict, tags: list) -> str:
    best = metrics.get("best_probe") or {}
    if isinstance(best, dict) and best.get("engine"):
        return str(best["engine"]).lower()
    specs = metrics.get("probe_specs") or []
    if isinstance(specs, list) and specs and isinstance(specs[0], dict):
        eng = specs[0].get("engine")
        if eng:
            return str(eng).lower()
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("engine:"):
            return tag.split(":", 1)[1].lower()
    return "unknown"


def _shannon(counts: list[int]) -> float:
    total = sum(counts)
    if total <= 0 or len(counts) <= 1:
        return 0.0
    h = 0.0
    for c in counts:
        if c <= 0:
            continue
        p = c / total
        h -= p * math.log2(p)
    return h / math.log2(len(counts))


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _minutes_since(value: str | None) -> float | None:
    ts = _parse_ts(value)
    if ts is None:
        return None
    return (datetime.now(timezone.utc).timestamp() - ts) / 60.0


def assess(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id, status, score, metrics_json, tags_json, updated_at, created_at
        FROM nodes
        WHERE id != 'root' AND id NOT LIKE 'tf:%'
        """
    ).fetchall()
    family_active: dict[str, int] = {}
    best_score = -math.inf
    best_ts: float | None = None
    critic_approved = 0
    critic_rejected = 0
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
            tags = json.loads(row["tags_json"] or "[]")
        except Exception:
            metrics, tags = {}, []
        eng = _engine_of(metrics, tags)
        if row["status"] not in ("pruned", "paused", "archive"):
            family_active[eng] = family_active.get(eng, 0) + 1
        if row["status"] == "validated":
            sc = float(row["score"] or 0.0)
            if sc > best_score:
                best_score = sc
                best_ts = _parse_ts(row["updated_at"]) or _parse_ts(row["created_at"])
        cs = metrics.get("critic_status")
        if cs == "approved":
            critic_approved += 1
        elif cs == "rejected":
            critic_rejected += 1

    counts = sorted(family_active.values(), reverse=True)
    diversity = _shannon(counts)
    total = sum(counts) or 1
    dominance_pct = (counts[0] / total * 100.0) if counts else 0.0
    now_ts = datetime.now(timezone.utc).timestamp()
    best_age_min = (now_ts - best_ts) / 60.0 if best_ts else None

    if not counts:
        verdict = "balanced"
    elif dominance_pct >= 65 and diversity < 0.55:
        verdict = "stuck" if (best_age_min is not None and best_age_min > 30) else "converging"
    elif diversity >= 0.75 and best_age_min is not None and best_age_min < 20:
        verdict = "exploring"
    elif best_age_min is not None and best_age_min > 45:
        verdict = "stuck"
    else:
        verdict = "balanced"

    critic_total = critic_approved + critic_rejected
    critic_reject_rate = (critic_rejected / critic_total) if critic_total else 0.0

    return {
        "verdict": verdict,
        "diversity": round(diversity, 3),
        "dominance_pct": round(dominance_pct, 2),
        "best_age_min": round(best_age_min, 2) if best_age_min is not None else None,
        "critic_reject_rate": round(critic_reject_rate, 3),
        "active_count": total,
        "family_count": len(family_active),
    }


def decide(assessment: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    every = VERDICT_TO_EVERY.get(assessment["verdict"], DEFAULT_EVERY)
    # Se Critic esta rejeitando muito, abrandar a forca para nao gerar lixo.
    if assessment["critic_reject_rate"] >= 0.6 and assessment["verdict"] != "stuck":
        every = max(every, 5)
    # Se tudo zerado (poucos nos), nao mexer no default.
    if (assessment.get("active_count") or 0) < 6:
        every = DEFAULT_EVERY
    # Hard clamp.
    every = max(EVERY_MIN, min(EVERY_MAX, int(every)))
    # Suaviza transicao para evitar swing on/off entre stuck(2) e exploring(6).
    if previous and isinstance(previous.get("force_exploration_every"), int):
        prev_every = previous["force_exploration_every"]
        delta = every - prev_every
        if abs(delta) > MAX_DELTA_PER_CYCLE:
            every = prev_every + (MAX_DELTA_PER_CYCLE if delta > 0 else -MAX_DELTA_PER_CYCLE)
    return {
        "force_exploration_every": int(every),
        "verdict": assessment["verdict"],
        "diversity": assessment["diversity"],
        "dominance_pct": assessment["dominance_pct"],
        "best_age_min": assessment["best_age_min"],
        "critic_reject_rate": assessment["critic_reject_rate"],
        "updated_at": _now(),
    }


def write_controls(conn: sqlite3.Connection, controls: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO settings(key, value_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
        """,
        (SETTING_KEY, json.dumps(controls, ensure_ascii=False), _now()),
    )


def read_controls(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT value_json FROM settings WHERE key=?", (SETTING_KEY,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value_json"])
    except Exception:
        return None


def _merge_controls(previous: dict[str, Any] | None, controls: dict[str, Any]) -> dict[str, Any]:
    """Preserva knobs manuais que tambem vivem em hyper_controls."""
    merged = dict(previous or {})
    merged.update(controls)
    return merged


def _changed(previous: dict[str, Any] | None, controls: dict[str, Any]) -> bool:
    if not previous:
        return True
    keys = (
        "force_exploration_every",
        "verdict",
        "diversity",
        "dominance_pct",
        "best_age_min",
        "critic_reject_rate",
    )
    return any(previous.get(key) != controls.get(key) for key in keys)


def _emit_event(conn: sqlite3.Connection, event_type: str, controls: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO events (campaign_id, ts, type, node_id, agent_role, message, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            None,
            _now(),
            event_type,
            None,
            "hyper_agent",
            (
                f"Hyper avaliou exploracao: every={controls['force_exploration_every']} "
                f"verdict={controls['verdict']} div={controls['diversity']} "
                f"age={controls['best_age_min']}min reject={controls['critic_reject_rate']}."
            ),
            json.dumps(controls, ensure_ascii=False),
        ),
    )


def run_once(conn: sqlite3.Connection, log: logging.Logger) -> dict[str, Any]:
    ensure_agents(conn)
    assessment = assess(conn)
    previous = read_controls(conn)
    decided = decide(assessment, previous)
    changed = _changed(previous, decided)
    heartbeat_age = _minutes_since((previous or {}).get("last_event_at"))
    heartbeat_due = heartbeat_age is None or heartbeat_age >= HEARTBEAT_EVENT_MINUTES
    controls = _merge_controls(previous, decided)
    controls["last_seen_at"] = _now()
    event_type = None
    if changed:
        event_type = "hyper_controls_updated"
    elif heartbeat_due:
        event_type = "hyper_controls_heartbeat"
    if event_type:
        controls["last_event_at"] = controls["last_seen_at"]
        _emit_event(conn, event_type, controls)
    write_controls(conn, controls)
    set_agent_status(
        conn,
        "hyper_agent",
        "standby",
        stats={
            "sidecar_alive": True,
            "last_seen_at": controls["last_seen_at"],
            "force_exploration_every": controls["force_exploration_every"],
            "verdict": controls["verdict"],
            "diversity": controls["diversity"],
            "dominance_pct": controls["dominance_pct"],
            "best_age_min": controls["best_age_min"],
            "critic_reject_rate": controls["critic_reject_rate"],
            "bubble": "Meta-controle ativo.",
        },
    )
    if event_type:
        log.info(
            f"{event_type}: every={controls['force_exploration_every']} verdict={controls['verdict']} "
            f"div={controls['diversity']} age={controls['best_age_min']}min reject={controls['critic_reject_rate']}"
        )
    return controls


def main() -> int:
    log = _setup_logger()
    log.info(f"hyper_agent iniciado db={DB_PATH} poll={POLL_SECONDS}s")
    if not DB_PATH.exists():
        log.error(f"DB nao existe: {DB_PATH}")
        return 1
    while True:
        try:
            with _connect() as conn:
                run_once(conn, log)
        except sqlite3.OperationalError as e:
            log.warning(f"sqlite busy/lock: {e}")
        except Exception as e:
            log.exception(f"erro no ciclo: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
