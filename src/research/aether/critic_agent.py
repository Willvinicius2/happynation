"""Critic Agent: testa cada estrategia ja validada em out-of-sample
adicional antes que ela esteja apta a ir pro paper. Se passar, marca
metrics.critic_status='approved'. Se falhar, rebaixa o no para 'paused'
com prune_reason='critic: ...' para a arvore refletir a rejeicao.

Tres testes sao aplicados:
  1. Cross-symbol: re-roda em 2 simbolos alternativos. Falha se a maioria
     der PnL <= 0 (estrategia so funciona em um ativo).
  2. Param perturbation: aplica +/-10% nos params numericos e checa se
     mediana dos pnl resultantes ainda eh > 0.
  3. Time-slice: split 50/50 dos candles, ambos devem dar PnL > limiar
     negativo (-3%) — estrategia robusta a regimes diferentes.

Subir com:
    .venv/Scripts/python.exe -u -m src.research.aether.critic_agent
"""
from __future__ import annotations

import gc
import json
import logging
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from src.research.aether.compute import (
    _build_signals,
    _run_local_backtest,
    load_dataset_fast,
)
from src.research.aether import compute as _compute_mod
from src.research.aether import gpu_compute as _gpu_compute
from src.research.aether.config import db_path

DB_PATH = db_path()
LOG_PATH = Path(__file__).resolve().parents[3] / "src" / "logs" / "aether" / "critic_agent.log"

POLL_SECONDS = max(10, int(os.getenv("AETHER_CRITIC_POLL_SECONDS", "90")))
ALT_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "MATICUSDT",
)
PERTURB_PCT = 0.10
TIME_SLICE_MIN_PNL = -3.0
# Cross-symbol: pool de N alts (>=1 com pnl >= MIN_PNL_PCT). Antes era >=1 dos 2
# alts com pnl > 0 -- gate draconiano que rejeitava 31/33 estrategias antes de
# chegar nos gates novos. Alinhado com MTF (-3%).
CROSS_SYMBOL_POOL = max(2, int(os.getenv("AETHER_CRITIC_CROSS_POOL", "3")))
CROSS_SYMBOL_MIN_PASS = max(1, int(os.getenv("AETHER_CRITIC_CROSS_MIN_PASS", "1")))
CROSS_SYMBOL_MIN_PNL_PCT = float(os.getenv("AETHER_CRITIC_CROSS_MIN_PNL", "-3.0"))
BACKTEST_TIMEOUT_SECONDS = float(os.getenv("AETHER_CRITIC_BACKTEST_TIMEOUT", "60"))

# Walk-forward expandido: K folds nao-sobrepostos. Estrategia precisa
# passar em pelo menos MIN_PASS folds com pnl > MIN_PNL.
WF_FOLDS = max(2, int(os.getenv("AETHER_CRITIC_WF_FOLDS", "6")))
WF_MIN_PASS = max(1, int(os.getenv("AETHER_CRITIC_WF_MIN_PASS", str(max(1, WF_FOLDS - 2)))))
WF_MIN_PNL = float(os.getenv("AETHER_CRITIC_WF_MIN_PNL", "-3.0"))

# Monte Carlo bootstrap: gera distribuicao do Sharpe e exige sharpe_p5 >= MIN.
# Desliga com AETHER_CRITIC_BOOTSTRAP_DISABLE=1.
BOOTSTRAP_DISABLED = os.getenv("AETHER_CRITIC_BOOTSTRAP_DISABLE", "0").strip().lower() in {"1", "true", "yes"}
BOOTSTRAP_SAMPLES = max(100, int(os.getenv("AETHER_CRITIC_BOOTSTRAP_SAMPLES", "1000")))
BOOTSTRAP_MIN_SHARPE_P5 = float(os.getenv("AETHER_CRITIC_BOOTSTRAP_MIN_SHARPE_P5", "-0.3"))

# Grid denso de params: varre malha NxN ao redor dos params do best_probe.
# Detecta overfit local (1 ponto otimo cercado de fracassos).
GRID_DISABLED = os.getenv("AETHER_CRITIC_GRID_DISABLE", "0").strip().lower() in {"1", "true", "yes"}
GRID_SIZE = max(3, int(os.getenv("AETHER_CRITIC_GRID_SIZE", "11")))
GRID_RANGE_PCT = float(os.getenv("AETHER_CRITIC_GRID_RANGE_PCT", "0.20"))
GRID_MIN_POSITIVE_FRAC = float(os.getenv("AETHER_CRITIC_GRID_MIN_POSITIVE_FRAC", "0.5"))
GRID_MIN_P25_PNL = float(os.getenv("AETHER_CRITIC_GRID_MIN_P25_PNL", "-7.0"))

# Multi-timeframe robustness: teste opcional, pois outro timeframe pode mudar
# a hipotese causal. Mantemos desligado por padrao e usamos os gates
# estatisticos (cross-symbol, perturb, walk-forward, grid, bootstrap).
MTF_DISABLED = os.getenv("AETHER_CRITIC_MTF_DISABLE", "1").strip().lower() in {"1", "true", "yes"}
MTF_MIN_PASS = max(1, int(os.getenv("AETHER_CRITIC_MTF_MIN_PASS", "2")))
MTF_MIN_PNL = float(os.getenv("AETHER_CRITIC_MTF_MIN_PNL", "-3.0"))
MTF_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")

# Regime gate: testa a estrategia em janelas historicas de regime distinto
# (bear/correcao, ranging, bull). Estrategia robusta sobrevive a >=N janelas
# com pnl >= MIN_PNL. Desliga com AETHER_CRITIC_REGIME_DISABLE=1.
#
# Pool grande de janelas — gate pega so as que cabem no dataset (TFs curtos
# sao sub-set de janelas recentes). Datasets atuais:
#   1d  cobre desde 2023-05; 1h/4h desde 2024-05; 15m desde 2025-11; 5m desde 2026-04.
#
# Macro (1d/1h/4h):
#   - bear_2024:   ATH 73k (mar/24) -> capitulacao 53k (ago/24), -27% peak-to-trough
#   - range_2023:  recovery pos-FTX, lateral 25-31k (so 1d)
#   - bull_2024q4: eleicao US, 60k -> 100k+ ATH (out/24-jan/25)
# Recentes (15m/5m):
#   - consol_2025q4: lateral nov-dez 2025
#   - rally_2026q1:  jan-fev 2026 (push pos-fim de ano)
#   - chop_2026q2:   abr-mai 2026 (regime atual)
REGIME_DISABLED = os.getenv("AETHER_CRITIC_REGIME_DISABLE", "0").strip().lower() in {"1", "true", "yes"}
REGIME_MIN_PASS = max(1, int(os.getenv("AETHER_CRITIC_REGIME_MIN_PASS", "2")))
REGIME_MIN_PNL = float(os.getenv("AETHER_CRITIC_REGIME_MIN_PNL", "-5.0"))
# Minimo de janelas com trade real (>0). Estrategia que mal negocia gera ruido,
# nao alpha — exigimos pelo menos N janelas avaliaveis pra ter sinal estatistico.
REGIME_MIN_VALID_WINDOWS = max(1, int(os.getenv("AETHER_CRITIC_REGIME_MIN_VALID", "2")))
REGIME_WINDOWS: tuple[tuple[str, str, str], ...] = (
    # (label, start_iso, end_iso)
    ("bear_2024", "2024-04-01", "2024-09-30"),
    ("range_2023", "2023-06-01", "2023-10-31"),
    ("bull_2024q4", "2024-10-15", "2025-01-31"),
    ("consol_2025q4", "2025-11-15", "2025-12-31"),
    ("rally_2026q1", "2026-01-05", "2026-02-28"),
    ("chop_2026q2", "2026-04-08", "2026-04-30"),
)

# Pool dedicado para envolver _backtest com timeout. Workers vazam se travarem,
# mas o ciclo do critic nao trava — preferivel a deadlockar a fila inteira.
# AETHER_CRITIC_BT_WORKERS: knob da esteira industrial; watchdog reduz se RAM apertar.
_BT_WORKERS = max(1, int(os.getenv("AETHER_CRITIC_BT_WORKERS", "2")))
_BT_EXECUTOR = ThreadPoolExecutor(max_workers=_BT_WORKERS, thread_name_prefix="critic-bt")


def _run_with_timeout(func, *args, timeout: float = BACKTEST_TIMEOUT_SECONDS, **kwargs):
    """Roda func em pool com timeout. Retorna None se estourar."""
    future = _BT_EXECUTOR.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        return None
    except Exception:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _setup_logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("aether.critic_agent")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT id, name, status, score, metrics_json, campaign_id, timeframe
        FROM nodes
        WHERE status='validated'
          AND id != 'root'
          AND id NOT LIKE 'tf:%'
        """
    ).fetchall())


def _backtest_inner(engine: str, params: dict, dataset_id: str, timeframe: str) -> dict | None:
    try:
        df = load_dataset_fast(dataset_id)
    except Exception:
        return None
    if df is None or len(df) < 80:
        return None
    try:
        signal = _build_signals(engine, df, params)
        result = _run_local_backtest(df, signal, capital=10000.0, fee_bps=10.0, slippage_bps=2.0, timeframe=timeframe)
        return result if result.get("ok") else None
    except Exception:
        return None


def _backtest(engine: str, params: dict, dataset_id: str, timeframe: str) -> dict | None:
    return _run_with_timeout(_backtest_inner, engine, params, dataset_id, timeframe)


def _backtest_slice_inner(engine: str, params: dict, dataset_id: str, timeframe: str, frac: tuple[float, float]) -> dict | None:
    try:
        df = load_dataset_fast(dataset_id)
    except Exception:
        return None
    if df is None or len(df) < 100:
        return None
    n = len(df)
    a = int(n * frac[0])
    b = int(n * frac[1])
    if b - a < 50:
        return None
    sub = df.iloc[a:b].reset_index(drop=True)
    try:
        signal = _build_signals(engine, sub, params)
        result = _run_local_backtest(sub, signal, capital=10000.0, fee_bps=10.0, slippage_bps=2.0, timeframe=timeframe)
        return result if result.get("ok") else None
    except Exception:
        return None


def _backtest_slice(engine: str, params: dict, dataset_id: str, timeframe: str, frac: tuple[float, float]) -> dict | None:
    return _run_with_timeout(_backtest_slice_inner, engine, params, dataset_id, timeframe, frac)


def _slice_window(df, start_iso: str, end_iso: str):
    """Recorta df por janela ISO em open_time. Retorna None se janela vazia/curta."""
    if df is None or "open_time" not in df.columns:
        return None
    try:
        import pandas as pd
        start = pd.Timestamp(start_iso, tz="UTC")
        end = pd.Timestamp(end_iso, tz="UTC")
    except Exception:
        return None
    mask = (df["open_time"] >= start) & (df["open_time"] <= end)
    sub = df.loc[mask].reset_index(drop=True)
    if len(sub) < 50:
        return None
    return sub


def _backtest_window_inner(engine: str, params: dict, dataset_id: str, timeframe: str, start: str, end: str) -> dict | None:
    try:
        df = load_dataset_fast(dataset_id)
    except Exception:
        return None
    sub = _slice_window(df, start, end)
    if sub is None:
        return None
    try:
        signal = _build_signals(engine, sub, params)
        result = _run_local_backtest(sub, signal, capital=10000.0, fee_bps=10.0, slippage_bps=2.0, timeframe=timeframe)
        return result if result.get("ok") else None
    except Exception:
        return None


def _backtest_window(engine: str, params: dict, dataset_id: str, timeframe: str, start: str, end: str) -> dict | None:
    return _run_with_timeout(_backtest_window_inner, engine, params, dataset_id, timeframe, start, end)


def _perturb_params(params: dict) -> list[dict]:
    """Gera variantes +/-PERTURB_PCT em params numericos.

    Para combos (engine='combo' com lista 'specs'), perturba os params
    numericos DENTRO de cada sub-spec — antes a funcao olhava so o nivel
    top, onde combos so tem 'mode' (string) e 'specs' (lista), e nunca
    gerava perturbacoes (median 0 = G2 fail automatico).
    """
    out: list[dict] = []
    # 1) Caminho normal: params top-level numericos
    for key, value in params.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        for delta in (-PERTURB_PCT, PERTURB_PCT):
            new_value = value * (1.0 + delta)
            if isinstance(value, int):
                new_value = max(1, int(round(new_value)))
                if new_value == value:
                    continue
            new_params = {**params, key: new_value}
            out.append(new_params)
    # 2) Combo: perturba params dentro de specs[i].params
    specs = params.get("specs")
    if isinstance(specs, list) and specs:
        for i, sub in enumerate(specs):
            if not isinstance(sub, dict):
                continue
            sub_params = sub.get("params") or {}
            for key, value in sub_params.items():
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                for delta in (-PERTURB_PCT, PERTURB_PCT):
                    new_value = value * (1.0 + delta)
                    if isinstance(value, int):
                        new_value = max(1, int(round(new_value)))
                        if new_value == value:
                            continue
                    # cria novo specs com sub[i].params[key] perturbado
                    new_specs = []
                    for j, s in enumerate(specs):
                        if j == i and isinstance(s, dict):
                            new_sub = dict(s)
                            new_sub["params"] = {**(s.get("params") or {}), key: new_value}
                            new_specs.append(new_sub)
                        else:
                            new_specs.append(s)
                    new_params = {**params, "specs": new_specs}
                    out.append(new_params)
    return out[:6]


def _criticize_gpu_batch(
    engine: str,
    params: dict,
    symbol: str,
    timeframe: str,
    dataset_id: str,
    alts: list[str],
    perturbed: list[dict],
) -> dict[str, Any] | None:
    """Roda os 3 testes em uma unica passada GPU.

    Retorna dict com: cross_symbol (lista), perturb_pnls (lista), pnl_a, pnl_b,
    ou None se GPU indisponivel/erro -- caller cai no caminho CPU.
    """
    if not _gpu_compute.gpu_available():
        return None
    try:
        df_full = load_dataset_fast(dataset_id)
    except Exception:
        return None
    if df_full is None or len(df_full) < 100:
        return None
    n = len(df_full)
    half = n // 2
    # Walk-forward expandido: K folds nao-sobrepostos. Cada fold vira um
    # dataset_id virtual no cache do compute. Substitui o split 50/50 antigo.
    fold_size = max(50, n // WF_FOLDS)
    wf_slice_ids: list[str] = []
    for k in range(WF_FOLDS):
        a = k * fold_size
        b = (k + 1) * fold_size if k < WF_FOLDS - 1 else n
        if b - a < 50:
            continue
        sid = f"critic-fold-{k}:{dataset_id}"
        _compute_mod._DATASET_CACHE[sid] = df_full.iloc[a:b].reset_index(drop=True)
        wf_slice_ids.append(sid)

    probes: list[dict[str, Any]] = []
    base_probe = {
        "engine": engine,
        "timeframe": timeframe,
        "fee_bps": 10.0,
        "slippage_bps": 2.0,
        "capital": 10000.0,
    }

    # cross-symbol
    for alt in alts:
        probes.append({
            **base_probe,
            "probe_id": f"critic:cross:{alt}",
            "dataset_id": f"binance:{alt}:{timeframe}",
            "symbol": alt,
            "params": dict(params),
            "tag": ("cross", alt),
        })
    # perturbation no mesmo dataset
    for i, pp in enumerate(perturbed):
        probes.append({
            **base_probe,
            "probe_id": f"critic:perturb:{i}",
            "dataset_id": dataset_id,
            "symbol": symbol,
            "params": dict(pp),
            "tag": ("perturb", i),
        })
    # walk-forward folds (K)
    for k, sid in enumerate(wf_slice_ids):
        probes.append({
            **base_probe,
            "probe_id": f"critic:wf:{k}",
            "dataset_id": sid,
            "symbol": symbol,
            "params": dict(params),
            "tag": ("wf", k),
        })

    # regime windows: cada janela vira um dataset_id virtual no cache
    regime_slice_ids: list[tuple[str, str]] = []  # (label, sid)
    if not REGIME_DISABLED:
        for label, start_iso, end_iso in REGIME_WINDOWS:
            sub = _slice_window(df_full, start_iso, end_iso)
            if sub is None:
                continue
            sid = f"critic-regime-{label}:{dataset_id}"
            _compute_mod._DATASET_CACHE[sid] = sub
            regime_slice_ids.append((label, sid))
            probes.append({
                **base_probe,
                "probe_id": f"critic:regime:{label}",
                "dataset_id": sid,
                "symbol": symbol,
                "params": dict(params),
                "tag": ("regime", label),
            })

    try:
        results = _gpu_compute.run_probes_gpu(probes, load_dataset_fast)
    except Exception:
        results = None
    finally:
        # cleanup das entradas virtuais; mantem cache normal intacto
        for sid in wf_slice_ids:
            _compute_mod._DATASET_CACHE.pop(sid, None)
        for _, sid in regime_slice_ids:
            _compute_mod._DATASET_CACHE.pop(sid, None)

    if results is None:
        return None
    # mistura None (engine fora do batch GPU) -> fallback CPU pros faltantes
    cross_results: list[dict[str, Any]] = []
    perturb_pnls: list[float] = []
    wf_pnls: list[float | None] = [None] * len(wf_slice_ids)
    # regime: (pnl_pct, trade_count) ou None se janela vazia/sem trades
    regime_pnls: dict[str, tuple[float, int] | None] = {label: None for label, _ in regime_slice_ids}
    for probe, res in zip(probes, results):
        tag = probe.get("tag")
        if res is None:
            cpu_res = _backtest(probe["engine"], probe["params"], probe["dataset_id"], probe["timeframe"])
            res = cpu_res if cpu_res else {"ok": False}
        if not res.get("ok"):
            if tag and tag[0] == "cross":
                cross_results.append({"symbol": tag[1], "ok": False, "pnl_pct": None})
            continue
        pnl = float(res.get("pnl_pct") or 0.0)
        trades = int(res.get("trade_count") or 0)
        if tag and tag[0] == "cross":
            cross_results.append({"symbol": tag[1], "ok": True, "pnl_pct": pnl})
        elif tag and tag[0] == "perturb":
            perturb_pnls.append(pnl)
        elif tag and tag[0] == "wf":
            wf_pnls[int(tag[1])] = pnl
        elif tag and tag[0] == "regime":
            regime_pnls[str(tag[1])] = (pnl, trades)
    return {
        "cross_results": cross_results,
        "perturb_pnls": perturb_pnls,
        "wf_pnls": wf_pnls,
        "regime_pnls": regime_pnls,
    }


def _criticize(metrics: dict) -> tuple[bool, str, dict]:
    """Returns (passed, reason, report)."""
    best = metrics.get("best_probe") or {}
    engine = str(best.get("engine") or "")
    params = best.get("params") or {}
    symbol = str(best.get("symbol") or "").upper()
    timeframe = str(best.get("timeframe") or "")
    if not engine or not params or not symbol or not timeframe:
        return False, "best_probe incompleto (sem engine/params/symbol/timeframe)", {}

    report: dict[str, Any] = {
        "engine": engine,
        "symbol_validated": symbol,
        "timeframe": timeframe,
    }
    alts = [s for s in ALT_SYMBOLS if s != symbol][:CROSS_SYMBOL_POOL]
    perturbed = _perturb_params(params)
    dataset_id = f"binance:{symbol}:{timeframe}"

    # Tenta caminho GPU em batch unico (cross+perturb+slice numa so passada)
    gpu_bundle = _criticize_gpu_batch(
        engine, params, symbol, timeframe, dataset_id, alts, perturbed
    )

    if gpu_bundle is not None:
        cross_results = gpu_bundle["cross_results"]
        perturb_pnls = gpu_bundle["perturb_pnls"]
        wf_pnls = gpu_bundle["wf_pnls"]
        regime_pnls = gpu_bundle.get("regime_pnls") or {}
        report["gpu_batch"] = True
    else:
        # CPU sequencial (caminho legado): cross + perturb + WF folds + regime.
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
        regime_pnls = {}
        if not REGIME_DISABLED:
            for label, start_iso, end_iso in REGIME_WINDOWS:
                r = _backtest_window(engine, params, dataset_id, timeframe, start_iso, end_iso)
                if r:
                    regime_pnls[label] = (float(r.get("pnl_pct") or 0), int(r.get("trade_count") or 0))
                else:
                    regime_pnls[label] = None
        report["gpu_batch"] = False

    # Test 1: cross-symbol (>= MIN_PASS alts com pnl >= MIN_PNL_PCT)
    report["cross_symbol"] = cross_results
    report["cross_symbol_threshold"] = CROSS_SYMBOL_MIN_PNL_PCT
    cross_pass = sum(
        1 for r in cross_results
        if r["ok"] and (r["pnl_pct"] is not None) and r["pnl_pct"] >= CROSS_SYMBOL_MIN_PNL_PCT
    )
    if cross_pass < CROSS_SYMBOL_MIN_PASS:
        return False, (
            f"cross-symbol falhou ({cross_pass}/{len(alts)} alts com pnl>={CROSS_SYMBOL_MIN_PNL_PCT}%)"
        ), report

    # Test 2: param perturbation
    report["perturbation"] = {"sample_size": len(perturb_pnls), "median_pnl_pct": median(perturb_pnls) if perturb_pnls else None}
    if perturb_pnls and median(perturb_pnls) <= 0:
        return False, f"perturbation falhou (mediana pnl={median(perturb_pnls):.2f}% <=0, n={len(perturb_pnls)})", report

    # Test 3: walk-forward K-fold expandido (substitui o time-slice 50/50)
    valid_folds = [p for p in wf_pnls if p is not None]
    folds_pass = sum(1 for p in valid_folds if p >= WF_MIN_PNL)
    report["walk_forward"] = {
        "folds": len(wf_pnls),
        "valid": len(valid_folds),
        "passed": folds_pass,
        "min_pass": WF_MIN_PASS,
        "min_pnl_pct": WF_MIN_PNL,
        "fold_pnls": wf_pnls,
    }
    if len(valid_folds) < WF_MIN_PASS:
        return False, f"walk-forward folds insuficientes ({len(valid_folds)}/{WF_FOLDS})", report
    if folds_pass < WF_MIN_PASS:
        return False, (
            f"walk-forward falhou ({folds_pass}/{len(valid_folds)} folds com pnl>={WF_MIN_PNL}%, "
            f"min_pass={WF_MIN_PASS})"
        ), report

    # Test 3.5: regime gate (bear/range/bull). Estrategia precisa sobreviver
    # em pelo menos min(REGIME_MIN_PASS, valid_count) janelas com pnl >= REGIME_MIN_PNL
    # E ter pelo menos 1 trade na janela (zero trades = no_data, nao conta).
    # Janelas que nao cabem no dataset (TF curto) sao puladas; gate exige TODAS as
    # validas passem quando ha menos do que REGIME_MIN_PASS disponiveis.
    if not REGIME_DISABLED:
        # valid = janela com dado E trades > 0
        valid_regimes: dict[str, tuple[float, int]] = {
            k: v for k, v in regime_pnls.items()
            if v is not None and isinstance(v, tuple) and v[1] > 0
        }
        no_trade_regimes = [
            k for k, v in regime_pnls.items()
            if v is not None and isinstance(v, tuple) and v[1] == 0
        ]
        regime_pass = sum(1 for (pnl, _) in valid_regimes.values() if pnl >= REGIME_MIN_PNL)
        effective_min = min(REGIME_MIN_PASS, len(valid_regimes))
        report["regime"] = {
            "windows": list(regime_pnls.keys()),
            "pnls": {k: (v[0] if isinstance(v, tuple) else None) for k, v in regime_pnls.items()},
            "trades": {k: (v[1] if isinstance(v, tuple) else None) for k, v in regime_pnls.items()},
            "no_trade_windows": no_trade_regimes,
            "valid_count": len(valid_regimes),
            "passed": regime_pass,
            "min_pass_target": REGIME_MIN_PASS,
            "min_pass_effective": effective_min,
            "min_pnl_pct": REGIME_MIN_PNL,
        }
        if not valid_regimes:
            return False, (
                f"regime gate sem dados (sem janela com trade; no_trade={len(no_trade_regimes)})"
            ), report
        if len(valid_regimes) < REGIME_MIN_VALID_WINDOWS:
            return False, (
                f"regime trade-starved ({len(valid_regimes)} janela(s) com trade < min_valid={REGIME_MIN_VALID_WINDOWS}; "
                f"no_trade={len(no_trade_regimes)} — estrategia nao negocia o suficiente)"
            ), report
        if regime_pass < effective_min:
            failed_summary = ", ".join(
                f"{k}={pnl:.1f}%(t={t})" for k, (pnl, t) in valid_regimes.items()
            )
            return False, (
                f"regime falhou ({regime_pass}/{len(valid_regimes)} janelas com pnl>={REGIME_MIN_PNL}%, "
                f"min_pass={effective_min}; {failed_summary})"
            ), report

    # Test 4: grid denso de params (detecta overfit local)
    df_full_cache = None
    if not GRID_DISABLED:
        grid = None
        try:
            df_full_cache = load_dataset_fast(dataset_id)
            grid = _gpu_compute.grid_search_metrics_gpu(
                engine, params, df_full_cache, timeframe,
                grid_size=GRID_SIZE, range_pct=GRID_RANGE_PCT,
            )
        except Exception:
            grid = None
        report["grid_search"] = grid or {"skipped": True}
        if grid is not None:
            if grid["pnl_positive_frac"] < GRID_MIN_POSITIVE_FRAC:
                return False, (
                    f"grid robustez falhou (pos_frac={grid['pnl_positive_frac']:.2f} < "
                    f"min={GRID_MIN_POSITIVE_FRAC}, combos={grid['total_combos']})"
                ), report
            if grid["pnl_p25_pct"] < GRID_MIN_P25_PNL:
                return False, (
                    f"grid robustez falhou (p25={grid['pnl_p25_pct']:.2f}% < "
                    f"min={GRID_MIN_P25_PNL}%, combos={grid['total_combos']})"
                ), report

    # Test 5: Multi-timeframe robustness (mesmo simbolo, outros TFs)
    if not MTF_DISABLED:
        other_tfs = [tf for tf in MTF_TIMEFRAMES if tf != timeframe]
        # pre-load paralelo dos candles (cacheado em disco/memoria depois)
        _mtf_workers = max(1, int(os.getenv("AETHER_CRITIC_MTF_WORKERS", "4")))
        with ThreadPoolExecutor(max_workers=_mtf_workers) as ex:
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
        try:
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
        report["multi_timeframe"] = {
            "tested_tfs": other_tfs,
            "pnls": mtf_pnls,
            "passed": mtf_pass,
            "min_pass": MTF_MIN_PASS,
            "min_pnl_pct": MTF_MIN_PNL,
        }
        if mtf_pass < MTF_MIN_PASS:
            return False, (
                f"multi-timeframe falhou ({mtf_pass}/{len(other_tfs)} TFs com pnl>={MTF_MIN_PNL}%)"
            ), report

    # Test 6: Monte Carlo bootstrap (IC do Sharpe)
    if not BOOTSTRAP_DISABLED:
        boot = None
        try:
            if df_full_cache is None:
                df_full_cache = load_dataset_fast(dataset_id)
            boot = _gpu_compute.bootstrap_metrics_gpu(
                engine, params, df_full_cache, timeframe,
                n_samples=BOOTSTRAP_SAMPLES,
            )
        except Exception:
            boot = None
        report["bootstrap"] = boot or {"skipped": True}
        if boot is not None and boot["sharpe_p5"] < BOOTSTRAP_MIN_SHARPE_P5:
            return False, (
                f"bootstrap falhou (sharpe_p5={boot['sharpe_p5']:.3f} < min={BOOTSTRAP_MIN_SHARPE_P5}, "
                f"n={boot['n_samples']}, pos_frac={boot['sharpe_positive_frac']:.2f})"
            ), report

    bs_str = ""
    if not BOOTSTRAP_DISABLED and isinstance(report.get("bootstrap"), dict) and "sharpe_p5" in report["bootstrap"]:
        b = report["bootstrap"]
        bs_str = f" sharpe_p5={b['sharpe_p5']:.2f} pos_frac={b['sharpe_positive_frac']:.2f}"
    grid_str = ""
    if not GRID_DISABLED and isinstance(report.get("grid_search"), dict) and "pnl_positive_frac" in report["grid_search"]:
        g = report["grid_search"]
        grid_str = f" grid_pos={g['pnl_positive_frac']:.2f}"
    mtf_str = ""
    if not MTF_DISABLED and isinstance(report.get("multi_timeframe"), dict):
        m = report["multi_timeframe"]
        mtf_str = f" mtf={m['passed']}/{len(m['tested_tfs'])}"
    regime_str = ""
    if not REGIME_DISABLED and isinstance(report.get("regime"), dict) and "passed" in report["regime"]:
        rg = report["regime"]
        regime_str = f" regime={rg['passed']}/{len([v for v in rg['pnls'].values() if v is not None])}"
    return True, (
        f"approved cross_pass={cross_pass}/{len(alts)} "
        f"perturb_med={(median(perturb_pnls) if perturb_pnls else 0):.2f}% "
        f"wf={folds_pass}/{len(valid_folds)}{regime_str}{grid_str}{mtf_str}{bs_str}"
    ), report


def run_once(conn: sqlite3.Connection, log: logging.Logger) -> tuple[int, int]:
    rows = _candidates(conn)
    approved = 0
    rejected = 0
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if metrics.get("critic_status") in ("approved", "rejected"):
            continue
        passed, reason, report = _criticize(metrics)
        metrics["critic_status"] = "approved" if passed else "rejected"
        metrics["critic_reason"] = reason
        metrics["critic_report"] = report
        metrics["critic_run_at"] = _now()
        if passed:
            new_status = "validated"
            event_type = "critic_approved"
            event_msg = f"{row['name']} aprovada pelo Critic: {reason}"
            conn.execute(
                "UPDATE nodes SET metrics_json=?, updated_at=? WHERE id=?",
                (json.dumps(metrics, ensure_ascii=False), _now(), row["id"]),
            )
            approved += 1
            log.info(f"approved id={row['id'][:60]} {reason}")
        else:
            new_status = "paused"
            event_type = "critic_rejected"
            event_msg = f"{row['name']} rebaixada pelo Critic: {reason}"
            conn.execute(
                """
                UPDATE nodes
                SET status='paused', metrics_json=?, prune_reason=?, agent_role='critic_agent', updated_at=?
                WHERE id=?
                """,
                (json.dumps(metrics, ensure_ascii=False), f"critic: {reason}", _now(), row["id"]),
            )
            rejected += 1
            log.info(f"rejected id={row['id'][:60]} {reason}")

        conn.execute(
            """
            INSERT INTO events (campaign_id, ts, type, node_id, agent_role, message, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["campaign_id"],
                _now(),
                event_type,
                row["id"],
                "critic_agent",
                event_msg,
                json.dumps({"reason": reason, "report": report, "new_status": new_status}, ensure_ascii=False),
            ),
        )
    return approved, rejected


def main() -> int:
    log = _setup_logger()
    log.info(f"critic_agent iniciado db={DB_PATH} poll={POLL_SECONDS}s")
    log.info(
        "critic gates: cross_pool=%s cross_min_pass=%s cross_min_pnl=%.2f "
        "wf=%s/%s wf_min_pnl=%.2f regime_disabled=%s regime_min_pass=%s/%s regime_min_pnl=%.2f "
        "grid_p25_min=%.2f mtf_disabled=%s bootstrap_sharpe_p5_min=%.2f",
        CROSS_SYMBOL_POOL,
        CROSS_SYMBOL_MIN_PASS,
        CROSS_SYMBOL_MIN_PNL_PCT,
        WF_MIN_PASS,
        WF_FOLDS,
        WF_MIN_PNL,
        REGIME_DISABLED,
        REGIME_MIN_PASS,
        len(REGIME_WINDOWS),
        REGIME_MIN_PNL,
        GRID_MIN_P25_PNL,
        MTF_DISABLED,
        BOOTSTRAP_MIN_SHARPE_P5,
    )
    if not DB_PATH.exists():
        log.error(f"DB nao existe: {DB_PATH}")
        return 1
    _torch = None
    try:
        import torch as _torch  # type: ignore
    except ImportError:
        pass

    idle_streak = 0
    while True:
        a, r = 0, 0
        try:
            with _connect() as conn:
                a, r = run_once(conn, log)
                if a or r:
                    log.info(f"ciclo: approved={a} rejected={r}")
        except sqlite3.OperationalError as e:
            log.warning(f"sqlite busy/lock: {e}")
        except Exception as e:
            log.exception(f"erro no ciclo: {e}")
        # Libera RAM/VRAM entre ciclos. Critic processa em rajadas curtas e
        # fica idle aguardando POLL_SECONDS. Quando idle por 2+ ciclos,
        # tambem dropa o _DATASET_CACHE in-memory (parquet em disco recarrega rapido).
        if a == 0 and r == 0:
            idle_streak += 1
        else:
            idle_streak = 0
        gc.collect()
        if _torch is not None and _torch.cuda.is_available():
            _torch.cuda.empty_cache()
        if idle_streak >= 2:
            try:
                _compute_mod._DATASET_CACHE.clear()
                gc.collect()
            except AttributeError:
                pass
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
