"""Tier de risco por estrategia.

SL/trail variam por tier. Estrategias melhores podem arriscar mais e ter trail
mais largo (pra capturar tendencias). Piores tem stops apertados.

Razao calibrada: SL = 2% da posicao, trailing ativa em +1%, floor +1.0%
(garante minimo 1% de lucro travado quando trail ativa).

Tier 1 (melhor):     posicao $200, SL -$4.00, trail +$2.00, floor +$2.00 (+1%)
Tier 2 (top 2-5):    posicao $150, SL -$3.00, trail +$1.50, floor +$1.50 (+1%)
Tier 3 (resto/sem historico): posicao $100, SL -$2.00, trail +$1.00, floor +$1.00 (+1%)

Ranking: PnL realizado (decrescente). Strategies sem trades vao pra tier 3.
Banca $1000: max ~6 posicoes T3 simultaneas (6 x $100 = $600). Sinais
excedentes sao bloqueados pelo RISK GUARD por falta de caixa — esperado.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Tier -> parametros de risco/posicao
# trail_floor_usd igual a trail_activation_usd: garante que quando o trail
# ativa, o pior caso ainda trava o minimo de 1% de lucro (giveback so corre
# acima desse piso).
TIER_CONFIG = {
    1: {
        "position_usd": 200.0,
        "sl_usd": 4.0,
        "trail_activation_usd": 2.0,
        "trail_floor_usd": 2.0,
        "trail_giveback_pct": 25.0,
    },
    2: {
        "position_usd": 150.0,
        "sl_usd": 3.0,
        "trail_activation_usd": 1.5,
        "trail_floor_usd": 1.5,
        "trail_giveback_pct": 25.0,
    },
    3: {
        "position_usd": 100.0,
        "sl_usd": 2.0,
        "trail_activation_usd": 1.0,
        "trail_floor_usd": 1.0,
        "trail_giveback_pct": 25.0,
    },
}

DEFAULT_TIER = 3
STATE_PATH = Path(os.getenv("PAPER_STATE_PATH", "src/logs/paper_balance/state.json"))


# Ladder de protecao escalonada por % de lucro do peak. Cada degrau:
# (peak_pct_threshold, locked_pct). Quando o peak atinge o threshold,
# o trail trava no minimo o locked_pct do notional.
# Resultado: trail nao "devolve" mais conforme o lucro cresce.
#
# Ex.: peak 5% -> trava 3%; peak 7% -> trava 5%; peak 10% -> trava 8%.
# O sistema combina com giveback adaptativo via max(): sempre o piso
# mais protetor entre giveback% do peak e o ladder vence.
PROFIT_LOCK_LADDER: list[tuple[float, float]] = [
    (5.0, 3.0),
    (7.0, 5.0),
    (10.0, 8.0),
    (15.0, 13.0),
    (20.0, 17.0),
    (30.0, 27.0),
    (50.0, 45.0),
    (75.0, 70.0),
]


def compute_ladder_floor_usd(peak_usd: float, notional_usd: float) -> tuple[float, float]:
    """Retorna (floor_usd, locked_pct) baseado no peak e notional da posicao.

    locked_pct=0 significa que ainda nao bateu nenhum degrau; floor_usd=0.
    """
    if notional_usd <= 0 or peak_usd <= 0:
        return 0.0, 0.0
    peak_pct = (peak_usd / notional_usd) * 100.0
    locked_pct = 0.0
    for threshold, lock in PROFIT_LOCK_LADDER:
        if peak_pct >= threshold:
            locked_pct = lock
        else:
            break
    return notional_usd * (locked_pct / 100.0), locked_pct


def _load_state() -> dict:
    try:
        with STATE_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def rank_strategies(min_trades: int = 5, require_positive: bool = True) -> dict[str, int]:
    """Le state.json e retorna {strategy_name: rank} (1 = melhor PnL).

    Por padrao so ranqueia strategies com PnL > 0 e >= 5 trades: estrategia
    perdedora ou com amostra pequena nao deveria receber tier melhor que uma
    sem historico (1 trade lucrativo nao garante Tier 1).
    """
    state = _load_state()
    stats = state.get("_meta", {}).get("strategy_stats", {})
    eligible = []
    for name, info in stats.items():
        trades = int(info.get("trades") or 0)
        pnl = float(info.get("pnl_usd") or 0.0)
        if trades < min_trades:
            continue
        if require_positive and pnl <= 0:
            continue
        eligible.append((name, pnl))
    eligible.sort(key=lambda item: item[1], reverse=True)
    return {name: rank for rank, (name, _) in enumerate(eligible, start=1)}


def tier_for_strategy(name: str, ranks: dict[str, int] | None = None) -> int:
    """1 = melhor; 2 = top 2-5; 3 = resto/sem historico."""
    if not name:
        return DEFAULT_TIER
    if ranks is None:
        ranks = rank_strategies()
    rank = ranks.get(name)
    if rank is None:
        return DEFAULT_TIER
    if rank == 1:
        return 1
    if rank <= 5:
        return 2
    return DEFAULT_TIER


def tier_config_for_votes(buy_votes: list[str]) -> dict:
    """Retorna config do MELHOR tier entre os voters (menor numero = melhor).

    Resultado inclui chave 'tier' pra logging.
    """
    ranks = rank_strategies()
    if not buy_votes:
        cfg = dict(TIER_CONFIG[DEFAULT_TIER])
        cfg["tier"] = DEFAULT_TIER
        cfg["source"] = "no_votes"
        return cfg
    best_tier = min(tier_for_strategy(name, ranks) for name in buy_votes)
    cfg = dict(TIER_CONFIG[best_tier])
    cfg["tier"] = best_tier
    cfg["source"] = "votes"
    return cfg
