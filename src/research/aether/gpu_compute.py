"""
Backtest vetorizado em GPU (PyTorch) para o Aether.

Substitui o caminho CPU em `compute._run_local_backtest` quando a GPU esta
disponivel. Agrupa probes do MESMO (dataset_id, timeframe) num unico kernel:
empilha (probes x candles) numa matriz, calcula sinais e equity em batch e
devolve a mesma estrutura que o caminho CPU.

API publica:
    gpu_available() -> bool
    run_probes_gpu(probes, df_loader) -> list[dict] | None

`run_probes_gpu` devolve None se a GPU falhou ou esta desabilitada -- o
caller deve cair de volta no caminho CPU.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any, Callable

import numpy as np
import pandas as pd


_TORCH = None
_DEVICE = None
_DISABLED = os.environ.get("AETHER_GPU_DISABLE", "0").strip().lower() in {"1", "true", "yes"}

# Cache de tensores base por dataset (close/high/low/tr/rets ja no device).
# Evita H2D transfer redundante e recomputo de TR/returns entre probes/engines.
# Chave: (dataset_id, len(df), tail_close) -- tail_close serve como invalidator
# barato quando o dataset cache do compute.py renova candles.
_GPU_TENSOR_CACHE: dict[tuple[str, int, float], dict[str, Any]] = {}
_GPU_ROLLING_CACHE: dict[tuple[str, int, float, str, int], Any] = {}
_GPU_CACHE_MAX = int(os.environ.get("AETHER_GPU_CACHE_MAX", "32"))


def _evict_cache_if_full() -> None:
    if len(_GPU_TENSOR_CACHE) > _GPU_CACHE_MAX:
        _GPU_TENSOR_CACHE.clear()
        _GPU_ROLLING_CACHE.clear()
        torch_mod = _TORCH if _TORCH not in (None, False) else None
        if torch_mod is not None:
            try:
                torch_mod.cuda.empty_cache()
            except Exception:
                pass


def _dataset_key(dsid: str, df: pd.DataFrame) -> tuple[str, int, float]:
    n = int(len(df))
    tail = float(df["close"].iloc[-1]) if n > 0 else 0.0
    return (dsid, n, tail)


def _ensure_dataset_tensors(dsid: str, df: pd.DataFrame) -> dict[str, Any] | None:
    """Sobe close/high/low/tr/returns pro device uma vez por dataset."""
    torch, device = _ensure_torch()
    if torch is None or torch is False:
        return None
    key = _dataset_key(dsid, df)
    cached = _GPU_TENSOR_CACHE.get(key)
    if cached is not None:
        return cached
    _evict_cache_if_full()
    close_np = df["close"].astype(np.float32).to_numpy()
    high_np = df["high"].astype(np.float32).to_numpy()
    low_np = df["low"].astype(np.float32).to_numpy()
    close = torch.from_numpy(close_np).to(device).unsqueeze(0)
    high = torch.from_numpy(high_np).to(device).unsqueeze(0)
    low = torch.from_numpy(low_np).to(device).unsqueeze(0)
    N = close.shape[-1]
    tr = torch.zeros((1, N), device=device, dtype=close.dtype)
    if N > 0:
        tr[:, 0] = high[:, 0] - low[:, 0]
    if N > 1:
        a = (high[:, 1:] - low[:, 1:]).abs()
        b = (high[:, 1:] - close[:, :-1]).abs()
        c = (low[:, 1:] - close[:, :-1]).abs()
        tr[:, 1:] = torch.maximum(torch.maximum(a, b), c)
    rets = torch.zeros_like(close)
    if N > 1:
        rets[:, 1:] = (close[:, 1:] - close[:, :-1]) / close[:, :-1]
    rets = torch.nan_to_num(rets, nan=0.0, posinf=0.0, neginf=0.0)
    delta = torch.zeros_like(close)
    if N > 1:
        delta[:, 1:] = close[:, 1:] - close[:, :-1]
    gain = delta.clamp(min=0.0)
    loss = (-delta).clamp(min=0.0)
    entry = {
        "key": key,
        "close": close,
        "high": high,
        "low": low,
        "tr": tr,
        "rets": rets,
        "gain": gain,
        "loss": loss,
    }
    _GPU_TENSOR_CACHE[key] = entry
    return entry


def _cached_rolling(
    dsid_key: tuple[str, int, float],
    name: str,
    period: int,
    compute_fn,
):
    """Cache LRU-light de rolling/EWM por (dataset, kind, period)."""
    cache_key = (*dsid_key, name, int(period))
    cached = _GPU_ROLLING_CACHE.get(cache_key)
    if cached is not None:
        return cached
    val = compute_fn()
    _GPU_ROLLING_CACHE[cache_key] = val
    return val


def _ensure_torch():
    global _TORCH, _DEVICE
    if _DISABLED:
        return None, None
    if _TORCH is not None:
        return _TORCH, _DEVICE
    try:
        import torch  # type: ignore
    except Exception:
        _TORCH = False
        return None, None
    try:
        if not torch.cuda.is_available():
            _TORCH = False
            return None, None
        _DEVICE = torch.device("cuda")
        _TORCH = torch
        return torch, _DEVICE
    except Exception:
        _TORCH = False
        return None, None


def gpu_available() -> bool:
    torch, _ = _ensure_torch()
    return torch is not None and torch is not False


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


# ---------------------------------------------------------------------------
# Helpers GPU: rolling/ewm em torch.
# ---------------------------------------------------------------------------

def _rolling_mean(t, window: int):
    torch, _ = _TORCH, _DEVICE
    if window <= 1:
        return t.clone()
    pad = window - 1
    cs = torch.cumsum(t, dim=-1)
    out = torch.empty_like(t)
    out[..., :pad] = float("nan")
    out[..., pad:] = (cs[..., window - 1 :] - torch.cat(
        [torch.zeros_like(cs[..., :1]), cs[..., : -window + 0 if window > 1 else None]], dim=-1
    )[..., : t.shape[-1] - pad]) / window
    return out


def _rolling_mean_simple(t, window: int):
    """Rolling mean usando conv1d (mais estavel numericamente que cumsum)."""
    torch, _ = _TORCH, _DEVICE
    if window <= 1:
        return t.clone()
    # t: (B, N)
    B, N = t.shape
    kernel = torch.ones(1, 1, window, device=t.device, dtype=t.dtype) / window
    x = t.unsqueeze(1)  # (B,1,N)
    y = torch.nn.functional.conv1d(x, kernel, padding=0).squeeze(1)  # (B, N-window+1)
    out = torch.full((B, N), float("nan"), device=t.device, dtype=t.dtype)
    out[:, window - 1 :] = y
    return out


def _rolling_std(t, window: int):
    torch, _ = _TORCH, _DEVICE
    if window <= 1:
        return torch.zeros_like(t)
    mean = _rolling_mean_simple(t, window)
    sq = _rolling_mean_simple(t * t, window)
    var = (sq - mean * mean).clamp(min=0.0)
    return torch.sqrt(var)


def _rolling_max(t, window: int):
    torch, _ = _TORCH, _DEVICE
    if window <= 1:
        return t.clone()
    B, N = t.shape
    out = torch.full((B, N), float("nan"), device=t.device, dtype=t.dtype)
    # unfold sliding windows: (B, N-window+1, window)
    if N >= window:
        u = t.unfold(-1, window, 1)
        out[:, window - 1 :] = u.max(dim=-1).values
    return out


def _rolling_min(t, window: int):
    torch, _ = _TORCH, _DEVICE
    if window <= 1:
        return t.clone()
    B, N = t.shape
    out = torch.full((B, N), float("nan"), device=t.device, dtype=t.dtype)
    if N >= window:
        u = t.unfold(-1, window, 1)
        out[:, window - 1 :] = u.min(dim=-1).values
    return out


def _ewm_alpha(t, alpha: float, min_periods: int):
    """EWM com adjust=False, equivalente ao pandas ewm(alpha=...).mean()."""
    torch, _ = _TORCH, _DEVICE
    B, N = t.shape
    out = torch.zeros_like(t)
    # iterar candles (loop curto O(N), mas em batch sobre B probes)
    out[:, 0] = t[:, 0]
    for i in range(1, N):
        out[:, i] = alpha * t[:, i] + (1.0 - alpha) * out[:, i - 1]
    if min_periods > 1:
        out[:, : min_periods - 1] = float("nan")
    return out


def _ffill_signal(sig):
    """Forward-fill de NaN -> mantem o sinal anterior. Trata 0 como 'sem sinal'.
    Aqui implementamos FF de zero: substitui 0s por ultimo nao-zero (com 0 inicial).
    Atua em uma matriz (B, N) onde sig in {-1, 0, 1}.
    """
    torch, _ = _TORCH, _DEVICE
    B, N = sig.shape
    out = sig.clone()
    # cumulative max-by-index hack: para cada posicao, ultimo indice onde sig != 0
    # idx contem o indice corrente onde sig != 0; se nunca achou, fica -1
    nonzero = (sig != 0).to(torch.int32)
    idx = torch.arange(N, device=sig.device, dtype=torch.int32).unsqueeze(0).expand(B, N) * nonzero
    # cummax para obter ultimo indice valido
    last_idx, _ = torch.cummax(idx, dim=-1)
    # se nunca houve nonzero ate agora, last_idx == 0 e sig[:,0] == 0 -> fica 0 mesmo
    out = sig.gather(1, last_idx.long())
    return out


# ---------------------------------------------------------------------------
# Geradores de sinais GPU em batch.
# Cada funcao recebe tensores OHLC (1, N) e uma lista de params (B itens),
# devolve sig (B, N) int8 em {-1,0,1}.
# ---------------------------------------------------------------------------

def _signals_ma_crossover_batch(close, params_list):
    torch, _ = _TORCH, _DEVICE
    B = len(params_list)
    N = close.shape[-1]
    out = torch.zeros((B, N), device=close.device, dtype=torch.float32)
    for i, p in enumerate(params_list):
        short = max(2, int(p.get("short_window", p.get("short", 20))))
        long = max(short + 1, int(p.get("long_window", p.get("long", 50))))
        long_only = bool(p.get("long_only", True))
        s = _rolling_mean_simple(close, short)
        l = _rolling_mean_simple(close, long)
        sig = torch.zeros_like(s)
        sig = torch.where((s > l), torch.ones_like(sig), sig)
        sig = torch.where((s < l), -torch.ones_like(sig), sig)
        if long_only:
            sig = sig.clamp(min=0)
        sig = torch.nan_to_num(sig, nan=0.0)
        out[i] = sig.squeeze(0)
    return out.to(torch.int8)


def _signals_bollinger_batch(close, params_list):
    torch, _ = _TORCH, _DEVICE
    B = len(params_list)
    N = close.shape[-1]
    out = torch.zeros((B, N), device=close.device, dtype=torch.float32)
    for i, p in enumerate(params_list):
        window = max(5, int(p.get("window", 20)))
        num_std = float(p.get("num_std", 2.0))
        long_only = bool(p.get("long_only", False))
        ma = _rolling_mean_simple(close, window)
        sd = _rolling_std(close, window)
        upper = ma + num_std * sd
        lower = ma - num_std * sd
        sig = torch.zeros_like(close)
        sig = torch.where(close < lower, torch.ones_like(sig), sig)
        sig = torch.where(close > upper, -torch.ones_like(sig), sig)
        if long_only:
            sig = sig.clamp(min=0)
        sig = torch.nan_to_num(sig, nan=0.0)
        out[i] = sig.squeeze(0)
    return out.to(torch.int8)


def _signals_rsi_batch(close, params_list, *, gain_cached=None, loss_cached=None, dsid_key=None):
    torch, _ = _TORCH, _DEVICE
    B = len(params_list)
    N = close.shape[-1]
    out = torch.zeros((B, N), device=close.device, dtype=torch.float32)
    if gain_cached is not None and loss_cached is not None:
        gain, loss = gain_cached, loss_cached
    else:
        delta = torch.zeros_like(close)
        delta[:, 1:] = close[:, 1:] - close[:, :-1]
        gain = delta.clamp(min=0.0)
        loss = (-delta).clamp(min=0.0)
    for i, p in enumerate(params_list):
        period = max(2, int(p.get("period", 14)))
        oversold = float(p.get("oversold", p.get("os", 30.0)))
        overbought = float(p.get("overbought", p.get("ob", 70.0)))
        long_only = bool(p.get("long_only", False))
        alpha = 1.0 / period
        if dsid_key is not None:
            avg_gain = _cached_rolling(dsid_key, "rsi_gain_ewm", period, lambda: _ewm_alpha(gain, alpha, period))
            avg_loss = _cached_rolling(dsid_key, "rsi_loss_ewm", period, lambda: _ewm_alpha(loss, alpha, period))
        else:
            avg_gain = _ewm_alpha(gain, alpha, period)
            avg_loss = _ewm_alpha(loss, alpha, period)
        rs = avg_gain / torch.where(avg_loss > 0, avg_loss, torch.full_like(avg_loss, float("nan")))
        rsi = 100 - (100 / (1 + rs))
        rsi = torch.nan_to_num(rsi, nan=50.0)
        sig = torch.zeros_like(close)
        sig = torch.where(rsi < oversold, torch.ones_like(sig), sig)
        sig = torch.where(rsi > overbought, -torch.ones_like(sig), sig)
        # ffill: replica logica do pandas (sig.replace(0, NaN).ffill().fillna(0))
        sig8 = sig.to(torch.int8)
        sig_ff = _ffill_signal(sig8)
        sig = sig_ff.to(close.dtype)
        if long_only:
            sig = sig.clamp(min=0)
        out[i] = sig.squeeze(0)
    return out.to(torch.int8)


def _signals_donchian_batch(high, low, close, params_list):
    """Stateful: tem que iterar bar a bar. Mantemos B em paralelo no eixo batch."""
    torch, _ = _TORCH, _DEVICE
    B = len(params_list)
    N = close.shape[-1]
    out = torch.zeros((B, N), device=close.device, dtype=torch.int8)
    # Pre-calcula upper/lower por probe (rolling max/min com window distinto)
    # B probes, N candles
    closes = close.expand(B, N)
    highs = high.expand(B, N)
    lows = low.expand(B, N)
    upper_e = torch.empty_like(closes)
    lower_e = torch.empty_like(closes)
    upper_x = torch.empty_like(closes)
    lower_x = torch.empty_like(closes)
    long_only_mask = torch.zeros(B, dtype=torch.bool, device=close.device)
    for i, p in enumerate(params_list):
        entry = max(2, int(p.get("entry_window", 20)))
        exit_w = max(2, int(p.get("exit_window", 10)))
        long_only_mask[i] = bool(p.get("long_only", True))
        upper_e[i : i + 1] = _rolling_max(highs[i : i + 1], entry)
        lower_e[i : i + 1] = _rolling_min(lows[i : i + 1], entry)
        upper_x[i : i + 1] = _rolling_max(highs[i : i + 1], exit_w)
        lower_x[i : i + 1] = _rolling_min(lows[i : i + 1], exit_w)
    pos = torch.zeros(B, device=close.device, dtype=torch.int8)
    for t in range(N):
        c = closes[:, t]
        ue = upper_e[:, t]
        le = lower_e[:, t]
        ux = upper_x[:, t]
        lx = lower_x[:, t]
        # entrada
        flat = pos == 0
        long_entry = flat & ~torch.isnan(ue) & (c >= ue)
        short_entry = flat & ~long_only_mask & ~torch.isnan(le) & (c <= le)
        pos = torch.where(long_entry, torch.ones_like(pos), pos)
        pos = torch.where(short_entry, -torch.ones_like(pos), pos)
        # saidas
        long_exit = (pos == 1) & ~torch.isnan(lx) & (c <= lx)
        short_exit = (pos == -1) & ~torch.isnan(ux) & (c >= ux)
        pos = torch.where(long_exit, torch.zeros_like(pos), pos)
        pos = torch.where(short_exit, torch.zeros_like(pos), pos)
        out[:, t] = pos
    return out


def _signals_supertrend_batch(high, low, close, params_list, *, tr_cached=None, dsid_key=None):
    torch, _ = _TORCH, _DEVICE
    B = len(params_list)
    N = close.shape[-1]
    out = torch.zeros((B, N), device=close.device, dtype=torch.int8)
    closes = close.expand(B, N)
    highs = high.expand(B, N)
    lows = low.expand(B, N)
    # TR (independente de period) -- reusa do cache se vier por parametro
    if tr_cached is not None:
        tr = tr_cached
    else:
        tr = torch.zeros((1, N), device=close.device, dtype=close.dtype)
        tr[:, 0] = high[:, 0] - low[:, 0]
        if N > 1:
            a = (high[:, 1:] - low[:, 1:]).abs()
            b = (high[:, 1:] - close[:, :-1]).abs()
            c = (low[:, 1:] - close[:, :-1]).abs()
            tr[:, 1:] = torch.maximum(torch.maximum(a, b), c)
    upper_b = torch.empty((B, N), device=close.device, dtype=close.dtype)
    lower_b = torch.empty((B, N), device=close.device, dtype=close.dtype)
    long_only_mask = torch.zeros(B, dtype=torch.bool, device=close.device)
    for i, p in enumerate(params_list):
        period = max(2, int(p.get("atr_period", 10)))
        mult = float(p.get("atr_mult", 3.0))
        long_only_mask[i] = bool(p.get("long_only", True))
        if dsid_key is not None:
            atr = _cached_rolling(dsid_key, "atr_mean", period, lambda: _rolling_mean_simple(tr, period))
        else:
            atr = _rolling_mean_simple(tr, period)
        hl2 = (highs[i : i + 1] + lows[i : i + 1]) / 2.0
        upper_b[i : i + 1] = hl2 + mult * atr
        lower_b[i : i + 1] = hl2 - mult * atr
    direction = torch.ones(B, device=close.device, dtype=torch.int8)
    for t in range(N):
        c = closes[:, t]
        ub = upper_b[:, t]
        lb = lower_b[:, t]
        flip_down = (direction == 1) & ~torch.isnan(lb) & (c < lb)
        flip_up = (direction == -1) & ~torch.isnan(ub) & (c > ub)
        direction = torch.where(flip_down, -torch.ones_like(direction), direction)
        direction = torch.where(flip_up, torch.ones_like(direction), direction)
        out[:, t] = direction
    out = torch.where(
        long_only_mask.unsqueeze(1).expand_as(out),
        out.clamp(min=0),
        out,
    )
    return out


# ---------------------------------------------------------------------------
# Backtest vetorizado: dado sig (B, N) e close (1, N), retorna metricas por probe.
# ---------------------------------------------------------------------------

def _backtest_batch(close, sig, capital: float, fee_bps: float, slippage_bps: float, timeframe: str):
    torch, _ = _TORCH, _DEVICE
    B, N = sig.shape
    sig_f = sig.to(close.dtype)
    # posicao aplicada no proximo bar (shift +1)
    pos = torch.zeros_like(sig_f)
    pos[:, 1:] = sig_f[:, :-1]
    # retornos do close
    rets = torch.zeros_like(close)
    rets[:, 1:] = (close[:, 1:] - close[:, :-1]) / close[:, :-1]
    rets = torch.nan_to_num(rets, nan=0.0, posinf=0.0, neginf=0.0)
    rets_b = rets.expand(B, N)
    # custo por mudanca de posicao
    cost_per_side = (fee_bps + slippage_bps) / 10_000.0
    pos_diff = torch.zeros_like(pos)
    pos_diff[:, 0] = pos[:, 0].abs()
    pos_diff[:, 1:] = (pos[:, 1:] - pos[:, :-1]).abs()
    transaction_costs = pos_diff * cost_per_side
    strat_rets = pos * rets_b - transaction_costs
    equity = capital * torch.cumprod(1.0 + strat_rets, dim=-1)
    final_equity = equity[:, -1]
    pnl_pct = (final_equity / capital - 1.0) * 100.0
    transitions = (pos_diff != 0).sum(dim=-1).to(torch.int64)
    trade_count = (transitions // 2 + (transitions % 2)).clamp(min=0)
    # max drawdown
    peak = torch.cummax(equity, dim=-1).values
    dd = (peak - equity) / torch.where(peak > 0, peak, torch.ones_like(peak))
    mdd_pct = dd.max(dim=-1).values * 100.0
    # sharpe
    mean = strat_rets.mean(dim=-1)
    std = strat_rets.std(dim=-1, unbiased=False)
    bpy = _bars_per_year(timeframe)
    sharpe = torch.where(std > 0, mean / std * math.sqrt(bpy), torch.zeros_like(std))
    return {
        "final_equity": final_equity,
        "pnl_pct": pnl_pct,
        "trade_count": trade_count,
        "mdd_pct": mdd_pct,
        "sharpe": sharpe,
        "equity": equity,
        "pos": pos,
    }


# ---------------------------------------------------------------------------
# Entrypoint: roda lista de probes em batch agrupados por (dataset_id, timeframe, engine).
# ---------------------------------------------------------------------------

ENGINES_GPU = {"ma_crossover", "bollinger", "rsi", "donchian", "supertrend"}


def _hit_rate_loop(equity_np: np.ndarray, pos_np: np.ndarray, capital: float):
    """Loop CPU pequeno para hit_rate: dificilmente vetorizavel sem ganho real."""
    wins = 0
    losses = 0
    in_pos = 0
    entry_eq = capital
    n = equity_np.shape[0]
    for i in range(1, n):
        if pos_np[i] != 0 and in_pos == 0:
            in_pos = pos_np[i]
            entry_eq = equity_np[i - 1]
        elif pos_np[i] != in_pos and in_pos != 0:
            if equity_np[i] > entry_eq:
                wins += 1
            else:
                losses += 1
            in_pos = pos_np[i]
            entry_eq = equity_np[i] if in_pos != 0 else entry_eq
    closed = wins + losses
    return (wins / closed) if closed > 0 else None


def bootstrap_metrics_gpu(
    engine: str,
    params: dict[str, Any],
    df: pd.DataFrame,
    timeframe: str,
    *,
    n_samples: int = 1000,
    block_size: int | None = None,
    fee_bps: float = 10.0,
    slippage_bps: float = 2.0,
) -> dict[str, Any] | None:
    """Block bootstrap dos retornos da estrategia para gerar IC do Sharpe/PnL.

    Estima 'sorte estatistica' do candidato principal: re-amostra blocos
    contiguos dos retornos da estrategia (preservando autocorrelacao local)
    e calcula Sharpe/PnL pra cada amostra. Retorna mean/std/p5/p50/p95.

    Criterio recomendado: rejeitar se sharpe_p5 < 0 (95% IC nao positivo).
    """
    torch, device = _ensure_torch()
    if torch is None or torch is False:
        return None
    engine = engine.lower()
    if engine not in ENGINES_GPU:
        return None
    N = int(len(df))
    if N < 60:
        return None
    if block_size is None:
        block_size = max(8, int(math.sqrt(N)))
    block_size = max(2, min(int(block_size), N // 4))

    close_np = df["close"].astype(np.float32).to_numpy()
    high_np = df["high"].astype(np.float32).to_numpy()
    low_np = df["low"].astype(np.float32).to_numpy()
    close = torch.from_numpy(close_np).to(device).unsqueeze(0)
    high = torch.from_numpy(high_np).to(device).unsqueeze(0)
    low = torch.from_numpy(low_np).to(device).unsqueeze(0)

    try:
        if engine == "ma_crossover":
            sig = _signals_ma_crossover_batch(close, [params])
        elif engine == "bollinger":
            sig = _signals_bollinger_batch(close, [params])
        elif engine == "rsi":
            sig = _signals_rsi_batch(close, [params])
        elif engine == "donchian":
            sig = _signals_donchian_batch(high, low, close, [params])
        elif engine == "supertrend":
            sig = _signals_supertrend_batch(high, low, close, [params])
        else:
            return None
    except Exception:
        return None

    sig_f = sig.to(close.dtype)
    pos = torch.zeros_like(sig_f)
    pos[:, 1:] = sig_f[:, :-1]
    rets = torch.zeros_like(close)
    rets[:, 1:] = (close[:, 1:] - close[:, :-1]) / close[:, :-1]
    rets = torch.nan_to_num(rets, nan=0.0, posinf=0.0, neginf=0.0)
    cost_per_side = (fee_bps + slippage_bps) / 10_000.0
    pos_diff = torch.zeros_like(pos)
    pos_diff[:, 0] = pos[:, 0].abs()
    pos_diff[:, 1:] = (pos[:, 1:] - pos[:, :-1]).abs()
    transaction_costs = pos_diff * cost_per_side
    strat_rets = (pos * rets - transaction_costs).squeeze(0)  # (N,)

    # block bootstrap: amostra n_samples sequencias de comprimento ~N
    n_blocks = max(1, N // block_size)
    pop = max(1, N - block_size + 1)
    idx = torch.randint(0, pop, (n_samples, n_blocks), device=device)
    expand = torch.arange(block_size, device=device).view(1, 1, block_size)
    idx_full = idx.unsqueeze(-1) + expand  # (n_samples, n_blocks, block_size)
    idx_flat = idx_full.view(n_samples, n_blocks * block_size)
    samples = strat_rets[idx_flat]  # (n_samples, n_blocks*block_size)

    bpy = _bars_per_year(timeframe)
    mean = samples.mean(dim=-1)
    std = samples.std(dim=-1, unbiased=False)
    sharpe = torch.where(std > 0, mean / std * math.sqrt(bpy), torch.zeros_like(std))
    log_rets = torch.log1p(samples.clamp(min=-0.999))
    cum_pnl_pct = (torch.exp(log_rets.sum(dim=-1)) - 1.0) * 100.0

    qs = torch.tensor([0.05, 0.5, 0.95], device=device, dtype=sharpe.dtype)
    sh_q = torch.quantile(sharpe, qs).cpu().numpy().tolist()
    pnl_q = torch.quantile(cum_pnl_pct, qs).cpu().numpy().tolist()

    return {
        "n_samples": int(n_samples),
        "block_size": int(block_size),
        "sharpe_mean": float(sharpe.mean().item()),
        "sharpe_std": float(sharpe.std(unbiased=False).item()),
        "sharpe_p5": float(sh_q[0]),
        "sharpe_p50": float(sh_q[1]),
        "sharpe_p95": float(sh_q[2]),
        "pnl_p5_pct": float(pnl_q[0]),
        "pnl_p50_pct": float(pnl_q[1]),
        "pnl_p95_pct": float(pnl_q[2]),
        "sharpe_positive_frac": float((sharpe > 0).float().mean().item()),
    }


def _numeric_param_keys(params: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for k, v in params.items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and v != 0:
            keys.append(k)
    return keys


def grid_search_metrics_gpu(
    engine: str,
    params: dict[str, Any],
    df: pd.DataFrame,
    timeframe: str,
    *,
    grid_size: int = 11,
    range_pct: float = 0.20,
    fee_bps: float = 10.0,
    slippage_bps: float = 2.0,
) -> dict[str, Any] | None:
    """Varre malha NxN ao redor dos params numericos pra medir robustez.

    Pega os 2 primeiros params numericos nao-zero, gera grid `grid_size`
    pontos em [val*(1-range_pct), val*(1+range_pct)], roda os
    grid_size**dim combos em uma chamada batch GPU. Retorna stats do
    pnl_pct dentro do grid.

    Criterio recomendado: pnl_positive_frac >= 0.5 e pnl_p25 >= -5%.
    """
    torch, device = _ensure_torch()
    if torch is None or torch is False:
        return None
    engine = engine.lower()
    if engine not in ENGINES_GPU:
        return None
    keys = _numeric_param_keys(params)
    if not keys:
        return None
    keys = keys[:2]  # 1D ou 2D no maximo
    grid_size = max(3, int(grid_size))
    range_pct = max(0.05, float(range_pct))

    axis_values: list[list[float]] = []
    for k in keys:
        base = float(params[k])
        lo = base * (1.0 - range_pct)
        hi = base * (1.0 + range_pct)
        pts = np.linspace(lo, hi, grid_size).tolist()
        # respeita inteiro se base for int
        if isinstance(params[k], int):
            pts = sorted({max(1, int(round(p))) for p in pts})
            if len(pts) < 2:
                pts = [int(params[k])]
        axis_values.append(pts)

    # produto cartesiano em memoria (max 11*11 = 121 combos)
    combos: list[dict[str, Any]] = []
    if len(keys) == 1:
        for v in axis_values[0]:
            combos.append({**params, keys[0]: v})
    else:
        for v0 in axis_values[0]:
            for v1 in axis_values[1]:
                combos.append({**params, keys[0]: v0, keys[1]: v1})
    if len(combos) < 2:
        return None

    N = int(len(df))
    if N < 60:
        return None
    close_np = df["close"].astype(np.float32).to_numpy()
    high_np = df["high"].astype(np.float32).to_numpy()
    low_np = df["low"].astype(np.float32).to_numpy()
    close = torch.from_numpy(close_np).to(device).unsqueeze(0)
    high = torch.from_numpy(high_np).to(device).unsqueeze(0)
    low = torch.from_numpy(low_np).to(device).unsqueeze(0)

    try:
        if engine == "ma_crossover":
            sig = _signals_ma_crossover_batch(close, combos)
        elif engine == "bollinger":
            sig = _signals_bollinger_batch(close, combos)
        elif engine == "rsi":
            sig = _signals_rsi_batch(close, combos)
        elif engine == "donchian":
            sig = _signals_donchian_batch(high, low, close, combos)
        elif engine == "supertrend":
            sig = _signals_supertrend_batch(high, low, close, combos)
        else:
            return None
    except Exception:
        return None

    B = sig.shape[0]
    sig_f = sig.to(close.dtype)
    pos = torch.zeros_like(sig_f)
    pos[:, 1:] = sig_f[:, :-1]
    rets = torch.zeros_like(close)
    rets[:, 1:] = (close[:, 1:] - close[:, :-1]) / close[:, :-1]
    rets = torch.nan_to_num(rets, nan=0.0, posinf=0.0, neginf=0.0)
    rets_b = rets.expand(B, sig.shape[-1])
    cost_per_side = (fee_bps + slippage_bps) / 10_000.0
    pos_diff = torch.zeros_like(pos)
    pos_diff[:, 0] = pos[:, 0].abs()
    pos_diff[:, 1:] = (pos[:, 1:] - pos[:, :-1]).abs()
    transaction_costs = pos_diff * cost_per_side
    strat_rets = pos * rets_b - transaction_costs
    log_rets = torch.log1p(strat_rets.clamp(min=-0.999))
    cum_pnl_pct = (torch.exp(log_rets.sum(dim=-1)) - 1.0) * 100.0

    qs = torch.tensor([0.05, 0.25, 0.5, 0.75, 0.95], device=device, dtype=cum_pnl_pct.dtype)
    pnl_q = torch.quantile(cum_pnl_pct, qs).cpu().numpy().tolist()
    return {
        "engine": engine,
        "axes": [{"key": k, "values": axis_values[i]} for i, k in enumerate(keys)],
        "total_combos": int(B),
        "pnl_min_pct": float(cum_pnl_pct.min().item()),
        "pnl_p5_pct": float(pnl_q[0]),
        "pnl_p25_pct": float(pnl_q[1]),
        "pnl_p50_pct": float(pnl_q[2]),
        "pnl_p75_pct": float(pnl_q[3]),
        "pnl_p95_pct": float(pnl_q[4]),
        "pnl_max_pct": float(cum_pnl_pct.max().item()),
        "pnl_positive_frac": float((cum_pnl_pct > 0).float().mean().item()),
    }


def run_probes_gpu(
    probes: list[dict[str, Any]],
    df_loader: Callable[[str], pd.DataFrame],
) -> list[dict[str, Any]] | None:
    """Roda lista de probes em batch GPU. Retorna None se GPU indisponivel.

    Probes que nao usam engine suportada (ou tem candles insuficientes) caem
    pra um dict de erro coerente com o caminho CPU.
    """
    torch, device = _ensure_torch()
    if torch is None or torch is False:
        return None

    # agrupa por (dataset_id, engine)  -- mesma serie de candles + mesma fn de sinal
    groups: dict[tuple[str, str], list[int]] = {}
    for idx, p in enumerate(probes):
        engine = str(p.get("engine") or "").lower()
        dsid = str(p.get("dataset_id") or "")
        if engine not in ENGINES_GPU or not dsid:
            continue
        groups.setdefault((dsid, engine), []).append(idx)

    if not groups:
        return None

    results: list[dict[str, Any] | None] = [None] * len(probes)
    t0_total = time.time()

    for (dsid, engine), idxs in groups.items():
        try:
            df = df_loader(dsid)
        except Exception as exc:
            for i in idxs:
                results[i] = {
                    **probes[i],
                    "ok": False,
                    "elapsed_s": 0.0,
                    "error": f"{exc.__class__.__name__}: {exc}"[:500],
                }
            continue

        if len(df) < 30:
            for i in idxs:
                results[i] = {
                    **probes[i],
                    "ok": False,
                    "elapsed_s": 0.0,
                    "error": f"poucos candles ({len(df)})",
                }
            continue

        # Cache GPU: 1 upload por dataset; reusa close/high/low/tr/rets/gain/loss
        ds_tensors = _ensure_dataset_tensors(dsid, df)
        if ds_tensors is None:
            for i in idxs:
                results[i] = {
                    **probes[i],
                    "ok": False,
                    "elapsed_s": 0.0,
                    "error": "gpu indisponivel apos init",
                }
            continue
        close = ds_tensors["close"]
        high = ds_tensors["high"]
        low = ds_tensors["low"]
        tr_cached = ds_tensors["tr"]
        gain_cached = ds_tensors["gain"]
        loss_cached = ds_tensors["loss"]
        rets_cached = ds_tensors["rets"]
        dsid_key = ds_tensors["key"]
        close_np = df["close"].astype(np.float32).to_numpy()  # ainda usado p/ bars count

        params_list = [dict(probes[i].get("params") or {}) for i in idxs]

        t0 = time.time()
        try:
            if engine == "ma_crossover":
                sig = _signals_ma_crossover_batch(close, params_list)
            elif engine == "bollinger":
                sig = _signals_bollinger_batch(close, params_list)
            elif engine == "rsi":
                sig = _signals_rsi_batch(
                    close, params_list,
                    gain_cached=gain_cached, loss_cached=loss_cached, dsid_key=dsid_key,
                )
            elif engine == "donchian":
                sig = _signals_donchian_batch(high, low, close, params_list)
            elif engine == "supertrend":
                sig = _signals_supertrend_batch(
                    high, low, close, params_list,
                    tr_cached=tr_cached, dsid_key=dsid_key,
                )
            else:
                continue
        except Exception as exc:
            for i in idxs:
                results[i] = {
                    **probes[i],
                    "ok": False,
                    "elapsed_s": round(time.time() - t0, 3),
                    "error": f"gpu signal {engine}: {exc.__class__.__name__}: {exc}"[:500],
                }
            continue

        # capital/fees/slippage podem variar por probe; agrupamos novamente.
        # Usamos vetor de capital/fees por linha.
        B = len(idxs)
        capital_v = torch.tensor(
            [float(probes[i].get("capital", 10000.0)) for i in idxs],
            device=device,
            dtype=close.dtype,
        )
        fee_v = torch.tensor(
            [float(probes[i].get("fee_bps", 10.0)) for i in idxs],
            device=device,
            dtype=close.dtype,
        )
        slip_v = torch.tensor(
            [float(probes[i].get("slippage_bps", 2.0)) for i in idxs],
            device=device,
            dtype=close.dtype,
        )
        timeframe = str(probes[idxs[0]].get("timeframe") or "")
        # Roda batch com capital=1 e ajusta no final (linear). Custo varia por probe -> aplica direto.
        sig_f = sig.to(close.dtype)
        pos = torch.zeros_like(sig_f)
        pos[:, 1:] = sig_f[:, :-1]
        # rets ja calculado e cacheado pelo _ensure_dataset_tensors
        rets_b = rets_cached.expand(B, sig.shape[-1])
        cost_per_side = (fee_v + slip_v) / 10_000.0  # (B,)
        pos_diff = torch.zeros_like(pos)
        pos_diff[:, 0] = pos[:, 0].abs()
        pos_diff[:, 1:] = (pos[:, 1:] - pos[:, :-1]).abs()
        transaction_costs = pos_diff * cost_per_side.unsqueeze(1)
        strat_rets = pos * rets_b - transaction_costs
        equity_unit = torch.cumprod(1.0 + strat_rets, dim=-1)
        equity = capital_v.unsqueeze(1) * equity_unit
        final_equity = equity[:, -1]
        pnl_pct = (final_equity / capital_v - 1.0) * 100.0
        transitions = (pos_diff != 0).sum(dim=-1).to(torch.int64)
        trade_count = (transitions // 2 + (transitions % 2)).clamp(min=0)
        peak = torch.cummax(equity, dim=-1).values
        dd = (peak - equity) / torch.where(peak > 0, peak, torch.ones_like(peak))
        mdd_pct = dd.max(dim=-1).values * 100.0
        mean = strat_rets.mean(dim=-1)
        std = strat_rets.std(dim=-1, unbiased=False)
        bpy = _bars_per_year(timeframe)
        sharpe = torch.where(std > 0, mean / std * math.sqrt(bpy), torch.zeros_like(std))

        equity_np = equity.detach().cpu().numpy()
        pos_np = pos.detach().cpu().numpy().astype(np.int8)
        final_np = final_equity.detach().cpu().numpy()
        pnl_np = pnl_pct.detach().cpu().numpy()
        mdd_np = mdd_pct.detach().cpu().numpy()
        sharpe_np = sharpe.detach().cpu().numpy()
        trades_np = trade_count.detach().cpu().numpy()

        elapsed = (time.time() - t0) / max(B, 1)
        for k, i in enumerate(idxs):
            cap = float(probes[i].get("capital", 10000.0))
            hit = _hit_rate_loop(equity_np[k], pos_np[k], cap)
            final_eq = float(final_np[k])
            results[i] = {
                **probes[i],
                "ok": True,
                "bars": int(close_np.size),
                "final_equity": round(final_eq, 4),
                "total_pnl": round(final_eq - cap, 4),
                "pnl_pct": round(float(pnl_np[k]), 4),
                "mdd_pct": round(float(mdd_np[k]), 4),
                "trade_count": int(trades_np[k]),
                "hit_rate": round(hit, 4) if hit is not None else None,
                "sharpe": round(float(sharpe_np[k]), 4),
                "elapsed_s": round(elapsed, 4),
                "gpu": True,
            }

    # devolve results -- entradas None significam que nao foi possivel (engine fora da lista)
    # sinaliza ao caller para rodar essas em CPU
    if any(r is None for r in results):
        return results  # caller cuida do fallback
    return results
