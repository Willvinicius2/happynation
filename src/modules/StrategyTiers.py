"""Tier de risco por estrategia.

SL/trail variam por tier. Estrategias melhores podem arriscar mais e ter trail
mais largo (pra capturar tendencias). Piores tem stops apertados.

Razao calibrada: SL = 2% da posicao, trailing ativa em +1%, floor +0.6%.
Antes: SL = 10% da posicao, trail ativava em +4% (largo demais, nunca disparava).

Tier 1 (melhor):     posicao $100, SL -$2.00, trail +$1.00, floor +$0.60
Tier 2 (top 2-5):    posicao  $75, SL -$1.50, trail +$0.75, floor +$0.45
Tier 3 (resto/sem historico): posicao $50, SL -$1.00, trail +$0.50, floor +$0.30

Ranking: PnL realizado (decrescente). Strategies sem trades vao pra tier 3.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Tier -> parametros de risco/posicao
TIER_CONFIG = {
    1: {
        "position_usd": 100.0,
        "sl_usd": 2.0,
        "trail_activation_usd": 1.0,
        "trail_floor_usd": 0.6,
        "trail_giveback_pct": 25.0,
    },
    2: {
        "position_usd": 75.0,
        "sl_usd": 1.5,
        "trail_activation_usd": 0.75,
        "trail_floor_usd": 0.45,
        "trail_giveback_pct": 25.0,
    },
    3: {
        "position_usd": 50.0,
        "sl_usd": 1.0,
        "trail_activation_usd": 0.5,
        "trail_floor_usd": 0.3,
        "trail_giveback_pct": 25.0,
    },
}

DEFAULT_TIER = 3
STATE_PATH = Path(os.getenv("PAPER_STATE_PATH", "src/logs/paper_balance/state.json"))


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
