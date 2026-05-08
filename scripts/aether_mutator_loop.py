"""Mutator deterministico: gera variantes de hipoteses validadas/aprovadas
sem chamar LLM. Mantem a esteira CPU/GPU ocupada com backtests determinis-
ticos quando o LLM esta em cooldown.

Estrategia:
  - A cada POLL segundos, escolhe N hyps com critic=approved ou status=validated
  - Para cada uma, gera M variantes perturbando params numericos +/- pct
  - Insere como nodes novos (status='candidate'), prefixo 'mut:' no id
  - Orchestrator pega no proximo ciclo e roda backtests

Run:
  .venv\\Scripts\\python.exe -u scripts\\aether_mutator_loop.py

Env:
  AETHER_MUTATOR_POLL_SECONDS=30
  AETHER_MUTATOR_PER_CYCLE=4         (variantes por ciclo)
  AETHER_MUTATOR_PERTURB_PCT=0.15
  AETHER_MUTATOR_MAX_PARENT_AGE_HOURS=48
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.aether.config import db_path  # noqa: E402
from src.research.aether.repository import upsert_node, get_active_campaign_id  # noqa: E402

LOG_PATH = ROOT / "src" / "logs" / "aether" / "mutator_loop.log"

POLL = max(5, int(os.getenv("AETHER_MUTATOR_POLL_SECONDS", "30")))
PER_CYCLE = max(1, int(os.getenv("AETHER_MUTATOR_PER_CYCLE", "4")))
PERTURB_PCT = float(os.getenv("AETHER_MUTATOR_PERTURB_PCT", "0.15"))
MAX_AGE_HOURS = int(os.getenv("AETHER_MUTATOR_MAX_PARENT_AGE_HOURS", "48"))


def _setup_logger():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("aether.mutator_loop")
    log.setLevel(logging.INFO)
    if log.handlers:
        return log
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


def _connect():
    conn = sqlite3.connect(str(db_path()), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _perturb_value(val, pct: float) -> object:
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        delta = max(1, int(round(abs(val) * pct)))
        return max(1, val + random.choice([-delta, delta]))
    if isinstance(val, float):
        delta = max(0.001, abs(val) * pct)
        return round(val + random.choice([-delta, delta]), 4)
    return val


def _perturb_params(params: dict, pct: float) -> dict:
    out = {}
    changed = False
    for k, v in (params or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            new = _perturb_value(v, pct)
            if new != v:
                changed = True
            out[k] = new
        else:
            out[k] = v
    return out if changed else {}


def _candidates(conn) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return list(conn.execute(
        """
        SELECT id, name, hypothesis, causal_mechanism, score, depth,
               metrics_json, timeframe, campaign_id, agent_role
        FROM nodes
        WHERE id != 'root' AND id NOT LIKE 'tf:%'
          AND id NOT LIKE 'mut:%'
          AND updated_at >= ?
          AND (
              status='validated'
              OR json_extract(metrics_json, '$.critic_status') = 'approved'
          )
          AND timeframe IS NOT NULL
        ORDER BY score DESC
        LIMIT 30
        """,
        (cutoff,),
    ).fetchall())


def _existing_mut_ids(conn) -> set[str]:
    rows = conn.execute("SELECT id FROM nodes WHERE id LIKE 'mut:%'").fetchall()
    return {r["id"] for r in rows}


def run_once(conn, log) -> int:
    cands = _candidates(conn)
    if not cands:
        return 0
    existing = _existing_mut_ids(conn)
    created = 0
    random.shuffle(cands)
    for parent in cands:
        if created >= PER_CYCLE:
            break
        try:
            metrics = json.loads(parent["metrics_json"] or "{}")
        except json.JSONDecodeError:
            continue
        probe_specs = metrics.get("probe_specs") or []
        if not isinstance(probe_specs, list) or not probe_specs:
            # tenta best_probe.params
            best = metrics.get("best_probe") or {}
            params = best.get("params") or {}
            engine = best.get("engine")
            if not engine or not params:
                continue
            probe_specs = [{"engine": engine, "params": params}]
        new_specs = []
        for spec in probe_specs:
            if not isinstance(spec, dict):
                continue
            engine = spec.get("engine")
            params = spec.get("params") or {}
            new_params = _perturb_params(params, PERTURB_PCT)
            if not new_params:
                new_params = dict(params)
            new_specs.append({"engine": engine, "params": new_params})
        if not new_specs:
            continue
        h = hashlib.sha1(json.dumps(new_specs, sort_keys=True).encode()).hexdigest()[:10]
        new_id = f"mut:{parent['id'][:30]}:{h}"
        if new_id in existing:
            continue
        engines = sorted({s["engine"] for s in new_specs if s.get("engine")})
        new_metrics = {
            "engines": engines,
            "probe_specs": new_specs,
            "mutator_parent": parent["id"],
            "perturb_pct": PERTURB_PCT,
            "generator": "deterministic_mutator",
        }
        try:
            upsert_node(
                conn,
                id=new_id,
                campaign_id=parent["campaign_id"],
                code="MUT",
                name=f"Mutacao de {parent['name'][:60]}",
                hypothesis=f"Variante perturbada (+/-{int(PERTURB_PCT*100)}%) de {parent['name'][:60]}.",
                parent_id=parent["id"],
                causal_mechanism=parent["causal_mechanism"] or "Variante deterministica para teste de robustez.",
                mutation_from_parent=f"Perturbacao numerica +/-{int(PERTURB_PCT*100)}% nos params.",
                timeframe=parent["timeframe"],
                agent_role=parent["agent_role"] or "deterministic_mutator",
                status="candidate",
                score=max(0.3, float(parent["score"]) * 0.85),
                depth=int(parent["depth"]) + 1,
                priority=max(0.3, float(parent["score"]) * 0.85),
                metrics=new_metrics,
                tags=["mutator", "deterministic"],
            )
            existing.add(new_id)
            created += 1
            log.info(f"mutated parent={parent['id'][:50]} -> {new_id}")
        except Exception as e:
            log.warning(f"falha mutar {parent['id']}: {e}")
    return created


def main() -> int:
    log = _setup_logger()
    log.info(f"mutator iniciado | poll={POLL}s | per_cycle={PER_CYCLE} | perturb_pct={PERTURB_PCT}")
    while True:
        try:
            with _connect() as conn:
                n = run_once(conn, log)
                if n:
                    log.info(f"ciclo: {n} mutacoes criadas")
        except sqlite3.OperationalError as e:
            log.warning(f"sqlite busy: {e}")
        except Exception as e:
            log.exception(f"erro: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
