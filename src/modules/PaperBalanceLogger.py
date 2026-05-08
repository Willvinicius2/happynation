"""
Logger centralizado para paper trading:
- Banca simulada global de $1000 compartilhada entre ativos
- Registra toda entrada (BUY) e saída (SELL) com PnL
- Gera: trades.csv (auditável), state.json (estado atual), summary.txt (resumo)
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

OUT_DIR = Path("src/logs/paper_balance")
OUT_DIR.mkdir(parents=True, exist_ok=True)

TRADES_CSV = OUT_DIR / "trades.csv"
STATE_JSON = OUT_DIR / "state.json"
SUMMARY_TXT = OUT_DIR / "summary.txt"

TOTAL_BANKROLL = 1000.0
INITIAL_BALANCE = TOTAL_BANKROLL

_lock = threading.Lock()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "sim", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


RISK_GUARD_ENABLED = _env_bool("RISK_GUARD_ENABLED", True)
MAX_DAILY_LOSS_PCT = _env_float("MAX_DAILY_LOSS_PCT", 1.25)
MAX_ASSET_DRAWDOWN_PCT = _env_float("MAX_ASSET_DRAWDOWN_PCT", 3.0)
MAX_GLOBAL_DRAWDOWN_PCT = _env_float("MAX_GLOBAL_DRAWDOWN_PCT", 5.0)
MAX_OPEN_POSITIONS = _env_int("MAX_OPEN_POSITIONS", 3)


def reload_config_from_env() -> dict:
    """Recarrega thresholds do .env em runtime sem reiniciar o bot.

    Use isso depois de editar o .env e quiser que mudancas tomem efeito.
    Retorna dict com valores efetivos.
    """
    global RISK_GUARD_ENABLED, MAX_DAILY_LOSS_PCT, MAX_ASSET_DRAWDOWN_PCT
    global MAX_GLOBAL_DRAWDOWN_PCT, MAX_OPEN_POSITIONS, MAX_CONSECUTIVE_LOSSES
    global ROUNDTRIP_FEE_PCT, SLIPPAGE_PCT, MAX_POSITION_USD, MIN_POSITION_USD
    load_dotenv(override=True)
    RISK_GUARD_ENABLED = _env_bool("RISK_GUARD_ENABLED", True)
    MAX_DAILY_LOSS_PCT = _env_float("MAX_DAILY_LOSS_PCT", 1.25)
    MAX_ASSET_DRAWDOWN_PCT = _env_float("MAX_ASSET_DRAWDOWN_PCT", 3.0)
    MAX_GLOBAL_DRAWDOWN_PCT = _env_float("MAX_GLOBAL_DRAWDOWN_PCT", 5.0)
    MAX_OPEN_POSITIONS = _env_int("MAX_OPEN_POSITIONS", 3)
    MAX_CONSECUTIVE_LOSSES = _env_int("MAX_CONSECUTIVE_LOSSES", 2)
    ROUNDTRIP_FEE_PCT = _env_float("PAPER_ROUNDTRIP_FEE_PCT", 0.20)
    SLIPPAGE_PCT = _env_float("PAPER_SLIPPAGE_PCT", 0.02)
    MAX_POSITION_USD = _env_float("PAPER_MAX_POSITION_USD", 250.0)
    MIN_POSITION_USD = _env_float("PAPER_MIN_POSITION_USD", 5.0)
    return {
        "RISK_GUARD_ENABLED": RISK_GUARD_ENABLED,
        "MAX_DAILY_LOSS_PCT": MAX_DAILY_LOSS_PCT,
        "MAX_ASSET_DRAWDOWN_PCT": MAX_ASSET_DRAWDOWN_PCT,
        "MAX_GLOBAL_DRAWDOWN_PCT": MAX_GLOBAL_DRAWDOWN_PCT,
        "MAX_OPEN_POSITIONS": MAX_OPEN_POSITIONS,
        "MAX_CONSECUTIVE_LOSSES": MAX_CONSECUTIVE_LOSSES,
        "ROUNDTRIP_FEE_PCT": ROUNDTRIP_FEE_PCT,
        "SLIPPAGE_PCT": SLIPPAGE_PCT,
        "MAX_POSITION_USD": MAX_POSITION_USD,
        "MIN_POSITION_USD": MIN_POSITION_USD,
    }
MAX_CONSECUTIVE_LOSSES = _env_int("MAX_CONSECUTIVE_LOSSES", 2)
MAX_POSITION_USD = _env_float("PAPER_MAX_POSITION_USD", 250.0)
MAX_POSITION_PCT = _env_float("PAPER_MAX_POSITION_PCT", 25.0)
MAX_TOTAL_ALLOCATED_PCT = _env_float("PAPER_MAX_TOTAL_ALLOCATED_PCT", 70.0)
MIN_CASH_RESERVE_USD = _env_float("PAPER_MIN_CASH_RESERVE_USD", 100.0)
MIN_POSITION_USD = _env_float("PAPER_MIN_POSITION_USD", 5.0)
ROUNDTRIP_FEE_PCT = _env_float("PAPER_ROUNDTRIP_FEE_PCT", 0.20)
SLIPPAGE_PCT = _env_float("PAPER_SLIPPAGE_PCT", 0.02)
AUTO_JUDGE_ENABLED = _env_bool("PAPER_AUTO_JUDGE_ENABLED", True)
JUDGE_MIN_TRADES = _env_int("PAPER_JUDGE_MIN_TRADES", 3)
JUDGE_MAX_LOSS_STREAK = _env_int("PAPER_JUDGE_MAX_LOSS_STREAK", 3)
JUDGE_MIN_WINRATE_PCT = _env_float("PAPER_JUDGE_MIN_WINRATE_PCT", 35.0)
JUDGE_MAX_DRAWDOWN_USD = _env_float("PAPER_JUDGE_MAX_DRAWDOWN_USD", 15.0)

# KILL_SWITCH fora de src/logs (que pode ser limpo por scripts).
# - Global: KILL_SWITCH na raiz -> emergencia, liquidacao a mercado.
# - Per-asset: src/logs/kill_switch/<ASSET>.json -> bloqueia novas, mantem
#   posicao ate proximo sinal natural (sem slippage forcado).
KILL_SWITCH_FILE = Path(os.getenv("KILL_SWITCH_FILE", "KILL_SWITCH"))
KILL_SWITCH_PER_ASSET_DIR = Path(os.getenv("KILL_SWITCH_PER_ASSET_DIR", "src/logs/kill_switch"))


def _global_kill_active() -> tuple[bool, str]:
    if KILL_SWITCH_FILE.exists():
        try:
            reason = KILL_SWITCH_FILE.read_text(encoding="utf-8").strip() or "kill switch acionado"
        except Exception:
            reason = "kill switch acionado"
        return True, reason
    return False, ""


def _asset_kill_active(asset: str) -> tuple[bool, str, dict]:
    """Le src/logs/kill_switch/<ASSET>.json. Auto-expira se expires_at passou."""
    if not asset:
        return False, "", {}
    path = KILL_SWITCH_PER_ASSET_DIR / f"{asset.upper()}.json"
    if not path.exists():
        return False, "", {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return False, "", {}
    expires_at = data.get("expires_at")
    if expires_at:
        try:
            from datetime import datetime, timezone
            exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if exp < datetime.now(timezone.utc):
                # expirou -> remove e libera
                try: path.unlink()
                except Exception: pass
                return False, "", {}
        except Exception:
            pass
    reason = data.get("reason") or "kill por asset acionado"
    return True, str(reason), data


def kill_switch_active(asset: str | None = None) -> tuple[bool, str]:
    """Retorna (True, motivo) se kill global OU kill do asset estiver ativo.

    Compativel com chamadas antigas sem `asset` (so checa global).
    """
    g_active, g_reason = _global_kill_active()
    if g_active:
        return True, g_reason
    if asset:
        a_active, a_reason, _ = _asset_kill_active(asset)
        if a_active:
            return True, a_reason
    return False, ""


def list_active_kills() -> dict:
    """Retorna estado consolidado dos kills (pra dashboard / debug)."""
    out = {"global": None, "assets": []}
    g_active, g_reason = _global_kill_active()
    if g_active:
        out["global"] = {"reason": g_reason}
    if KILL_SWITCH_PER_ASSET_DIR.exists():
        for p in KILL_SWITCH_PER_ASSET_DIR.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8-sig"))
                data["asset"] = p.stem.upper()
                out["assets"].append(data)
            except Exception:
                continue
    return out


def _load_state():
    if STATE_JSON.exists():
        try:
            with open(STATE_JSON, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            # JSON corrompido: tenta backup .bak antes de zerar
            bak = STATE_JSON.with_suffix(STATE_JSON.suffix + ".bak")
            if bak.exists():
                try:
                    with open(bak, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass
            return {}
    return {}


def _save_state(state):
    """Atomic write + file-lock inter-processo. Escreve .tmp e renomeia. Mantem .bak."""
    from src.modules.FileLock import file_lock

    tmp = STATE_JSON.with_suffix(STATE_JSON.suffix + ".tmp")
    bak = STATE_JSON.with_suffix(STATE_JSON.suffix + ".bak")
    lock = STATE_JSON.with_suffix(STATE_JSON.suffix + ".lock")
    with file_lock(lock, timeout=15.0):
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except (OSError, AttributeError):
                pass
        if STATE_JSON.exists():
            try:
                os.replace(STATE_JSON, bak)
            except OSError:
                pass
        os.replace(tmp, STATE_JSON)


def _iter_assets(state):
    return ((asset, data) for asset, data in state.items() if not str(asset).startswith("_") and isinstance(data, dict))


def _ensure_meta_state(state):
    meta = state.setdefault("_meta", {})
    meta.setdefault("total_bankroll", TOTAL_BANKROLL)
    meta.setdefault("strategy_stats", {})
    meta.setdefault("last_votes", {})
    meta.setdefault("paused_strategies", {})
    allocated = 0.0
    market_value = 0.0
    realized_pnl = 0.0
    for _, asset_state in _iter_assets(state):
        _ensure_balance_fields(asset_state)
        allocated += float(asset_state.get("allocated_usd") or 0.0)
        market_value += float(asset_state.get("market_value") or 0.0)
        realized_pnl += float(asset_state.get("total_pnl_usd") or 0.0)
    total = float(meta.get("total_bankroll") or TOTAL_BANKROLL)
    meta.setdefault("cash_balance", total - allocated)
    meta["allocated_usd"] = allocated
    meta["market_value"] = market_value
    meta["equity"] = float(meta.get("cash_balance", 0.0)) + market_value
    meta["realized_pnl_usd"] = realized_pnl
    meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
    return meta


def _cost_multiplier() -> float:
    return 1.0 + ((ROUNDTRIP_FEE_PCT / 2.0) + SLIPPAGE_PCT) / 100.0


def _buy_cost_usd(notional: float) -> float:
    return float(notional) * (_cost_multiplier() - 1.0)


def _strategy_stats(meta: dict, name: str) -> dict:
    stats = meta.setdefault("strategy_stats", {}).setdefault(name, {})
    stats.setdefault("trades", 0)
    stats.setdefault("wins", 0)
    stats.setdefault("losses", 0)
    stats.setdefault("pnl_usd", 0.0)
    stats.setdefault("loss_streak", 0)
    stats.setdefault("paused", False)
    stats.setdefault("pause_reason", "")
    # Tier 3: gross profit/loss + profit_factor pro DriftAgent comparar com backtest.
    stats.setdefault("gross_profit", 0.0)
    stats.setdefault("gross_loss", 0.0)
    # profit_factor = gross_profit / abs(gross_loss); 0 quando gross_loss = 0 e
    # gross_profit > 0 => infinite -> capamos em 99 pra serializar bem.
    stats.setdefault("profit_factor", 0.0)
    return stats


def _recalc_profit_factor(stats: dict) -> None:
    """Atualiza stats['profit_factor'] a partir de gross_profit/gross_loss."""
    gp = float(stats.get("gross_profit") or 0.0)
    gl = abs(float(stats.get("gross_loss") or 0.0))
    if gl > 0:
        pf = gp / gl
    elif gp > 0:
        pf = 99.0  # infinito artificial -- nenhuma perda registrada ainda
    else:
        pf = 0.0
    stats["profit_factor"] = round(pf, 4)


def _judge_strategy(meta: dict, name: str) -> None:
    if not AUTO_JUDGE_ENABLED:
        return
    stats = _strategy_stats(meta, name)
    trades = int(stats.get("trades") or 0)
    wins = int(stats.get("wins") or 0)
    loss_streak = int(stats.get("loss_streak") or 0)
    pnl = float(stats.get("pnl_usd") or 0.0)
    winrate = wins / trades * 100 if trades else 0.0
    reason = ""
    if loss_streak >= JUDGE_MAX_LOSS_STREAK:
        reason = f"{loss_streak} perdas seguidas"
    elif trades >= JUDGE_MIN_TRADES and winrate < JUDGE_MIN_WINRATE_PCT:
        reason = f"winrate {winrate:.1f}% < {JUDGE_MIN_WINRATE_PCT:.1f}%"
    elif pnl <= -JUDGE_MAX_DRAWDOWN_USD:
        reason = f"pnl ${pnl:.2f} <= -${JUDGE_MAX_DRAWDOWN_USD:.2f}"
    stats["paused"] = bool(reason)
    stats["pause_reason"] = reason
    paused = meta.setdefault("paused_strategies", {})
    if reason:
        paused[name] = reason
    else:
        paused.pop(name, None)


def _get_asset_state(state, asset, strategy):
    _ensure_meta_state(state)
    if asset not in state:
        state[asset] = {
            "strategy": strategy,
            "balance": 0.0,
            "cash_balance": 0.0,
            "allocated_usd": 0.0,
            "market_value": 0.0,
            "unrealized_pnl_usd": 0.0,
            "initial_balance": float(TOTAL_BANKROLL),
            "position": None,  # dict {entry_price, qty, time, strategy} quando comprado
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl_usd": 0.0,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
    _ensure_balance_fields(state[asset])
    _ensure_risk_state(state[asset])
    return state[asset]


def _ensure_balance_fields(asset_state):
    position = asset_state.get("position")

    if position:
        entry_price = float(position.get("entry_price") or 0.0)
        qty = float(position.get("qty") or 0.0)
        allocated = float(asset_state.get("allocated_usd") or position.get("allocated_usd") or entry_price * qty)
        market_value = float(asset_state.get("market_value") or allocated)
        position.setdefault("allocated_usd", allocated)
        asset_state["cash_balance"] = 0.0
        asset_state["allocated_usd"] = allocated
        asset_state["market_value"] = market_value
        asset_state["unrealized_pnl_usd"] = market_value - allocated
        asset_state["balance"] = market_value
    else:
        asset_state["cash_balance"] = 0.0
        asset_state["allocated_usd"] = 0.0
        asset_state["market_value"] = 0.0
        asset_state["unrealized_pnl_usd"] = 0.0
        asset_state["balance"] = 0.0
    return asset_state


def mark_to_market(asset: str, price: float) -> None:
    """Atualiza valor de mercado e PnL aberto da posição paper."""
    with _lock:
        state = _load_state()
        if asset not in state or not state[asset].get("position"):
            return
        s = state[asset]
        _ensure_balance_fields(s)
        pos = s["position"]
        qty = float(pos.get("qty") or 0.0)
        allocated = float(pos.get("allocated_usd") or pos.get("entry_price", 0.0) * qty)
        market_value = float(price) * qty
        s["allocated_usd"] = allocated
        s["market_value"] = market_value
        s["unrealized_pnl_usd"] = market_value - allocated
        s["balance"] = float(s.get("cash_balance", 0.0)) + market_value
        _ensure_meta_state(state)
        _save_state(state)


def plan_paper_buy(asset: str, strategy: str, price: float, requested_qty: float, override_max_usd: float | None = None) -> tuple[bool, float, str]:
    """Calcula quantidade paper respeitando caixa global e limite por posicao.

    Nota: o backtest do Aether NAO considera MIN_CASH_RESERVE_USD nem position
    caps — ele valida edge da estrategia com capital cheio. Aqui aplicamos as
    travas de risco operacional. Diferenca entre backtest e paper eh esperada
    e contabilizada em `meta.risk_blocked` para visibilidade no dashboard.

    `override_max_usd` permite ao tier de estrategia setar um cap especifico
    (ex: tier 1 quer $400 mesmo com MAX_POSITION_USD global em $100). O cap
    final ainda respeita MAX_POSITION_PCT do bankroll total como sanidade.
    """
    with _lock:
        state = _load_state()
        asset_state = _get_asset_state(state, asset, strategy)
        if asset_state.get("position") is not None:
            return False, 0.0, "posicao paper ja aberta neste ativo"
        meta = _ensure_meta_state(state)
        requested_notional = max(float(price) * float(requested_qty), 0.0)
        free_cash = max(float(meta.get("cash_balance", 0.0)), 0.0)
        total = float(meta.get("total_bankroll") or TOTAL_BANKROLL)
        # Tier override sobrepoe MAX_POSITION_USD global, mas mantem teto pelo
        # MAX_POSITION_PCT do bankroll pra evitar ordem absurda em banca pequena.
        effective_max_usd = float(override_max_usd) if override_max_usd and override_max_usd > 0 else MAX_POSITION_USD
        position_cap = min(effective_max_usd, total * (MAX_POSITION_PCT / 100.0))
        total_cap = total * (MAX_TOTAL_ALLOCATED_PCT / 100.0)
        allocated = float(meta.get("allocated_usd") or 0.0)
        portfolio_room = max(total_cap - allocated, 0.0)
        spendable_cash = max(free_cash - MIN_CASH_RESERVE_USD, 0.0)
        target_notional = min(
            requested_notional,
            position_cap,
            portfolio_room,
            spendable_cash / _cost_multiplier(),
        )
        if target_notional < MIN_POSITION_USD:
            limiting = "caixa"
            if portfolio_room < MIN_POSITION_USD:
                limiting = "exposicao_total"
            elif spendable_cash < MIN_POSITION_USD:
                limiting = "reserva"
            blocked = meta.setdefault("risk_blocked", {})
            blocked[limiting] = int(blocked.get(limiting, 0)) + 1
            blocked["total"] = int(blocked.get("total", 0)) + 1
            blocked["last_at"] = datetime.now().isoformat(timespec="seconds")
            _save_state(state)
            reason = (
                f"saldo/exposicao insuficiente ({limiting}): livre ${free_cash:.2f}, "
                f"reserva ${MIN_CASH_RESERVE_USD:.2f}, espaco portfolio ${portfolio_room:.2f}"
            )
            return False, 0.0, reason
        qty = target_notional / float(price)
        reason = ""
        if target_notional < requested_notional:
            reason = f"quantidade ajustada para ${target_notional:.2f} por caixa, reserva e exposicao"
        return True, qty, reason


def get_position_owners(asset: str) -> list[str]:
    """Retorna lista de estrategias que votaram BUY na abertura desta posicao.

    Usado pelo modo "owner decides": so a estrategia dona pode votar venda.
    Retorna [] se nao ha posicao aberta ou se nao temos registro dos votos.
    """
    with _lock:
        state = _load_state()
        a = state.get(asset)
        if not isinstance(a, dict):
            return []
        position = a.get("position")
        if not isinstance(position, dict):
            return []
        votes = position.get("entry_votes") or []
        return [str(v) for v in votes if v]


def get_open_position_qty(asset: str) -> float:
    """Quantidade aberta no paper para um ativo."""
    with _lock:
        state = _load_state()
        position = state.get(asset, {}).get("position") if isinstance(state.get(asset), dict) else None
        if not isinstance(position, dict):
            return 0.0
        return float(position.get("qty") or 0.0)


def record_strategy_votes(asset: str, snapshot: dict) -> None:
    """Salva o ultimo mapa de votos para auditoria do dashboard."""
    with _lock:
        state = _load_state()
        meta = _ensure_meta_state(state)
        payload = dict(snapshot)
        payload["asset"] = asset
        payload["time"] = datetime.now().isoformat(timespec="seconds")
        meta.setdefault("last_votes", {})[asset] = payload
        for name in payload.get("buy_votes", []) + payload.get("sell_votes", []) + payload.get("neutral_votes", []):
            _strategy_stats(meta, str(name))
        _save_state(state)


def get_paused_strategies() -> dict[str, str]:
    with _lock:
        state = _load_state()
        meta = _ensure_meta_state(state)
        return dict(meta.get("paused_strategies") or {})


MAX_STRATEGY_ACTIVITY_ENTRIES = _env_int("MAX_STRATEGY_ACTIVITY_ENTRIES", 200)


def _prune_strategy_activity(activity: dict) -> None:
    """Mantem somente os MAX_STRATEGY_ACTIVITY_ENTRIES mais recentes."""
    if len(activity) <= MAX_STRATEGY_ACTIVITY_ENTRIES:
        return
    # Ordena por updated_at decrescente; remove o resto.
    items = sorted(
        activity.items(),
        key=lambda kv: kv[1].get("updated_at", ""),
        reverse=True,
    )
    keep = dict(items[:MAX_STRATEGY_ACTIVITY_ENTRIES])
    activity.clear()
    activity.update(keep)


def update_strategy_activity(strategy_name: str, voted: bool) -> None:
    """Conta total de chamadas e quantas foram votos nao-neutros.
    Permite identificar estrategias mortas (sempre retornam None)."""
    with _lock:
        state = _load_state()
        meta = _ensure_meta_state(state)
        activity = meta.setdefault("strategy_activity", {})
        rec = activity.setdefault(strategy_name, {"calls": 0, "non_neutral": 0})
        rec["calls"] = int(rec.get("calls") or 0) + 1
        if voted:
            rec["non_neutral"] = int(rec.get("non_neutral") or 0) + 1
        rec["last_voted"] = bool(voted)
        rec["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _prune_strategy_activity(activity)
        _save_state(state)


def record_timeframe_mismatch(strategy_name: str, expected: str, actual: str) -> None:
    """Registra estrategia pulada por timeframe diferente do bot.
    O Aether (ou processo equivalente) pode ler isso e parar de promover essas."""
    with _lock:
        state = _load_state()
        meta = _ensure_meta_state(state)
        mismatches = meta.setdefault("timeframe_mismatches", {})
        rec = mismatches.setdefault(strategy_name, {"count": 0})
        rec["count"] = int(rec.get("count") or 0) + 1
        rec["expected"] = expected
        rec["actual"] = actual
        rec["last_seen"] = datetime.now().isoformat(timespec="seconds")
        _save_state(state)


def record_unknown_entry_mode(mode_name: str) -> None:
    """Loga tentativa de uso de entry_mode invalido (ex: aggressive deprecated)."""
    with _lock:
        state = _load_state()
        meta = _ensure_meta_state(state)
        unknown = meta.setdefault("unknown_entry_modes", {})
        rec = unknown.setdefault(mode_name, {"count": 0})
        rec["count"] = int(rec.get("count") or 0) + 1
        rec["last_seen"] = datetime.now().isoformat(timespec="seconds")
        _save_state(state)


def get_dead_strategies(min_calls: int = 200, max_active_pct: float = 1.0) -> list[str]:
    """Retorna estrategias que nunca (ou quase nunca) votam.
    Default: chamadas >= 200 e taxa de voto < 1%."""
    with _lock:
        state = _load_state()
        meta = _ensure_meta_state(state)
        activity = meta.get("strategy_activity") or {}
        dead = []
        for name, rec in activity.items():
            calls = int(rec.get("calls") or 0)
            non_neutral = int(rec.get("non_neutral") or 0)
            if calls < min_calls:
                continue
            pct = non_neutral / calls * 100 if calls else 0
            if pct < max_active_pct:
                dead.append(name)
        return dead


def update_strategy_performance(meta: dict, strategy_names: list[str], pnl_usd: float) -> None:  # noqa: D401
    pnl_value = float(pnl_usd or 0.0)
    names = list(dict.fromkeys(str(name) for name in strategy_names if name))
    for name in names:
        stats = _strategy_stats(meta, name)
        stats["trades"] = int(stats.get("trades") or 0) + 1
        stats["pnl_usd"] = float(stats.get("pnl_usd") or 0.0) + pnl_value
        if pnl_value > 0:
            stats["wins"] = int(stats.get("wins") or 0) + 1
            stats["loss_streak"] = 0
            stats["gross_profit"] = float(stats.get("gross_profit") or 0.0) + pnl_value
        else:
            stats["losses"] = int(stats.get("losses") or 0) + 1
            stats["loss_streak"] = int(stats.get("loss_streak") or 0) + 1
            # gross_loss e armazenado como NEGATIVO (somatorio direto dos pnls perdedores)
            stats["gross_loss"] = float(stats.get("gross_loss") or 0.0) + pnl_value
        _recalc_profit_factor(stats)
        _judge_strategy(meta, name)


def _ensure_risk_state(asset_state):
    today = datetime.now().date().isoformat()
    risk = asset_state.setdefault("risk", {})
    if risk.get("day") != today:
        risk["day"] = today
        risk["day_start_balance"] = float(asset_state.get("balance", INITIAL_BALANCE))
        risk["daily_realized_pnl"] = 0.0
    risk.setdefault("consecutive_losses", 0)
    risk.setdefault("last_trade_pnl_pct", 0.0)
    risk.setdefault("last_block_reason", "")
    return risk


def _append_trade_csv(row):
    header = not TRADES_CSV.exists()
    with open(TRADES_CSV, "a", encoding="utf-8") as f:
        if header:
            f.write("timestamp,asset,strategy,side,price,qty,pnl_usd,pnl_pct,balance_after\n")
        f.write(",".join(str(row[k]) for k in ["timestamp","asset","strategy","side","price","qty","pnl_usd","pnl_pct","balance_after"]) + "\n")


def _write_summary_legacy_disabled(state):
    lines = []
    lines.append("=" * 90)
    lines.append(f"📊 PAPER TRADING — BANCA TOTAL ${TOTAL_BANKROLL:.0f} (${INITIAL_BALANCE:.0f}/ativo × {NUM_ASSETS} ativos)")
    lines.append(f"   Última atualização: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    lines.append("=" * 90)
    lines.append(f"{'Ativo':<10} {'Estratégia':<28} {'Balance':>12} {'P&L $':>10} {'P&L %':>8} {'Trades':>7} {'WR':>6} {'LossSeq':>7} {'Pos':>7}")
    lines.append("-" * 90)

    total_balance = 0.0
    total_invested = 0.0
    for asset, s in sorted(state.items()):
        risk = _ensure_risk_state(s)
        bal = s["balance"]
        init = s["initial_balance"]
        pnl_usd = bal - init
        pnl_pct = pnl_usd / init * 100 if init else 0.0
        trades = s["total_trades"]
        wr = (s["wins"] / trades * 100) if trades else 0.0
        pos = "LONG" if s.get("position") else "-"
        strat = s.get("strategy", "-")
        loss_seq = int(risk.get("consecutive_losses", 0))
        total_balance += bal
        total_invested += init
        lines.append(f"{asset:<10} {strat:<28} ${bal:>10.2f} ${pnl_usd:>+8.2f} {pnl_pct:>+7.2f}% {trades:>7} {wr:>5.1f}% {loss_seq:>7} {pos:>7}")

    total_pnl = total_balance - total_invested
    total_pct = total_pnl / total_invested * 100 if total_invested else 0.0
    lines.append("-" * 90)
    lines.append(f"{'TOTAL':<10} {'':<28} ${total_balance:>10.2f} ${total_pnl:>+8.2f} {total_pct:>+7.2f}% (investido ${total_invested:.0f})")
    lines.append("=" * 90)

    text = "\n".join(lines)
    with open(SUMMARY_TXT, "w", encoding="utf-8") as f:
        f.write(text)
    return text


def _write_summary(state):
    meta = _ensure_meta_state(state)
    total_bankroll = float(meta.get("total_bankroll") or TOTAL_BANKROLL)
    equity = float(meta.get("equity") or total_bankroll)
    cash = float(meta.get("cash_balance") or 0.0)
    allocated = float(meta.get("allocated_usd") or 0.0)
    market_value = float(meta.get("market_value") or 0.0)
    total_pnl = equity - total_bankroll
    total_pct = total_pnl / total_bankroll * 100 if total_bankroll else 0.0

    lines = [
        "=" * 90,
        f"PAPER TRADING - BANCA GLOBAL ${total_bankroll:.2f}",
        (
            f"   Livre: ${cash:.2f} | Alocado: ${allocated:.2f} | Mercado: ${market_value:.2f} | "
            f"Patrimonio: ${equity:.2f} | PnL: ${total_pnl:+.2f} ({total_pct:+.2f}%)"
        ),
        f"   Ultima atualizacao: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}",
        "=" * 90,
        f"{'Ativo':<10} {'Estrategia':<28} {'Mercado':>12} {'P&L aberto':>12} {'P&L real':>10} {'Trades':>7} {'WR':>6} {'LossSeq':>7} {'Pos':>7}",
        "-" * 90,
    ]

    for asset, s in sorted(_iter_assets(state)):
        risk = _ensure_risk_state(s)
        market = float(s.get("market_value") or 0.0)
        unrealized = float(s.get("unrealized_pnl_usd") or 0.0)
        realized = float(s.get("total_pnl_usd") or 0.0)
        trades = int(s.get("total_trades") or 0)
        wr = (int(s.get("wins") or 0) / trades * 100) if trades else 0.0
        pos = "LONG" if s.get("position") else "-"
        strat = s.get("strategy", "-")
        loss_seq = int(risk.get("consecutive_losses", 0))
        lines.append(
            f"{asset:<10} {strat:<28} ${market:>10.2f} ${unrealized:>+10.2f} "
            f"${realized:>+8.2f} {trades:>7} {wr:>5.1f}% {loss_seq:>7} {pos:>7}"
        )

    lines.extend([
        "-" * 90,
        f"{'TOTAL':<10} {'':<28} ${market_value:>10.2f} ${total_pnl:>+10.2f}",
        "=" * 90,
    ])

    text = "\n".join(lines)
    with open(SUMMARY_TXT, "w", encoding="utf-8") as f:
        f.write(text)
    return text


def log_buy(asset: str, strategy: str, price: float, qty: float, metadata: dict | None = None):
    """Registra abertura de posição comprada."""
    with _lock:
        state = _load_state()
        s = _get_asset_state(state, asset, strategy)
        if s["position"] is not None:
            # Já comprado — ignora duplicidade
            return
        meta = _ensure_meta_state(state)
        ts = datetime.now().isoformat(timespec="seconds")
        notional = float(price) * float(qty)
        cost_usd = _buy_cost_usd(notional)
        total_spend = notional + cost_usd
        cash_before = float(meta.get("cash_balance", TOTAL_BANKROLL))
        if total_spend > cash_before + 1e-9:
            raise ValueError(f"saldo paper insuficiente: precisa ${total_spend:.2f}, livre ${cash_before:.2f}")
        cash_after = cash_before - total_spend
        vote_snapshot = dict(metadata or {})
        entry_strategies = list(vote_snapshot.get("buy_votes") or [strategy])
        s["position"] = {
            "entry_price": price,
            "qty": qty,
            "time": ts,
            "strategy": strategy,
            "allocated_usd": total_spend,
            "entry_cost_usd": cost_usd,
            "entry_total_spend": total_spend,
            "entry_votes": entry_strategies,
            "vote_snapshot": vote_snapshot,
        }
        s["strategy"] = strategy
        s["cash_balance"] = 0.0
        s["allocated_usd"] = total_spend
        s["market_value"] = notional
        s["unrealized_pnl_usd"] = -cost_usd
        s["balance"] = notional
        meta["cash_balance"] = cash_after
        _ensure_meta_state(state)
        _save_state(state)
        # Granularidade: alem do nome do wrapper (MultiStrategy), inclui as
        # estrategias internas que votaram BUY -- assim o dashboard mostra
        # exatamente qual sub-estrategia disparou o trade.
        votes_label = "+".join(str(v) for v in entry_strategies if v) if entry_strategies else strategy
        strategy_csv = f"{strategy}({votes_label})" if votes_label and votes_label != strategy else strategy
        _append_trade_csv({
            "timestamp": ts, "asset": asset, "strategy": strategy_csv, "side": "BUY",
            "price": f"{price:.6f}", "qty": f"{qty:.6f}",
            "pnl_usd": "", "pnl_pct": "", "balance_after": f"{s['balance']:.2f}",
        })
        _write_summary(state)


def log_sell_legacy_disabled(asset: str, price: float, qty: float):
    """Registra fechamento de posição (calcula PnL)."""
    with _lock:
        state = _load_state()
        if asset not in state or state[asset]["position"] is None:
            # Vendeu sem posição aberta? registra mas sem PnL
            return
        s = state[asset]
        risk = _ensure_risk_state(s)
        pos = s["position"]
        entry_price = pos["entry_price"]
        entry_qty = pos["qty"]
        strategy = pos["strategy"]

        pnl_pct = (price - entry_price) / entry_price * 100
        notional = float(pos.get("allocated_usd") or (entry_price * entry_qty))
        proceeds = float(price) * float(qty or entry_qty)
        pnl_usd = proceeds - notional
        s["cash_balance"] = float(s.get("cash_balance", 0.0)) + proceeds
        s["balance"] = s["cash_balance"]
        s["allocated_usd"] = 0.0
        s["market_value"] = 0.0
        s["unrealized_pnl_usd"] = 0.0
        s["total_trades"] += 1
        s["total_pnl_usd"] += pnl_usd
        if pnl_pct > 0:
            s["wins"] += 1
            risk["consecutive_losses"] = 0
        else:
            s["losses"] += 1
            risk["consecutive_losses"] = int(risk.get("consecutive_losses", 0)) + 1
        risk["daily_realized_pnl"] = float(risk.get("daily_realized_pnl", 0.0)) + pnl_usd
        risk["last_trade_pnl_pct"] = pnl_pct
        risk["last_block_reason"] = ""
        s["position"] = None

        ts = datetime.now().isoformat(timespec="seconds")
        _save_state(state)
        _append_trade_csv({
            "timestamp": ts, "asset": asset, "strategy": strategy, "side": "SELL",
            "price": f"{price:.6f}", "qty": f"{qty:.6f}",
            "pnl_usd": f"{pnl_usd:+.2f}", "pnl_pct": f"{pnl_pct:+.3f}",
            "balance_after": f"{s['balance']:.2f}",
        })
        _write_summary(state)


def log_sell(asset: str, price: float, qty: float):
    """Registra fechamento de posicao e devolve o resultado ao caixa global."""
    with _lock:
        state = _load_state()
        if asset not in state or state[asset].get("position") is None:
            return
        s = state[asset]
        meta = _ensure_meta_state(state)
        risk = _ensure_risk_state(s)
        pos = s["position"]
        entry_price = float(pos["entry_price"])
        entry_qty = float(pos["qty"])
        strategy = pos["strategy"]

        sell_qty = float(qty or entry_qty)
        # `notional` ja inclui custo de entrada (calculado em _buy_cost_usd como
        # metade da fee round-trip + slippage). Aqui cobramos apenas a OUTRA metade
        # da fee (a perna de venda) + slippage de venda. Antes a fee era cobrada inteira aqui,
        # somando 1.5x fee no round-trip — bug que mascarava lucro paper.
        notional = float(pos.get("allocated_usd") or (entry_price * entry_qty))
        proceeds = float(price) * sell_qty
        fee_usd = proceeds * (ROUNDTRIP_FEE_PCT / 2.0 / 100)
        slippage_usd = proceeds * (SLIPPAGE_PCT / 100)
        proceeds_after_costs = proceeds - fee_usd - slippage_usd
        pnl_usd = proceeds_after_costs - notional
        pnl_pct = (pnl_usd / notional * 100) if notional else 0.0
        entry_votes = list(pos.get("entry_votes") or [strategy])

        meta["cash_balance"] = float(meta.get("cash_balance", 0.0)) + proceeds_after_costs
        s["cash_balance"] = 0.0
        s["balance"] = 0.0
        s["allocated_usd"] = 0.0
        s["market_value"] = 0.0
        s["unrealized_pnl_usd"] = 0.0
        s["total_trades"] += 1
        s["total_pnl_usd"] += pnl_usd
        if pnl_usd > 0:
            s["wins"] += 1
            risk["consecutive_losses"] = 0
        else:
            s["losses"] += 1
            risk["consecutive_losses"] = int(risk.get("consecutive_losses", 0)) + 1
        risk["daily_realized_pnl"] = float(risk.get("daily_realized_pnl", 0.0)) + pnl_usd
        risk["last_trade_pnl_pct"] = pnl_pct
        risk["last_block_reason"] = ""
        update_strategy_performance(meta, entry_votes, pnl_usd)
        s["position"] = None

        ts = datetime.now().isoformat(timespec="seconds")
        _ensure_meta_state(state)
        _save_state(state)
        votes_label = "+".join(str(v) for v in entry_votes if v) if entry_votes else strategy
        strategy_csv = f"{strategy}({votes_label})" if votes_label and votes_label != strategy else strategy
        _append_trade_csv({
            "timestamp": ts, "asset": asset, "strategy": strategy_csv, "side": "SELL",
            "price": f"{price:.6f}", "qty": f"{sell_qty:.6f}",
            "pnl_usd": f"{pnl_usd:+.2f}", "pnl_pct": f"{pnl_pct:+.3f}",
            "balance_after": f"{state['_meta']['equity']:.2f}",
        })
        _write_summary(state)


def reset_all():
    """Zera o estado de todos os ativos. Use pra começar do zero."""
    with _lock:
        if STATE_JSON.exists():
            STATE_JSON.unlink()
        if TRADES_CSV.exists():
            TRADES_CSV.unlink()
        if SUMMARY_TXT.exists():
            SUMMARY_TXT.unlink()

def reset_clean_simulation() -> dict:
    """Inicia uma simulacao paper limpa com banca global."""
    with _lock:
        if TRADES_CSV.exists():
            TRADES_CSV.unlink()
        state = {
            "_meta": {
                "total_bankroll": TOTAL_BANKROLL,
                "cash_balance": TOTAL_BANKROLL,
                "allocated_usd": 0.0,
                "market_value": 0.0,
                "equity": TOTAL_BANKROLL,
                "realized_pnl_usd": 0.0,
                "strategy_stats": {},
                "last_votes": {},
                "paused_strategies": {},
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        }
        _save_state(state)
        _write_summary(state)
        return state


def get_summary() -> str:
    """Retorna texto do resumo atual."""
    state = _load_state()
    return _write_summary(state)


def get_risk_snapshot_legacy_disabled(asset: str, strategy: str = "-") -> dict:
    """Retorna o estado de risco atual de um ativo."""
    with _lock:
        state = _load_state()
        s = _get_asset_state(state, asset, strategy)
        risk = _ensure_risk_state(s)
        balance = float(s.get("balance", INITIAL_BALANCE))
        initial = float(s.get("initial_balance", INITIAL_BALANCE))
        day_start = float(risk.get("day_start_balance", balance))
        daily_pnl_pct = ((balance - day_start) / day_start * 100) if day_start else 0.0
        drawdown_pct = ((initial - balance) / initial * 100) if initial else 0.0
        return {
            "asset": asset,
            "balance": balance,
            "daily_pnl_pct": daily_pnl_pct,
            "drawdown_pct": max(drawdown_pct, 0.0),
            "consecutive_losses": int(risk.get("consecutive_losses", 0)),
            "position_open": s.get("position") is not None,
            "last_block_reason": risk.get("last_block_reason", ""),
        }


def can_open_new_position_legacy_disabled(asset: str, strategy: str = "-") -> tuple[bool, str]:
    """
    Bloqueia novas entradas quando o ativo esta em perda sequencial.
    As travas sao lidas do .env para facilitar ajuste sem tocar no codigo.
    """
    if not RISK_GUARD_ENABLED:
        return True, ""

    with _lock:
        state = _load_state()
        s = _get_asset_state(state, asset, strategy)
        risk = _ensure_risk_state(s)
        balance = float(s.get("balance", INITIAL_BALANCE))
        initial = float(s.get("initial_balance", INITIAL_BALANCE))
        day_start = float(risk.get("day_start_balance", balance))
        daily_pnl_pct = ((balance - day_start) / day_start * 100) if day_start else 0.0
        drawdown_pct = ((initial - balance) / initial * 100) if initial else 0.0
        loss_seq = int(risk.get("consecutive_losses", 0))

        reason = ""
        if daily_pnl_pct <= -MAX_DAILY_LOSS_PCT:
            reason = f"perda diaria {daily_pnl_pct:.2f}% <= -{MAX_DAILY_LOSS_PCT:.2f}%"
        elif drawdown_pct >= MAX_ASSET_DRAWDOWN_PCT:
            reason = f"drawdown do ativo {drawdown_pct:.2f}% >= {MAX_ASSET_DRAWDOWN_PCT:.2f}%"
        elif loss_seq >= MAX_CONSECUTIVE_LOSSES:
            reason = f"{loss_seq} perdas seguidas >= limite {MAX_CONSECUTIVE_LOSSES}"

        risk["last_block_reason"] = reason
        _save_state(state)
        _write_summary(state)
        return (not reason), reason


def get_risk_snapshot(asset: str, strategy: str = "-") -> dict:
    """Retorna risco do ativo usando a banca global como referencia."""
    with _lock:
        state = _load_state()
        s = _get_asset_state(state, asset, strategy)
        risk = _ensure_risk_state(s)
        meta = _ensure_meta_state(state)
        balance = float(meta.get("equity", TOTAL_BANKROLL))
        initial = float(meta.get("total_bankroll", TOTAL_BANKROLL))
        day_start = float(risk.get("day_start_balance") or initial)
        daily_pnl_pct = ((balance - day_start) / day_start * 100) if day_start else 0.0
        drawdown_pct = ((initial - balance) / initial * 100) if initial else 0.0
        return {
            "asset": asset,
            "balance": balance,
            "daily_pnl_pct": daily_pnl_pct,
            "drawdown_pct": max(drawdown_pct, 0.0),
            "consecutive_losses": int(risk.get("consecutive_losses", 0)),
            "position_open": s.get("position") is not None,
            "last_block_reason": risk.get("last_block_reason", ""),
        }


def can_open_new_position(asset: str, strategy: str = "-") -> tuple[bool, str]:
    """Bloqueia entrada por kill switch, drawdown global, sequencia de perdas ou posicao aberta."""
    killed, kill_reason = kill_switch_active(asset)
    if killed:
        return False, f"KILL SWITCH: {kill_reason}"

    if not RISK_GUARD_ENABLED:
        return True, ""

    with _lock:
        state = _load_state()
        s = _get_asset_state(state, asset, strategy)
        risk = _ensure_risk_state(s)
        if s.get("position") is not None:
            reason = "posicao paper ja aberta neste ativo"
            risk["last_block_reason"] = reason
            _save_state(state)
            _write_summary(state)
            return False, reason

        meta = _ensure_meta_state(state)
        balance = float(meta.get("equity", TOTAL_BANKROLL))
        initial = float(meta.get("total_bankroll", TOTAL_BANKROLL))
        day_start = float(risk.get("day_start_balance") or initial)
        daily_pnl_pct = ((balance - day_start) / day_start * 100) if day_start else 0.0
        drawdown_pct = ((initial - balance) / initial * 100) if initial else 0.0
        loss_seq = int(risk.get("consecutive_losses", 0))

        # Drawdown global agregado: usa equity da banca total, nao por ativo.
        global_dd_pct = ((initial - balance) / initial * 100) if initial else 0.0

        # Conta posicoes abertas em outros ativos.
        open_positions = sum(1 for _, asset_state in _iter_assets(state) if asset_state.get("position") is not None)

        reason = ""
        if global_dd_pct >= MAX_GLOBAL_DRAWDOWN_PCT:
            reason = f"CIRCUIT BREAKER: drawdown global {global_dd_pct:.2f}% >= {MAX_GLOBAL_DRAWDOWN_PCT:.2f}%"
        elif open_positions >= MAX_OPEN_POSITIONS:
            reason = f"posicoes abertas {open_positions} >= MAX_OPEN_POSITIONS {MAX_OPEN_POSITIONS}"
        elif daily_pnl_pct <= -MAX_DAILY_LOSS_PCT:
            reason = f"perda diaria {daily_pnl_pct:.2f}% <= -{MAX_DAILY_LOSS_PCT:.2f}%"
        elif drawdown_pct >= MAX_ASSET_DRAWDOWN_PCT:
            reason = f"drawdown do ativo {drawdown_pct:.2f}% >= {MAX_ASSET_DRAWDOWN_PCT:.2f}%"
        elif loss_seq >= MAX_CONSECUTIVE_LOSSES:
            reason = f"{loss_seq} perdas seguidas >= limite {MAX_CONSECUTIVE_LOSSES}"

        risk["last_block_reason"] = reason
        _save_state(state)
        _write_summary(state)
        return (not reason), reason
