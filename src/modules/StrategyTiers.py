"""Tier de risco por estrategia.

SL/trail variam por tier. Estrategias melhores podem arriscar mais e ter trail
mais largo (pra capturar tendencias). Piores tem stops apertados.

Razao calibrada (2026-05-12, backtest config F): SL = 2.5% da posicao, trailing
ativa em +1.5%, floor +1.2% (garante minimo 1.2% de lucro travado quando trail ativa).

Tier 1 (melhor):     posicao $300, SL -$7.50, trail +$4.50, floor +$3.60
Tier 2 (top 2-5):    posicao $225, SL -$5.625, trail +$3.375, floor +$2.70
Tier 3 (resto/sem historico): posicao $150, SL -$3.75, trail +$2.25, floor +$1.80

Sizing 2026-05-11: 1.5x (era $200/$150/$100 = 10% T3; agora $300/$225/$150 = 15% T3).
Recalibragem 2026-05-12: SL 2%->2.5%, trail_act 1%->1.5%, floor 1%->1.2%. Backtest
nos 55 trades reais: o overlay antigo (SL 2% / trail +1%) cortava vencedores cedo e
realizava perdas pequenas demais (PF 0.59); afrouxar pra config F reduziu a perda do
overlay ~55% (PF 0.78). SL <2% piora muito (mean-reverters precisam de espaco).

Ranking: PnL realizado (decrescente). Enquanto o paper ainda nao tem amostra
minima, o ranking validado do Aether preenche Tier 1/2 como fallback
conservador. Strategies com paper negativo/pausado continuam tier 3.
Banca $1000: max ~4 posicoes T3 simultaneas (4 x $150 = $600, ~70% allocated cap).
Sinais excedentes sao bloqueados pelo RISK GUARD por falta de caixa — esperado.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Tier -> parametros de risco/posicao
# Calibracao config F (2026-05-12): SL 2.5% da posicao, trail ativa em +1.5%,
# floor +1.2% (giveback adaptativo so corre acima desse piso).
TIER_CONFIG = {
    1: {
        "position_usd": 300.0,
        "sl_usd": 7.5,
        "trail_activation_usd": 4.5,
        "trail_floor_usd": 3.6,
        "trail_giveback_pct": 25.0,
    },
    2: {
        "position_usd": 225.0,
        "sl_usd": 5.625,
        "trail_activation_usd": 3.375,
        "trail_floor_usd": 2.7,
        "trail_giveback_pct": 25.0,
    },
    3: {
        "position_usd": 150.0,
        "sl_usd": 3.75,
        "trail_activation_usd": 2.25,
        "trail_floor_usd": 1.8,
        "trail_giveback_pct": 25.0,
    },
}

DEFAULT_TIER = 3
STATE_PATH = Path(os.getenv("PAPER_STATE_PATH", "src/logs/paper_balance/state.json"))
ACTIVE_STRATEGIES_PATH = Path(os.getenv("AETHER_ACTIVE_STRATEGIES_PATH", "src/logs/research/active_strategies.json"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "sim", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


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


def _load_active_records() -> list[dict]:
    try:
        with ACTIVE_STRATEGIES_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    records = data.get("strategies") if isinstance(data, dict) else data
    return [r for r in records or [] if isinstance(r, dict)]


def _strategy_name_from_active_record(record: dict) -> str | None:
    strategy_id = str(record.get("id") or "")
    if not strategy_id:
        return None
    probe_specs = record.get("probe_specs") if isinstance(record.get("probe_specs"), list) else []
    if record.get("engine") == "combo" or len(probe_specs) > 1:
        return f"Aether:combo#{strategy_id[-8:]}"
    if record.get("engine"):
        return f"Aether:{record['engine']}#{strategy_id[-8:]}"
    if record.get("template"):
        return f"{record['template']}#{strategy_id[:8]}"
    return None


def _backtest_value(summary: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(summary.get(key) or default)
    except (TypeError, ValueError):
        return default


def _fallback_backtest_candidates(state: dict, already_ranked: set[str]) -> list[tuple[str, float, float, float, int, str, str]]:
    meta = state.get("_meta", {}) if isinstance(state, dict) else {}
    stats = meta.get("strategy_stats", {}) if isinstance(meta, dict) else {}
    paused = meta.get("paused_strategies", {}) if isinstance(meta, dict) else {}
    activity = meta.get("strategy_activity", {}) if isinstance(meta, dict) else {}
    min_trades = _env_int("TIER_BACKTEST_MIN_TRADES", 30)
    min_pnl = _env_float("TIER_BACKTEST_MIN_PNL_PCT", 0.0)
    min_sharpe = _env_float("TIER_BACKTEST_MIN_SHARPE", 1.0)
    max_mdd = _env_float("TIER_BACKTEST_MAX_MDD_PCT", 35.0)
    dead_min_calls = _env_int("AETHER_DEAD_MIN_CALLS", 1000)
    dead_max_active_pct = _env_float("AETHER_DEAD_MAX_ACTIVE_PCT", 0.1)
    candidates: list[tuple[str, float, float, float, int, str, str]] = []
    for record in _load_active_records():
        name = _strategy_name_from_active_record(record)
        if not name or name in already_ranked or name in paused:
            continue
        paper = stats.get(name, {}) if isinstance(stats, dict) else {}
        paper_trades = int(paper.get("trades") or 0) if isinstance(paper, dict) else 0
        paper_pnl = float(paper.get("pnl_usd") or 0.0) if isinstance(paper, dict) else 0.0
        if paper_trades > 0 and paper_pnl <= 0:
            continue
        act = activity.get(name, {}) if isinstance(activity, dict) else {}
        calls = int(act.get("calls") or 0) if isinstance(act, dict) else 0
        non_neutral = int(act.get("non_neutral") or 0) if isinstance(act, dict) else 0
        active_pct = (non_neutral / calls * 100.0) if calls else 100.0
        if calls >= dead_min_calls and active_pct < dead_max_active_pct:
            continue
        summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
        if summary.get("validation_passed") is not True or summary.get("critic_status") != "approved":
            continue
        bt_trades = int(_backtest_value(summary, "trade_count"))
        pnl = _backtest_value(summary, "pnl_pct")
        sharpe = _backtest_value(summary, "sharpe")
        mdd = _backtest_value(summary, "mdd_pct", 999.0)
        if bt_trades < min_trades or pnl <= min_pnl or sharpe < min_sharpe or mdd > max_mdd:
            continue
        asset = str(record.get("asset") or record.get("symbol") or "")
        engine = str(record.get("engine") or record.get("template") or "")
        candidates.append((name, pnl, sharpe, mdd, bt_trades, asset, engine))
    candidates.sort(key=lambda item: (item[1], item[2], -item[3], item[4]), reverse=True)
    return candidates


def rank_strategies(
    min_trades: int = 5,
    require_positive: bool = True,
    backtest_fallback: bool | None = None,
) -> dict[str, int]:
    """Le state.json e retorna {strategy_name: rank} (1 = melhor PnL).

    Por padrao so ranqueia strategies com PnL > 0 e >= 5 trades: estrategia
    perdedora ou com amostra pequena nao deveria receber tier melhor que uma
    sem historico (1 trade lucrativo nao garante Tier 1). Se faltarem nomes
    elegiveis no paper, o fallback usa o manifest validado do Aether para
    preencher ate os 5 primeiros ranks sem promover estrategias ja negativas
    no paper.
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
    ranked_names = [name for name, _ in eligible]
    use_fallback = _env_bool("TIER_BACKTEST_FALLBACK", True) if backtest_fallback is None else bool(backtest_fallback)
    if use_fallback:
        fallback_limit = _env_int("TIER_BACKTEST_FALLBACK_LIMIT", 5)
        max_per_asset = _env_int("TIER_BACKTEST_MAX_PER_ASSET", 2)
        max_per_engine = _env_int("TIER_BACKTEST_MAX_PER_ENGINE", 2)
        asset_counts: dict[str, int] = {}
        engine_counts: dict[str, int] = {}
        seen = set(ranked_names)
        for name, _pnl, _sharpe, _mdd, _trades, asset, engine in _fallback_backtest_candidates(state, seen):
            if len(ranked_names) >= fallback_limit:
                break
            if asset and max_per_asset > 0 and asset_counts.get(asset, 0) >= max_per_asset:
                continue
            if engine and max_per_engine > 0 and engine_counts.get(engine, 0) >= max_per_engine:
                continue
            ranked_names.append(name)
            seen.add(name)
            if asset:
                asset_counts[asset] = asset_counts.get(asset, 0) + 1
            if engine:
                engine_counts[engine] = engine_counts.get(engine, 0) + 1
    return {name: rank for rank, name in enumerate(ranked_names, start=1)}


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
