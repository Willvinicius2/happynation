from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import os
from typing import Any

import pandas as pd

from src.modules.StrategyResearchEngine import StrategySpec


StrategyFn = Callable[..., bool | None]

ENTRY_MODES = {
    # "aggressive" REMOVIDO: voto isolado nao pode comprar. Piso global e 2.
    "normal": {"min_buy_votes": 2, "min_buy_weight": 2.0, "min_sell_votes": 1, "min_sell_weight": 1.0, "required_buy_votes": []},
    "conservative": {"min_buy_votes": 3, "min_buy_weight": 3.0, "min_sell_votes": 1, "min_sell_weight": 1.0, "required_buy_votes": []},
    "champions_plus": {"min_buy_votes": 2, "min_buy_weight": 2.5, "min_sell_votes": 1, "min_sell_weight": 1.0, "required_buy_votes": ["Champions"]},
}

# Pisos absolutos de seguranca: ninguem pode burlar.
HARD_FLOOR_BUY_VOTES = 2
HARD_FLOOR_BUY_WEIGHT = 2.0
HARD_FLOOR_SELL_VOTES = 1
HARD_FLOOR_SELL_WEIGHT = 1.0


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}


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


@dataclass(frozen=True)
class VoteSpec:
    name: str
    function: StrategyFn
    params: dict[str, Any] = field(default_factory=dict)
    needs_instance: bool = False
    weight: float = 1.0
    timeframe: str | None = None


# Multi-TF: estrategias com TF maior que o bot ganham candles via resample.
# TFs menores que o bot sao puladas (nao da pra inferir candles intra-bar).
_TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480, "12h": 720,
    "1d": 1440, "3d": 4320, "1w": 10080,
}

_PANDAS_TF_RULE = {
    "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h", "12h": "12h",
    "1d": "1D", "3d": "3D", "1w": "1W",
}


def _tf_minutes(tf: str | None) -> int | None:
    if not tf:
        return None
    return _TF_MINUTES.get(tf.strip().lower())


def _resample_to_tf(df: pd.DataFrame, target_tf: str) -> pd.DataFrame | None:
    """Converte df OHLC do TF base do bot para target_tf via resample.

    Espera df com coluna `open_time` (datetime, UTC) ou index datetime, e
    colunas open/high/low/close/volume. Retorna None se nao houver dados
    suficientes pra um candle do target_tf.
    """
    rule = _PANDAS_TF_RULE.get(target_tf.strip().lower())
    if not rule:
        return None
    work = df.copy(deep=True)
    if "open_time" in work.columns:
        work = work.set_index(pd.to_datetime(work["open_time"], utc=True))
    elif not isinstance(work.index, pd.DatetimeIndex):
        return None
    # Suporta dois schemas: Aether (open/high/low/close) e BinanceTraderBot
    # (open_price/high_price/low_price/close_price). Pega o primeiro que match.
    cols = {c.lower(): c for c in work.columns}
    def _pick(*names):
        for n in names:
            if n in cols: return cols[n]
        return None
    o = _pick("open", "open_price")
    h = _pick("high", "high_price")
    l = _pick("low", "low_price")
    c = _pick("close", "close_price")
    v = _pick("volume")
    if not all([o, h, l, c]):
        return None
    agg = {o: "first", h: "max", l: "min", c: "last"}
    if v:
        agg[v] = "sum"
    out = work.resample(rule, label="right", closed="right").agg(agg).dropna()
    if len(out) < 30:
        return None
    out = out.reset_index()
    if "open_time" not in out.columns and out.columns[0] != "open_time":
        out = out.rename(columns={out.columns[0]: "open_time"})
    return out


def _default_vote_specs(champion_args: dict[str, Any] | None = None, include_scalp: bool = False) -> list[VoteSpec]:
    """Cesta legada esvaziada em 2026-05-07.

    As 9 estrategias historicas (Champions/RSI/MovingAverage/MA_RSI_Volume/Vortex/
    ATR/TEMA/HullMA/HighWinRateScalp) reprovaram em backtest de 2 anos no Critic v4
    (manifest validation 1A.5: 0/20 passaram >=4/6 gates; Champions OOS=0%). O bot
    agora opera EXCLUSIVAMENTE com estrategias deployadas pelo Aether (Critic v5
    com regime gate). Sem Aether-deploy, o bot fica em standby — nao cai em
    estrategias historicas.

    Os arquivos em src/strategies/ (champions_strategy.py, rsi_strategy.py, etc)
    foram preservados como historico/referencia, mas nao sao mais registrados.
    """
    return []


def MultiStrategy(
    self,
    stock_data: pd.DataFrame,
    verbose: bool = True,
    champion_args: dict[str, Any] | None = None,
    min_buy_votes: int = 2,
    min_sell_votes: int = 2,
    min_buy_weight: float | None = None,
    min_sell_weight: float | None = None,
    bot_timeframe: str | None = None,
    entry_mode: str | None = None,
    required_buy_votes: list[str] | None = None,
    include_scalp: bool = False,
    strategies: list[VoteSpec] | None = None,
    active_strategy_specs: list[StrategySpec] | None = None,
    aether_only: bool | None = None,
    tf_data: dict[str, pd.DataFrame] | None = None,
) -> bool | None:
    """Votacao entre estrategias independentes.

    Regras:
    - todas as estrategias rodam no mesmo ciclo, sem retorno antecipado;
    - cada estrategia recebe uma copia dos candles para nao mutar as outras;
    - erro de uma estrategia vira erro isolado e nao derruba o ciclo;
    - quando vendido, votos de venda contam como "nao comprar";
    - quando comprado, votos de compra contam como "manter comprado";
    - por padrao, quando ha estrategias Aether ativas para o ativo, elas
      substituem a cesta legada na decisao operacional.
    """
    legacy_specs = strategies or _default_vote_specs(champion_args, include_scalp=include_scalp)
    specs = legacy_specs
    active_specs: list[VoteSpec] = []
    if active_strategy_specs:
        for spec in active_strategy_specs:
            params = dict(spec.params)
            timeframe = params.pop("_timeframe", None)
            active_specs.append(VoteSpec(
                name=spec.name,
                function=spec.function,
                params=params,
                needs_instance=spec.needs_instance,
                weight=float(params.pop("_weight", 1.0)) if "_weight" in params else 1.0,
                timeframe=timeframe,
            ))

    aether_only_mode = _env_truthy("AETHER_STRATEGIES_ONLY", True) if aether_only is None else bool(aether_only)
    using_aether_only = bool(active_specs) and aether_only_mode
    if using_aether_only:
        specs = active_specs
    elif active_specs:
        specs = list(legacy_specs) + active_specs

    mode_name = (entry_mode or os.getenv("PAPER_ENTRY_MODE") or "normal").strip().lower()
    if mode_name not in ENTRY_MODES:
        # Modo desconhecido (ex: "aggressive" deprecated): cai para "normal" silenciosamente
        # mas registra pra investigacao.
        try:
            from src.modules.PaperBalanceLogger import record_unknown_entry_mode
            record_unknown_entry_mode(mode_name)
        except Exception:
            pass
        mode_name = "normal"
    mode = ENTRY_MODES[mode_name]
    min_buy_votes = int(mode.get("min_buy_votes", min_buy_votes))
    min_buy_weight = float(mode.get("min_buy_weight", min_buy_weight or min_buy_votes))
    min_sell_votes = int(mode.get("min_sell_votes", min_sell_votes))
    min_sell_weight = float(mode.get("min_sell_weight", min_sell_weight or min_sell_votes))
    required_buy_votes = list(required_buy_votes or mode.get("required_buy_votes", []))

    if using_aether_only:
        mode_name = "aether_only"
        min_buy_votes = max(1, _env_int("AETHER_ONLY_MIN_BUY_VOTES", 2))
        min_sell_votes = max(1, _env_int("AETHER_ONLY_MIN_SELL_VOTES", 1))
        min_buy_weight = max(1.0, _env_float("AETHER_ONLY_MIN_BUY_WEIGHT", 2.0))
        min_sell_weight = max(1.0, _env_float("AETHER_ONLY_MIN_SELL_WEIGHT", 1.0))
        required_buy_votes = []

    paused_strategies: dict[str, str] = {}
    try:
        from src.modules.PaperBalanceLogger import get_paused_strategies

        paused_strategies = get_paused_strategies()
    except Exception:
        paused_strategies = {}

    dead_strategies: set[str] = set()
    if _env_truthy("AETHER_SKIP_DEAD_STRATEGIES", True):
        try:
            from src.modules.PaperBalanceLogger import get_dead_strategies

            dead_strategies = set(get_dead_strategies(
                min_calls=_env_int("AETHER_DEAD_MIN_CALLS", 1000),
                max_active_pct=_env_float("AETHER_DEAD_MAX_ACTIVE_PCT", 0.1),
            ))
        except Exception:
            dead_strategies = set()

    # Pisos absolutos de seguranca para a cesta legada. No modo Aether-only,
    # os pisos vem de AETHER_ONLY_* para permitir coleta agressiva explicitamente,
    # mas o default operacional exige confluencia de dois votos.
    if not using_aether_only:
        min_buy_votes = max(min_buy_votes, HARD_FLOOR_BUY_VOTES)
        min_sell_votes = max(min_sell_votes, HARD_FLOOR_SELL_VOTES)
        min_buy_weight = float(min_buy_weight if min_buy_weight is not None else min_buy_votes)
        min_sell_weight = float(min_sell_weight if min_sell_weight is not None else min_sell_votes)
        min_buy_weight = max(min_buy_weight, HARD_FLOOR_BUY_WEIGHT)
        min_sell_weight = max(min_sell_weight, HARD_FLOOR_SELL_WEIGHT)

    buy_votes: list[str] = []
    sell_votes: list[str] = []
    neutral_votes: list[str] = []
    skipped_votes: list[str] = []
    errors: list[str] = []
    buy_weight = 0.0
    sell_weight = 0.0

    # tf_data tem prioridade absoluta: cada TF vem do hub com candles nativos
    # (sem resample). Se nao veio, cai no fluxo legado (resample do stock_data).
    tf_data_norm: dict[str, pd.DataFrame] | None = None
    if tf_data:
        tf_data_norm = {tf.lower(): df for tf, df in tf_data.items() if df is not None}
        # Se bot_timeframe nao veio mas tf_data tem, infere bot_timeframe como o menor TF disponivel.
        if not bot_timeframe and tf_data_norm:
            bot_timeframe = min(
                tf_data_norm.keys(),
                key=lambda t: _tf_minutes(t) or 999999,
            )

    in_position = bool(getattr(self, "actual_trade_position", False))
    for spec in specs:
        if spec.name in paused_strategies:
            skipped_votes.append(f"{spec.name}:pausada")
            continue
        if not in_position and spec.name in dead_strategies:
            skipped_votes.append(f"{spec.name}:sem_sinal")
            continue
        spec_tf = (spec.timeframe or "").lower() or None
        spec_tf_min = _tf_minutes(spec_tf) if spec_tf else None
        bot_tf_min = _tf_minutes(bot_timeframe)
        strategy_df: pd.DataFrame | None = None

        # 1) Preferir tf_data nativo do hub
        if tf_data_norm is not None and spec_tf and spec_tf in tf_data_norm:
            strategy_df = tf_data_norm[spec_tf]
        elif tf_data_norm is not None and spec_tf is None:
            # Strategy sem TF declarado: usa o TF base (bot_timeframe) ou o menor disponivel.
            base_tf = (bot_timeframe or "").lower()
            if base_tf in tf_data_norm:
                strategy_df = tf_data_norm[base_tf]
            else:
                # pega o menor TF disponivel
                avail = sorted(tf_data_norm.keys(), key=lambda t: _tf_minutes(t) or 999999)
                if avail:
                    strategy_df = tf_data_norm[avail[0]]
        elif tf_data_norm is not None and spec_tf and spec_tf not in tf_data_norm:
            # Hub nao trouxe esse TF. Tenta resample do TF mais rapido do hub se for possivel.
            avail = sorted(tf_data_norm.keys(), key=lambda t: _tf_minutes(t) or 999999)
            for cand in avail:
                cand_min = _tf_minutes(cand)
                if cand_min and spec_tf_min and cand_min < spec_tf_min:
                    try:
                        rs = _resample_to_tf(tf_data_norm[cand], spec_tf)
                    except Exception as exc:
                        errors.append(f"{spec.name}:resample_{exc.__class__.__name__}")
                        rs = None
                    if rs is not None:
                        strategy_df = rs
                        break
            if strategy_df is None:
                skipped_votes.append(f"{spec.name}@{spec.timeframe}:no_tf_data")
                continue
        else:
            # Fluxo legado: stock_data unico + resample/skip por relacao com bot_timeframe.
            if spec_tf_min and bot_tf_min and spec_tf_min < bot_tf_min:
                skipped_votes.append(f"{spec.name}@{spec.timeframe}<bot")
                try:
                    from src.modules.PaperBalanceLogger import record_timeframe_mismatch
                    record_timeframe_mismatch(spec.name, spec.timeframe, bot_timeframe)
                except Exception:
                    pass
                continue
            if spec_tf_min and bot_tf_min and spec_tf_min > bot_tf_min:
                try:
                    rs = _resample_to_tf(stock_data, spec.timeframe)
                except Exception as exc:
                    errors.append(f"{spec.name}:resample_{exc.__class__.__name__}")
                    continue
                if rs is None:
                    skipped_votes.append(f"{spec.name}@{spec.timeframe}:no_data")
                    continue
                strategy_df = rs
            else:
                strategy_df = stock_data

        if strategy_df is None or strategy_df.empty:
            skipped_votes.append(f"{spec.name}:empty_df")
            continue

        try:
            strategy_data = strategy_df.copy(deep=True)
            if spec.needs_instance:
                decision = spec.function(self, stock_data=strategy_data, verbose=False, **spec.params)
            else:
                decision = spec.function(stock_data=strategy_data, verbose=False, **spec.params)
        except TypeError:
            try:
                strategy_data = strategy_df.copy(deep=True)
                if spec.needs_instance:
                    decision = spec.function(self, strategy_data, verbose=False, **spec.params)
                else:
                    decision = spec.function(strategy_data, verbose=False, **spec.params)
            except Exception as exc:
                errors.append(f"{spec.name}:{exc.__class__.__name__}")
                continue
        except Exception as exc:
            errors.append(f"{spec.name}:{exc.__class__.__name__}")
            continue

        if decision is True:
            buy_votes.append(spec.name)
            buy_weight += spec.weight
        elif decision is False:
            sell_votes.append(spec.name)
            sell_weight += spec.weight
        else:
            neutral_votes.append(spec.name)

    owner_decides = using_aether_only and _env_truthy("AETHER_OWNER_DECIDES_EXIT", True)
    owner_strategies: list[str] = []
    if in_position and owner_decides:
        try:
            from src.modules.PaperBalanceLogger import get_position_owners
            operation_code = getattr(self, "operation_code", None)
            if operation_code:
                owner_strategies = get_position_owners(operation_code)
                owner_strategies = [
                    name for name in owner_strategies
                    if name not in paused_strategies and name not in dead_strategies
                ]
        except Exception:
            owner_strategies = []

    if in_position:
        if owner_strategies:
            # Owner-decides: so a estrategia que abriu pode fechar.
            # Outras estrategias (compra/venda/neutra) sao ignoradas no exit.
            owner_voted_sell = any(name in sell_votes for name in owner_strategies)
            final_decision: bool | None = False if owner_voted_sell else True
        else:
            # Fallback consenso: usado quando posicao foi aberta antes do tracking
            # ou modo non-aether. SL/trail USD continuam como defesa fixa.
            final_decision = False if len(sell_votes) >= min_sell_votes and sell_weight >= min_sell_weight else True
    else:
        required_ok = all(name in buy_votes for name in required_buy_votes)
        final_decision = True if len(buy_votes) >= min_buy_votes and buy_weight >= min_buy_weight and required_ok else None

    # Tier de risco/posicao baseado no ranking PnL das strategies que votaram comprar.
    # Aplicado quando a decisao final e abrir nova posicao.
    tier_config = None
    if final_decision is True and not in_position and buy_votes:
        try:
            from src.modules.StrategyTiers import tier_config_for_votes
            tier_config = tier_config_for_votes(buy_votes)
        except Exception as exc:
            tier_config = None
            errors.append(f"tier:{exc.__class__.__name__}")

    snapshot = {
        "mode": mode_name,
        "buy_votes": buy_votes,
        "sell_votes": sell_votes,
        "neutral_votes": neutral_votes,
        "skipped_votes": skipped_votes,
        "errors": errors,
        "buy_weight": buy_weight,
        "sell_weight": sell_weight,
        "min_buy_votes": min_buy_votes,
        "min_sell_votes": min_sell_votes,
        "min_buy_weight": min_buy_weight,
        "min_sell_weight": min_sell_weight,
        "required_buy_votes": required_buy_votes,
        "owner_strategies": owner_strategies,
        "decision": final_decision,
        "tier_config": tier_config,
        "tfs_available": sorted(list(tf_data_norm.keys())) if tf_data_norm else [],
    }
    setattr(self, "last_strategy_snapshot", snapshot)
    try:
        from src.modules.PaperBalanceLogger import record_strategy_votes, update_strategy_activity

        operation_code = getattr(self, "operation_code", None)
        if operation_code:
            record_strategy_votes(operation_code, snapshot)
            # Atualiza contador de atividade por estrategia (detecta estrategias mortas).
            for spec in specs:
                voted = spec.name in buy_votes or spec.name in sell_votes
                update_strategy_activity(spec.name, voted=voted)
    except Exception:
        pass

    if verbose:
        print("-------")
        print(f"MultiStrategy ({mode_name})")
        print(f" | Compras: {len(buy_votes)} / peso {buy_weight:.1f} -> {', '.join(buy_votes) or '-'}")
        print(f" | Vendas: {len(sell_votes)} / peso {sell_weight:.1f} -> {', '.join(sell_votes) or '-'}")
        print(f" | Neutras: {len(neutral_votes)} -> {', '.join(neutral_votes) or '-'}")
        if skipped_votes:
            print(f" | Fora do timeframe: {', '.join(skipped_votes)}")
        if required_buy_votes:
            print(f" | Obrigatorias: {', '.join(required_buy_votes)}")
        if errors:
            print(f" | Erros ignorados: {', '.join(errors)}")
        print(
            " | Decisao: "
            f"{'Comprar' if final_decision is True and not in_position else 'Manter comprado' if final_decision is True else 'Vender' if final_decision is False else 'Aguardar'}"
        )
        print("-------")

    return final_decision


MultiStrategy.__strategy_name__ = "MultiStrategy"  # type: ignore[attr-defined]
