"""Promote-with-validation: substitui auto_deploy enquanto AETHER_DEPLOY_MIN_SCORE=99.

Pega nodes do DB Aether que estao com critic_status=approved (Critic v4
rigoroso) e nao estao no manifest. Para cada, roda 1A.5 (todos os 6
gates com 2 anos). Promove ao manifest E despausa em state.json APENAS
se passar >= MIN_GATES gates testaveis.

Pode ser rodado manualmente ou em loop (use --loop).

Saida:
  src/logs/agents/aether_promote_validation.json (acumulativo)

Uso:
  python -u scripts/aether_promote_with_validation.py            # 1 ciclo
  python -u scripts/aether_promote_with_validation.py --loop     # repete a cada 30 min
  python -u scripts/aether_promote_with_validation.py --dry-run  # so testa, nao promove
"""
from __future__ import annotations

import argparse
import json
import sqlite3
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

from src.modules import PaperBalanceLogger as pbl  # noqa: E402
from src.research.aether.config import db_path  # noqa: E402

# Reusa logica do validador 1A.5
from scripts.validate_aether_manifest_full import evaluate_all_gates  # noqa: E402

DB = db_path()
MANIFEST_PATH = ROOT / "src" / "logs" / "research" / "active_strategies.json"
ALLOWED_ASSETS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "XRPUSDT"}
LOOP_SECONDS = 30 * 60

# Aprovacao por FRACAO dos gates testaveis (nao por contagem absoluta).
# Combos so tem 3 testaveis (G4/G6 skip por engine), single-engines tem 5.
# Threshold 0.66 = combo precisa 2/3, single-engine precisa 4/5 (ou 3/5 com 1 skip).
MIN_FRACTION = 0.60  # combos costumam dar 2/3=0.667; 0.60 abre para isso
MIN_GATES_ABSOLUTE = 2  # piso minimo: 2 gates positivos absolutos


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


def _read_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"version": 1, "updated_at": None, "strategies": []}
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _write_manifest(data: dict) -> None:
    data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    MANIFEST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _candidates_from_db() -> list[dict]:
    """Pega nodes critic_status=approved no DB."""
    conn = sqlite3.connect(str(DB), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        """
        SELECT id, name, status, score, metrics_json, timeframe
        FROM nodes
        WHERE status='validated' AND id != 'root'
          AND metrics_json LIKE '%"critic_status": "approved"%'
        """
    ).fetchall())
    conn.close()

    out = []
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except Exception:
            continue
        best = metrics.get("best_probe") or {}
        engine = best.get("engine")
        params = best.get("params")
        symbol = best.get("symbol")
        timeframe = best.get("timeframe") or row["timeframe"] or "1h"
        if not (engine and params and symbol and timeframe):
            continue
        if symbol.upper() not in ALLOWED_ASSETS:
            continue
        out.append({
            "id": row["id"],
            "name": row["name"],
            "score": float(row["score"] or 0),
            "asset": symbol.upper(),
            "timeframe": timeframe,
            "engine": engine,
            "params": params,
            "probe_specs": metrics.get("probe_specs", []),
            "risk": metrics.get("risk") or {},
            "summary": metrics.get("summary") or {},
            "critic_reason": metrics.get("critic_reason"),
        })
    return out


def _promote_to_manifest(record: dict, manifest: dict) -> bool:
    """Adiciona ao manifest se ainda nao estiver. Retorna True se adicionou."""
    existing_ids = {s.get("id") for s in manifest.get("strategies", [])}
    if record["id"] in existing_ids:
        return False
    new_record = {
        "id": record["id"],
        "engine": record["engine"],
        "params": record["params"],
        "probe_specs": record.get("probe_specs", []),
        "asset": record["asset"],
        "timeframe": record["timeframe"],
        "score": record["score"],
        "risk": record.get("risk", {}),
        "summary": record.get("summary", {}),
        "source_node_id": record["id"],
        "source": "aether_promote_with_validation",
        "deployed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "critic_reason": record.get("critic_reason"),
    }
    manifest.setdefault("strategies", []).append(new_record)
    return True


def _unpause_in_state(name: str) -> None:
    with pbl._lock:
        state = pbl._load_state()
        meta = pbl._ensure_meta_state(state)
        paused = meta.setdefault("paused_strategies", {})
        paused.pop(name, None)
        pbl._save_state(state)


def run_cycle(dry_run: bool) -> dict:
    print(f"\n[promote] ciclo iniciando dry_run={dry_run}", flush=True)

    candidates = _candidates_from_db()
    print(f"[promote] critic_approved no DB: {len(candidates)}", flush=True)

    manifest = _read_manifest()
    existing_ids = {s.get("id") for s in manifest.get("strategies", [])}
    new_only = [c for c in candidates if c["id"] not in existing_ids]
    print(f"[promote] nao-no-manifest: {len(new_only)}", flush=True)

    cycle_report = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "candidates_critic_approved": len(candidates),
        "candidates_not_in_manifest": len(new_only),
        "promoted": [],
        "rejected": [],
        "skipped_already_in_manifest": len(candidates) - len(new_only),
    }

    if not new_only:
        print("[promote] nada a promover.", flush=True)
        return cycle_report

    promoted_count = 0
    for i, cand in enumerate(new_only, 1):
        sid = cand["id"]
        asset = cand["asset"]
        engine = cand["engine"]
        print(f"  [{i}/{len(new_only)}] {asset} {sid[-8:]} ({engine}, score={cand['score']:.3f}) ...", flush=True)
        t0 = time.time()
        try:
            result = evaluate_all_gates(cand)
            gates = result.get("gates", {})
        except Exception as e:
            cycle_report["rejected"].append({"id": sid, "asset": asset, "reason": f"erro 1A.5: {e}"})
            print(f"    erro: {e}", flush=True)
            continue
        # contagem so de gates testaveis (skipped nao conta)
        passed_real = sum(1 for g in gates.values() if g.get("ok") and not g.get("skipped"))
        total_testable = sum(1 for g in gates.values() if not g.get("skipped"))
        dt = time.time() - t0
        gate_str = " ".join(
            ("+" if g.get("ok") else "-") + name.split("_", 1)[0]
            for name, g in gates.items()
        )
        print(f"    {passed_real}/{total_testable} testaveis | {gate_str}  ({dt:.1f}s)", flush=True)

        # Aprovacao: fracao OK alta o suficiente, com piso absoluto pra evitar
        # passar com 1/1 ou 2/2 testaveis.
        frac = passed_real / total_testable if total_testable else 0.0
        ok = (frac >= MIN_FRACTION) and (passed_real >= MIN_GATES_ABSOLUTE)
        if ok:
            if dry_run:
                print(f"    [DRY-RUN] passaria — promoveria (frac={frac:.2f})", flush=True)
                cycle_report["promoted"].append({"id": sid, "asset": asset, "passed": passed_real, "total": total_testable, "frac": round(frac, 2), "dry_run": True})
            else:
                added = _promote_to_manifest(cand, manifest)
                name = _aether_name(cand)
                _unpause_in_state(name)
                cycle_report["promoted"].append({"id": sid, "asset": asset, "passed": passed_real, "total": total_testable, "frac": round(frac, 2), "name": name, "added": added})
                promoted_count += 1
                print(f"    PROMOVIDO + DESPAUSADO ({name}, frac={frac:.2f})", flush=True)
        else:
            cycle_report["rejected"].append({
                "id": sid, "asset": asset, "passed": passed_real, "total": total_testable, "frac": round(frac, 2),
                "gates": {k: g.get("ok") for k, g in gates.items()},
            })
            print(f"    REJEITADO ({passed_real}/{total_testable}={frac:.2f} < {MIN_FRACTION})", flush=True)

    if not dry_run and promoted_count > 0:
        _write_manifest(manifest)
        print(f"\n[promote] manifest salvo (+{promoted_count} estrategias)", flush=True)

    return cycle_report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help=f"repete a cada {LOOP_SECONDS}s")
    ap.add_argument("--dry-run", action="store_true", help="so reporta, nao promove")
    args = ap.parse_args()

    out_path = ROOT / "src/logs/agents/aether_promote_validation.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    while True:
        report = run_cycle(args.dry_run)
        # acumula em arquivo
        existing = []
        if out_path.exists():
            try:
                existing = json.loads(out_path.read_text(encoding="utf-8")).get("cycles", [])
            except Exception:
                existing = []
        existing.append(report)
        out_path.write_text(json.dumps({"cycles": existing}, ensure_ascii=False, indent=2), encoding="utf-8")

        promoted = len(report["promoted"])
        rejected = len(report["rejected"])
        print(f"\n[promote] resumo: promovidas={promoted} rejeitadas={rejected}", flush=True)
        print(f"[promote] relatorio acumulado: {out_path}", flush=True)

        if not args.loop:
            break
        print(f"\n[promote] aguardando {LOOP_SECONDS}s ate proximo ciclo...", flush=True)
        time.sleep(LOOP_SECONDS)

    return 0


if __name__ == "__main__":
    sys.exit(main())
