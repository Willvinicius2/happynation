"""
Camada evolutiva: gera novas estrategias por mutacao, crossover e random sampling.

Cada candidato gerado tem um campo `origin` que descreve como ele nasceu:
  - "grid"       — varredura ordenada do grid pre-definido
  - "random"     — amostragem aleatoria dentro dos ranges
  - "mutation"   — pequena perturbacao de uma estrategia existente
  - "crossover"  — combinacao dos params de duas estrategias do mesmo template

A linhagem (parents) eh propagada para que o dashboard possa mostrar
de quem cada nova estrategia descende.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Iterable

from src.research.templates import (
    RISK_PROFILES,
    TEMPLATE_GRIDS,
    TemplateSpec,
)


PARAM_RANGES: dict[str, dict[str, tuple[float, float, str]]] = {
    "TrendMomentumConfluence": {
        "fast": (5, 30, "int"), "slow": (25, 90, "int"), "trend": (80, 250, "int"),
        "rsi_period": (7, 21, "int"), "rsi_buy": (45, 65, "int"), "rsi_sell": (35, 50, "int"),
        "vol_mult": (0.8, 2.2, "float"),
    },
    "MeanReversionATR": {
        "rsi_period": (7, 21, "int"), "rsi_buy": (15, 35, "int"), "rsi_exit": (50, 75, "int"),
        "bb_period": (15, 40, "int"), "bb_mult": (1.6, 3.0, "float"),
        "atr_period": (10, 25, "int"), "atr_tp": (1.0, 4.0, "float"),
        "trend": (100, 250, "int"),
    },
    "BreakoutStrength": {
        "period": (15, 70, "int"), "atr_period": (8, 25, "int"), "atr_grow": (1.0, 1.25, "float"),
        "rsi_period": (10, 18, "int"), "rsi_min": (45, 65, "int"), "vol_mult": (1.0, 2.5, "float"),
    },
    "MACD_RSI_Volume": {
        "macd_fast": (6, 20, "int"), "macd_slow": (20, 60, "int"), "signal": (6, 14, "int"),
        "rsi_period": (10, 18, "int"), "rsi_low": (35, 52, "int"), "rsi_high": (58, 75, "int"),
        "trend": (80, 250, "int"),
    },
    "VolatilityRegimeBreakout": {
        "atr_period": (10, 25, "int"), "atr_window": (30, 100, "int"), "vol_squeeze": (0.5, 0.95, "float"),
        "breakout_period": (15, 60, "int"), "rsi_period": (10, 18, "int"), "rsi_min": (45, 65, "int"),
    },
    "DualMA_Trend": {
        "fast": (3, 25, "int"), "slow": (15, 120, "int"), "trend": (60, 250, "int"),
    },
    "RSI_Reversal": {
        "rsi_period": (5, 30, "int"), "low": (10, 40, "int"), "high": (60, 90, "int"), "confirm": (1, 8, "int"),
    },
    "Bollinger_Breakout": {
        "period": (10, 60, "int"), "mult": (1.4, 3.5, "float"), "vol_mult": (0.6, 2.4, "float"),
    },
    "MACD_Trend": {
        "fast": (5, 25, "int"), "slow": (15, 80, "int"), "signal": (5, 18, "int"),
    },
    "Donchian_Breakout": {
        "period": (10, 80, "int"), "atr_period": (5, 28, "int"), "atr_grow": (0.95, 1.25, "float"),
    },
}


RISK_RANGES: dict[str, tuple[float, float, str]] = {
    "take_profit_pct": (0.8, 8.0, "float"),
    "stop_loss_pct":   (0.8, 4.0, "float"),
    "max_hold_candles": (24, 240, "int"),
}


def _is_valid(template: str, params: dict[str, Any]) -> bool:
    if template == "DualMA_Trend":
        return params["fast"] < params["slow"] < params["trend"]
    if template == "TrendMomentumConfluence":
        return params["fast"] < params["slow"] < params["trend"] and params["rsi_buy"] > params["rsi_sell"]
    if template == "MeanReversionATR":
        return params["rsi_buy"] < params["rsi_exit"]
    if template == "MACD_RSI_Volume":
        return params["macd_fast"] < params["macd_slow"] and params["rsi_low"] < params["rsi_high"]
    if template == "RSI_Reversal":
        return params["low"] < params["high"]
    if template == "MACD_Trend":
        return params["fast"] < params["slow"]
    return True


def _coerce(value: float, kind: str) -> float | int:
    if kind == "int":
        return int(round(value))
    return round(value, 3)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Random sampling
# ---------------------------------------------------------------------------

def random_candidate(rng: random.Random, template: str | None = None) -> tuple[TemplateSpec, dict[str, Any], str]:
    template = template or rng.choice(list(PARAM_RANGES.keys()))
    for _ in range(40):
        params: dict[str, Any] = {}
        for name, (lo, hi, kind) in PARAM_RANGES[template].items():
            value = rng.uniform(lo, hi)
            params[name] = _coerce(value, kind)
        if _is_valid(template, params):
            risk = _random_risk(rng)
            return TemplateSpec(template=template, params=params), risk, "random"
    raise RuntimeError(f"nao consegui gerar candidato valido para {template}")


def _random_risk(rng: random.Random) -> dict[str, Any]:
    risk: dict[str, Any] = {}
    for name, (lo, hi, kind) in RISK_RANGES.items():
        risk[name] = _coerce(rng.uniform(lo, hi), kind)
    if risk["stop_loss_pct"] >= risk["take_profit_pct"]:
        risk["stop_loss_pct"] = round(risk["take_profit_pct"] * rng.uniform(0.4, 0.8), 2)
    return risk


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------

def mutate(
    rng: random.Random,
    template: str,
    params: dict[str, Any],
    risk: dict[str, Any],
    sigma: float = 0.18,
    n_mutations: int = 2,
) -> tuple[TemplateSpec, dict[str, Any]]:
    """Perturba `n_mutations` params (param ou risk) com ruido gaussiano relativo."""
    new_params = dict(params)
    new_risk = dict(risk)
    keys: list[tuple[str, str]] = (
        [("param", k) for k in PARAM_RANGES[template]]
        + [("risk", k) for k in RISK_RANGES]
    )
    rng.shuffle(keys)

    mutated = 0
    for kind, key in keys:
        if mutated >= n_mutations:
            break
        if kind == "param":
            lo, hi, vkind = PARAM_RANGES[template][key]
            current = float(new_params.get(key, (lo + hi) / 2))
            noise = rng.gauss(0, sigma) * max(abs(current), (hi - lo) * 0.1)
            new_params[key] = _coerce(_clamp(current + noise, lo, hi), vkind)
        else:
            lo, hi, vkind = RISK_RANGES[key]
            current = float(new_risk.get(key, (lo + hi) / 2))
            noise = rng.gauss(0, sigma) * max(abs(current), (hi - lo) * 0.1)
            new_risk[key] = _coerce(_clamp(current + noise, lo, hi), vkind)
        mutated += 1

    if not _is_valid(template, new_params):
        return TemplateSpec(template=template, params=params), risk
    if new_risk.get("stop_loss_pct", 1) >= new_risk.get("take_profit_pct", 1):
        new_risk["stop_loss_pct"] = round(float(new_risk["take_profit_pct"]) * rng.uniform(0.4, 0.8), 2)
    return TemplateSpec(template=template, params=new_params), new_risk


# ---------------------------------------------------------------------------
# Crossover (mesmo template)
# ---------------------------------------------------------------------------

def crossover(
    rng: random.Random,
    a_params: dict[str, Any], a_risk: dict[str, Any],
    b_params: dict[str, Any], b_risk: dict[str, Any],
    template: str,
) -> tuple[TemplateSpec, dict[str, Any]]:
    new_params: dict[str, Any] = {}
    for key in PARAM_RANGES[template]:
        source = a_params if rng.random() < 0.5 else b_params
        if key in source:
            new_params[key] = source[key]
    if not _is_valid(template, new_params):
        # fallback: pega tudo do melhor (a)
        new_params = dict(a_params)
    new_risk: dict[str, Any] = {}
    for key in RISK_RANGES:
        source = a_risk if rng.random() < 0.5 else b_risk
        if key in source:
            new_risk[key] = source[key]
    if new_risk.get("stop_loss_pct", 1) >= new_risk.get("take_profit_pct", 1):
        new_risk["stop_loss_pct"] = round(float(new_risk["take_profit_pct"]) * rng.uniform(0.4, 0.8), 2)
    return TemplateSpec(template=template, params=new_params), new_risk
