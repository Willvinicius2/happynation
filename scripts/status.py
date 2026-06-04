"""Health check + snapshot de PnL do HAPPYNATIONBOT.

Mostra:
  - processos Aether/bot vivos
  - manifest: estrategias por ativo
  - state.json: pausadas
  - trades.csv: total trades, PnL por estrategia/ativo (ultimas 24h)
  - top winners/losers

Uso:
  python -u scripts/status.py
  python -u scripts/status.py --hours 48   # janela diferente
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "src" / "logs" / "research" / "active_strategies.json"
STATE = ROOT / "src" / "logs" / "paper_balance" / "state.json"
TRADES = ROOT / "src" / "logs" / "paper_balance" / "trades.csv"
AUTO_DEPLOY_LOG = ROOT / "src" / "logs" / "aether" / "auto_deploy.log"
AETHER_RANKING_URL = (
    "http://127.0.0.1:8765/api/ranking"
    "?capital=1000&limit=250&campaign_id=active&dedupe=true&hide_blocked=false&discoveries_limit=80"
)
AUTO_DEPLOY_DETAIL_KEYS = ("gate", "record", "blocklist", "caps", "cap_detail")
AETHER_HEALTH_EVENTS = {
    "task_failed": "task_failed",
    "continuous_error": "continuous_research_error",
    "cycle_busy": "cycle_database_busy",
    "continuous_busy": "continuous_research_busy",
    "web_backoff": "web_research_backoff",
    "restart_requeued": "restart_requeued_tasks",
}
EXPECTED_PROCESS_MODULES = [
    "aether.server",
    "aether.auto_validator",
    "aether.hyper_agent",
    "aether.critic_agent",
    "aether.auto_deploy",
    "deploy_watchdog",
    "promote_loop",
    "bot",
    "dashboard",
]


def section(title: str) -> None:
    print("\n" + "=" * 70)
    print(f" {title}")
    print("=" * 70)


def list_aether_processes() -> list[dict]:
    """Lista processos python aether via PowerShell."""
    cmd = [
        "powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'aether|src\\.main|src\\.dashboard|aether_promote|run_critic|scripts\\._watchdog_stack|_watchdog_stack' } | "
        "Select-Object ProcessId,ParentProcessId,@{N='Module';E={ if ($_.CommandLine -match 'src\\.research\\.aether\\.(\\w+)') { 'aether.' + $matches[1] } "
        "elseif ($_.CommandLine -match 'src\\.main') { 'bot' } "
        "elseif ($_.CommandLine -match 'aether_promote_with_validation') { 'promote_loop' } "
        "elseif ($_.CommandLine -match 'scripts\\._watchdog_stack|_watchdog_stack') { 'deploy_watchdog' } "
        "elseif ($_.CommandLine -match 'src\\.dashboard') { 'dashboard' } "
        "else { 'other' } }} | ConvertTo-Json -Compress"
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return []
        data = out.stdout.strip()
        if not data:
            return []
        parsed = json.loads(data)
        return [parsed] if isinstance(parsed, dict) else parsed
    except Exception:
        return []


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}


def process_id(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def summarize_module_processes(module: str, procs: list[dict], *, promote_expected: bool = False) -> dict:
    rows = [p for p in procs if p.get("Module") == module]
    pids = sorted(pid for pid in (process_id(p.get("ProcessId")) for p in rows) if pid is not None)
    if not pids:
        if module == "promote_loop" and not promote_expected:
            return {"flag": "OFF", "pids": [], "roots": [], "workers": [], "note": "esperado desligado"}
        return {"flag": "FALTA", "pids": [], "roots": [], "workers": [], "note": ""}

    pid_set = set(pids)
    roots: list[int] = []
    workers: list[int] = []
    for p in rows:
        pid = process_id(p.get("ProcessId"))
        if pid is None:
            continue
        parent = process_id(p.get("ParentProcessId"))
        if parent in pid_set:
            workers.append(pid)
        else:
            roots.append(pid)

    roots = sorted(roots)
    workers = sorted(workers)
    if module == "promote_loop" and not promote_expected:
        flag = "RISCO"
        note = "promote_loop deveria estar off"
    elif not roots:
        flag = "ORFAO"
        note = "sem supervisor raiz"
    elif len(roots) > 1:
        flag = "DUP"
        note = "multiplos supervisores"
    else:
        flag = "OK"
        note = ""
    return {"flag": flag, "pids": pids, "roots": roots, "workers": workers, "note": note}


def format_process_summary(summary: dict) -> str:
    pids = summary.get("pids") or []
    pids_str = ", ".join(str(p) for p in pids) if pids else "-"
    detail = f"pids: {pids_str}"
    roots = summary.get("roots") or []
    workers = summary.get("workers") or []
    if pids:
        detail += f" roots: {', '.join(str(p) for p in roots) or '-'} workers: {', '.join(str(p) for p in workers) or '-'}"
    note = summary.get("note")
    if note:
        detail += f" ({note})"
    return detail


def show_processes() -> None:
    section("PROCESSOS ATIVOS")
    procs = list_aether_processes()
    by_module: dict[str, list[dict]] = defaultdict(list)
    for p in procs:
        by_module[p.get("Module", "other")].append(p)

    promote_expected = env_bool("HAPPYNATION_EXPECT_PROMOTE_LOOP", False)
    for mod in EXPECTED_PROCESS_MODULES:
        summary = summarize_module_processes(mod, by_module.get(mod, []), promote_expected=promote_expected)
        print(f"  [{summary['flag']:5}] {mod:25} {format_process_summary(summary)}")
    other = [m for m in by_module if m not in EXPECTED_PROCESS_MODULES]
    for mod in other:
        pids = [p.get("ProcessId") for p in by_module[mod]]
        print(f"  [INFO ] {mod:25} pids: {pids}")


def show_manifest() -> None:
    section("MANIFEST BOT/PAPER ATIVO")
    if not MANIFEST.exists():
        print("  manifest nao existe")
        return
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    strats = data.get("strategies", [])
    by_asset: dict[str, int] = defaultdict(int)
    by_engine: dict[str, int] = defaultdict(int)
    for s in strats:
        by_asset[s.get("asset", "?")] += 1
        by_engine[s.get("engine", "?")] += 1
    print(f"  total: {len(strats)} estrategias")
    print(f"  por ativo:")
    for asset, n in sorted(by_asset.items(), key=lambda x: -x[1]):
        print(f"    {asset}: {n}")
    print(f"  por engine:")
    for eng, n in sorted(by_engine.items(), key=lambda x: -x[1]):
        print(f"    {eng}: {n}")


def show_aether_ranking() -> None:
    section("RANKING VISUAL AETHER")
    try:
        with urllib.request.urlopen(AETHER_RANKING_URL, timeout=8) as res:
            data = json.loads(res.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"  indisponivel: {exc}")
        return

    reset = data.get("ranking_reset") or {}
    reset_flag = "ativo" if reset.get("active") else "inativo"
    print(f"  reset visual: {reset_flag}")
    if reset.get("active"):
        print(f"  antigas ocultas: {reset.get('hidden_count', 0)}")
        print(f"  reset_at: {reset.get('reset_at', '-')}")
    print(f"  novas aprovadas no ranking: {data.get('count', 0)}")
    print(f"  novas descobertas no pipeline: {len(data.get('discoveries') or [])}")


def resolve_aether_db_path() -> Path:
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from src.research.aether.config import db_path

        return Path(db_path())
    except Exception:
        return ROOT / "runtime" / "aether.sqlite3"


def safe_json_loads(raw) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def nested_int(data: dict, *keys: str) -> int:
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return 0
        cur = cur.get(key)
    try:
        return int(cur or 0)
    except (TypeError, ValueError):
        return 0


def utc_cutoff(minutes: int, now: datetime | None = None) -> str:
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base.astimezone(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_minutes_since(value: str | None, now: datetime | None = None) -> float | None:
    parsed = parse_utc_ts(value)
    if parsed is None:
        return None
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return max(0.0, (base.astimezone(timezone.utc) - parsed).total_seconds() / 60.0)


def task_status_counts(conn: sqlite3.Connection, cutoff: str | None = None) -> dict[str, int]:
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks WHERE status IN ('queued', 'running') GROUP BY status"
        ).fetchall()
    except sqlite3.Error:
        return {}
    counts = {str(row["status"] or "?"): int(row["n"] or 0) for row in rows}
    if cutoff:
        try:
            recent_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM tasks
                WHERE updated_at >= ?
                  AND status IN ('completed', 'failed')
                GROUP BY status
                """,
                (cutoff,),
            ).fetchall()
        except sqlite3.Error:
            recent_rows = []
        for row in recent_rows:
            counts[f"recent_{row['status']}"] = int(row["n"] or 0)
    return counts


def task_runtime_details(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    details: dict[str, object] = {}
    try:
        oldest = conn.execute(
            "SELECT MIN(updated_at) AS oldest FROM tasks WHERE status='running'"
        ).fetchone()
    except sqlite3.Error:
        oldest = None
    oldest_at = str(oldest["oldest"] or "") if oldest and oldest["oldest"] else ""
    if oldest_at:
        details["oldest_running_at"] = oldest_at
        details["oldest_running_age_min"] = round(age_minutes_since(oldest_at, now) or 0.0, 2)

    try:
        rows = conn.execute(
            """
            SELECT agent_role, COUNT(*) AS n
            FROM tasks
            WHERE status='running'
            GROUP BY agent_role
            ORDER BY n DESC, agent_role ASC
            LIMIT 8
            """
        ).fetchall()
    except sqlite3.Error:
        rows = []
    running_by_agent = {str(row["agent_role"] or "?"): int(row["n"] or 0) for row in rows}
    if running_by_agent:
        details["running_by_agent"] = running_by_agent
    return details


def read_hyper_controls(conn: sqlite3.Connection) -> dict:
    try:
        row = conn.execute("SELECT value_json FROM settings WHERE key='hyper_controls'").fetchone()
    except sqlite3.Error:
        return {}
    return safe_json_loads(row["value_json"]) if row else {}


def recent_event_health(conn: sqlite3.Connection, cutoff: str) -> dict[str, dict[str, object]]:
    placeholders = ",".join("?" for _ in AETHER_HEALTH_EVENTS)
    labels_by_type = {event_type: label for label, event_type in AETHER_HEALTH_EVENTS.items()}
    try:
        rows = conn.execute(
            f"""
            SELECT type, COUNT(*) AS n, MAX(ts) AS latest
            FROM events
            WHERE ts >= ?
              AND type IN ({placeholders})
            GROUP BY type
            """,
            (cutoff, *AETHER_HEALTH_EVENTS.values()),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    health = {label: {"count": 0, "latest": ""} for label in AETHER_HEALTH_EVENTS}
    for row in rows:
        label = labels_by_type.get(str(row["type"] or ""))
        if label:
            health[label] = {"count": int(row["n"] or 0), "latest": str(row["latest"] or "")}
    return health


def latest_aether_productivity_snapshot(
    db: Path | None = None,
    *,
    window_minutes: int = 60,
    now: datetime | None = None,
) -> dict:
    db_file = Path(db or resolve_aether_db_path())
    if not db_file.exists():
        return {"db": str(db_file), "exists": False}

    cutoff = utc_cutoff(window_minutes, now)
    snapshot = {
        "db": str(db_file),
        "exists": True,
        "window_minutes": window_minutes,
        "research_cycles": 0,
        "research_last_at": "",
        "llm_nodes": 0,
        "llm_hypotheses": 0,
        "web_nodes": 0,
        "web_hypotheses": 0,
        "llm_accept_cycles": 0,
        "llm_accept_last_at": "",
        "llm_created": 0,
        "llm_rejected_family_cap": 0,
        "llm_rejected_duplicate": 0,
        "critic_approved": 0,
        "critic_rejected": 0,
        "tasks": {},
        "task_runtime": {},
        "health": {},
        "hyper": {},
    }
    with sqlite3.connect(str(db_file), timeout=10.0) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT ts, type, payload_json
                FROM events
                WHERE ts >= ?
                  AND type IN ('research_completed', 'llm_research_completed', 'critic_approved', 'critic_rejected')
                ORDER BY ts ASC
                """,
                (cutoff,),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for row in rows:
            event_type = str(row["type"] or "")
            payload = safe_json_loads(row["payload_json"])
            ts = str(row["ts"] or "")
            if event_type == "research_completed":
                snapshot["research_cycles"] += 1
                snapshot["research_last_at"] = ts
                snapshot["llm_nodes"] += nested_int(payload, "llm", "created_nodes")
                snapshot["llm_hypotheses"] += nested_int(payload, "llm", "created_hypotheses")
                snapshot["web_nodes"] += nested_int(payload, "web", "created_nodes")
                snapshot["web_hypotheses"] += nested_int(payload, "web", "created_hypotheses")
            elif event_type == "llm_research_completed":
                snapshot["llm_accept_cycles"] += 1
                snapshot["llm_accept_last_at"] = ts
                snapshot["llm_created"] += nested_int(payload, "created")
                snapshot["llm_rejected_family_cap"] += nested_int(payload, "rejected_family_cap")
                snapshot["llm_rejected_duplicate"] += nested_int(payload, "rejected_duplicate")
            elif event_type == "critic_approved":
                snapshot["critic_approved"] += 1
            elif event_type == "critic_rejected":
                snapshot["critic_rejected"] += 1
        snapshot["tasks"] = task_status_counts(conn, cutoff)
        snapshot["task_runtime"] = task_runtime_details(conn, now)
        snapshot["health"] = recent_event_health(conn, cutoff)
        snapshot["hyper"] = read_hyper_controls(conn)
    return snapshot


def format_age_minutes(value) -> str:
    try:
        minutes = float(value)
    except (TypeError, ValueError):
        return "-"
    if minutes >= 1440:
        return f"{minutes:.0f}min ({minutes / 1440:.1f}d)"
    return f"{minutes:.0f}min"


def show_aether_productivity() -> None:
    section("PRODUTIVIDADE AETHER")
    snapshot = latest_aether_productivity_snapshot()
    if not snapshot.get("exists"):
        print(f"  db nao existe: {snapshot.get('db')}")
        return

    print(f"  janela DB: ultimos {snapshot.get('window_minutes', 60)}min")
    print(
        "  pesquisa: ciclos={cycles} llm_nodes={llm_nodes} web_nodes={web_nodes} "
        "llm_h={llm_h} web_h={web_h} ultimo={last}".format(
            cycles=snapshot.get("research_cycles", 0),
            llm_nodes=snapshot.get("llm_nodes", 0),
            web_nodes=snapshot.get("web_nodes", 0),
            llm_h=snapshot.get("llm_hypotheses", 0),
            web_h=snapshot.get("web_hypotheses", 0),
            last=snapshot.get("research_last_at") or "-",
        )
    )
    print(
        "  llm aceitos: ciclos={cycles} criados={created} rej_familia={family} "
        "rej_dup={duplicate} ultimo={last}".format(
            cycles=snapshot.get("llm_accept_cycles", 0),
            created=snapshot.get("llm_created", 0),
            family=snapshot.get("llm_rejected_family_cap", 0),
            duplicate=snapshot.get("llm_rejected_duplicate", 0),
            last=snapshot.get("llm_accept_last_at") or "-",
        )
    )
    approved = int(snapshot.get("critic_approved") or 0)
    rejected = int(snapshot.get("critic_rejected") or 0)
    total = approved + rejected
    reject_rate = (rejected / total * 100.0) if total else 0.0
    print(f"  critic: approved={approved} rejected={rejected} reject_rate={reject_rate:.1f}%")
    tasks = snapshot.get("tasks") or {}
    task_parts = [f"{key}={tasks[key]}" for key in sorted(tasks)]
    runtime = snapshot.get("task_runtime") or {}
    extras: list[str] = []
    if runtime.get("oldest_running_age_min") is not None:
        extras.append(f"oldest_running={format_age_minutes(runtime.get('oldest_running_age_min'))}")
    print(f"  tasks: {' '.join(task_parts) if task_parts else '-'} {' '.join(extras)}".rstrip())
    running_by_agent = runtime.get("running_by_agent") if isinstance(runtime.get("running_by_agent"), dict) else {}
    if running_by_agent:
        top_agents = sorted(running_by_agent.items(), key=lambda item: (-int(item[1]), str(item[0])))[:6]
        print("  tasks por agente: " + " ".join(f"{agent}={count}" for agent, count in top_agents))
    health = snapshot.get("health") or {}
    if health:
        health_order = ("task_failed", "continuous_error", "cycle_busy", "continuous_busy", "web_backoff", "restart_requeued")
        health_parts = []
        for key in health_order:
            item = health.get(key) or {}
            try:
                count = int(item.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            health_parts.append(f"{key}={count}")
        print("  saude recente: " + " ".join(health_parts))
    hyper = snapshot.get("hyper") or {}
    if hyper:
        print(
            "  hyper: verdict={verdict} every={every} age={age} reject={reject} "
            "focus={focus_open}/{focus_budget} tasks={running}/{queued} reset_after={reset_after} last_seen={last_seen}".format(
                verdict=hyper.get("verdict", "-"),
                every=hyper.get("force_exploration_every", "-"),
                age=format_age_minutes(hyper.get("best_age_min")),
                reject=hyper.get("critic_reject_rate", "-"),
                focus_open=hyper.get("focus_open_count", "-"),
                focus_budget=hyper.get("focus_open_budget", "-"),
                running=hyper.get("running_tasks", "-"),
                queued=hyper.get("queued_tasks", "-"),
                reset_after=hyper.get("ranking_reset_after") or "-",
                last_seen=hyper.get("last_seen_at") or hyper.get("updated_at") or "-",
            )
        )


def tail_lines(path: Path, limit: int = 300) -> list[str]:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            return list(deque(f, maxlen=limit))
    except OSError:
        return []


def parse_auto_deploy_summary(line: str) -> dict[str, int]:
    if " deploy: " not in line:
        return {}
    payload = line.split(" deploy: ", 1)[1]
    out: dict[str, int] = {}
    for key, value in re.findall(r"([a-z_]+)=(-?\d+)", payload):
        out[key] = int(value)
    return out


def parse_reason_counts(raw: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        key, value = part.rsplit(":", 1)
        try:
            out[key.strip()] = int(value)
        except ValueError:
            continue
    return out


def parse_auto_deploy_detail(line: str) -> dict[str, dict[str, int]]:
    marker = " deploy detail: "
    if marker not in line:
        return {}
    payload = line.split(marker, 1)[1].strip()
    matches = list(re.finditer(r"(?:(?<=\s)|^)(" + "|".join(AUTO_DEPLOY_DETAIL_KEYS) + r")=", payload))
    details: dict[str, dict[str, int]] = {}
    for idx, match in enumerate(matches):
        key = match.group(1)
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(payload)
        details[key] = parse_reason_counts(payload[match.end():end].strip())
    return details


def log_timestamp(line: str) -> str:
    return line[:23].strip() if len(line) >= 23 else "?"


def latest_auto_deploy_snapshot(log_path: Path = AUTO_DEPLOY_LOG) -> dict:
    lines = tail_lines(log_path)
    summary_idx = -1
    for idx, line in reversed(list(enumerate(lines))):
        if " deploy: " in line:
            summary_idx = idx
            break
    summary_line = lines[summary_idx] if summary_idx >= 0 else ""
    detail_line = ""
    if summary_idx >= 0:
        for line in lines[summary_idx + 1:]:
            if " deploy detail: " in line:
                detail_line = line
                break
    return {
        "summary_at": log_timestamp(summary_line) if summary_line else "",
        "detail_at": log_timestamp(detail_line) if detail_line else "",
        "summary": parse_auto_deploy_summary(summary_line) if summary_line else {},
        "details": parse_auto_deploy_detail(detail_line) if detail_line else {},
    }


def print_reason_group(title: str, counts: dict[str, int], limit: int = 5) -> None:
    if not counts:
        return
    print(f"  {title}:")
    for key, value in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]:
        print(f"    {key}: {value}")


def show_auto_deploy_flow() -> None:
    section("FUNIL AUTO-DEPLOY AETHER")
    if not AUTO_DEPLOY_LOG.exists():
        print("  auto_deploy.log nao existe")
        return
    snapshot = latest_auto_deploy_snapshot(AUTO_DEPLOY_LOG)
    summary = snapshot.get("summary") or {}
    details = snapshot.get("details") or {}
    if not summary:
        print("  nenhum ciclo de deploy encontrado")
        return
    print(f"  ultimo ciclo: {snapshot.get('summary_at') or '?'}")
    print(
        "  candidates={candidates} after_dedupe={after_dedupe} deployed={deployed} "
        "unchanged={skipped_unchanged} removed={removed_stale} blocked={blocked}".format(
            candidates=summary.get("candidates", 0),
            after_dedupe=summary.get("after_dedupe", 0),
            deployed=summary.get("deployed", 0),
            skipped_unchanged=summary.get("skipped_unchanged", 0),
            removed_stale=summary.get("removed_stale", 0),
            blocked=summary.get("blocked", 0),
        )
    )
    print_reason_group("gates", details.get("gate") or {})
    print_reason_group("blocklist", details.get("blocklist") or {})
    print_reason_group("caps", details.get("caps") or {})
    print_reason_group("caps detalhe", details.get("cap_detail") or {})
    print_reason_group("records invalidos", details.get("record") or {})


def show_paused() -> None:
    section("ESTRATEGIAS PAUSADAS (state.json)")
    if not STATE.exists():
        print("  state.json nao existe")
        return
    s = json.loads(STATE.read_text(encoding="utf-8"))
    paused = s.get("_meta", {}).get("paused_strategies", {})
    print(f"  total pausadas: {len(paused)}")
    aether = [k for k in paused if k.startswith("Aether:")]
    legacy = [k for k in paused if not k.startswith("Aether:")]
    print(f"    Aether: {len(aether)}")
    print(f"    Legacy: {len(legacy)}")
    if legacy:
        print(f"    legacy nomes: {', '.join(legacy)}")


def parse_ts(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def show_trades(hours: int) -> None:
    section(f"TRADES ULTIMAS {hours}H")
    if not TRADES.exists():
        print("  trades.csv nao existe")
        return
    cutoff = datetime.now() - timedelta(hours=hours)
    rows: list[dict] = []
    with TRADES.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            ts = parse_ts(r.get("timestamp", ""))
            if ts is None or ts < cutoff:
                continue
            rows.append(r)
    if not rows:
        print(f"  sem trades nas ultimas {hours}h")
        # mostra 3 mais recentes total
        with TRADES.open(encoding="utf-8") as f:
            all_rows = list(csv.DictReader(f))
        if all_rows:
            print(f"  total no historico: {len(all_rows)} (3 mais recentes:)")
            for r in all_rows[-3:]:
                print(f"    {r.get('timestamp')} {r.get('asset')} {r.get('strategy')} {r.get('side')} pnl={r.get('pnl_usd') or '-'}")
        return
    print(f"  total trades: {len(rows)}")

    # PnL por estrategia
    pnl_by_strat: dict[str, float] = defaultdict(float)
    count_by_strat: dict[str, int] = defaultdict(int)
    pnl_by_asset: dict[str, float] = defaultdict(float)
    count_by_asset: dict[str, int] = defaultdict(int)
    total_pnl = 0.0
    for r in rows:
        try:
            pnl = float(r.get("pnl_usd") or 0)
        except ValueError:
            pnl = 0.0
        if r.get("side", "").upper() != "SELL":
            continue  # so SELL fecha trade com pnl
        strat = r.get("strategy", "?")
        asset = r.get("asset", "?")
        pnl_by_strat[strat] += pnl
        count_by_strat[strat] += 1
        pnl_by_asset[asset] += pnl
        count_by_asset[asset] += 1
        total_pnl += pnl

    print(f"  PnL acumulado (SELL fills): ${total_pnl:+.2f}")
    print(f"\n  por ativo:")
    for asset, pnl in sorted(pnl_by_asset.items(), key=lambda x: -x[1]):
        print(f"    {asset}: ${pnl:+.2f} ({count_by_asset[asset]} trades)")
    print(f"\n  top 5 estrategias por PnL:")
    top = sorted(pnl_by_strat.items(), key=lambda x: -x[1])[:5]
    for s, p in top:
        print(f"    {s[:50]}: ${p:+.2f} ({count_by_strat[s]} trades)")
    print(f"\n  bottom 5 estrategias por PnL:")
    bot = sorted(pnl_by_strat.items(), key=lambda x: x[1])[:5]
    for s, p in bot:
        print(f"    {s[:50]}: ${p:+.2f} ({count_by_strat[s]} trades)")


def show_balance() -> None:
    section("BALANCO PAPER")
    if not STATE.exists():
        return
    s = json.loads(STATE.read_text(encoding="utf-8"))
    meta = s.get("_meta", {})
    print(f"  bankroll: ${meta.get('total_bankroll', 0):.2f}")
    print(f"  equity:   ${meta.get('equity', 0):.2f}")
    print(f"  realized: ${meta.get('realized_pnl_usd', 0):+.2f}")
    print(f"  market_value: ${meta.get('market_value', 0):.2f}")
    print(f"  allocated: ${meta.get('allocated_usd', meta.get('allocated', 0)):.2f}")
    print(f"  updated_at: {meta.get('updated_at', '?')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args()

    print(f"\nHAPPYNATIONBOT status @ {datetime.now().isoformat(timespec='seconds')}")
    print(f"janela: ultimas {args.hours}h")

    show_processes()
    show_manifest()
    show_aether_ranking()
    show_aether_productivity()
    show_auto_deploy_flow()
    show_paused()
    show_balance()
    show_trades(args.hours)

    print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
