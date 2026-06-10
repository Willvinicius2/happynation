"""
Compute layer do Aether adaptado ao HAPPYNATIONBOT.

Substitui a dependencia do legacy alpha_forge / datasets.yaml / parquets por:
  - Candles baixados direto da Binance via python-binance.
  - Engines de estrategia (ma_crossover, bollinger, rsi, donchian, supertrend)
    implementadas inline com pandas/numpy.
  - Backtest engine local simples com fee bilateral.

Mantem a mesma API publica que o orchestrator espera:
  list_dataset_entries, load_dataset_fast, run_probe, run_sweep,
  default_probes_for_timeframe.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from .config import DEFAULT_WORKERS, checkpoint_dir
from .scoring import aggregate_node_metrics


# ---------------------------------------------------------------------------
# Catalogo sintetico de datasets: pares HAPPYNATION x timeframes do bot.
# Cada dataset_id mapeia diretamente para (symbol, interval) na Binance.
# ---------------------------------------------------------------------------

_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "MATICUSDT",
)
_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")
# Janelas alvo: 1m=7d, 5m=30d, 15m=6mo, 1h=2y, 4h=2y, 1d=3y
_BARS_BY_TIMEFRAME = {
    "1m": 10080,   # ~7 dias (43200/mes seria pesado demais)
    "5m": 8640,    # 30 dias
    "15m": 17280,  # ~6 meses
    "1h": 17520,   # ~2 anos
    "4h": 4380,    # ~2 anos
    "1d": 1095,    # ~3 anos
}
# TTL do cache parquet por TF (segundos). Janelas longas em TFs altos
# nao precisam refetch a cada hora; o ultimo candle 1d so fecha no fim do dia.
_CACHE_TTL_BY_TIMEFRAME = {
    "1m": 60 * 5,         # 5 min
    "5m": 60 * 60,        # 1h
    "15m": 60 * 60,       # 1h
    "1h": 60 * 60,        # 1h
    "4h": 60 * 60 * 4,    # 4h
    "1d": 60 * 60 * 24,   # 24h
}


def _dataset_id(symbol: str, timeframe: str) -> str:
    return f"binance:{symbol}:{timeframe}"


def _synthetic_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for tf in _TIMEFRAMES:
        for symbol in _SYMBOLS:
            entries.append(
                {
                    "dataset_id": _dataset_id(symbol, tf),
                    "symbol": symbol,
                    "timeframe": tf,
                    "source": "binance",
                    "start_ts": "live",
                }
            )
    return entries


def list_dataset_entries(legacy_root: Path | str | None = None) -> list[dict[str, Any]]:
    """Catalogo sintetico - legacy_root e ignorado de proposito."""
    return _synthetic_entries()


# ---------------------------------------------------------------------------
# Loader de candles via Binance com cache em disco.
# ---------------------------------------------------------------------------

_DATASET_CACHE: dict[str, pd.DataFrame] = {}
_CACHE_TTL_DEFAULT = 60 * 60  # fallback 1h
_CANDLE_CACHE_DIR = Path(__file__).resolve().parents[2] / "logs" / "aether" / "candles"


def _candle_cache_path(dataset_id: str) -> Path:
    safe = dataset_id.replace(":", "_")
    return _CANDLE_CACHE_DIR / f"{safe}.parquet"


def _candle_cache_pickle_path(dataset_id: str) -> Path:
    safe = dataset_id.replace(":", "_")
    return _CANDLE_CACHE_DIR / f"{safe}.pkl"


def _ttl_for(dataset_id: str) -> int:
    parts = dataset_id.split(":")
    if len(parts) == 3:
        return _CACHE_TTL_BY_TIMEFRAME.get(parts[2], _CACHE_TTL_DEFAULT)
    return _CACHE_TTL_DEFAULT


def _read_cached_candles(dataset_id: str) -> pd.DataFrame | None:
    ttl = _ttl_for(dataset_id)
    for path, loader in (
        (_candle_cache_path(dataset_id), pd.read_parquet),
        (_candle_cache_pickle_path(dataset_id), pd.read_pickle),
    ):
        if not path.exists():
            continue
        age = time.time() - path.stat().st_mtime
        if age > ttl:
            continue
        try:
            return loader(path)
        except Exception:
            continue
    return None


def _write_candle_cache(dataset_id: str, df: pd.DataFrame) -> None:
    try:
        _CANDLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(_candle_cache_path(dataset_id), index=False)
    except Exception:
        try:
            _CANDLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            df.to_pickle(_candle_cache_pickle_path(dataset_id))
        except Exception:
            pass


_BINANCE_PAGE_LIMIT = 1000  # spot klines hard cap por request


def _fetch_binance_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    """Pagina via endTime ate atingir `limit` candles ou esgotar historico
    do listing. Tolera pares com listing recente (retorna o que existir)."""
    from binance.client import Client

    api_key = os.environ.get("BINANCE_API_KEY") or ""
    api_secret = os.environ.get("BINANCE_API_SECRET") or ""
    client = Client(api_key=api_key, api_secret=api_secret)
    cols = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "qav",
        "trades",
        "tbbav",
        "tbqav",
        "_",
    ]
    chunks: list[pd.DataFrame] = []
    end_time: int | None = None
    remaining = max(1, int(limit))
    while remaining > 0:
        page = min(_BINANCE_PAGE_LIMIT, remaining)
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": page}
        if end_time is not None:
            params["endTime"] = end_time
        raw = client.get_klines(**params)
        if not raw:
            break
        chunk = pd.DataFrame(raw, columns=cols)
        chunks.append(chunk)
        end_time = int(chunk["open_time"].iloc[0]) - 1
        remaining -= len(chunk)
        if len(chunk) < page:
            # listing recente / fim do historico disponivel
            break
    if not chunks:
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume"])
    df = (
        pd.concat(chunks, ignore_index=True)
        .drop_duplicates(subset=["open_time"])
        .sort_values("open_time")
        .reset_index(drop=True)
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    return df[["open_time", "open", "high", "low", "close", "volume"]]


def load_dataset_fast(dataset_id: str, legacy_root: str | Path | None = None) -> pd.DataFrame:
    if dataset_id in _DATASET_CACHE:
        return _DATASET_CACHE[dataset_id]
    cached = _read_cached_candles(dataset_id)
    if cached is not None and not cached.empty:
        _DATASET_CACHE[dataset_id] = cached
        return cached
    parts = dataset_id.split(":")
    if len(parts) != 3 or parts[0] != "binance":
        raise ValueError(f"dataset_id desconhecido: {dataset_id}")
    _, symbol, timeframe = parts
    limit = _BARS_BY_TIMEFRAME.get(timeframe, 1500)
    df = _fetch_binance_klines(symbol, timeframe, limit)
    if df.empty:
        raise RuntimeError(f"sem candles para {dataset_id}")
    _DATASET_CACHE[dataset_id] = df
    _write_candle_cache(dataset_id, df)
    return df


# ---------------------------------------------------------------------------
# Engines de estrategia (sinais long/short via numpy).
# Convencao: signal_series e um pd.Series int em {-1, 0, 1}, alinhada ao close.
# ---------------------------------------------------------------------------

from .compute_signals import (
    _signals_ma_crossover,
    _signals_bollinger,
    _rsi_series,
    _signals_rsi,
    _signals_donchian,
    _signals_supertrend,
    _signals_macd,
    _signals_stochastic,
    _signals_adx,
    _signals_keltner,
    _signals_vwap_dev,
    _signals_roc,
    _signals_williams_r,
    _signals_cci,
    _signals_mfi,
    _signals_obv_trend,
    _signals_psar,
    _signals_atr_breakout,
    _signals_zscore,
    _signals_regime_filter,
    _signals_vol_compression,
    _signals_structure_break,
    _signals_golden_cross,
    _signals_swing_pivot,
    _signals_weekly_divergence,
    _signals_path_simulator,
    _signals_combo,
    _build_signals,
    _signals_sma_cross_exits,
    _signals_sniper_csbender,
    _signals_rsi_divergence,
    _signals_hull_suite,
    _signals_hybrid_breakout,
)


# ---------------------------------------------------------------------------
# Backtest engine local simples.
# ---------------------------------------------------------------------------

def _bars_per_year(timeframe: str) -> int:
    return {
        "1m": 60 * 24 * 365,
        "5m": 12 * 24 * 365,
        "10m": 6 * 24 * 365,
        "15m": 4 * 24 * 365,
        "30m": 2 * 24 * 365,
        "1h": 24 * 365,
        "4h": 6 * 365,
        "1d": 365,
    }.get(timeframe, 24 * 365)


def _sharpe_from_returns(rets: np.ndarray, timeframe: str) -> float:
    if rets.size < 3:
        return 0.0
    sd = float(rets.std(ddof=0))
    if sd <= 0 or not math.isfinite(sd):
        return 0.0
    return float(rets.mean() / sd * math.sqrt(_bars_per_year(timeframe)))


def _run_local_backtest(
    df: pd.DataFrame,
    signal: pd.Series,
    *,
    capital: float,
    fee_bps: float,
    slippage_bps: float,
    timeframe: str,
) -> dict[str, Any]:
    close = df["close"].astype(float).reset_index(drop=True)
    sig = signal.reset_index(drop=True).fillna(0).astype(int)
    if len(close) < 30:
        return {
            "ok": False,
            "error": f"poucos candles ({len(close)})",
        }
    # posicao e valida do candle SEGUINTE (executa no proximo bar)
    pos = sig.shift(1).fillna(0).astype(int)
    rets = close.pct_change().fillna(0).values
    pos_arr = pos.values
    # custo de transacao: paga taker_fee + slippage cada vez que muda a posicao (cada lado)
    cost_per_side = (fee_bps + slippage_bps) / 10_000.0
    pos_diff = np.abs(np.diff(pos_arr, prepend=0))
    transaction_costs = pos_diff * cost_per_side
    strategy_returns = pos_arr * rets - transaction_costs
    equity = capital * np.cumprod(1.0 + strategy_returns)
    final_equity = float(equity[-1]) if equity.size else capital
    pnl_pct = (final_equity / capital - 1.0) * 100.0
    # contagem de trades = cada vez que a posicao muda para algo diferente (entrada+saida ~ 2 mudancas, conta 1 trade)
    transitions = int(((np.diff(pos_arr, prepend=0) != 0)).sum())
    trade_count = max(0, transitions // 2 + (transitions % 2))
    # Maximo drawdown.
    peak = np.maximum.accumulate(equity) if equity.size else np.array([capital])
    dd = (peak - equity) / np.where(peak > 0, peak, 1.0)
    mdd_pct = float(dd.max() * 100.0) if dd.size else 0.0

    # Estatisticas por trade: PF e expectancy ajudam o Critic a filtrar
    # estrategias que ganham por cauda de poucos trades, mas sao ruins no lote.
    trade_returns: list[float] = []
    current_direction = 0
    current_trade_return = 1.0
    for pos_value, strat_ret in zip(pos_arr, strategy_returns):
        pos_value = int(pos_value)
        if pos_value == 0:
            if current_direction != 0:
                trade_returns.append(current_trade_return - 1.0)
                current_direction = 0
                current_trade_return = 1.0
            continue
        if current_direction == 0:
            current_direction = pos_value
            current_trade_return = 1.0
        elif pos_value != current_direction:
            trade_returns.append(current_trade_return - 1.0)
            current_direction = pos_value
            current_trade_return = 1.0
        current_trade_return *= 1.0 + float(strat_ret)
    if current_direction != 0:
        trade_returns.append(current_trade_return - 1.0)

    gross_profit = float(sum(max(0.0, capital * tr) for tr in trade_returns))
    gross_loss = float(sum(max(0.0, -capital * tr) for tr in trade_returns))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    expectancy_pct = (sum(trade_returns) / len(trade_returns) * 100.0) if trade_returns else 0.0

    # hit rate aproximado: trades vencedores / total
    wins = 0
    losses = 0
    in_pos = 0
    entry_eq = capital
    for i in range(1, len(equity)):
        if pos_arr[i] != 0 and in_pos == 0:
            in_pos = pos_arr[i]
            entry_eq = equity[i - 1]
        elif pos_arr[i] != in_pos and in_pos != 0:
            if equity[i] > entry_eq:
                wins += 1
            else:
                losses += 1
            in_pos = pos_arr[i]
            entry_eq = equity[i] if in_pos != 0 else entry_eq
    closed = wins + losses
    hit_rate = (wins / closed) if closed > 0 else None
    sharpe = _sharpe_from_returns(strategy_returns, timeframe)
    return {
        "ok": True,
        "bars": int(len(close)),
        "final_equity": round(final_equity, 4),
        "total_pnl": round(final_equity - capital, 4),
        "pnl_pct": round(pnl_pct, 4),
        "mdd_pct": round(mdd_pct, 4),
        "trade_count": trade_count,
        "closed_trades": len(trade_returns),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "profit_factor": round(profit_factor, 4) if math.isfinite(profit_factor) else profit_factor,
        "expectancy_pct": round(expectancy_pct, 4),
        "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
        "sharpe": round(sharpe, 4),
    }


def run_probe(probe: dict[str, Any], legacy_root: str | None = None) -> dict[str, Any]:
    t0 = time.time()
    try:
        dataset_id = str(probe["dataset_id"])
        timeframe = str(probe.get("timeframe") or "")
        df = load_dataset_fast(dataset_id)
        signal = _build_signals(str(probe["engine"]), df, dict(probe.get("params") or {}))
        result = _run_local_backtest(
            df,
            signal,
            capital=float(probe.get("capital", 10000.0)),
            fee_bps=float(probe.get("fee_bps", 10.0)),
            slippage_bps=float(probe.get("slippage_bps", 2.0)),
            timeframe=timeframe,
        )
        if not result.get("ok"):
            return {
                **probe,
                "ok": False,
                "elapsed_s": round(time.time() - t0, 3),
                "error": result.get("error") or "backtest failed",
            }
        return {
            **probe,
            **result,
            "elapsed_s": round(time.time() - t0, 3),
        }
    except Exception as exc:
        return {
            **probe,
            "ok": False,
            "elapsed_s": round(time.time() - t0, 3),
            "error": f"{exc.__class__.__name__}: {exc}"[:500],
        }


# ---------------------------------------------------------------------------
# Sweep com cache em disco / ProcessPool.
# ---------------------------------------------------------------------------

def _write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    last_error: OSError | None = None
    for attempt in range(6):
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_error = exc
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            time.sleep(0.05 * (attempt + 1))
    if last_error:
        raise last_error


def _failed_probe_from_exception(probe: dict[str, Any], exc: BaseException, started: float) -> dict[str, Any]:
    return {
        **probe,
        "ok": False,
        "elapsed_s": round(time.time() - started, 3),
        "error": f"{exc.__class__.__name__}: {exc}"[:500],
    }


def run_sweep(
    *,
    probes: list[dict[str, Any]],
    legacy_root: Path | None = None,
    workers: int | None = None,
    sweep_id: str | None = None,
    checkpoint_path: Path | None = None,
    resume: bool = True,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    sweep = sweep_id or f"sweep-{uuid.uuid4().hex[:10]}"
    max_workers = max(1, int(workers or DEFAULT_WORKERS))
    checkpoint = checkpoint_path or checkpoint_dir() / f"{sweep}.json"

    completed_by_id: dict[str, dict[str, Any]] = {}
    if resume and checkpoint.exists():
        try:
            raw = json.loads(checkpoint.read_text(encoding="utf-8"))
            completed_by_id = {r["probe_id"]: r for r in raw.get("results", []) if "probe_id" in r}
        except Exception:
            completed_by_id = {}

    todo = [p for p in probes if p.get("probe_id") not in completed_by_id]
    results = list(completed_by_id.values())
    started = time.time()
    failed = sum(1 for r in results if not r.get("ok"))
    done = len(results)

    def task_progress(items: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
        progress: dict[str, dict[str, int]] = {}
        for item in items:
            task_id = item.get("_task_id")
            if not task_id:
                continue
            bucket = progress.setdefault(str(task_id), {"completed": 0, "failed": 0})
            bucket["completed"] += 1
            if not item.get("ok"):
                bucket["failed"] += 1
        return progress

    def emit_progress(payload: dict[str, Any]) -> None:
        try:
            _write_checkpoint(checkpoint, payload)
        except OSError as exc:
            # Checkpoint e dado de observabilidade/resume; nao pode derrubar o lote.
            payload = {**payload, "checkpoint_error": f"{exc.__class__.__name__}: {exc}"[:300]}
        if progress_callback:
            progress_callback({k: v for k, v in payload.items() if k != "results"})

    emit_progress(
        {
            "sweep_id": sweep,
            "status": "running",
            "workers": max_workers,
            "total": len(probes),
            "completed": done,
            "failed": failed,
            "results": results,
            "task_progress": task_progress(results),
            "checkpoint_path": str(checkpoint),
        }
    )

    if todo:
        # Tenta primeiro caminho GPU (batch). Probes nao suportadas/que falharam
        # voltam pro caminho CPU normal.
        gpu_results: list[dict[str, Any] | None] | None = None
        try:
            from . import gpu_compute as _gpu

            if _gpu.gpu_available():
                gpu_results = _gpu.run_probes_gpu(
                    todo, lambda dsid: load_dataset_fast(dsid)
                )
        except Exception:
            gpu_results = None

        if gpu_results is not None:
            cpu_todo: list[dict[str, Any]] = []
            for i, r in enumerate(gpu_results):
                if r is None:
                    cpu_todo.append(todo[i])
                else:
                    results.append(r)
                    done += 1
                    if not r.get("ok"):
                        failed += 1
            todo = cpu_todo
            elapsed = max(time.time() - started, 0.001)
            throughput = done / elapsed * 60.0
            remaining = len(probes) - done
            eta = remaining / max(throughput / 60.0, 0.001)
            emit_progress(
                {
                    "sweep_id": sweep,
                    "status": "running",
                    "workers": max_workers,
                    "total": len(probes),
                    "completed": done,
                    "failed": failed,
                    "eta_seconds": round(eta, 1),
                    "throughput_per_min": round(throughput, 2),
                    "checkpoint_path": str(checkpoint),
                    "results": results,
                    "task_progress": task_progress(results),
                    "gpu_used": True,
                }
            )

    if todo:
        if os.environ.get("AETHER_PREWARM_DATASETS", "1").strip().lower() in {"1", "true", "yes", "on"}:
            dataset_ids = sorted({str(p.get("dataset_id") or "") for p in todo if p.get("dataset_id")})
            warmed = 0
            for dataset_id in dataset_ids:
                try:
                    load_dataset_fast(dataset_id)
                    warmed += 1
                except Exception:
                    pass
            emit_progress(
                {
                    "sweep_id": sweep,
                    "status": "running",
                    "phase": "datasets_warmed",
                    "workers": max_workers,
                    "total": len(probes),
                    "completed": done,
                    "failed": failed,
                    "datasets_warmed": warmed,
                    "datasets_total": len(dataset_ids),
                    "checkpoint_path": str(checkpoint),
                    "results": results,
                    "task_progress": task_progress(results),
                }
            )

        # Em workers > 1, ProcessPool re-importa; mantemos o caminho simples via pool.
        if max_workers == 1:
            for probe in todo:
                result = run_probe(probe, str(legacy_root) if legacy_root else None)
                results.append(result)
                done += 1
                if not result.get("ok"):
                    failed += 1
                elapsed = max(time.time() - started, 0.001)
                throughput = done / elapsed * 60.0
                remaining = len(probes) - done
                eta = remaining / max(throughput / 60.0, 0.001)
                emit_progress(
                    {
                        "sweep_id": sweep,
                        "status": "running",
                        "workers": max_workers,
                        "total": len(probes),
                        "completed": done,
                        "failed": failed,
                        "eta_seconds": round(eta, 1),
                        "throughput_per_min": round(throughput, 2),
                        "checkpoint_path": str(checkpoint),
                        "results": results,
                        "task_progress": task_progress(results),
                    }
                )
        else:
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(run_probe, probe, str(legacy_root) if legacy_root else None): probe
                    for probe in todo
                }
                for future in as_completed(futures):
                    probe = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        # Broken workers should fail the probe, not the whole
                        # batch. Otherwise one process-pool crash marks every
                        # agent task in the shared sweep as failed.
                        result = _failed_probe_from_exception(probe, exc, started)
                    results.append(result)
                    done += 1
                    if not result.get("ok"):
                        failed += 1
                    elapsed = max(time.time() - started, 0.001)
                    throughput = done / elapsed * 60.0
                    remaining = len(probes) - done
                    eta = remaining / max(throughput / 60.0, 0.001)
                    emit_progress(
                        {
                            "sweep_id": sweep,
                            "status": "running",
                            "workers": max_workers,
                            "total": len(probes),
                            "completed": done,
                            "failed": failed,
                            "eta_seconds": round(eta, 1),
                            "throughput_per_min": round(throughput, 2),
                            "checkpoint_path": str(checkpoint),
                            "results": results,
                            "task_progress": task_progress(results),
                        }
                    )

    elapsed_total = max(time.time() - started, 0.001)
    summary = aggregate_node_metrics(results)
    final = {
        "sweep_id": sweep,
        "status": "completed",
        "workers": max_workers,
        "total": len(probes),
        "completed": len(results),
        "failed": sum(1 for r in results if not r.get("ok")),
        "elapsed_s": round(elapsed_total, 2),
        "throughput_per_min": round(len(results) / elapsed_total * 60.0, 2),
        "checkpoint_path": str(checkpoint),
        "summary": summary,
        "results": results,
        "task_progress": task_progress(results),
    }
    emit_progress(final)
    return final


# ---------------------------------------------------------------------------
# Geracao de probes default por timeframe.
# ---------------------------------------------------------------------------

# 2026-05-12: incluidos DOGE/AVAX/BNB. O probe pegava so os 6 primeiros, entao
# esses ativos do portfolio do bot nunca eram testados (zero estrategias validadas).
_PRIORITY_SYMBOLS = {
    "BTCUSDT": 0, "ETHUSDT": 1, "SOLUSDT": 2, "XRPUSDT": 3, "ADAUSDT": 4,
    "LINKUSDT": 5, "DOGEUSDT": 6, "AVAXUSDT": 7, "BNBUSDT": 8,
}


def default_probes_for_timeframe(
    legacy_root: Path | None,
    timeframe: str,
    *,
    limit: int = 60,
    engine_specs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    entries = [e for e in list_dataset_entries() if e.get("timeframe") == timeframe]
    entries.sort(key=lambda e: _PRIORITY_SYMBOLS.get(e.get("symbol"), 99))
    selected = entries[:9]

    if engine_specs:
        clean_specs = [
            {"engine": str(spec["engine"]), "params": dict(spec.get("params") or {})}
            for spec in engine_specs
            if isinstance(spec, dict) and spec.get("engine")
        ]
        engines = [(str(spec["engine"]), dict(spec.get("params") or {})) for spec in clean_specs]
        if len(clean_specs) > 1:
            engines.insert(0, ("combo", {"mode": "consensus", "specs": clean_specs}))
    else:
        engines = [
            ("ma_crossover", {"short_window": 10, "long_window": 30, "long_only": True}),
            ("ma_crossover", {"short_window": 20, "long_window": 50, "long_only": True}),
            ("bollinger", {"window": 20, "num_std": 2.0, "long_only": False}),
            ("rsi", {"period": 14, "oversold": 30.0, "overbought": 70.0, "long_only": False}),
            ("donchian", {"entry_window": 20, "exit_window": 10, "long_only": True}),
        ]

    probes: list[dict[str, Any]] = []
    for entry in selected:
        for engine, params in engines:
            param_hash = hashlib.sha1(
                json.dumps(params, sort_keys=True).encode("utf-8")
            ).hexdigest()[:10]
            probe_id = f"{timeframe}:{entry['dataset_id']}:{engine}:{param_hash}"
            probes.append(
                {
                    "probe_id": probe_id,
                    "dataset_id": entry["dataset_id"],
                    "symbol": entry.get("symbol"),
                    "timeframe": timeframe,
                    "engine": engine,
                    "params": params,
                    "fee_bps": 10.0,
                    "slippage_bps": 2.0,
                    "spread_bps": 0.0,
                    "capital": 10000.0,
                    "fraction": 0.1,
                    "leverage": 2.0,
                }
            )
    return probes[:limit]
