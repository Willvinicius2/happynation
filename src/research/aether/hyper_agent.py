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

from src.research.aether import tree_focus
from src.research.aether.campaigns import get_active_campaign_id
from src.research.aether.config import TIMEFRAMES, db_path, sqlite_busy_retry_seconds, sqlite_busy_timeout_ms
from src.research.aether.db import get_setting
from src.research.aether.repository import set_agent_status

DB_PATH = db_path()
LOG_PATH = Path(__file__).resolve().parents[3] / "src" / "logs" / "aether" / "hyper_agent.log"

POLL_SECONDS = 120
BUSY_RETRY_SECONDS = sqlite_busy_retry_seconds()
BUSY_RETRY_MAX_SECONDS = max(BUSY_RETRY_SECONDS, min(float(POLL_SECONDS), BUSY_RETRY_SECONDS * 6))
SETTING_KEY = "hyper_controls"
DEFAULT_EVERY = 4
EVERY_MIN = 2
EVERY_MAX = 12
# Mudancas bruscas no `every` causam comportamento on/off no orchestrator.
# Limita a delta por ciclo para evitar oscilacao agressiva.
MAX_DELTA_PER_CYCLE = 2
HEARTBEAT_EVENT_MINUTES = max(1, int(os.getenv("AETHER_HYPER_HEARTBEAT_EVENT_MINUTES", "10")))
FOCUS_BUDGET_MIN = max(6, int(os.getenv("AETHER_HYPER_FOCUS_BUDGET_MIN", str(max(12, len(TIMEFRAMES) * 2)))))
FOCUS_BUDGET_NORMAL = max(FOCUS_BUDGET_MIN, int(os.getenv("AETHER_HYPER_FOCUS_BUDGET_NORMAL", str(max(18, len(TIMEFRAMES) * 3)))))
FOCUS_BUDGET_STUCK = max(FOCUS_BUDGET_NORMAL, int(os.getenv("AETHER_HYPER_FOCUS_BUDGET_STUCK", str(max(24, len(TIMEFRAMES) * 4)))))
FOCUS_BUDGET_MAX = max(FOCUS_BUDGET_STUCK, int(os.getenv("AETHER_HYPER_FOCUS_BUDGET_MAX", str(max(72, len(TIMEFRAMES) * 12)))))

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
    try:
        busy_timeout = max(1000, int(os.getenv("AETHER_HYPER_SQLITE_BUSY_TIMEOUT_MS", "10000")))
    except (TypeError, ValueError):
        busy_timeout = 10000
    busy_timeout = min(sqlite_busy_timeout_ms(), busy_timeout)
    conn = sqlite3.connect(str(DB_PATH), timeout=busy_timeout / 1000.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
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


def _busy_retry_sleep_seconds(consecutive_busy: int) -> float:
    consecutive = max(1, int(consecutive_busy or 1))
    return min(BUSY_RETRY_MAX_SECONDS, BUSY_RETRY_SECONDS * (2 ** min(consecutive - 1, 4)))


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}


def _ranking_reset_after(conn: sqlite3.Connection, campaign_id: str) -> str | None:
    if not _env_truthy("AETHER_HYPER_RESPECT_RANKING_RESET", True):
        return None
    try:
        state = get_setting(conn, f"ranking_hidden_node_ids:{campaign_id}", default={})
    except Exception:
        return None
    if isinstance(state, dict) and state.get("reset_at"):
        return str(state["reset_at"])
    return None


def assess(conn: sqlite3.Connection) -> dict[str, Any]:
    campaign_id = get_active_campaign_id(conn)
    reset_after = _ranking_reset_after(conn, campaign_id)
    reset_clause = "AND created_at > ?" if reset_after else ""
    params: list[Any] = [campaign_id]
    if reset_after:
        params.append(reset_after)
    rows = conn.execute(
        f"""
        SELECT id, status, score, metrics_json, tags_json, updated_at, created_at
        FROM nodes
        WHERE campaign_id=?
          AND id != 'root'
          AND id NOT LIKE 'tf:%'
          {reset_clause}
        """,
        params,
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
    focus_state: dict[str, Any] = {}
    running_tasks = 0
    queued_tasks = 0
    try:
        focus_state = tree_focus.assess(conn, campaign_id)
        task_rows = conn.execute(
            """
            SELECT status, COUNT(*) AS n
            FROM tasks
            WHERE campaign_id=? AND status IN ('queued', 'running')
            GROUP BY status
            """,
            (campaign_id,),
        ).fetchall()
        for task_row in task_rows:
            if task_row["status"] == "running":
                running_tasks = int(task_row["n"] or 0)
            elif task_row["status"] == "queued":
                queued_tasks = int(task_row["n"] or 0)
    except Exception:
        focus_state = {}

    return {
        "verdict": verdict,
        "diversity": round(diversity, 3),
        "dominance_pct": round(dominance_pct, 2),
        "best_age_min": round(best_age_min, 2) if best_age_min is not None else None,
        "critic_reject_rate": round(critic_reject_rate, 3),
        "active_count": total,
        "family_count": len(family_active),
        "focus_open_count": int(focus_state.get("open_count") or 0),
        "focus_actionable_total": int(focus_state.get("actionable_total") or 0),
        "focus_completion_ratio": float(focus_state.get("completion_ratio") or 0.0),
        "running_tasks": running_tasks,
        "queued_tasks": queued_tasks,
        "ranking_reset_after": reset_after,
    }


def _focus_budget_for(assessment: dict[str, Any]) -> int | None:
    actionable = int(assessment.get("focus_actionable_total") or 0)
    if actionable < FOCUS_BUDGET_MIN:
        return None
    completion = float(assessment.get("focus_completion_ratio") or 0.0)
    reject_rate = float(assessment.get("critic_reject_rate") or 0.0)
    running_tasks = int(assessment.get("running_tasks") or 0)
    if completion < 0.55 or reject_rate >= 0.60:
        return FOCUS_BUDGET_MIN
    if assessment.get("verdict") == "stuck" and completion >= 0.70 and reject_rate <= 0.25:
        productive_budget = max(FOCUS_BUDGET_STUCK, running_tasks * 2)
        return min(FOCUS_BUDGET_MAX, productive_budget)
    if assessment.get("verdict") == "stuck" and completion >= 0.80:
        return min(FOCUS_BUDGET_MAX, FOCUS_BUDGET_STUCK)
    if assessment.get("verdict") in {"converging", "balanced"} and completion >= 0.70:
        return min(FOCUS_BUDGET_MAX, FOCUS_BUDGET_NORMAL)
    return FOCUS_BUDGET_MIN


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
    controls = {
        "force_exploration_every": int(every),
        "verdict": assessment["verdict"],
        "diversity": assessment["diversity"],
        "dominance_pct": assessment["dominance_pct"],
        "best_age_min": assessment["best_age_min"],
        "critic_reject_rate": assessment["critic_reject_rate"],
        "focus_open_count": assessment.get("focus_open_count"),
        "focus_completion_ratio": assessment.get("focus_completion_ratio"),
        "running_tasks": assessment.get("running_tasks"),
        "queued_tasks": assessment.get("queued_tasks"),
        "ranking_reset_after": assessment.get("ranking_reset_after"),
        "updated_at": _now(),
    }
    focus_budget = _focus_budget_for(assessment)
    if focus_budget is not None:
        controls["focus_open_budget"] = int(focus_budget)
    return controls


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
        "focus_open_budget",
        "focus_open_count",
        "focus_completion_ratio",
        "running_tasks",
        "queued_tasks",
        "ranking_reset_after",
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
                f"age={controls['best_age_min']}min reject={controls['critic_reject_rate']} "
                f"focus={controls.get('focus_open_count')}/{controls.get('focus_open_budget')}."
            ),
            json.dumps(controls, ensure_ascii=False),
        ),
    )


def run_once(conn: sqlite3.Connection, log: logging.Logger) -> dict[str, Any]:
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
    write_controls(conn, controls)
    if event_type:
        event_controls = dict(controls)
        event_controls["last_event_at"] = event_controls["last_seen_at"]
        try:
            _emit_event(conn, event_type, event_controls)
        except sqlite3.OperationalError as exc:
            log.warning(f"event skipped: {exc}")
        else:
            controls = event_controls
            write_controls(conn, controls)
    try:
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
                "focus_open_budget": controls.get("focus_open_budget"),
                "focus_open_count": controls.get("focus_open_count"),
                "focus_completion_ratio": controls.get("focus_completion_ratio"),
                "running_tasks": controls.get("running_tasks"),
                "queued_tasks": controls.get("queued_tasks"),
                "ranking_reset_after": controls.get("ranking_reset_after"),
                "bubble": "Meta-controle ativo.",
            },
        )
    except sqlite3.OperationalError as exc:
        log.warning(f"agent_status skipped: {exc}")
    if event_type:
        log.info(
            f"{event_type}: every={controls['force_exploration_every']} verdict={controls['verdict']} "
            f"div={controls['diversity']} age={controls['best_age_min']}min reject={controls['critic_reject_rate']} "
            f"focus={controls.get('focus_open_count')}/{controls.get('focus_open_budget')}"
        )
    return controls


def main() -> int:
    log = _setup_logger()
    log.info(f"hyper_agent iniciado db={DB_PATH} poll={POLL_SECONDS}s")
    if not DB_PATH.exists():
        log.error(f"DB nao existe: {DB_PATH}")
        return 1
    consecutive_busy = 0
    while True:
        sleep_for = POLL_SECONDS
        try:
            with _connect() as conn:
                run_once(conn, log)
            consecutive_busy = 0
        except sqlite3.OperationalError as e:
            consecutive_busy += 1
            sleep_for = _busy_retry_sleep_seconds(consecutive_busy)
            log.warning(f"sqlite busy/lock #{consecutive_busy}: {e}; retry={sleep_for:.1f}s")
        except Exception as e:
            consecutive_busy = 0
            log.exception(f"erro no ciclo: {e}")
        time.sleep(sleep_for)


if __name__ == "__main__":
    sys.exit(main())
