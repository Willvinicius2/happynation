"""Etapa 1A.5: valida top-5 do manifest rodando TODOS os 6 gates.

Diferenca para validate_aether_manifest_top.py: nao para no primeiro gate
que falha. Roda os 6 gates de forma independente e produz matriz por
estrategia x gate.

Reusa todos os helpers internos do critic_agent.py mas reimplementa o
loop de gates sem early-return.

Saida:
  src/logs/agents/aether_manifest_full_validation.json

Uso:
  python -u scripts/validate_aether_manifest_full.py
"""
from __future__ import annotations

import json
import sys
import time
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.aether import compute as _compute_mod  # noqa: E402
from src.research.aether import gpu_compute as _gpu_compute  # noqa: E402
from src.research.aether.compute import load_dataset_fast  # noqa: E402
from src.research.aether.critic_agent import (  # noqa: E402
    ALT_SYMBOLS,
    BOOTSTRAP_DISABLED,
    BOOTSTRAP_MIN_SHARPE_P5,
    BOOTSTRAP_SAMPLES,
    CROSS_SYMBOL_MIN_PASS,
    CROSS_SYMBOL_MIN_PNL_PCT,
    CROSS_SYMBOL_POOL,
    GRID_DISABLED,
    GRID_MIN_P25_PNL,
    GRID_MIN_POSITIVE_FRAC,
    GRID_RANGE_PCT,
    GRID_SIZE,
    MTF_DISABLED,
    MTF_MIN_PASS,
    MTF_MIN_PNL,
    MTF_TIMEFRAMES,
    WF_FOLDS,
    WF_MIN_PASS,
    WF_MIN_PNL,
    _backtest,
    _backtest_slice,
    _criticize_gpu_batch,
    _perturb_params,
)

MANIFEST_PATH = ROOT / "src" / "logs" / "research" / "active_strategies.json"
TOP_N_PER_ASSET = 5


def evaluate_all_gates(record: dict) -> dict:
    """Roda os 6 gates SEM early-return. Retorna dict com cada gate."""
    engine = str(record.get("engine") or "")
    params = dict(record.get("params") or {})
    symbol = str(record.get("asset") or "").upper()
    timeframe = str(record.get("timeframe") or "1h")

    if not engine or not params or not symbol or not timeframe:
        return {"error": "best_probe incompleto", "gates": {}}

    alts = [s for s in ALT_SYMBOLS if s != symbol][:CROSS_SYMBOL_POOL]
    perturbed = _perturb_params(params)
    dataset_id = f"binance:{symbol}:{timeframe}"

    # === GPU batch (cross+perturb+WF) ===
    gpu_bundle = _criticize_gpu_batch(
        engine, params, symbol, timeframe, dataset_id, alts, perturbed
    )
    if gpu_bundle is not None:
        cross_results = gpu_bundle["cross_results"]
        perturb_pnls = gpu_bundle["perturb_pnls"]
        wf_pnls = gpu_bundle["wf_pnls"]
    else:
        cross_results = []
        for alt in alts:
            ds = f"binance:{alt}:{timeframe}"
            res = _backtest(engine, params, ds, timeframe)
            if res is None:
                cross_results.append({"symbol": alt, "ok": False, "pnl_pct": None})
            else:
                cross_results.append({"symbol": alt, "ok": True, "pnl_pct": float(res.get("pnl_pct") or 0)})
        perturb_pnls = []
        for pp in perturbed:
            res = _backtest(engine, pp, dataset_id, timeframe)
            if res is None:
                continue
            perturb_pnls.append(float(res.get("pnl_pct") or 0))
        wf_pnls = []
        for k in range(WF_FOLDS):
            frac = (k / WF_FOLDS, (k + 1) / WF_FOLDS)
            r = _backtest_slice(engine, params, dataset_id, timeframe, frac)
            wf_pnls.append(float(r.get("pnl_pct") or 0) if r else None)

    gates: dict = {}

    # G1 cross-symbol
    cross_pass = sum(
        1 for r in cross_results
        if r["ok"] and (r["pnl_pct"] is not None) and r["pnl_pct"] >= CROSS_SYMBOL_MIN_PNL_PCT
    )
    gates["G1_cross_symbol"] = {
        "ok": cross_pass >= CROSS_SYMBOL_MIN_PASS,
        "passed": cross_pass,
        "min_pass": CROSS_SYMBOL_MIN_PASS,
        "results": cross_results,
        "min_profit_pct": CROSS_SYMBOL_MIN_PNL_PCT,
    }

    # G2 perturbation
    perturb_med = median(perturb_pnls) if perturb_pnls else 0
    gates["G2_perturbation"] = {
        "ok": bool(perturb_pnls) and perturb_med > 0,
        "median_pnl_pct": round(perturb_med, 3),
        "n": len(perturb_pnls),
    }

    # G3 walk-forward K-fold
    valid_folds = [p for p in wf_pnls if p is not None]
    folds_pass = sum(1 for p in valid_folds if p >= WF_MIN_PNL)
    gates["G3_walk_forward"] = {
        "ok": (len(valid_folds) >= WF_MIN_PASS) and (folds_pass >= WF_MIN_PASS),
        "passed": folds_pass,
        "valid_folds": len(valid_folds),
        "total_folds": WF_FOLDS,
        "min_pass": WF_MIN_PASS,
        "fold_pnls": [round(p, 3) if p is not None else None for p in wf_pnls],
        "min_pnl_pct": WF_MIN_PNL,
    }

    # G4 grid denso (pula se desligado)
    df_full_cache = None
    if not GRID_DISABLED:
        grid = None
        try:
            df_full_cache = load_dataset_fast(dataset_id)
            grid = _gpu_compute.grid_search_metrics_gpu(
                engine, params, df_full_cache, timeframe,
                grid_size=GRID_SIZE, range_pct=GRID_RANGE_PCT,
            )
        except Exception as e:
            grid = {"error": str(e)}
        if grid and "error" not in grid and "pnl_positive_frac" in grid:
            gates["G4_grid_dense"] = {
                "ok": (grid["pnl_positive_frac"] >= GRID_MIN_POSITIVE_FRAC)
                      and (grid["pnl_p25_pct"] >= GRID_MIN_P25_PNL),
                "positive_frac": round(grid["pnl_positive_frac"], 3),
                "p25_pnl_pct": round(grid["pnl_p25_pct"], 3),
                "median_pnl_pct": round(grid.get("pnl_median_pct", 0), 3),
                "total_combos": grid.get("total_combos", 0),
                "min_positive_frac": GRID_MIN_POSITIVE_FRAC,
            }
        else:
            gates["G4_grid_dense"] = {"ok": False, "skipped": True, "reason": (grid or {}).get("error", "no_data")}
    else:
        gates["G4_grid_dense"] = {"ok": True, "skipped": True, "reason": "grid_disabled"}

    # G5 multi-TF (pula se desligado)
    if not MTF_DISABLED:
        other_tfs = [tf for tf in MTF_TIMEFRAMES if tf != timeframe]
        try:
            with ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(lambda tf: load_dataset_fast(f"binance:{symbol}:{tf}"), other_tfs))
            mtf_probes = [
                {
                    "probe_id": f"critic:mtf:{tf}",
                    "dataset_id": f"binance:{symbol}:{tf}",
                    "symbol": symbol,
                    "timeframe": tf,
                    "engine": engine,
                    "params": dict(params),
                    "fee_bps": 10.0,
                    "slippage_bps": 2.0,
                    "capital": 10000.0,
                }
                for tf in other_tfs
            ]
            mtf_results = _gpu_compute.run_probes_gpu(mtf_probes, load_dataset_fast)
        except Exception:
            mtf_results = None
        mtf_pnls: dict[str, float | None] = {}
        if mtf_results is not None:
            for p, r in zip(mtf_probes, mtf_results):
                if r is None:
                    cpu_res = _backtest(p["engine"], p["params"], p["dataset_id"], p["timeframe"])
                    r = cpu_res if cpu_res else {"ok": False}
                mtf_pnls[p["timeframe"]] = float(r["pnl_pct"]) if r.get("ok") else None
        mtf_pass = sum(1 for v in mtf_pnls.values() if v is not None and v >= MTF_MIN_PNL)
        gates["G5_multi_timeframe"] = {
            "ok": mtf_pass >= MTF_MIN_PASS,
            "pnls_by_tf": {k: round(v, 3) if v is not None else None for k, v in mtf_pnls.items()},
            "passed": mtf_pass,
            "min_pass": MTF_MIN_PASS,
        }
    else:
        gates["G5_multi_timeframe"] = {"ok": True, "skipped": True, "reason": "mtf_disabled"}

    # G6 bootstrap MC (pula se desligado)
    if not BOOTSTRAP_DISABLED:
        boot = None
        try:
            if df_full_cache is None:
                df_full_cache = load_dataset_fast(dataset_id)
            boot = _gpu_compute.bootstrap_metrics_gpu(
                engine, params, df_full_cache, timeframe,
                n_samples=BOOTSTRAP_SAMPLES,
            )
        except Exception as e:
            boot = {"error": str(e)}
        if boot and "error" not in boot and "sharpe_p5" in boot:
            gates["G6_bootstrap_mc"] = {
                "ok": boot["sharpe_p5"] >= BOOTSTRAP_MIN_SHARPE_P5,
                "sharpe_p5": round(boot["sharpe_p5"], 3),
                "sharpe_positive_frac": round(boot.get("sharpe_positive_frac", 0), 3),
                "n_samples": boot.get("n_samples", BOOTSTRAP_SAMPLES),
                "min_sharpe_p5": BOOTSTRAP_MIN_SHARPE_P5,
            }
        else:
            gates["G6_bootstrap_mc"] = {"ok": False, "skipped": True, "reason": (boot or {}).get("error", "no_data")}
    else:
        gates["G6_bootstrap_mc"] = {"ok": True, "skipped": True, "reason": "bootstrap_disabled"}

    return {"gates": gates}


def main() -> int:
    print(f"[manifest-full] lendo {MANIFEST_PATH}", flush=True)
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    strategies = data.get("strategies", [])
    print(f"[manifest-full] total: {len(strategies)}", flush=True)

    by_asset: dict[str, list[dict]] = defaultdict(list)
    for s in strategies:
        by_asset[s.get("asset", "?")].append(s)
    for asset, lst in by_asset.items():
        lst.sort(key=lambda r: float(r.get("score") or 0.0), reverse=True)

    selected: list[tuple[str, dict]] = []
    for asset, lst in by_asset.items():
        for r in lst[:TOP_N_PER_ASSET]:
            selected.append((asset, r))
    print(f"[manifest-full] validando {len(selected)} (top-{TOP_N_PER_ASSET}/ativo)\n", flush=True)

    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifest_total": len(strategies),
        "validated_count": len(selected),
        "results": [],
    }

    t_total = time.time()
    for i, (asset, record) in enumerate(selected, 1):
        sid = record.get("id", "?")
        engine = record.get("engine", "?")
        score = float(record.get("score") or 0)
        t0 = time.time()
        print(f"[{i}/{len(selected)}] {asset} {sid[-8:]} ({engine}, score={score:.3f}) ...", flush=True)

        out = evaluate_all_gates(record)
        gates = out.get("gates", {})
        passed = sum(1 for g in gates.values() if g.get("ok"))
        total = len(gates)
        dt = time.time() - t0

        gate_str = " ".join(
            ("+" if g.get("ok") else "-") + name.split("_", 1)[0]
            for name, g in gates.items()
        )
        print(f"    {passed}/{total} | {gate_str}  ({dt:.1f}s)", flush=True)

        report["results"].append({
            "asset": asset,
            "id": sid,
            "engine": engine,
            "score": score,
            "passed": passed,
            "total": total,
            "gates": gates,
            "elapsed_seconds": round(dt, 1),
        })

    out_path = ROOT / "src/logs/agents/aether_manifest_full_validation.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    elapsed_total = time.time() - t_total
    print("\n" + "=" * 70, flush=True)
    print("MATRIZ POR GATE (todos os 6 testados sem short-circuit)", flush=True)

    # Header
    headers = ["G1cross", "G2pert", "G3wf", "G4grid", "G5mtf", "G6boot"]
    by_asset_results: dict[str, list[dict]] = defaultdict(list)
    for r in report["results"]:
        by_asset_results[r["asset"]].append(r)

    for asset, lst in by_asset_results.items():
        print(f"\n{asset}:", flush=True)
        for r in lst:
            gates_d = r["gates"]
            cells = []
            for name, label in zip(
                ["G1_cross_symbol", "G2_perturbation", "G3_walk_forward",
                 "G4_grid_dense", "G5_multi_timeframe", "G6_bootstrap_mc"],
                headers
            ):
                g = gates_d.get(name, {})
                if g.get("skipped"):
                    cells.append("skip")
                elif g.get("ok"):
                    cells.append(" OK ")
                else:
                    cells.append("FAIL")
            cell_str = " | ".join(cells)
            print(f"  {r['id'][-8:]} ({r['engine'][:6]}, s={r['score']:.2f}) {r['passed']}/6 | {cell_str}", flush=True)

    # Summary por gate
    print("\n" + "=" * 70, flush=True)
    print("SUMARIO POR GATE (passes em 20 strategies):", flush=True)
    for name, label in zip(
        ["G1_cross_symbol", "G2_perturbation", "G3_walk_forward",
         "G4_grid_dense", "G5_multi_timeframe", "G6_bootstrap_mc"],
        headers
    ):
        ok = sum(1 for r in report["results"] if r["gates"].get(name, {}).get("ok"))
        skipped = sum(1 for r in report["results"] if r["gates"].get(name, {}).get("skipped"))
        print(f"  {label:8}: {ok}/20 passou ({skipped} skipped)", flush=True)

    print(f"\ntempo total: {elapsed_total:.1f}s", flush=True)
    print(f"relatorio: {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
