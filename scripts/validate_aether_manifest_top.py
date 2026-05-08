"""Etapa 1A: triagem rapida do manifest Aether.

Pega top-5 por score de cada ativo (5 ativos x 5 = 25 max strategies),
roda os 6 gates do Critic v4 com 2 anos de dados (1h × 17520 candles).

Reusa _criticize() do critic_agent.py — sao as mesmas regras que ja
validaram o manifest, mas agora com o dataset 5x maior (que so foi
disponibilizado depois da pagincao Binance).

Saida:
  src/logs/agents/aether_manifest_top_validation.json

Uso:
  python -u scripts/validate_aether_manifest_top.py
"""
from __future__ import annotations

import json
import sys
import time
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.aether.critic_agent import _criticize  # noqa: E402

MANIFEST_PATH = ROOT / "src" / "logs" / "research" / "active_strategies.json"
TOP_N_PER_ASSET = 5


def _build_metrics(record: dict) -> dict:
    """Constroi o dict 'metrics' que _criticize espera (best_probe wrapper)."""
    engine = record.get("engine") or ""
    params = dict(record.get("params") or {})
    asset = record.get("asset") or ""
    timeframe = record.get("timeframe") or "1h"
    return {
        "best_probe": {
            "engine": engine,
            "params": params,
            "symbol": asset,
            "timeframe": timeframe,
        }
    }


def main() -> int:
    print(f"[manifest-validate] lendo {MANIFEST_PATH}", flush=True)
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    strategies = data.get("strategies", [])
    print(f"[manifest-validate] total no manifest: {len(strategies)}", flush=True)

    by_asset: dict[str, list[dict]] = defaultdict(list)
    for s in strategies:
        by_asset[s.get("asset", "?")].append(s)
    for asset, lst in by_asset.items():
        lst.sort(key=lambda r: float(r.get("score") or 0.0), reverse=True)

    selected: list[tuple[str, dict]] = []
    print("\n[manifest-validate] top-5 selecionados:", flush=True)
    for asset, lst in by_asset.items():
        top = lst[:TOP_N_PER_ASSET]
        for r in top:
            selected.append((asset, r))
        print(f"  {asset}: top {len(top)} de {len(lst)} (scores: {[round(float(x.get('score') or 0), 3) for x in top]})", flush=True)
    print(f"\n[manifest-validate] total a validar: {len(selected)}\n", flush=True)

    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifest_total": len(strategies),
        "validated_count": len(selected),
        "top_n_per_asset": TOP_N_PER_ASSET,
        "results": [],
    }

    t_total = time.time()
    for i, (asset, record) in enumerate(selected, 1):
        sid = record.get("id", "?")
        engine = record.get("engine", "?")
        score = float(record.get("score") or 0)
        t0 = time.time()
        print(f"[{i}/{len(selected)}] {asset} {sid[-8:]} ({engine}, score={score:.3f}) ...", flush=True)

        metrics = _build_metrics(record)
        try:
            passed, reason, gate_report = _criticize(metrics)
        except Exception as e:
            passed = False
            reason = f"erro: {e.__class__.__name__}: {e}"
            gate_report = {}

        dt = time.time() - t0
        flag = "APROV" if passed else "REPRO"
        print(f"    [{flag}] {reason}  ({dt:.1f}s)", flush=True)

        report["results"].append({
            "asset": asset,
            "id": sid,
            "engine": engine,
            "score": score,
            "passed": passed,
            "reason": reason,
            "gate_report": gate_report,
            "elapsed_seconds": round(dt, 1),
        })

    out = ROOT / "src/logs/agents/aether_manifest_top_validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    elapsed_total = time.time() - t_total
    print("\n" + "=" * 70, flush=True)
    print(f"VEREDITO MANIFEST TOP-{TOP_N_PER_ASSET} (Critic v4 com 2 anos)", flush=True)
    by_asset_results: dict[str, list[dict]] = defaultdict(list)
    for r in report["results"]:
        by_asset_results[r["asset"]].append(r)
    total_pass = sum(1 for r in report["results"] if r["passed"])
    total = len(report["results"])
    for asset, lst in by_asset_results.items():
        ok = sum(1 for r in lst if r["passed"])
        print(f"  {asset}: {ok}/{len(lst)} aprovadas")
        for r in lst:
            mark = "+" if r["passed"] else "-"
            print(f"    [{mark}] {r['id'][-8:]} ({r['engine']}, score={r['score']:.3f})")
    print(f"\n  -> total: {total_pass}/{total} aprovadas ({100*total_pass/total:.0f}%)", flush=True)
    print(f"  -> tempo total: {elapsed_total:.1f}s", flush=True)
    print(f"\nrelatorio: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
