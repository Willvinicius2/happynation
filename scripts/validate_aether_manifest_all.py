"""Validate every active Aether manifest strategy with heavy critic gates.

This is the all-manifest companion to validate_aether_manifest_full.py. It
does not mutate the bot manifest or paper state. It writes:

  src/logs/agents/aether_manifest_all_heavy_validation.json
  src/logs/agents/aether_manifest_all_heavy_validation.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_aether_manifest_full import evaluate_all_gates  # noqa: E402

MANIFEST_PATH = ROOT / "src" / "logs" / "research" / "active_strategies.json"
STATE_PATH = ROOT / "src" / "logs" / "paper_balance" / "state.json"
OUT_JSON = ROOT / "src" / "logs" / "agents" / "aether_manifest_all_heavy_validation.json"
OUT_CSV = ROOT / "src" / "logs" / "agents" / "aether_manifest_all_heavy_validation.csv"

GATE_ORDER = [
    "G1_cross_symbol",
    "G2_perturbation",
    "G3_walk_forward",
    "G4_grid_dense",
    "G5_multi_timeframe",
    "G6_bootstrap_mc",
]


def _load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _aether_name(record: dict) -> str:
    sid = str(record.get("id") or "")
    suffix = sid[-8:]
    engine = record.get("engine") or ""
    probe_specs = record.get("probe_specs")
    if engine == "combo" or (isinstance(probe_specs, list) and len(probe_specs) > 1):
        return f"Aether:combo#{suffix}"
    if engine:
        return f"Aether:{engine}#{suffix}"
    return f"unknown#{sid[:8]}"


def _paper_stats_by_name() -> tuple[dict, set[str]]:
    state = _load_json(STATE_PATH, {})
    meta = state.get("_meta") or {}
    stats = meta.get("strategy_stats") or {}
    paused = set((meta.get("paused_strategies") or {}).keys())
    return stats if isinstance(stats, dict) else {}, paused


def _gate_summary(gates: dict) -> tuple[int, int, float]:
    passed = 0
    testable = 0
    for name in GATE_ORDER:
        gate = gates.get(name) or {}
        if gate.get("skipped"):
            continue
        testable += 1
        if gate.get("ok"):
            passed += 1
    frac = passed / testable if testable else 0.0
    return passed, testable, frac


def _recommendation(frac: float, passed: int, testable: int, paper: dict, paused: bool) -> str:
    trades = int(paper.get("trades") or 0)
    pnl = float(paper.get("pnl_usd") or 0.0)
    loss_streak = int(paper.get("loss_streak") or 0)
    pf = float(paper.get("profit_factor") or 0.0)
    if paused:
        return "CUT_OR_REVALIDATE"
    if testable and frac >= 0.80 and (trades == 0 or pnl >= 0 or pf >= 1.0):
        return "KEEP_CORE"
    if testable and frac >= 0.60 and not (trades >= 2 and loss_streak >= 2 and pnl < 0):
        return "KEEP_WATCH"
    if trades >= 2 and loss_streak >= 2 and pnl < 0:
        return "CUT_OR_PAUSE"
    if testable and passed >= 2:
        return "EXPERIMENTAL"
    return "CUT_OR_PAUSE"


def _manifest_metric(record: dict, key: str, default=None):
    summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
    value = summary.get(key)
    return default if value is None else value


def _sort_key(record: dict) -> tuple:
    asset = str(record.get("asset") or "")
    score = float(record.get("score") or 0.0)
    pnl = float(_manifest_metric(record, "pnl_pct", 0.0) or 0.0)
    return (asset, -score, -pnl, str(record.get("id") or ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="optional max strategies for a smoke run")
    args = parser.parse_args()

    manifest = _load_json(MANIFEST_PATH, {})
    strategies = list(manifest.get("strategies") or [])
    strategies.sort(key=_sort_key)
    if args.limit and args.limit > 0:
        strategies = strategies[: args.limit]

    paper_stats, paused_names = _paper_stats_by_name()
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)

    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifest_path": str(MANIFEST_PATH),
        "manifest_updated_at": manifest.get("updated_at"),
        "manifest_total": len(manifest.get("strategies") or []),
        "validated_count": len(strategies),
        "criteria": {
            "engine": "scripts.validate_aether_manifest_full.evaluate_all_gates",
            "costs": "Aether critic defaults: capital=10000, fee_bps=10, slippage_bps=2",
            "gates": GATE_ORDER,
            "recommendation": "KEEP_CORE >=80% non-skipped gates plus non-negative paper; KEEP_WATCH >=60%; paper loss streak can downgrade.",
        },
        "results": [],
    }

    print(f"[manifest-all] lendo {MANIFEST_PATH}", flush=True)
    print(f"[manifest-all] validando {len(strategies)} de {report['manifest_total']} estrategias", flush=True)
    t_all = time.time()

    rows_for_csv = []
    for i, record in enumerate(strategies, 1):
        sid = str(record.get("id") or "?")
        name = _aether_name(record)
        asset = str(record.get("asset") or "?")
        engine = str(record.get("engine") or "?")
        score = float(record.get("score") or 0.0)
        t0 = time.time()
        print(f"[{i}/{len(strategies)}] {asset} {sid[-8:]} {engine} score={score:.3f}", flush=True)
        try:
            result = evaluate_all_gates(record)
            gates = result.get("gates") or {}
            error = result.get("error")
        except Exception as exc:
            gates = {}
            error = f"{exc.__class__.__name__}: {exc}"
        passed, testable, frac = _gate_summary(gates)
        elapsed = time.time() - t0
        paper = dict(paper_stats.get(name) or {})
        paused = name in paused_names or bool(paper.get("paused"))
        rec = _recommendation(frac, passed, testable, paper, paused)
        gate_cells = {
            g: ("skip" if (gates.get(g) or {}).get("skipped") else ("ok" if (gates.get(g) or {}).get("ok") else "fail"))
            for g in GATE_ORDER
        }
        print(f"    {passed}/{testable} frac={frac:.2f} rec={rec} ({elapsed:.1f}s)", flush=True)

        item = {
            "asset": asset,
            "id": sid,
            "aether_name": name,
            "source_node_id": record.get("source_node_id") or record.get("node_id"),
            "engine": engine,
            "timeframe": record.get("timeframe"),
            "score": score,
            "manifest": {
                "pnl_pct": _manifest_metric(record, "pnl_pct"),
                "sharpe": _manifest_metric(record, "sharpe"),
                "mdd_pct": _manifest_metric(record, "mdd_pct"),
                "trade_count": _manifest_metric(record, "trade_count"),
                "critic_status": _manifest_metric(record, "critic_status"),
            },
            "paper": paper,
            "paused": paused,
            "passed_gates": passed,
            "testable_gates": testable,
            "gate_fraction": round(frac, 4),
            "recommendation": rec,
            "gates": gates,
            "error": error,
            "elapsed_seconds": round(elapsed, 1),
        }
        report["results"].append(item)

        rows_for_csv.append({
            "asset": asset,
            "id": sid,
            "aether_name": name,
            "engine": engine,
            "timeframe": record.get("timeframe"),
            "score": score,
            "manifest_pnl_pct": _manifest_metric(record, "pnl_pct"),
            "manifest_sharpe": _manifest_metric(record, "sharpe"),
            "manifest_mdd_pct": _manifest_metric(record, "mdd_pct"),
            "manifest_trade_count": _manifest_metric(record, "trade_count"),
            "critic_status": _manifest_metric(record, "critic_status"),
            "paper_trades": paper.get("trades", 0),
            "paper_wins": paper.get("wins", 0),
            "paper_losses": paper.get("losses", 0),
            "paper_pnl_usd": paper.get("pnl_usd", 0.0),
            "paper_loss_streak": paper.get("loss_streak", 0),
            "paper_profit_factor": paper.get("profit_factor", 0.0),
            "paused": paused,
            "passed_gates": passed,
            "testable_gates": testable,
            "gate_fraction": round(frac, 4),
            "recommendation": rec,
            "elapsed_seconds": round(elapsed, 1),
            **gate_cells,
        })

    summary = {}
    for item in report["results"]:
        summary[item["recommendation"]] = summary.get(item["recommendation"], 0) + 1
    report["summary"] = {
        "elapsed_seconds": round(time.time() - t_all, 1),
        "by_recommendation": dict(sorted(summary.items())),
    }

    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows_for_csv[0].keys()) if rows_for_csv else [])
        if rows_for_csv:
            writer.writeheader()
            writer.writerows(rows_for_csv)

    print("\n" + "=" * 72, flush=True)
    print(f"[manifest-all] resumo: {report['summary']['by_recommendation']}", flush=True)
    print(f"[manifest-all] tempo total: {report['summary']['elapsed_seconds']}s", flush=True)
    print(f"[manifest-all] json: {OUT_JSON}", flush=True)
    print(f"[manifest-all] csv:  {OUT_CSV}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
