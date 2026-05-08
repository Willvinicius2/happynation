from __future__ import annotations

import argparse
import asyncio
import os
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from binance.client import Client
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from src.modules.StrategyResearchEngine import (
    ResearchRules,
    StrategyResearchResult,
    discover_extra_strategy_specs,
    get_default_strategy_specs,
    validate_strategy,
)
from src.research.active import list_active, remove as remove_active
from src.research.catalog import StrategyCatalog
from src.research.discovery import DiscoveryRunner
from src.research.templates import TEMPLATE_GRIDS, RISK_PROFILES, total_candidate_count
from src.research.validator import ValidationCriteria
from src.research_bus import ResearchBus
from src.strategy_research import fetch_klines


warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

DASHBOARD_PATH = Path(__file__).parent / "static" / "research_dashboard.html"
CATALOG_PATH = Path(__file__).parent / "logs" / "research" / "strategy_catalog.json"

bus = ResearchBus()
app = FastAPI(title="HAPPYNATIONBOT Strategy Research")
_run_lock = threading.Lock()
_run_thread: threading.Thread | None = None
_stop_flag = threading.Event()
_discovery_runner: DiscoveryRunner | None = None
_catalog: StrategyCatalog | None = None


def _get_catalog() -> StrategyCatalog:
    global _catalog
    if _catalog is None:
        CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _catalog = StrategyCatalog(CATALOG_PATH)
    return _catalog


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_ts() -> float:
    return time.time()


def _params_str(params: dict[str, Any]) -> str:
    return ";".join(f"{key}={value}" for key, value in sorted(params.items()))


def _serialize_result(result: StrategyResearchResult) -> dict[str, Any]:
    risk_parts: list[str] = []
    if result.take_profit_pct is not None:
        risk_parts.append(f"tp={result.take_profit_pct}")
    if result.stop_loss_pct is not None:
        risk_parts.append(f"sl={result.stop_loss_pct}")
    if result.max_hold_candles is not None:
        risk_parts.append(f"hold={result.max_hold_candles}")
    return {
        "strategy": result.strategy,
        "asset": result.asset,
        "params": result.params,
        "params_str": _params_str(result.params),
        "risk_str": "/".join(risk_parts) or "-",
        "passed": bool(result.passed),
        "score": float(result.score),
        "consistency_score": float(result.consistency_score),
        "winrate_pct": float(result.winrate_pct),
        "profit_factor": float(result.profit_factor),
        "max_drawdown_pct": float(result.max_drawdown_pct),
        "total_profit_pct": float(result.total_profit_pct),
        "total_trades": int(result.total_trades),
        "max_loss_streak": int(result.max_loss_streak),
        "windows_passed": int(result.windows_passed),
        "windows_total": int(result.windows_total),
        "min_window_profit_pct": float(result.min_window_profit_pct),
        "seconds": float(result.seconds),
        "error": result.error,
    }


class StartRequest(BaseModel):
    assets: str = "BTCUSDT,ETHUSDT,SOLUSDT,JUPUSDT"
    interval: str = "1h"
    history: str = "8 months ago UTC"
    workers: int = 4
    include_extras: bool = False
    window_days: int = 60
    min_window_profit: float = 50.0
    min_window_trades: int = 6
    min_total_trades: int = 20
    min_winrate: float = 50.0
    max_drawdown: float = 25.0
    min_profit_factor: float = 1.15
    max_loss_streak: int = 4
    min_consistency_score: float = 55.0
    fee: float = 0.1


def _validate_with_emit(asset: str, params_label: str, data: pd.DataFrame, strategy, rules: ResearchRules):
    bus.emit({
        "type": "job_started",
        "ts": _now_ts(),
        "strategy": strategy.name,
        "asset": asset,
        "params": params_label,
    })
    return validate_strategy(asset, data, strategy, rules)


def _do_research(req: StartRequest) -> None:
    started = _now_ts()
    try:
        bus.emit({"type": "log", "ts": _now_ts(), "level": "info",
                  "msg": f"Iniciando pesquisa | ativos={req.assets} | history={req.history} | workers={req.workers}"})

        assets = [a.strip().upper() for a in req.assets.split(",") if a.strip()]
        if not assets:
            raise ValueError("Nenhum ativo informado")

        client = Client(os.getenv("BINANCE_API_KEY", ""), os.getenv("BINANCE_SECRET_KEY", ""))
        bus.emit({"type": "log", "ts": _now_ts(), "level": "info",
                  "msg": f"Baixando candles de {len(assets)} ativos..."})

        data_by_asset: dict[str, pd.DataFrame] = {}
        for asset in assets:
            if _stop_flag.is_set():
                break
            data_by_asset[asset] = fetch_klines(client, asset, req.interval, req.history)
            bus.emit({"type": "log", "ts": _now_ts(), "level": "info",
                      "msg": f"{asset}: {len(data_by_asset[asset])} candles carregados"})

        if _stop_flag.is_set():
            bus.emit({"type": "run_stopped", "ts": _now_ts()})
            return

        strategies = get_default_strategy_specs()
        if req.include_extras:
            strategies.extend(discover_extra_strategy_specs())

        rules = ResearchRules(
            window_days=req.window_days,
            min_window_profit_pct=req.min_window_profit,
            min_window_trades=req.min_window_trades,
            min_total_trades=req.min_total_trades,
            min_winrate_pct=req.min_winrate,
            max_drawdown_pct=req.max_drawdown,
            min_profit_factor=req.min_profit_factor,
            max_loss_streak=req.max_loss_streak,
            min_consistency_score=req.min_consistency_score,
            trade_fee_pct=req.fee,
        )

        jobs = [(asset, data, strategy) for asset, data in data_by_asset.items() for strategy in strategies]
        bus.emit({
            "type": "run_started",
            "ts": started,
            "ran_at": _now_iso(),
            "total_jobs": len(jobs),
            "params": {
                "assets": assets,
                "interval": req.interval,
                "history": req.history,
                "workers": req.workers,
                "window_days": req.window_days,
                "min_window_profit": req.min_window_profit,
                "min_winrate": req.min_winrate,
                "max_drawdown": req.max_drawdown,
                "min_profit_factor": req.min_profit_factor,
                "min_consistency_score": req.min_consistency_score,
                "include_extras": req.include_extras,
            },
        })
        bus.emit({"type": "log", "ts": _now_ts(), "level": "info",
                  "msg": f"{len(jobs)} testes em fila ({len(strategies)} estrategias x {len(assets)} ativos)"})

        with ThreadPoolExecutor(max_workers=max(1, req.workers)) as executor:
            futures = {}
            for asset, data, strategy in jobs:
                if _stop_flag.is_set():
                    break
                params_label = _params_str(strategy.params)
                future = executor.submit(_validate_with_emit, asset, params_label, data, strategy, rules)
                futures[future] = (asset, strategy.name, params_label)

            for future in as_completed(futures):
                asset, strategy_name, params_label = futures[future]
                try:
                    result = future.result()
                    serialized = _serialize_result(result)
                    bus.emit({"type": "job_done", "ts": _now_ts(), "result": serialized})
                    if serialized["error"]:
                        bus.emit({"type": "log", "ts": _now_ts(), "level": "err",
                                  "msg": f"ERRO {strategy_name} {asset} :: {serialized['error']}"})
                    else:
                        label = "APROVADA" if serialized["passed"] else "podada"
                        bus.emit({
                            "type": "log",
                            "ts": _now_ts(),
                            "level": "ok" if serialized["passed"] else "warn",
                            "msg": (f"{label} {strategy_name} {asset} "
                                    f"score={serialized['score']:.1f} "
                                    f"win={serialized['winrate_pct']:.1f}% "
                                    f"pf={serialized['profit_factor']:.2f} "
                                    f"dd={serialized['max_drawdown_pct']:.1f}%"),
                        })
                except Exception as exc:
                    bus.emit({"type": "log", "ts": _now_ts(), "level": "err",
                              "msg": f"Falha {strategy_name} {asset}: {exc.__class__.__name__}: {exc}"})

        if _stop_flag.is_set():
            bus.emit({"type": "run_stopped", "ts": _now_ts(),
                      "duration_s": _now_ts() - started})
            bus.emit({"type": "log", "ts": _now_ts(), "level": "warn", "msg": "Pesquisa interrompida pelo usuario."})
        else:
            bus.emit({"type": "run_finished", "ts": _now_ts(),
                      "duration_s": _now_ts() - started})
            bus.emit({"type": "log", "ts": _now_ts(), "level": "ok",
                      "msg": f"Pesquisa concluida em {_now_ts() - started:.1f}s"})
    except Exception as exc:
        bus.emit({"type": "run_failed", "ts": _now_ts(),
                  "error": f"{exc.__class__.__name__}: {exc}"})
        bus.emit({"type": "log", "ts": _now_ts(), "level": "err",
                  "msg": f"Pesquisa abortada: {exc.__class__.__name__}: {exc}"})


@app.on_event("startup")
async def _startup() -> None:
    bus.attach_loop(asyncio.get_event_loop())


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    if not DASHBOARD_PATH.exists():
        return HTMLResponse("<h1>Dashboard nao encontrado</h1>", status_code=404)
    return HTMLResponse(DASHBOARD_PATH.read_text(encoding="utf-8"))


@app.get("/state")
async def state() -> JSONResponse:
    return JSONResponse(bus.snapshot())


@app.post("/start")
async def start(req: StartRequest) -> JSONResponse:
    global _run_thread
    with _run_lock:
        if _run_thread is not None and _run_thread.is_alive():
            return JSONResponse({"ok": False, "msg": "Pesquisa ja em andamento"}, status_code=409)
        _stop_flag.clear()
        _run_thread = threading.Thread(target=_do_research, args=(req,), daemon=True, name="research-runner")
        _run_thread.start()
    return JSONResponse({"ok": True})


@app.post("/stop")
async def stop() -> JSONResponse:
    global _discovery_runner
    if _discovery_runner is not None:
        _discovery_runner.stop()
        bus.emit({"type": "log", "ts": _now_ts(), "level": "warn",
                  "msg": "Stop solicitado: aguardando candidato atual terminar."})
        return JSONResponse({"ok": True, "mode": "discovery"})
    if _run_thread is None or not _run_thread.is_alive():
        return JSONResponse({"ok": False, "msg": "Nenhuma pesquisa rodando"}, status_code=409)
    _stop_flag.set()
    bus.emit({"type": "log", "ts": _now_ts(), "level": "warn",
              "msg": "Pedido de parada recebido. Aguardando jobs em andamento finalizarem..."})
    return JSONResponse({"ok": True})


# =================== DISCOVERY ===================

class DiscoverRequest(BaseModel):
    assets: str = "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,JUPUSDT"
    interval: str = "1h"
    history: str = "8 months ago UTC"
    sample_asset: str | None = None
    candidates_per_round: int = 60
    max_rounds: int | None = None
    seed: int | None = None
    # research rules
    window_days: int = 60
    min_window_profit: float = 8.0
    min_window_trades: int = 4
    min_total_trades: int = 15
    min_winrate: float = 50.0
    max_drawdown: float = 25.0
    min_profit_factor: float = 1.15
    max_loss_streak: int = 4
    min_consistency_score: float = 50.0
    fee: float = 0.1
    # validation criteria
    min_scenarios_passed_ratio: float = 0.6
    min_avg_winrate: float = 50.0
    min_avg_profit_factor: float = 1.15
    max_avg_drawdown: float = 25.0
    min_avg_trades: float = 12.0
    fee_levels: list[float] = [0.05, 0.1, 0.2]


def _discovery_worker(req: DiscoverRequest) -> None:
    global _discovery_runner
    started = _now_ts()
    try:
        bus.emit({"type": "log", "ts": _now_ts(), "level": "info",
                  "msg": f"Iniciando descoberta | ativos={req.assets} | history={req.history} | candidatos/round={req.candidates_per_round}"})
        assets = [a.strip().upper() for a in req.assets.split(",") if a.strip()]
        if not assets:
            raise ValueError("Nenhum ativo informado")

        client = Client(os.getenv("BINANCE_API_KEY", ""), os.getenv("BINANCE_SECRET_KEY", ""))
        bus.emit({"type": "log", "ts": _now_ts(), "level": "info",
                  "msg": f"Baixando candles de {len(assets)} ativos..."})

        data_by_asset: dict[str, pd.DataFrame] = {}
        for asset in assets:
            data_by_asset[asset] = fetch_klines(client, asset, req.interval, req.history)
            bus.emit({"type": "log", "ts": _now_ts(), "level": "info",
                      "msg": f"{asset}: {len(data_by_asset[asset])} candles"})

        rules = ResearchRules(
            window_days=req.window_days,
            min_window_profit_pct=req.min_window_profit,
            min_window_trades=req.min_window_trades,
            min_total_trades=req.min_total_trades,
            min_winrate_pct=req.min_winrate,
            max_drawdown_pct=req.max_drawdown,
            min_profit_factor=req.min_profit_factor,
            max_loss_streak=req.max_loss_streak,
            min_consistency_score=req.min_consistency_score,
            trade_fee_pct=req.fee,
        )
        criteria = ValidationCriteria(
            min_scenarios_passed_ratio=req.min_scenarios_passed_ratio,
            min_median_winrate=req.min_avg_winrate,
            min_median_profit_factor=req.min_avg_profit_factor,
            max_median_drawdown_pct=req.max_avg_drawdown,
            min_total_trades_avg=req.min_avg_trades,
            fee_levels=tuple(req.fee_levels),
        )

        catalog = _get_catalog()
        bus.emit({"type": "catalog_stats", "ts": _now_ts(), "stats": catalog.stats()})

        runner = DiscoveryRunner(
            catalog=catalog,
            data_by_asset=data_by_asset,
            rules=rules,
            criteria=criteria,
            emit=bus.emit,
            sample_asset=req.sample_asset or assets[0],
            candidates_per_round=req.candidates_per_round,
            max_rounds=req.max_rounds,
            seed=req.seed,
        )
        _discovery_runner = runner
        runner.run()
    except Exception as exc:
        bus.emit({"type": "run_failed", "ts": _now_ts(),
                  "error": f"{exc.__class__.__name__}: {exc}"})
        bus.emit({"type": "log", "ts": _now_ts(), "level": "err",
                  "msg": f"Descoberta abortada: {exc.__class__.__name__}: {exc}"})
    finally:
        _discovery_runner = None
        try:
            _get_catalog().save()
        except Exception:
            pass
        bus.emit({"type": "log", "ts": _now_ts(), "level": "info",
                  "msg": f"Descoberta encerrada apos {(_now_ts() - started):.1f}s"})


@app.post("/discover/start")
async def discover_start(req: DiscoverRequest) -> JSONResponse:
    global _run_thread
    with _run_lock:
        if _run_thread is not None and _run_thread.is_alive():
            return JSONResponse({"ok": False, "msg": "Ja existe pesquisa rodando"}, status_code=409)
        _stop_flag.clear()
        _run_thread = threading.Thread(target=_discovery_worker, args=(req,), daemon=True, name="discovery-runner")
        _run_thread.start()
    return JSONResponse({"ok": True})


@app.get("/catalog")
async def catalog_view() -> JSONResponse:
    catalog = _get_catalog()
    return JSONResponse({
        "stats": catalog.stats(),
        "approved": [e.to_dict() for e in catalog.approved(limit=80)],
    })


@app.get("/discover/info")
async def discover_info() -> JSONResponse:
    return JSONResponse({
        "templates": list(TEMPLATE_GRIDS.keys()),
        "templates_grid": TEMPLATE_GRIDS,
        "risk_profiles": RISK_PROFILES,
        "total_candidates": total_candidate_count(),
    })


@app.get("/active")
async def active_view() -> JSONResponse:
    return JSONResponse({"strategies": list_active()})


@app.delete("/active/{strategy_id}")
async def active_remove(strategy_id: str) -> JSONResponse:
    ok = remove_active(strategy_id)
    return JSONResponse({"ok": ok})


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = bus.subscribe()
    try:
        await websocket.send_json({"type": "snapshot", "state": bus.snapshot()})
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        bus.unsubscribe(queue)


def main() -> None:
    parser = argparse.ArgumentParser(description="Servidor + dashboard ao vivo do pesquisador de estrategias.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(
        "src.research_server:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
