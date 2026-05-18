"""Lean deploy: le DB do Aether, constroi records e escreve active_strategies.json
SEM importar src.modules.* (que tao com binance lib travada).

Replica a logica de auto_deploy.py:
- Pega validated nodes
- Aplica gates (score, trades, mdd, pnl, critic_status)
- Constroi deploy records identicos ao _record_for
- Aplica caps de fam/asset/asset_family_tf E o novo hypothesis-base/asset
- Escreve manifest
"""
import json
import os
import sys
import sqlite3
import hashlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "src" / "logs" / "research" / "active_strategies.json"
DB = Path(os.environ.get("AETHER_RUNTIME_DIR", str(Path.home() / "AppData" / "Local" / "Aether" / "runtime"))) / "aether.sqlite3"

# Gates - replica os defaults do start_aether.bat com MIN_SCORE=0.45
MIN_SCORE = float(os.environ.get("AETHER_DEPLOY_MIN_SCORE", "0.45"))
MIN_TRADES = int(os.environ.get("AETHER_DEPLOY_MIN_TRADES", "12"))
MAX_MDD = float(os.environ.get("AETHER_DEPLOY_MAX_MDD_PCT", "35.0"))
MIN_PNL = float(os.environ.get("AETHER_DEPLOY_MIN_PNL_PCT", "0.5"))
CRITIC_GATE = os.environ.get("AETHER_DEPLOY_CRITIC_GATE", "approved").strip().lower()
ALLOWED_ASSETS = {a.strip().upper() for a in os.environ.get("AETHER_DEPLOY_ALLOWED_ASSETS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,MATICUSDT").split(",") if a.strip()}
DEPLOY_TOP_N = int(os.environ.get("AETHER_DEPLOY_TOP_N", "150"))
MAX_PER_FAMILY = int(os.environ.get("AETHER_DEPLOY_MAX_PER_FAMILY", "0"))
MAX_PER_ASSET = int(os.environ.get("AETHER_DEPLOY_MAX_PER_ASSET", "0"))
MAX_PER_ASSET_FAMILY_TF = int(os.environ.get("AETHER_DEPLOY_MAX_PER_ASSET_FAMILY_TF", "0"))
MAX_PER_HYPOTHESIS_ASSET = int(os.environ.get("AETHER_DEPLOY_MAX_PER_HYPOTHESIS_ASSET", "2"))
REQUIRE_COMBO_FOR_MULTI = os.environ.get("AETHER_DEPLOY_REQUIRE_COMBO_FOR_MULTI", "1").strip().lower() in {"1", "true", "yes"}

DEFAULT_RISK = {
    "take_profit_pct": 10.0,
    "stop_loss_pct": 2.0,
    "max_hold_candles": 96,
}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def active_id(node_id, engine, params, symbol, timeframe):
    payload = json.dumps({
        "node_id": node_id, "engine": engine, "params": params,
        "symbol": symbol, "timeframe": timeframe,
    }, sort_keys=True)
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:14]
    return f"aether-{h}"


def hypothesis_base(name):
    return (name or "").split(" / ")[0].strip().lower() or "?"


def main():
    if not DB.exists():
        print(f"DB nao encontrado: {DB}", file=sys.stderr)
        return 1

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        select id, name, status, score, metrics_json, timeframe
        from nodes
        where status='validated'
        order by score desc
    """).fetchall()
    print(f"validated nodes: {len(rows)}")

    candidates = []
    rejected = {"no_critic": 0, "score": 0, "trades": 0, "mdd": 0, "pnl": 0, "no_engine": 0, "no_symbol": 0, "asset_blocked": 0}
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except Exception:
            metrics = {}
        critic = metrics.get("critic_status")
        if CRITIC_GATE == "approved" and critic != "approved":
            rejected["no_critic"] += 1
            continue
        score = float(row["score"] or 0)
        if score < MIN_SCORE:
            rejected["score"] += 1
            continue
        trades = int(metrics.get("trade_count") or metrics.get("trades") or 0)
        if trades < MIN_TRADES:
            rejected["trades"] += 1
            continue
        mdd = float(metrics.get("mdd_pct") or 0)
        if mdd > MAX_MDD:
            rejected["mdd"] += 1
            continue
        pnl = float(metrics.get("pnl_pct") or 0)
        if pnl < MIN_PNL:
            rejected["pnl"] += 1
            continue
        best = metrics.get("best_probe") or {}
        engine = best.get("engine")
        if not engine:
            rejected["no_engine"] += 1
            continue
        params = best.get("params")
        if not isinstance(params, dict) or not params:
            rejected["no_engine"] += 1
            continue
        probe_specs = metrics.get("probe_specs") if isinstance(metrics.get("probe_specs"), list) else []
        clean_specs = [
            {"engine": str(spec.get("engine")), "params": dict(spec.get("params") or {})}
            for spec in probe_specs
            if isinstance(spec, dict) and spec.get("engine")
        ]
        if len(clean_specs) > 1:
            if REQUIRE_COMBO_FOR_MULTI and str(engine) != "combo":
                continue
            if str(engine) == "combo":
                params = {**params, "mode": params.get("mode") or "consensus", "specs": params.get("specs") or clean_specs}
        symbol = (best.get("symbol") or "").upper()
        if not symbol:
            rejected["no_symbol"] += 1
            continue
        if ALLOWED_ASSETS and symbol not in ALLOWED_ASSETS:
            rejected["asset_blocked"] += 1
            continue
        timeframe = str(best.get("timeframe") or row["timeframe"] or "")
        rec_id = active_id(row["id"], str(engine), params, symbol, timeframe)
        record = {
            "id": rec_id,
            "engine": str(engine),
            "template": None,
            "params": params,
            "probe_specs": clean_specs,
            "asset": symbol,
            "timeframe": timeframe,
            "score": score,
            "risk": dict(DEFAULT_RISK),
            "summary": {
                "name": row["name"],
                "pnl_pct": metrics.get("pnl_pct"),
                "sharpe": metrics.get("sharpe"),
                "mdd_pct": metrics.get("mdd_pct"),
                "trade_count": metrics.get("trade_count"),
                "hit_rate": best.get("hit_rate"),
                "validation_passed": bool(metrics.get("validation_passed")),
                "validation_stage": metrics.get("validation_stage"),
                "validated_at": metrics.get("validated_at"),
                "validated_by": metrics.get("validated_by"),
                "critic_status": metrics.get("critic_status"),
                "critic_reason": metrics.get("critic_reason"),
                "combo_count": metrics.get("combo_count"),
            },
            "source_node_id": row["id"],
            "source": "aether_auto_deploy",
            "deployed_at": now_iso(),
        }
        candidates.append(record)

    print(f"after gates: {len(candidates)}; rejected: {rejected}")

    # Functional dedup (pnl/trades/mdd/hit ~ identicos)
    by_func = {}
    for rec in candidates:
        s = rec["summary"]
        fp = (
            rec["asset"], rec["timeframe"],
            round(float(s.get("pnl_pct") or 0), 1),
            int(s.get("trade_count") or 0),
            round(float(s.get("mdd_pct") or 0), 1),
            round(float(s.get("hit_rate") or 0), 3),
        )
        if fp not in by_func or rec["score"] > by_func[fp]["score"]:
            by_func[fp] = rec
    candidates = sorted(by_func.values(), key=lambda r: -r["score"])
    print(f"after functional dedup: {len(candidates)}")

    # Caps
    selected = []
    fam_c, asset_c, aft_c, hyp_c = {}, {}, {}, {}
    for rec in candidates:
        family = rec.get("template") or rec.get("engine") or "?"
        asset = rec["asset"]
        aft = f"{asset}|{family}|{rec['timeframe']}"
        hyp_key = (hypothesis_base(rec["summary"].get("name")), asset)
        if MAX_PER_FAMILY > 0 and fam_c.get(family, 0) >= MAX_PER_FAMILY: continue
        if MAX_PER_ASSET > 0 and asset_c.get(asset, 0) >= MAX_PER_ASSET: continue
        if MAX_PER_ASSET_FAMILY_TF > 0 and aft_c.get(aft, 0) >= MAX_PER_ASSET_FAMILY_TF: continue
        if MAX_PER_HYPOTHESIS_ASSET > 0 and hyp_c.get(hyp_key, 0) >= MAX_PER_HYPOTHESIS_ASSET: continue
        selected.append(rec)
        fam_c[family] = fam_c.get(family, 0) + 1
        asset_c[asset] = asset_c.get(asset, 0) + 1
        aft_c[aft] = aft_c.get(aft, 0) + 1
        hyp_c[hyp_key] = hyp_c.get(hyp_key, 0) + 1
        if len(selected) >= DEPLOY_TOP_N:
            break

    print(f"after caps: {len(selected)}")

    # Backup + write manifest
    if MANIFEST.exists():
        MANIFEST.with_suffix(".json.bak.before_force").write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    payload = {"version": 1, "updated_at": now_iso(), "strategies": selected}
    MANIFEST.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {MANIFEST} with {len(selected)} strategies")

    # Stats por TF + asset
    from collections import Counter
    tfs = Counter(s["timeframe"] for s in selected)
    assets = Counter(s["asset"] for s in selected)
    engines = Counter(s["engine"] for s in selected)
    print(f"by timeframe: {dict(tfs)}")
    print(f"by asset: {dict(assets)}")
    print(f"by engine: {dict(engines)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
