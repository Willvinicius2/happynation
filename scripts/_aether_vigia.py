"""Vigia focada no Aether. A cada poll:
- Checa que os 5 procs do Aether (+ 2 do bot) estao vivos. Emite ALERTA na hora se algum cair.
- Le metricas frescas do DB: idade do ultimo evento research_completed, idade do ultimo
  llm_trace, idade do ultimo node validated, contagens das ultimas 2h (validated,
  approved, rejected), deploy ativo (do log).
- Emite ANOMALIA se algum freshness estourar threshold.
- A cada 30min emite heartbeat com stats compactos.

stdout = stream de eventos para o Monitor. Stderr = ignorado.
"""
from __future__ import annotations
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Windows console defaulta pra cp1252; emojis explodem. Forca utf-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(r"c:\Users\wvini\OneDrive\Área de Trabalho\openclaude\HAPPYNATIONBOT")
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from src.research.aether.config import db_path  # noqa: E402

POLL_SECONDS = 300       # 5min entre checks
HEARTBEAT_EVERY = 6      # ~30min (6 * 5min)
LLM_STALL_MIN = 60       # 60min — o orquestrador faz burst do LLM e depois drena fila
RESEARCH_STALL_MIN = 15  # avisar se nenhum research_completed em 15min
VALIDATED_STALL_MIN = 180  # avisar se nenhum validated em 3h
COLD_START_GRACE_MIN = 25  # nao emite anomalia nos primeiros N min do vigia (cold start)


def now_local() -> str:
    return datetime.now().strftime("%H:%M:%S")


def utc_age_minutes(ts: str | None) -> float:
    if not ts:
        return 1e9
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return 1e9
    return (datetime.now(timezone.utc) - t).total_seconds() / 60.0


def check_pids() -> list[str]:
    """Retorna lista de procs mortos (vazia = ok)."""
    dead: list[str] = []
    for pid_file in ("logs/aether.pids", "logs/happynation.pids"):
        p = ROOT / pid_file
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            pid, name = parts[0], parts[1]
            try:
                out = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}"],
                    capture_output=True, text=True, timeout=5,
                )
                if f" {pid} " not in out.stdout:
                    dead.append(f"{name}(pid{pid})")
            except Exception:
                pass
    return dead


def read_db_stats() -> dict:
    conn = sqlite3.connect(str(db_path()), timeout=10.0)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # last event by kind
    last_research = cur.execute(
        "SELECT ts FROM events WHERE type='research_completed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_llm = cur.execute(
        "SELECT ts FROM llm_traces ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_validated = cur.execute(
        "SELECT updated_at FROM nodes WHERE status='validated' ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    # counts last 2h (UTC cutoff)
    cutoff = datetime.now(timezone.utc).timestamp() - 2 * 3600
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    validated_2h = cur.execute(
        "SELECT count(*) FROM nodes WHERE status='validated' AND updated_at > ?",
        (cutoff_iso,),
    ).fetchone()[0]
    llm_2h = cur.execute(
        "SELECT count(*), coalesce(sum(cost_usd),0) FROM llm_traces WHERE ts > ?",
        (cutoff_iso,),
    ).fetchone()
    # validated total
    validated_total = cur.execute(
        "SELECT count(*) FROM nodes WHERE status='validated'"
    ).fetchone()[0]
    conn.close()
    return {
        "research_age_min": utc_age_minutes(last_research[0] if last_research else None),
        "llm_age_min": utc_age_minutes(last_llm[0] if last_llm else None),
        "validated_age_min": utc_age_minutes(last_validated[0] if last_validated else None),
        "validated_2h": validated_2h,
        "llm_calls_2h": llm_2h[0],
        "llm_cost_2h": float(llm_2h[1]),
        "validated_total": validated_total,
    }


def parse_critic_counts_2h() -> tuple[int, int]:
    """Conta approved/rejected nas ultimas 2h via critic_agent.log (timestamps locais)."""
    log = ROOT / "src/logs/aether/critic_agent.log"
    if not log.exists():
        return 0, 0
    cutoff = datetime.now().timestamp() - 2 * 3600
    cutoff_str = datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d %H:%M:%S")
    approved = rejected = 0
    try:
        with log.open(encoding="utf-8", errors="ignore") as f:
            # ler de tras pra frente seria mais rapido, mas o log nao e gigante
            for line in f:
                if line[:19] < cutoff_str:
                    continue
                if " approved id=" in line:
                    approved += 1
                elif " rejected id=" in line:
                    rejected += 1
    except Exception:
        pass
    return approved, rejected


def parse_deploy_last_line() -> str:
    log = ROOT / "src/logs/aether/auto_deploy.log"
    if not log.exists():
        return "?"
    try:
        with log.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", errors="ignore")
        # achar "deploy:" mais recente
        m = re.findall(r"deploy:.*", tail)
        return m[-1] if m else "?"
    except Exception:
        return "?"


def main() -> None:
    print(f"[{now_local()}] aether-vigia iniciado (poll={POLL_SECONDS}s, heartbeat a cada {POLL_SECONDS*HEARTBEAT_EVERY//60}min, grace cold-start={COLD_START_GRACE_MIN}min)", flush=True)
    i = 0
    started_at = time.time()
    # Anomalia mede "stall durante a vida do vigia", nao idade absoluta no DB.
    # Ts da ultima atividade vista DESDE o startup do vigia (None = ainda nao viu).
    last_seen_validated_total: int | None = None
    last_growth_time = started_at  # quando validated_total subiu pela ultima vez
    last_llm_age_seen: float | None = None  # menor llm_age_min visto -> sinaliza atividade fresca
    last_research_age_seen: float | None = None
    while True:
        i += 1
        anomalies: list[str] = []
        try:
            dead = check_pids()
            if dead:
                print(f"[{now_local()}] ALERTA processos mortos: {' '.join(dead)}", flush=True)
        except Exception as e:
            print(f"[{now_local()}] ERRO check_pids: {e}", flush=True)
            dead = []
        try:
            stats = read_db_stats()
        except Exception as e:
            print(f"[{now_local()}] ERRO read_db_stats: {e}", flush=True)
            time.sleep(POLL_SECONDS)
            continue
        # update trackers — "atividade durante vigia"
        now_t = time.time()
        if last_seen_validated_total is None:
            last_seen_validated_total = stats["validated_total"]
        elif stats["validated_total"] > last_seen_validated_total:
            last_seen_validated_total = stats["validated_total"]
            last_growth_time = now_t
        # consideramos LLM/research "fresh" se a idade DB caiu (data nova chegou)
        if last_llm_age_seen is None or stats["llm_age_min"] < last_llm_age_seen:
            last_llm_age_seen = stats["llm_age_min"]
        if last_research_age_seen is None or stats["research_age_min"] < last_research_age_seen:
            last_research_age_seen = stats["research_age_min"]

        vigia_age_min = (now_t - started_at) / 60.0
        if vigia_age_min >= COLD_START_GRACE_MIN:
            # LLM stall: idade atual > limite E idade nunca caiu desde startup
            llm_seen_fresh = last_llm_age_seen is not None and last_llm_age_seen < LLM_STALL_MIN
            if stats["llm_age_min"] > LLM_STALL_MIN and llm_seen_fresh:
                anomalies.append(f"LLM silencioso ha {stats['llm_age_min']:.0f}min (era fresco antes)")
            elif stats["llm_age_min"] > LLM_STALL_MIN and not llm_seen_fresh and vigia_age_min > LLM_STALL_MIN:
                anomalies.append(f"LLM nunca ficou fresco em {vigia_age_min:.0f}min de vigia")
            # Research stall: mesmo padrao
            research_seen_fresh = last_research_age_seen is not None and last_research_age_seen < RESEARCH_STALL_MIN
            if stats["research_age_min"] > RESEARCH_STALL_MIN and research_seen_fresh:
                anomalies.append(f"research_completed ha {stats['research_age_min']:.0f}min")
            # Validated stall: count nao subiu por X min
            stall_min = (now_t - last_growth_time) / 60.0
            if stall_min > VALIDATED_STALL_MIN:
                anomalies.append(f"sem validated novo ha {stall_min:.0f}min de vigia")
            if anomalies and not dead:
                print(f"[{now_local()}] ⚠️  ANOMALIA: {'; '.join(anomalies)}", flush=True)
        # heartbeat periodico
        if i % HEARTBEAT_EVERY == 0:
            appr, rej = parse_critic_counts_2h()
            deploy = parse_deploy_last_line()
            print(
                f"[{now_local()}] ❤️  aether OK | "
                f"validated total={stats['validated_total']} (2h+{stats['validated_2h']}) | "
                f"critic 2h: aprovou {appr} rejeitou {rej} | "
                f"LLM 2h: {stats['llm_calls_2h']} calls ${stats['llm_cost_2h']:.2f} | "
                f"ultimo LLM ha {stats['llm_age_min']:.0f}min | "
                f"{deploy[:80]}",
                flush=True,
            )
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
