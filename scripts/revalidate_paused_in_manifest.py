"""Revalida entries do manifest que estao em paused_strategies (state.json),
rodando 1A.5 (todos os 6 gates) com 2 anos. Se passar >= MIN_FRACTION,
despausa.

Diferente do aether_promote_with_validation: nao depende do DB Aether,
le direto do manifest. Util para entries antigas que sumiram do DB.

Saida:
  src/logs/agents/revalidate_paused_report.json

Uso:
  python -u scripts/revalidate_paused_in_manifest.py
  python -u scripts/revalidate_paused_in_manifest.py --dry-run
"""
from __future__ import annotations

import argparse
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

from src.modules import PaperBalanceLogger as pbl  # noqa: E402
from scripts.validate_aether_manifest_full import evaluate_all_gates  # noqa: E402

MANIFEST_PATH = ROOT / "src" / "logs" / "research" / "active_strategies.json"
MIN_FRACTION = 0.60
MIN_GATES_ABSOLUTE = 2


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    state = pbl._load_state()
    paused = set(state.get("_meta", {}).get("paused_strategies", {}).keys())

    print(f"[revalidate] manifest tem {len(manifest['strategies'])} entries", flush=True)
    print(f"[revalidate] paused_strategies tem {len(paused)} entries", flush=True)

    # Cruza: pega entries do manifest cujo aether_name esta em paused
    candidates = []
    for r in manifest.get("strategies", []):
        n = _aether_name(r)
        if n in paused:
            candidates.append((r, n))
    print(f"[revalidate] candidatos (no manifest E pausados): {len(candidates)}\n", flush=True)

    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_candidates": len(candidates),
        "approved": [],
        "rejected": [],
    }

    if not candidates:
        print("[revalidate] nada a fazer.", flush=True)
        return 0

    approved_to_unpause: list[tuple[str, str]] = []
    for i, (record, name) in enumerate(candidates, 1):
        sid = record.get("id", "?")
        engine = record.get("engine", "?")
        asset = record.get("asset", "?")
        score = float(record.get("score") or 0)
        t0 = time.time()
        print(f"  [{i}/{len(candidates)}] {asset} {sid[-12:]} ({engine}, score={score:.3f})", flush=True)

        try:
            result = evaluate_all_gates(record)
            gates = result.get("gates", {})
        except Exception as e:
            report["rejected"].append({"id": sid, "name": name, "asset": asset, "reason": f"erro 1A.5: {e}"})
            print(f"    erro: {e}", flush=True)
            continue

        passed_real = sum(1 for g in gates.values() if g.get("ok") and not g.get("skipped"))
        total_testable = sum(1 for g in gates.values() if not g.get("skipped"))
        frac = passed_real / total_testable if total_testable else 0.0
        dt = time.time() - t0
        gate_str = " ".join(
            ("+" if g.get("ok") else "-") + nm.split("_", 1)[0]
            for nm, g in gates.items()
        )
        print(f"    {passed_real}/{total_testable}={frac:.2f} | {gate_str}  ({dt:.1f}s)", flush=True)

        if frac >= MIN_FRACTION and passed_real >= MIN_GATES_ABSOLUTE:
            if args.dry_run:
                print(f"    [DRY-RUN] passaria — despausaria", flush=True)
                report["approved"].append({"id": sid, "name": name, "asset": asset, "passed": passed_real, "total": total_testable, "frac": round(frac, 2), "dry_run": True})
            else:
                approved_to_unpause.append((name, sid))
                report["approved"].append({"id": sid, "name": name, "asset": asset, "passed": passed_real, "total": total_testable, "frac": round(frac, 2)})
                print(f"    APROVADA — despausando", flush=True)
        else:
            report["rejected"].append({"id": sid, "name": name, "asset": asset, "passed": passed_real, "total": total_testable, "frac": round(frac, 2)})
            print(f"    REJEITADA ({frac:.2f} < {MIN_FRACTION})", flush=True)

    # Aplica despausa em batch
    if approved_to_unpause and not args.dry_run:
        with pbl._lock:
            state = pbl._load_state()
            meta = pbl._ensure_meta_state(state)
            paused_dict = meta.setdefault("paused_strategies", {})
            for name, _ in approved_to_unpause:
                paused_dict.pop(name, None)
            pbl._save_state(state)
        print(f"\n[revalidate] {len(approved_to_unpause)} despausadas em state.json", flush=True)

    out = ROOT / "src/logs/agents/revalidate_paused_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60, flush=True)
    print(f"RESUMO: {len(report['approved'])} aprovadas, {len(report['rejected'])} rejeitadas", flush=True)
    print(f"relatorio: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
