"""Pega o ranking do aether server e escreve direto no active_strategies.json,
bypassando o cap de 5 do deploy_record. Lean: so json + urllib."""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "src" / "logs" / "research" / "active_strategies.json"

MAX_DEPLOY = int(os.environ.get("FORCE_MAX_DEPLOY", "50"))
MAX_PER_HYPOTHESIS_ASSET = int(os.environ.get("FORCE_MAX_PER_HYP_ASSET", "2"))
MIN_SCORE = float(os.environ.get("FORCE_MIN_SCORE", "0.45"))


def fetch_ranking():
    url = "http://127.0.0.1:8765/api/ranking?limit=200&include_deployed=true&hide_blocked=false"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def hypothesis_base(name):
    return (name or "").split(" / ")[0].strip().lower()


def main():
    data = fetch_ranking()
    rows = data.get("strategies") or data.get("rows") or []
    print(f"ranking total: {len(rows)}")
    eligible = [r for r in rows if r.get("readiness") == "paper_ready" and float(r.get("score") or 0) >= MIN_SCORE]
    print(f"paper_ready & score>={MIN_SCORE}: {len(eligible)}")

    # Sort por score desc
    eligible.sort(key=lambda r: float(r.get("score") or 0), reverse=True)

    # Aplicar cap por hipotese-base + ativo (igual ao novo auto_deploy)
    selected = []
    hyp_counts = {}
    for r in eligible:
        asset = r.get("asset") or "?"
        name = (r.get("strategy") or {}).get("name") or r.get("name") or ""
        key = (hypothesis_base(name), asset)
        if hyp_counts.get(key, 0) >= MAX_PER_HYPOTHESIS_ASSET:
            continue
        selected.append(r)
        hyp_counts[key] = hyp_counts.get(key, 0) + 1
        if len(selected) >= MAX_DEPLOY:
            break

    print(f"after hypothesis dedup (max {MAX_PER_HYPOTHESIS_ASSET}/asset): {len(selected)}")

    # Construir registros do manifest
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out_strategies = []
    for r in selected:
        rec = r.get("active_record") or {}
        if not rec or not rec.get("id"):
            continue
        rec.setdefault("source", "force_ranking_deploy")
        rec.setdefault("deployed_at", now_iso)
        out_strategies.append(rec)

    print(f"with active_record: {len(out_strategies)}")

    if not out_strategies:
        print("nada pra deploy; abortando sem mexer no manifest")
        return 1

    # Backup + escrita
    backup = MANIFEST.with_suffix(".json.bak")
    if MANIFEST.exists():
        backup.write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    payload = {"version": 1, "updated_at": now_iso, "strategies": out_strategies}
    MANIFEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"escrito: {MANIFEST} ({len(out_strategies)} estrategias)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
