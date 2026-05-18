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
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "src" / "logs" / "research" / "active_strategies.json"
STATE = ROOT / "src" / "logs" / "paper_balance" / "state.json"
TRADES = ROOT / "src" / "logs" / "paper_balance" / "trades.csv"
AETHER_RANKING_URL = (
    "http://127.0.0.1:8765/api/ranking"
    "?capital=1000&limit=250&campaign_id=active&dedupe=true&hide_blocked=false&discoveries_limit=80"
)


def section(title: str) -> None:
    print("\n" + "=" * 70)
    print(f" {title}")
    print("=" * 70)


def list_aether_processes() -> list[dict]:
    """Lista processos python aether via PowerShell."""
    cmd = [
        "powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'aether|src\\.main|src\\.dashboard|aether_promote|run_critic' } | "
        "Select-Object ProcessId,ParentProcessId,@{N='Module';E={ if ($_.CommandLine -match 'src\\.research\\.aether\\.(\\w+)') { 'aether.' + $matches[1] } "
        "elseif ($_.CommandLine -match 'src\\.main') { 'bot' } "
        "elseif ($_.CommandLine -match 'aether_promote_with_validation') { 'promote_loop' } "
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


def show_processes() -> None:
    section("PROCESSOS ATIVOS")
    procs = list_aether_processes()
    by_module: dict[str, list[int]] = defaultdict(list)
    for p in procs:
        by_module[p.get("Module", "other")].append(p.get("ProcessId"))

    expected = ["aether.server", "aether.auto_validator", "aether.hyper_agent",
                "aether.critic_agent", "promote_loop", "bot", "dashboard"]
    for mod in expected:
        pids = by_module.get(mod, [])
        flag = "OK" if pids else "FALTA"
        pids_str = ", ".join(str(p) for p in pids) if pids else "-"
        print(f"  [{flag:5}] {mod:25} pids: {pids_str}")
    other = [m for m in by_module if m not in expected]
    for mod in other:
        print(f"  [INFO ] {mod:25} pids: {by_module[mod]}")


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
    print(f"  allocated: ${meta.get('allocated', 0):.2f}")
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
    show_paused()
    show_balance()
    show_trades(args.hours)

    print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
