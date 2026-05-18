from __future__ import annotations

import csv
import json
import os
import re
import socket
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from src.modules.PaperBalanceLogger import reset_clean_simulation


app = FastAPI(title="HAPPYNATIONBOT Trade Dashboard")

ROOT = Path(__file__).resolve().parents[1]
PAPER_DIR = ROOT / "src" / "logs" / "paper_balance"
PAPER_STATE = PAPER_DIR / "state.json"
TRADES_CSV = PAPER_DIR / "trades.csv"
ACTIVE_STRATEGIES = ROOT / "src" / "logs" / "research" / "active_strategies.json"
TERMINAL_LOG = ROOT / "src" / "logs" / "terminal.log"
TRADING_BOT_LOG = ROOT / "src" / "logs" / "trading_bot.log"
BOT_LOCK = ROOT / "src" / "logs" / ".bot.lock"
MAIN_FILE = ROOT / "src" / "main.py"

state: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def update(symbol: str, data: dict):
    with _lock:
        if symbol not in state:
            state[symbol] = {}
        state[symbol].update(data)
        state[symbol]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_state() -> dict:
    with _lock:
        return dict(state)


_json_alerts: dict[str, dict[str, Any]] = {}


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        _json_alerts.pop(str(path), None)
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        _json_alerts.pop(str(path), None)
        return data
    except Exception as exc:
        _json_alerts[str(path)] = {
            "path": str(path),
            "error": f"{type(exc).__name__}: {exc}",
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return default


def get_json_alerts() -> list[dict[str, Any]]:
    return list(_json_alerts.values())


def _tail_lines(path: Path, limit: int = 200) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except Exception:
        return []


def _read_trades(limit: int = 80) -> list[dict[str, Any]]:
    if not TRADES_CSV.exists():
        return []
    try:
        with TRADES_CSV.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return []
    return rows[-limit:][::-1]


def _configured_loop_assets() -> set[str]:
    if not MAIN_FILE.exists():
        return set()
    try:
        source = MAIN_FILE.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return set()
    active_source = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    symbols_by_var: dict[str, str] = {}
    for match in re.finditer(
        r"(\w+)\s*=\s*StockStartModel\([\s\S]*?baseStock\s*=\s*\"([A-Z0-9]+)\"[\s\S]*?quoteStock\s*=\s*\"([A-Z0-9]+)\"",
        active_source,
    ):
        symbols_by_var[match.group(1)] = f"{match.group(2)}{match.group(3)}"
    matches = list(re.finditer(r"stocks_traded_list\s*=\s*\[([^\]]*)\]", active_source))
    list_match = matches[-1] if matches else None
    if not list_match:
        return set(symbols_by_var.values())
    names = re.findall(r"\b[A-Z0-9_]+USDT\b|\b[A-Z0-9_]+\b", list_match.group(1))
    return {symbols_by_var[name] for name in names if name in symbols_by_var}


def _active_assets_from_bot(live: dict[str, Any]) -> set[str]:
    assets = _configured_loop_assets()
    if not assets:
        assets = {str(asset) for asset in live.keys() if not str(asset).startswith("_")}
    else:
        assets.update(str(asset) for asset in live.keys() if not str(asset).startswith("_"))
    return assets


def _strategy_key(strategy: dict[str, Any]) -> str:
    engine = strategy.get("engine") or strategy.get("template") or "?"
    return f"Aether:{engine}#{str(strategy.get('id') or '')[-8:]}"


def _active_strategy_rows(paper: dict[str, Any], live: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _read_json(ACTIVE_STRATEGIES, {"strategies": []})
    strategies = payload.get("strategies") if isinstance(payload, dict) else []
    if not isinstance(strategies, list):
        strategies = []
    active_assets = _active_assets_from_bot(live)
    lines = _tail_lines(TERMINAL_LOG, 1200)
    out: list[dict[str, Any]] = []
    for item in strategies:
        if not isinstance(item, dict):
            continue
        summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
        name = summary.get("name") or item.get("id") or "Aether strategy"
        key = _strategy_key(item)
        asset = str(item.get("asset") or item.get("symbol") or "")
        hits = [line for line in lines if key in line or str(name)[:70] in line]
        out.append(
            {
                "id": item.get("id"),
                "key": key,
                "name": name,
                "asset": asset,
                "engine": item.get("engine") or item.get("template"),
                "timeframe": item.get("timeframe"),
                "score": item.get("score"),
                "source": item.get("source"),
                "deployed_at": item.get("deployed_at"),
                "pnl_pct": summary.get("pnl_pct"),
                "sharpe": summary.get("sharpe"),
                "mdd_pct": summary.get("mdd_pct"),
                "trade_count": summary.get("trade_count"),
                "validation_passed": summary.get("validation_passed"),
                "validation_stage": summary.get("validation_stage"),
                "critic_status": summary.get("critic_status"),
                "critic_reason": summary.get("critic_reason"),
                "in_bot_loop": asset in active_assets,
                "selected_count": len(hits),
                "last_selected": hits[-1] if hits else "",
                "risk": item.get("risk") if isinstance(item.get("risk"), dict) else {},
            }
        )
    return out


def _paper_rows(paper: dict[str, Any], live: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    paper_assets = {k for k in paper.keys() if not str(k).startswith("_")}
    live_assets = {k for k in live.keys() if not str(k).startswith("_")}
    for asset in sorted(paper_assets | live_assets):
        p = paper.get(asset, {}) if isinstance(paper.get(asset), dict) else {}
        l = live.get(asset, {}) if isinstance(live.get(asset), dict) else {}
        position = p.get("position") if isinstance(p.get("position"), dict) else None
        entry_price = float(position.get("entry_price") or 0) if position else 0.0
        qty = float(position.get("qty") or 0) if position else 0.0
        allocated_usd = float(p.get("allocated_usd") or entry_price * qty) if position else 0.0
        live_price = l.get("price")
        market_value = float(p.get("market_value") or allocated_usd)
        if position and live_price:
            market_value = float(live_price) * qty
        cash_balance = float(p.get("cash_balance") or 0.0)
        equity = market_value if position else 0.0
        unrealized_pnl_usd = market_value - allocated_usd if position else 0.0
        realized_pnl_usd = float(p.get("total_pnl_usd") or 0.0)
        trades = int(p.get("total_trades") or 0)
        wins = int(p.get("wins") or 0)
        pnl_basis = allocated_usd if position and allocated_usd else abs(realized_pnl_usd)
        peak_usd = position.get("peak_unrealized_usd") if position else None
        trough_usd = position.get("trough_unrealized_usd") if position else None
        entry_time = position.get("time") if position else None
        entry_votes = position.get("entry_votes") if position else None
        vote_snapshot = position.get("vote_snapshot") if position else None
        tier_cfg = vote_snapshot.get("tier_config") if isinstance(vote_snapshot, dict) else None
        strategy_key = ""
        if isinstance(entry_votes, list) and entry_votes:
            strategy_key = str(entry_votes[0])
        rows.append(
            {
                "asset": asset,
                "strategy": p.get("strategy") or "-",
                "balance": equity,
                "cash_balance": cash_balance,
                "allocated_usd": allocated_usd,
                "market_value": market_value if position else 0.0,
                "unrealized_pnl_usd": unrealized_pnl_usd,
                "realized_pnl_usd": realized_pnl_usd,
                "initial_balance": 0.0,
                "pnl_usd": unrealized_pnl_usd + realized_pnl_usd,
                "pnl_pct": ((unrealized_pnl_usd + realized_pnl_usd) / pnl_basis * 100) if pnl_basis else 0,
                "trades": trades,
                "wins": wins,
                "losses": int(p.get("losses") or 0),
                "winrate": (wins / trades * 100) if trades else 0,
                "position": bool(position),
                "entry_price": entry_price,
                "qty": qty,
                "entry_time": entry_time,
                "peak_unrealized_usd": float(peak_usd) if peak_usd is not None else None,
                "trough_unrealized_usd": float(trough_usd) if trough_usd is not None else None,
                "live_price": live_price,
                "live_decision": l.get("decision"),
                "live_cycle": l.get("cycle"),
                "live_updated_at": l.get("updated_at"),
                "risk": p.get("risk") if isinstance(p.get("risk"), dict) else {},
                "tier_config": tier_cfg if isinstance(tier_cfg, dict) else None,
                "vote_snapshot": vote_snapshot if isinstance(vote_snapshot, dict) else None,
                "strategy_key": strategy_key,
                "entry_votes": entry_votes if isinstance(entry_votes, list) else [],
            }
        )
    return rows


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", ""))
    except Exception:
        return None


def _today_stats(paper_rows: list[dict[str, Any]], trades: list[dict[str, Any]]) -> dict[str, Any]:
    today = datetime.now().date()
    realized_today = 0.0
    closed_today = 0
    wins_today = 0
    for tr in trades:
        if str(tr.get("side")) != "SELL":
            continue
        ts = _parse_iso(str(tr.get("timestamp") or ""))
        if not ts or ts.date() != today:
            continue
        try:
            pnl = float(tr.get("pnl_usd") or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        realized_today += pnl
        closed_today += 1
        if pnl > 0:
            wins_today += 1
    unrealized_open = sum(float(r.get("unrealized_pnl_usd") or 0) for r in paper_rows if r.get("position"))
    exposure = sum(float(r.get("allocated_usd") or 0) for r in paper_rows if r.get("position"))
    return {
        "result_today_usd": round(realized_today + unrealized_open, 2),
        "realized_today_usd": round(realized_today, 2),
        "unrealized_open_usd": round(unrealized_open, 2),
        "closed_today": closed_today,
        "winrate_today": round((wins_today / closed_today * 100) if closed_today else 0, 1),
        "exposure_open_usd": round(exposure, 2),
        "open_positions": sum(1 for r in paper_rows if r.get("position")),
    }


def _strategy_to_key_and_tf(strategy_full: str) -> tuple[str, str]:
    """Extrai 'Aether:engine#hash' e timeframe (se houver) de strings tipo
    'MultiStrategy(Aether:combo#abcd1234)'. Retorna (key, tf) ou (raw, '')."""
    if not strategy_full:
        return "", ""
    text = str(strategy_full)
    m = re.search(r"Aether:[\w]+#[a-f0-9]+", text)
    key = m.group(0) if m else text
    tf_match = re.search(r"\b(1m|3m|5m|15m|30m|1h|2h|4h|6h|12h|1d)\b", text)
    return key, (tf_match.group(0) if tf_match else "")


def _strategy_meta_lookup(strategies: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Indexa estrategias ativas por key (Aether:engine#hash) pra lookup de timeframe."""
    out: dict[str, dict[str, Any]] = {}
    for item in strategies or []:
        key = item.get("key")
        if not key:
            continue
        out[str(key)] = item
    return out


def _equity_by_strategy(trades: list[dict[str, Any]]) -> dict[str, list[float]]:
    """Curva de equity (PnL acumulado) por estrategia, em ordem cronologica.

    Itera SELLs do mais antigo pro mais recente. Cada estrategia recebe uma
    lista de PnL acumulado apos cada trade. Frontend usa isso pra sparkline.
    """
    out: dict[str, list[float]] = {}
    cum: dict[str, float] = {}
    for tr in reversed(list(trades or [])):
        if str(tr.get("side")) != "SELL":
            continue
        try:
            pnl = float(tr.get("pnl_usd") or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        key, _ = _strategy_to_key_and_tf(str(tr.get("strategy") or ""))
        if not key:
            continue
        cum[key] = cum.get(key, 0.0) + pnl
        out.setdefault(key, []).append(round(cum[key], 4))
    return out


def _unified_trades(
    paper_rows: list[dict[str, Any]],
    raw_trades: list[dict[str, Any]],
    strategies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Une posicoes abertas + trades fechados num mesmo formato pra tabela rica.

    Cada item descreve uma operacao do ponto de vista do usuario: entrada, saida,
    pico/vale, take/stop, duracao, estrategia. Status: open | closed.
    """
    strat_meta = _strategy_meta_lookup(strategies)
    items: list[dict[str, Any]] = []
    now_dt = datetime.now()

    # 1) posicoes abertas (uma por ativo, no maximo)
    for r in paper_rows:
        if not r.get("position"):
            continue
        entry = float(r.get("entry_price") or 0)
        qty = float(r.get("qty") or 0)
        live_price = r.get("live_price")
        try:
            live_price_f = float(live_price) if live_price is not None else None
        except (TypeError, ValueError):
            live_price_f = None
        tier = r.get("tier_config") or {}
        sl_usd = tier.get("sl_usd")
        trail_act = tier.get("trail_activation_usd")
        take_price = (entry + (float(trail_act) / qty)) if qty and trail_act not in (None, 0) else None
        stop_price = (entry - (float(sl_usd) / qty)) if qty and sl_usd not in (None, 0) else None
        opened = _parse_iso(r.get("entry_time") or "")
        duration_s = int((now_dt - opened).total_seconds()) if opened else None
        strat_key = r.get("strategy_key") or ""
        if not strat_key:
            strat_key, _ = _strategy_to_key_and_tf(r.get("strategy") or "")
        _, tf = _strategy_to_key_and_tf(r.get("strategy") or "")
        meta_strat = strat_meta.get(strat_key, {})
        if not tf:
            tf = str(meta_strat.get("timeframe") or "")
        items.append({
            "id": f"open:{r['asset']}",
            "status": "open",
            "asset": r["asset"],
            "side": "LONG",
            "size_usd": float(r.get("allocated_usd") or 0),
            "qty": qty,
            "entry_price": entry,
            "exit_price": None,
            "current_price": live_price_f if live_price_f is not None else entry,
            "take_price": take_price,
            "stop_price": stop_price,
            "max_neg_usd": r.get("trough_unrealized_usd"),
            "max_pos_usd": r.get("peak_unrealized_usd"),
            "pnl_usd": float(r.get("unrealized_pnl_usd") or 0),
            "pnl_pct": (float(r.get("unrealized_pnl_usd") or 0) / float(r.get("allocated_usd") or 1) * 100) if r.get("allocated_usd") else 0,
            "opened_at": r.get("entry_time"),
            "closed_at": None,
            "duration_s": duration_s,
            "strategy_key": strat_key,
            "strategy_full": r.get("strategy"),
            "strategy_engine": meta_strat.get("engine") or meta_strat.get("template"),
            "timeframe": tf,
            "vote_snapshot": r.get("vote_snapshot"),
            "tier_config": tier,
            "entry_votes": r.get("entry_votes") or [],
        })

    # 2) trades fechados (cada SELL parea com a BUY anterior do mesmo ativo)
    # raw_trades vem em ordem reversa (mais novo primeiro). Pra cada SELL, a BUY
    # esta em algum indice MAIOR (mais antigo).
    for i, tr in enumerate(raw_trades):
        if str(tr.get("side")) != "SELL":
            continue
        # Encontra BUY correspondente
        buy = None
        for j in range(i + 1, len(raw_trades)):
            if raw_trades[j].get("asset") == tr.get("asset") and raw_trades[j].get("side") == "BUY":
                buy = raw_trades[j]
                break
        try:
            sell_price = float(tr.get("price") or 0)
            sell_qty = float(tr.get("qty") or 0)
            pnl_u = float(tr.get("pnl_usd") or 0)
            pnl_p = float(tr.get("pnl_pct") or 0)
        except (TypeError, ValueError):
            sell_price, sell_qty, pnl_u, pnl_p = 0.0, 0.0, 0.0, 0.0
        try:
            buy_price = float(buy.get("price")) if buy else None
        except (TypeError, ValueError):
            buy_price = None
        opened_at = tr.get("entry_time") or (buy.get("timestamp") if buy else None)
        closed_at = tr.get("timestamp")
        opened_dt = _parse_iso(opened_at or "")
        closed_dt = _parse_iso(closed_at or "")
        duration_s = int((closed_dt - opened_dt).total_seconds()) if opened_dt and closed_dt else None
        max_pos_raw = tr.get("max_pos_usd")
        max_neg_raw = tr.get("max_neg_usd")
        try:
            max_pos = float(max_pos_raw) if max_pos_raw not in (None, "") else None
        except (TypeError, ValueError):
            max_pos = None
        try:
            max_neg = float(max_neg_raw) if max_neg_raw not in (None, "") else None
        except (TypeError, ValueError):
            max_neg = None
        size_usd = (buy_price or sell_price) * sell_qty if sell_qty else 0.0
        strat_key, tf = _strategy_to_key_and_tf(str(tr.get("strategy") or ""))
        meta_strat = strat_meta.get(strat_key, {})
        if not tf:
            tf = str(meta_strat.get("timeframe") or "")
        items.append({
            "id": f"closed:{closed_at}:{tr.get('asset')}",
            "status": "closed",
            "asset": tr.get("asset"),
            "side": "LONG",
            "size_usd": size_usd,
            "qty": sell_qty,
            "entry_price": buy_price,
            "exit_price": sell_price,
            "current_price": sell_price,
            "take_price": None,
            "stop_price": None,
            "max_neg_usd": max_neg,
            "max_pos_usd": max_pos,
            "pnl_usd": pnl_u,
            "pnl_pct": pnl_p,
            "opened_at": opened_at,
            "closed_at": closed_at,
            "duration_s": duration_s,
            "strategy_key": strat_key,
            "strategy_full": tr.get("strategy"),
            "strategy_engine": meta_strat.get("engine") or meta_strat.get("template"),
            "timeframe": tf,
        })
    return items


def _overview(paper_rows: list[dict[str, Any]], strategies: list[dict[str, Any]], trades: list[dict[str, Any]], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = meta or {}
    has_meta = bool(meta)
    initial = float(meta.get("total_bankroll") or 1000.0)
    allocated = float(meta.get("allocated_usd") or sum(float(row.get("allocated_usd") or 0) for row in paper_rows))
    market_value = float(meta.get("market_value") or sum(float(row.get("market_value") or 0) for row in paper_rows))
    cash = float(meta.get("cash_balance")) if has_meta and meta.get("cash_balance") is not None else max(initial - allocated, 0.0)
    balance = float(meta.get("equity") or (cash + market_value))
    total_trades = sum(int(row["trades"] or 0) for row in paper_rows)
    wins = sum(int(row["wins"] or 0) for row in paper_rows)
    return {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "initial": round(initial, 2),
        "balance": round(balance, 2),
        "cash_balance": round(cash, 2),
        "allocated_usd": round(allocated, 2),
        "pnl_usd": round(balance - initial, 2),
        "pnl_pct": round(((balance / initial - 1) * 100) if initial else 0, 4),
        "trades": total_trades,
        "winrate": round((wins / total_trades * 100) if total_trades else 0, 2),
        "active_strategies": len(strategies),
        "strategies_in_loop": sum(1 for item in strategies if item.get("in_bot_loop")),
        "strategy_selected_events": sum(int(item.get("selected_count") or 0) for item in strategies),
        "last_trade_at": trades[0].get("timestamp") if trades else None,
    }


def _bot_status() -> dict[str, Any]:
    """Inferencia se o bot principal esta rodando.

    Sinais (ordem de confianca):
      1. PID do .bot.lock + processo vivo no SO
      2. mtime do trading_bot.log (idade em segundos)
      3. ultimo timestamp de decisao no log
    """
    import time, os
    pid_alive = False
    pid_value: int | None = None
    if BOT_LOCK.exists():
        try:
            pid_value = int(BOT_LOCK.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            pid_value = None
        if pid_value and pid_value > 0:
            try:
                if os.name == "nt":
                    import ctypes
                    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_value)
                    if handle:
                        ctypes.windll.kernel32.CloseHandle(handle)
                        pid_alive = True
                else:
                    os.kill(pid_value, 0)
                    pid_alive = True
            except Exception:
                pid_alive = False

    log_age_s: float | None = None
    if TRADING_BOT_LOG.exists():
        try:
            log_age_s = time.time() - TRADING_BOT_LOG.stat().st_mtime
        except Exception:
            log_age_s = None

    # Estado: alive (verde) ate 90s; warn (amarelo) ate 5min; dead (vermelho) acima
    if pid_alive and (log_age_s is None or log_age_s < 90):
        state_label, color = "RODANDO", "ok"
    elif pid_alive and log_age_s is not None and log_age_s < 300:
        state_label, color = "QUIETO", "wait"
    elif pid_alive:
        state_label, color = "TRAVADO?", "wait"
    else:
        state_label, color = "PARADO", "no"

    return {
        "alive": pid_alive,
        "pid": pid_value,
        "log_age_seconds": int(log_age_s) if log_age_s is not None else None,
        "label": state_label,
        "color": color,
    }


def build_payload(trade_limit: int = 80) -> dict[str, Any]:
    live = get_state()
    paper = _read_json(PAPER_STATE, {})
    if not isinstance(paper, dict):
        paper = {}
    trades = _read_trades(trade_limit)
    paper_rows = _paper_rows(paper, live)
    strategies = _active_strategy_rows(paper, live)
    meta = paper.get("_meta", {}) if isinstance(paper.get("_meta"), dict) else {}
    events = [
        line
        for line in _tail_lines(TERMINAL_LOG, 400)
        if "Estrategia ativa selecionada" in line or "AETHER_ACTIVE" in line or "[PAPER]" in line
    ][-120:][::-1]
    # "Juiz das estrategias": separar Aether (validadas pelo critic v4) das legacy.
    # User pediu pra mostrar SO as validadas pelo aether nesse widget. As legacy
    # ficam num bucket separado (oculto por padrao) pra nao perder a info historica.
    raw_stats = meta.get("strategy_stats", {}) if isinstance(meta.get("strategy_stats"), dict) else {}
    aether_stats = {k: v for k, v in raw_stats.items() if str(k).startswith("Aether:")}
    legacy_stats = {k: v for k, v in raw_stats.items() if not str(k).startswith("Aether:")}
    # Tier ranking pra mostrar qual estrategia esta em qual tier (1=melhor, 3=resto).
    try:
        from src.modules.StrategyTiers import rank_strategies, tier_for_strategy
        ranks = rank_strategies()
        tier_ranking = {name: tier_for_strategy(name, ranks) for name in raw_stats.keys()}
    except Exception:
        tier_ranking = {}
    # Kills ativos (global + per-asset). Read-only no dashboard;
    # remocao via botao /api/kill/clear.
    try:
        from src.modules.PaperBalanceLogger import list_active_kills
        kills = list_active_kills()
    except Exception:
        kills = {"global": None, "assets": []}
    today = _today_stats(paper_rows, trades)
    unified = _unified_trades(paper_rows, trades, strategies)
    equity_by_strategy = _equity_by_strategy(trades)
    return {
        "overview": _overview(paper_rows, strategies, trades, meta),
        "today": today,
        "unified_trades": unified,
        "equity_by_strategy": equity_by_strategy,
        "bot_status": _bot_status(),
        "paper": paper_rows,
        "active_strategies": strategies,
        "strategy_stats": aether_stats,  # apenas validadas pelo aether
        "strategy_stats_legacy": legacy_stats,  # legacy (Vortex/RSI/etc), oculto por padrao
        "tier_ranking": tier_ranking,  # {strategy_name: tier(1/2/3)}
        "last_votes": meta.get("last_votes", {}) if isinstance(meta.get("last_votes"), dict) else {},
        "paused_strategies": meta.get("paused_strategies", {}) if isinstance(meta.get("paused_strategies"), dict) else {},
        "trades": trades,
        "events": events,
        "live": live,
        "json_alerts": get_json_alerts(),
        "kills": kills,
        "paths": {
            "paper_state": str(PAPER_STATE),
            "trades_csv": str(TRADES_CSV),
            "active_strategies": str(ACTIVE_STRATEGIES),
            "terminal_log": str(TERMINAL_LOG),
        },
    }


@app.get("/api/state")
def api_state():
    return get_state()


@app.get("/api/monitor")
def api_monitor(trade_limit: int = Query(default=80, ge=1, le=500)):
    return build_payload(trade_limit=trade_limit)


@app.post("/api/kill/clear")
def api_kill_clear(scope: str = Query(...), asset: str | None = Query(default=None)):
    """Remove kill switch.
    scope=global -> remove KILL_SWITCH na raiz.
    scope=asset  -> remove src/logs/kill_switch/<asset>.json.
    """
    from pathlib import Path as _Path
    if scope == "global":
        f = _Path("KILL_SWITCH")
        existed = f.exists()
        try: f.unlink()
        except FileNotFoundError: pass
        return {"ok": True, "scope": "global", "removed": existed}
    if scope == "asset" and asset:
        f = _Path("src/logs/kill_switch") / f"{asset.upper()}.json"
        existed = f.exists()
        try: f.unlink()
        except FileNotFoundError: pass
        return {"ok": True, "scope": "asset", "asset": asset.upper(), "removed": existed}
    return {"ok": False, "error": "scope invalido (global|asset) ou asset ausente"}


@app.post("/api/paper/reset")
def api_paper_reset():
    reset_clean_simulation()
    return {"ok": True, "payload": build_payload(trade_limit=80)}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "sim", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _select_port(start_port: int = 8000) -> int:
    if _port_available(start_port):
        return start_port
    if not _env_bool("TRADE_DASHBOARD_ALLOW_PORT_FALLBACK", False):
        raise RuntimeError(
            f"porta {start_port} ja esta em uso; feche o dashboard atual "
            "ou defina TRADE_DASHBOARD_ALLOW_PORT_FALLBACK=1 para subir em outra porta"
        )
    port = start_port + 1
    while not _port_available(port):
        port += 1
    return port


def start(host="0.0.0.0", port=8000):
    uvicorn.run(app, host=host, port=port, log_level="error")


def main() -> None:
    try:
        port = _select_port(_env_int("TRADE_DASHBOARD_PORT", 8000))
    except RuntimeError as exc:
        print(f"Trade dashboard nao iniciado: {exc}")
        raise SystemExit(1) from exc
    print(f"Trade dashboard running at http://127.0.0.1:{port}")
    # Passa o app object direto em vez de "src.dashboard:app" — sem import string,
    # uvicorn nao tenta criar subprocesso de import nem ativar reload.
    # (No Windows o venv ja mostra um par pai/filho python.exe por causa do shim
    # do launcher; isso eh inerente ao venv e nao tem como evitar daqui.)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


DASHBOARD_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HAPPYNATIONBOT Trade Monitor</title>
<style>
:root{--bg:#0b0d10;--panel:#11151a;--panel2:#161c23;--line:#252e38;--text:#edf2f7;--muted:#7f8a98;--green:#55d68b;--red:#ff6b6b;--amber:#f2c45c;--blue:#6bb7ff;--chip:#1e2530;--gold:#ffd166}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,Segoe UI,Arial,sans-serif}button{font:inherit}
.wrap{max-width:1440px;margin:0 auto;padding:20px 24px 60px}
.top{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:24px}
.brand{display:flex;align-items:center;gap:12px}
.mark{width:34px;height:34px;border-radius:8px;background:var(--green);color:#07110b;display:grid;place-items:center;font-weight:800}
.brand h1{font-size:18px;margin:0;letter-spacing:-.01em}
.brand span{color:var(--muted);font-size:12px}
.toolbar{display:flex;gap:8px;align-items:center;color:var(--muted);font-size:12px}
.toolbar button{background:var(--chip);border:1px solid var(--line);color:var(--text);padding:8px 12px;border-radius:6px;cursor:pointer;transition:background .15s}
.toolbar button:hover{background:var(--panel2)}
.bot-badge{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;padding:3px 9px;border-radius:999px;border:1px solid var(--line);background:var(--chip);color:var(--muted);margin-left:8px;vertical-align:middle}
.bot-badge.ok{color:#0f1a14;background:var(--green);border-color:var(--green)}
.bot-badge.ok::before{content:"";width:8px;height:8px;border-radius:50%;background:#0f1a14;animation:pulse 1.4s infinite ease-in-out}
.bot-badge.wait{color:#1a1305;background:var(--amber);border-color:var(--amber)}
.bot-badge.no{color:#fff;background:var(--red);border-color:var(--red)}
.bot-badge.no::before{content:"";width:8px;height:8px;border-radius:50%;background:#fff}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}

/* Status pill (running - 08/05/2026 09:40:05) */
.status-pill{display:inline-flex;align-items:center;gap:8px;padding:5px 12px;border:1px solid var(--line);border-radius:999px;background:var(--chip);font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;color:var(--muted)}
.status-pill .dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.status-pill.running{color:var(--green);border-color:rgba(85,214,139,.45)}
.status-pill.running .dot{background:var(--green);animation:pulse 1.2s infinite ease-in-out;box-shadow:0 0 8px var(--green)}
.status-pill.wait{color:var(--amber);border-color:rgba(242,196,92,.45)}
.status-pill.wait .dot{background:var(--amber)}
.status-pill.no{color:var(--red);border-color:rgba(255,107,107,.45)}
.status-pill.no .dot{background:var(--red)}

/* Tab nav principal */
.tabnav{display:flex;gap:0;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:4px;margin-bottom:18px;overflow:hidden}
.tabnav button{flex:1;background:transparent;border:0;color:var(--muted);padding:9px 16px;border-radius:7px;font-size:13px;font-weight:500;letter-spacing:.01em;cursor:pointer;transition:all .15s;text-align:center}
.tabnav button:hover{color:var(--text);background:var(--panel2)}
.tabnav button[aria-selected="true"]{background:var(--chip);color:var(--text);box-shadow:inset 0 0 0 1px var(--line)}
.tabnav button .badge{display:inline-block;margin-left:6px;padding:1px 6px;font-size:10px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;background:var(--panel2);color:var(--muted);border-radius:999px;border:1px solid var(--line)}
.tabnav button[aria-selected="true"] .badge{background:var(--bg);color:var(--text)}

.tabpanel{display:none}
.tabpanel.active{display:block}

/* HERO: 3 numeros importantes em destaque */
.hero{display:grid;grid-template-columns:1.4fr 1fr 1fr 1fr;gap:0;margin-bottom:18px;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.hero .cell{padding:18px 22px;border-left:1px solid var(--line)}
.hero .cell:first-child{border-left:0;background:var(--panel2)}
.hero .lbl{color:var(--muted);font-size:10px;letter-spacing:.08em;text-transform:uppercase;margin-bottom:6px}
.hero .big{font:600 32px/1 ui-monospace,SFMono-Regular,Consolas,monospace;letter-spacing:-.02em}
.hero .sub{font-size:12px;color:var(--muted);margin-top:6px}

/* KPIs secundarios — bem mais discretos */
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:20px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 14px}
.kpi span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em}
.kpi strong{display:block;font:600 18px/1.2 ui-monospace,SFMono-Regular,Consolas,monospace;margin-top:4px}

.pos{color:var(--green)}.neg{color:var(--red)}.warn{color:var(--amber)}.blue{color:var(--blue)}.gold{color:var(--gold)}

.grid{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(360px,1fr);gap:14px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.panel h2{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--panel2);display:flex;justify-content:space-between;align-items:center}
.panel h2 .meta{font-size:11px;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Consolas,monospace;text-transform:none;letter-spacing:0}

table{width:100%;border-collapse:collapse}
th,td{padding:9px 12px;border-bottom:1px solid #1c242e;text-align:left;vertical-align:middle}
th{color:var(--muted);font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.06em}
td.num,th.num{text-align:right;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
tr:last-child td{border-bottom:0}

.pill{display:inline-flex;align-items:center;gap:4px;border:1px solid var(--line);background:var(--chip);border-radius:999px;padding:2px 8px;color:var(--muted);font-size:11px;white-space:nowrap}
.pill.ok{border-color:rgba(85,214,139,.45);color:var(--green)}
.pill.no{border-color:rgba(255,107,107,.45);color:var(--red)}
.pill.wait{border-color:rgba(242,196,92,.45);color:var(--amber)}
.tier{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:22px;padding:0 5px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px;font-weight:700;border-radius:6px;border:1px solid var(--line)}
.tier.t1{background:rgba(255,209,102,.16);color:var(--gold);border-color:rgba(255,209,102,.4)}
.tier.t2{background:rgba(107,183,255,.13);color:var(--blue);border-color:rgba(107,183,255,.4)}
.tier.t3{background:var(--chip);color:var(--muted)}

.muted{color:var(--muted)}.small{font-size:11px;color:var(--muted)}
.stack{display:grid;gap:14px}
.empty{padding:32px;text-align:center;color:var(--muted);font-size:13px}

/* Cards de posicao */
.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;padding:10px}
.asset{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:14px}
.assetTop{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.asset h3{margin:0;font-size:16px;letter-spacing:-.01em}
.assetGrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.livedot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);vertical-align:middle;margin-left:6px;animation:livepulse 1.6s infinite ease-in-out;box-shadow:0 0 8px var(--green)}
@keyframes livepulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.45;transform:scale(.85)}}
.flash-up{animation:flashup .4s}.flash-down{animation:flashdown .4s}
@keyframes flashup{0%{background:rgba(85,214,139,.25)}100%{background:transparent}}
@keyframes flashdown{0%{background:rgba(255,107,107,.25)}100%{background:transparent}}
.metric span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:2px}
.metric strong{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:14px}

/* Resumo de estrategias — só números grandes */
.strat-summary{padding:18px 22px}
.strat-summary .total{display:flex;align-items:baseline;gap:10px;margin-bottom:14px}
.strat-summary .total .n{font:600 38px/1 ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--text);letter-spacing:-.02em}
.strat-summary .total .lbl{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.strat-summary .breakdown{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.strat-summary .tcell{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:10px 12px;text-align:center}
.strat-summary .tcell .label{display:block;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.strat-summary .tcell .value{font:600 18px/1 ui-monospace,SFMono-Regular,Consolas,monospace}
.strat-summary .tcell.t1 .value{color:var(--gold)}
.strat-summary .tcell.t2 .value{color:var(--blue)}
.strat-summary .tcell.t3 .value{color:var(--muted)}

/* Sub-tabs (Trades: Todos/Abertos/Fechados/Pendentes) */
.subtabs{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:0 0 14px}
.subtabs .seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
.subtabs .seg button{background:transparent;border:0;color:var(--muted);padding:7px 14px;font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;cursor:pointer;border-right:1px solid var(--line)}
.subtabs .seg button:last-child{border-right:0}
.subtabs .seg button[aria-pressed="true"]{background:var(--chip);color:var(--text)}
.subtabs .seg button:hover:not([aria-pressed="true"]){color:var(--text)}
.subtabs .search{flex:1;min-width:240px;display:flex;align-items:center;gap:8px;padding:0 12px;border:1px solid var(--line);background:var(--panel);border-radius:8px;color:var(--muted)}
.subtabs .search input{flex:1;background:transparent;border:0;color:var(--text);padding:8px 0;font-size:13px;outline:none}
.subtabs .search input::placeholder{color:var(--muted)}
.subtabs .reset{background:var(--chip);border:1px solid var(--line);color:var(--muted);padding:7px 12px;border-radius:8px;font-size:12px;cursor:pointer}
.subtabs .reset:hover{color:var(--text)}

/* Tabela rica de trades */
.richtable{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.richtable .scroll{overflow-x:auto}
.richtable table{min-width:1280px;width:100%;border-collapse:collapse;font-size:12.5px}
.richtable th{position:sticky;top:0;background:var(--panel2);color:var(--muted);font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.06em;padding:10px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line);user-select:none}
.richtable th:first-child,.richtable th.lft{text-align:left}
.richtable th[data-sort]{cursor:pointer;transition:color .12s}
.richtable th[data-sort]:hover{color:var(--text)}
.richtable th[data-sort]::after{content:"";display:inline-block;width:0;height:0;margin-left:6px;vertical-align:middle;opacity:.3;border-left:4px solid transparent;border-right:4px solid transparent}
.richtable th[data-sort][aria-sort="asc"]{color:var(--text)}
.richtable th[data-sort][aria-sort="asc"]::after{border-bottom:5px solid currentColor;opacity:1}
.richtable th[data-sort][aria-sort="desc"]{color:var(--text)}
.richtable th[data-sort][aria-sort="desc"]::after{border-top:5px solid currentColor;opacity:1}
.richtable td{padding:9px 10px;text-align:right;border-bottom:1px solid #1a212a;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-variant-numeric:tabular-nums;white-space:nowrap}
.richtable td:first-child,.richtable td.lft{text-align:left}
.richtable tr:hover td{background:rgba(255,255,255,.015)}
.richtable .ativo{font-weight:700;font-family:system-ui,-apple-system,sans-serif;letter-spacing:-.01em}
.richtable .sentido{display:inline-flex;padding:2px 9px;border-radius:5px;font-family:system-ui,-apple-system,sans-serif;font-size:11px;font-weight:700;letter-spacing:.04em}
.richtable .sentido.compra{background:rgba(85,214,139,.18);color:var(--green)}
.richtable .sentido.venda{background:rgba(255,107,107,.18);color:var(--red)}
.richtable .estrat{font-family:system-ui,-apple-system,sans-serif;font-size:12px;color:var(--text)}
.richtable .estrat .hash{color:var(--muted);font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px;margin-left:4px}
.richtable .tf{display:inline-block;padding:1px 6px;border:1px solid var(--line);border-radius:4px;color:var(--muted);font-size:11px}
.richtable .empty-row td{padding:32px;text-align:center;color:var(--muted);font-family:system-ui,-apple-system,sans-serif}
.richtable .dim{color:var(--muted)}

/* Drawer lateral de detalhe do trade */
.drawer-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.55);opacity:0;pointer-events:none;transition:opacity .18s;z-index:90}
.drawer-backdrop.open{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;height:100vh;width:min(520px,92vw);background:var(--panel);border-left:1px solid var(--line);transform:translateX(100%);transition:transform .22s ease-out;z-index:100;display:flex;flex-direction:column;box-shadow:-12px 0 32px rgba(0,0,0,.4)}
.drawer.open{transform:translateX(0)}
.drawer header{display:flex;justify-content:space-between;align-items:flex-start;padding:18px 20px;border-bottom:1px solid var(--line);background:var(--panel2)}
.drawer header h3{margin:0;font-size:18px;letter-spacing:-.01em}
.drawer header .sub{color:var(--muted);font-size:12px;margin-top:4px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
.drawer .close{background:transparent;border:1px solid var(--line);color:var(--muted);width:30px;height:30px;border-radius:6px;cursor:pointer;font-size:16px;line-height:1}
.drawer .close:hover{color:var(--text);border-color:var(--ink-4,var(--muted))}
.drawer .body{flex:1;overflow:auto;padding:18px 20px}
.drawer .grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;margin-bottom:18px}
.drawer .grid2 .cell{padding:10px 12px;background:var(--panel2);border:1px solid var(--line);border-radius:6px}
.drawer .grid2 .cell .lbl{display:block;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.drawer .grid2 .cell .v{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:14px;font-variant-numeric:tabular-nums}
.drawer h4{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin:18px 0 8px;font-weight:600}
.drawer ul.vote-list{list-style:none;margin:0;padding:0;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.drawer ul.vote-list li{padding:8px 12px;border-bottom:1px solid var(--line);font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;display:flex;justify-content:space-between;gap:10px}
.drawer ul.vote-list li:last-child{border-bottom:0}
.drawer ul.vote-list li .tag{padding:1px 7px;border-radius:4px;font-size:10px;text-transform:uppercase;letter-spacing:.05em;font-family:system-ui,sans-serif;font-weight:600}
.drawer ul.vote-list li .tag.buy{background:rgba(85,214,139,.18);color:var(--green)}
.drawer ul.vote-list li .tag.sell{background:rgba(255,107,107,.18);color:var(--red)}
.drawer ul.vote-list li .tag.neutral{background:var(--chip);color:var(--muted)}
.drawer pre.json{margin:0;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:12px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px;line-height:1.5;color:#c9d2dc;white-space:pre-wrap;word-break:break-word;max-height:280px;overflow:auto}

.richtable tbody tr{cursor:pointer}
.richtable tbody tr.selected td{background:rgba(107,183,255,.07)}

/* Risco / paused / kills */
.risk-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.risk-card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.risk-card h3{margin:0 0 10px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-weight:600}
.risk-card .row{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid #1a212a;font-size:13px}
.risk-card .row:last-child{border-bottom:0}
.risk-card .row strong{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-variant-numeric:tabular-nums}

/* Eventos collapsable */
details.events-wrap{border:1px solid var(--line);border-radius:10px;background:var(--panel);overflow:hidden}
details.events-wrap summary{padding:12px 16px;cursor:pointer;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em;list-style:none;background:var(--panel2);border-bottom:1px solid transparent}
details.events-wrap[open] summary{border-bottom-color:var(--line)}
details.events-wrap summary::-webkit-details-marker{display:none}
details.events-wrap summary::after{content:"▾";float:right;color:var(--muted)}
details.events-wrap[open] summary::after{content:"▴"}
.events{max-height:280px;overflow:auto;padding:10px 16px}
.event{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px;color:#b9c2cc;border-bottom:1px solid #1d252e;padding:6px 0;white-space:pre-wrap}
.event:last-child{border-bottom:0}

@media(max-width:1100px){.hero{grid-template-columns:1fr 1fr}.kpis{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.cards{grid-template-columns:1fr}.wrap{padding:16px}}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="brand"><div class="mark">HN</div><div><h1>Trade Monitor <span id="botBadge" class="bot-badge">--</span></h1><span>Paper / tier-based sizing</span></div></div>
    <div class="toolbar">
      <span id="statusPill" class="status-pill"><span class="dot"></span><span id="statusText">--</span></span>
      <span id="updated" class="small">-</span>
      <button id="resetBtn">Resetar banca</button>
      <button id="refreshBtn">Atualizar</button>
    </div>
  </header>

  <section id="alerts"></section>
  <section id="killsBox" class="hidden"></section>

  <nav class="tabnav" role="tablist" aria-label="Navegacao principal">
    <button data-tab="overview" aria-selected="true">Visao geral</button>
    <button data-tab="trades" aria-selected="false">Trades <span class="badge" id="tabTradesBadge">0</span></button>
    <button data-tab="strategies" aria-selected="false">Estrategias <span class="badge" id="tabStratsBadge">0</span></button>
    <button data-tab="risk" aria-selected="false">Risco <span class="badge" id="tabRiskBadge">0</span></button>
    <button data-tab="logs" aria-selected="false">Logs <span class="badge" id="tabLogsBadge">0</span></button>
  </nav>

  <!-- ========== VISAO GERAL ========== -->
  <section class="tabpanel active" data-tab="overview">
    <section class="hero" id="hero"></section>
    <section class="kpis" id="todayKpis"></section>
    <section class="grid">
      <div class="stack">
        <div class="panel"><h2>Posicoes abertas <span class="meta" id="posCount">0</span></h2><div class="cards" id="assets"></div></div>
      </div>
      <div class="stack">
        <div class="panel" id="stratSummary"></div>
        <details class="events-wrap"><summary>Eventos recentes</summary><div class="events" id="eventsOverview"></div></details>
      </div>
    </section>
  </section>

  <!-- ========== TRADES ========== -->
  <section class="tabpanel" data-tab="trades">
    <div class="subtabs" role="toolbar" aria-label="Filtros">
      <div class="seg" id="tradeFilter">
        <button data-filter="all" aria-pressed="true">Todos <span id="cAll">0</span></button>
        <button data-filter="open" aria-pressed="false">Abertos <span id="cOpen">0</span></button>
        <button data-filter="closed" aria-pressed="false">Fechados <span id="cClosed">0</span></button>
        <button data-filter="pending" aria-pressed="false">Pendentes <span id="cPending">0</span></button>
      </div>
      <label class="search">
        <span aria-hidden="true">&#x1F50D;</span>
        <input id="tradeSearch" type="search" placeholder="Buscar: ativo, estrategia, timeframe">
      </label>
      <button class="reset" id="tradeExport" title="Exportar trades filtrados como CSV">Export CSV</button>
      <button class="reset" id="tradeReset">Limpar filtros</button>
    </div>
    <div class="richtable">
      <div class="scroll">
        <table id="richTradeTable">
          <thead>
            <tr>
              <th class="lft" data-sort="asset">Ativo</th>
              <th data-sort="size_usd">Tamanho</th>
              <th data-sort="side">Sentido</th>
              <th data-sort="entry_price">Compra</th>
              <th data-sort="exit_price">Venda</th>
              <th data-sort="current_price">Atual</th>
              <th data-sort="take_price">Take</th>
              <th data-sort="stop_price">Stop/BE</th>
              <th data-sort="max_neg_usd">Max.Neg</th>
              <th data-sort="pnl_usd">PnL Atual</th>
              <th data-sort="max_pos_usd">Max.Pos</th>
              <th data-sort="opened_at">Abertura</th>
              <th data-sort="closed_at">Fechamento</th>
              <th data-sort="duration_s">Tempo</th>
              <th class="lft" data-sort="strategy_key">Estrategia</th>
              <th data-sort="timeframe">TF</th>
            </tr>
          </thead>
          <tbody id="richTradeBody"></tbody>
        </table>
      </div>
    </div>
  </section>

  <!-- ========== ESTRATEGIAS ========== -->
  <section class="tabpanel" data-tab="strategies">
    <div class="grid">
      <div class="panel"><h2>Ranking de estrategias <span class="meta" id="rankMeta">por PnL realizado</span></h2><div id="ranking"></div></div>
      <div class="stack">
        <div class="panel" id="stratSummary2"></div>
        <div class="panel"><h2>Estrategias ativas no loop <span class="meta" id="activeMeta">0</span></h2><div id="activeList"></div></div>
      </div>
    </div>
  </section>

  <!-- ========== RISCO ========== -->
  <section class="tabpanel" data-tab="risk">
    <div class="risk-grid" id="riskGrid"></div>
    <div class="panel" style="margin-top:14px"><h2>Estrategias pausadas <span class="meta" id="pausedMeta">0</span></h2><div id="pausedList"></div></div>
  </section>

  <!-- ========== LOGS ========== -->
  <section class="tabpanel" data-tab="logs">
    <div class="panel"><h2>Eventos recentes <span class="meta" id="eventsMeta">0</span></h2><div class="events" id="eventsLogs" style="max-height:540px"></div></div>
  </section>

</div>

<!-- Drawer lateral de detalhes do trade -->
<div id="drawerBackdrop" class="drawer-backdrop"></div>
<aside id="drawer" class="drawer" aria-hidden="true">
  <header>
    <div>
      <h3 id="drawerTitle">—</h3>
      <div class="sub" id="drawerSub">—</div>
    </div>
    <button class="close" id="drawerClose" aria-label="Fechar">&times;</button>
  </header>
  <div class="body" id="drawerBody"></div>
</aside>
<script>
const $ = (id) => document.getElementById(id);
const fmt = (v,d=2) => Number(v || 0).toLocaleString("pt-BR",{minimumFractionDigits:d,maximumFractionDigits:d});
const pct = (v) => `${Number(v || 0) >= 0 ? "+" : ""}${fmt(v,2)}%`;
const cls = (v) => Number(v || 0) >= 0 ? "pos" : "neg";
const sign = (v) => (Number(v || 0) >= 0 ? "+" : "");
function esc(v){return String(v ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;")}
function pill(text, type=""){return `<span class="pill ${type}">${esc(text)}</span>`}
function tsLabel(s){if(!s)return "—";const v=String(s);return v.length>=19?v.slice(0,19).replace("T"," "):v}
function durLabel(s){if(s==null)return "—";if(s<60)return s+"s";const m=Math.floor(s/60);if(m<60)return m+"m";const h=Math.floor(m/60);return h+"h "+(m%60)+"m"}
function sparkline(values, w=80, h=22){
  if (!values || values.length < 2) return `<svg width="${w}" height="${h}" aria-hidden="true"></svg>`;
  const min = Math.min(0, ...values), max = Math.max(0, ...values);
  const span = (max - min) || 1;
  const sx = i => (i / (values.length - 1)) * (w - 2) + 1;
  const sy = v => h - 1 - ((v - min) / span) * (h - 2);
  let d = "";
  values.forEach((v, i) => { d += (i ? " L" : "M") + sx(i).toFixed(1) + " " + sy(v).toFixed(1); });
  const last = values[values.length - 1];
  const color = last >= 0 ? "var(--green)" : "var(--red)";
  const baseY = sy(0).toFixed(1);
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">
    <line x1="0" y1="${baseY}" x2="${w}" y2="${baseY}" stroke="var(--line)" stroke-dasharray="2 3"/>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>`;
}

let LATEST = null;
const state = { tab: "overview", filter: "all", search: "", sort: { col: "opened_at", dir: "desc" } };

async function load(){
  try {
    const res = await fetch("/api/monitor?trade_limit=200",{cache:"no-store"});
    const data = await res.json();
    LATEST = data;
    render(data);
  } catch (e) {
    console.error("monitor load failed", e);
  }
}

function render(data){
  const o = data.overview || {};
  const bs = data.bot_status || {};
  const today = data.today || {};
  const unified = data.unified_trades || [];

  // Status pill (running / parado / quieto) - formato "rodando - 08/05/2026, 09:40:05"
  const sp = $("statusPill");
  const stxt = $("statusText");
  sp.className = "status-pill " + (bs.color === "ok" ? "running" : bs.color === "wait" ? "wait" : bs.color === "no" ? "no" : "");
  const updated = o.updated_at || "";  // formato "YYYY-MM-DD HH:MM:SS"
  let stampStr = "";
  if (updated && updated.length >= 19) {
    const [d, h] = updated.split(" ");
    if (d && h) {
      const [y, mo, da] = d.split("-");
      stampStr = `${da}/${mo}/${y}, ${h}`;
    }
  }
  const ageStr = bs.log_age_seconds !== null && bs.log_age_seconds !== undefined
    ? ` (${bs.log_age_seconds < 60 ? bs.log_age_seconds + "s" : Math.round(bs.log_age_seconds/60) + "m"})`
    : "";
  stxt.textContent = `${(bs.label || "?").toLowerCase()}${stampStr ? " - " + stampStr : ""}${ageStr}`;

  const badge = $("botBadge");
  badge.className = "bot-badge " + (bs.color || "");
  badge.textContent = `${bs.label || "?"}`;
  badge.title = `BOT alive=${bs.alive} pid=${bs.pid || "-"} log_age=${bs.log_age_seconds || "?"}s`;
  $("updated").textContent = `Atualizado ${o.updated_at || "-"}`;

  // Alertas e kills
  renderAlerts(data);
  renderKills(data);

  // ===== Visao geral =====
  $("hero").innerHTML = [
    {lbl:"Patrimonio",   big:`$${fmt(o.balance)}`, sub:`Inicial $${fmt(o.initial)}`, c:cls(o.pnl_usd)},
    {lbl:"PnL total",    big:`${sign(o.pnl_usd)}$${fmt(o.pnl_usd)}`, sub:pct(o.pnl_pct), c:cls(o.pnl_usd)},
    {lbl:"Em posicao",   big:`$${fmt(o.allocated_usd)}`, sub:`Livre $${fmt(o.cash_balance)}`, c:"blue"},
    {lbl:"Trades / WR",  big:`${o.trades||0}`, sub:`WR ${fmt(o.winrate,1)}%`, c:""}
  ].map(x=>`<div class="cell"><div class="lbl">${x.lbl}</div><div class="big ${x.c}">${x.big}</div><div class="sub">${x.sub}</div></div>`).join("");

  $("todayKpis").innerHTML = [
    {l:"Resultado hoje", v:`${sign(today.result_today_usd)}$${fmt(today.result_today_usd)}`, c:cls(today.result_today_usd)},
    {l:"Trades hoje",    v:`${today.closed_today||0}`, c:""},
    {l:"Win rate hoje",  v:`${fmt(today.winrate_today||0,1)}%`, c:""},
    {l:"Exposicao aberta", v:`$${fmt(today.exposure_open_usd)}`, c:"blue"}
  ].map(x=>`<div class="kpi"><span>${x.l}</span><strong class="${x.c}">${x.v}</strong></div>`).join("");

  renderPositionCards(data);

  // Stats agregados (compartilhado entre Visao geral e Estrategias)
  const stratActive = (data.active_strategies || []).length;
  const ranking = data.tier_ranking || {};
  const tCounts = Object.values(ranking);
  const t1 = tCounts.filter(t=>t===1).length;
  const t2 = tCounts.filter(t=>t===2).length;
  const t3 = stratActive - t1 - t2;
  const stats = data.strategy_stats || {};
  const stratWithTrades = Object.values(stats).filter(s => Number(s.trades||0) > 0).length;
  const stratSummaryHtml = `
    <h2>Estrategias rodando <span class="meta">${stratWithTrades} com historico</span></h2>
    <div class="strat-summary">
      <div class="total"><div class="n">${stratActive}</div><div class="lbl">total ativas no loop</div></div>
      <div class="breakdown">
        <div class="tcell t1"><span class="label">Tier 1</span><span class="value">${t1}</span></div>
        <div class="tcell t2"><span class="label">Tier 2</span><span class="value">${t2}</span></div>
        <div class="tcell t3"><span class="label">Tier 3</span><span class="value">${t3}</span></div>
      </div>
    </div>`;
  $("stratSummary").innerHTML = stratSummaryHtml;
  if ($("stratSummary2")) $("stratSummary2").innerHTML = stratSummaryHtml;

  // ===== Trades (rich table) =====
  const counts = { all: unified.length, open: 0, closed: 0, pending: 0 };
  for (const t of unified) {
    if (t.status === "open") counts.open++;
    else if (t.status === "closed") counts.closed++;
    else counts.pending++;
  }
  $("cAll").textContent = counts.all;
  $("cOpen").textContent = counts.open;
  $("cClosed").textContent = counts.closed;
  $("cPending").textContent = counts.pending;
  $("tabTradesBadge").textContent = counts.all;
  renderRichTable();

  // ===== Estrategias =====
  const rankRows = Object.entries(stats)
    .filter(([_,s]) => Number(s.trades||0) > 0)
    .map(([name,s]) => {
      const tradeCount = Number(s.trades || 0);
      const wins = Number(s.wins || 0);
      const wr = tradeCount ? wins / tradeCount * 100 : 0;
      const tier = ranking[name] || 3;
      const shortName = name.split("#").pop() || name;
      return {name, shortName, trades: tradeCount, wr, pnl: Number(s.pnl_usd||0), tier};
    })
    .sort((a,b) => b.pnl - a.pnl);
  $("rankMeta").textContent = `${rankRows.length} ${rankRows.length===1?"estrategia operada":"estrategias operadas"}`;
  const equityMap = data.equity_by_strategy || {};
  $("ranking").innerHTML = rankRows.length ? simpleTable(["#","Tier","Estrategia","Trades","WR","PnL","Curva"], rankRows.map((r,i) => [
    `<strong style="color:var(--muted);font-family:ui-monospace,monospace">${String(i+1).padStart(2,"0")}</strong>`,
    `<span class="tier t${r.tier}">T${r.tier}</span>`,
    `<span class="small" title="${esc(r.name)}">#${esc(r.shortName)}</span>`,
    r.trades,
    `${fmt(r.wr,0)}%`,
    `<span class="${cls(r.pnl)}">${sign(r.pnl)}$${fmt(r.pnl)}</span>`,
    sparkline(equityMap[r.name] || [])
  ])) : `<div class="empty">Nenhuma estrategia entrou em trade ainda.</div>`;
  $("tabStratsBadge").textContent = stratActive;

  const actives = data.active_strategies || [];
  $("activeMeta").textContent = `${actives.length} ${actives.length===1?"estrategia":"estrategias"}`;
  $("activeList").innerHTML = actives.length ? simpleTable(
    ["Estrategia","Engine","TF","Score","Asset","Loop"],
    actives.slice(0,40).map(a => [
      `<span class="small" title="${esc(a.key)}">#${esc((a.key||"").split("#").pop()||"-")}</span>`,
      esc(a.engine || "-"),
      `<span class="tf">${esc(a.timeframe || "-")}</span>`,
      a.score ? fmt(a.score, 2) : "—",
      esc(a.asset || "-"),
      a.in_bot_loop ? pill("ATIVA","ok") : pill("STAND-BY","")
    ])
  ) : `<div class="empty">Nenhuma estrategia ativa carregada.</div>`;

  // ===== Risco =====
  renderRisk(data);

  // ===== Logs =====
  const events = data.events || [];
  const evHtml = events.slice(0,80).map(e=>`<div class="event">${esc(e)}</div>`).join("") || `<div class="empty">Nenhum evento recente.</div>`;
  $("eventsOverview").innerHTML = events.slice(0,20).map(e=>`<div class="event">${esc(e)}</div>`).join("") || `<div class="empty">Nenhum evento recente.</div>`;
  $("eventsLogs").innerHTML = evHtml;
  $("eventsMeta").textContent = `${events.length} ${events.length===1?"linha":"linhas"}`;
  $("tabLogsBadge").textContent = events.length;
}

function renderAlerts(data){
  const alerts = data.json_alerts || [];
  $("alerts").innerHTML = alerts.length === 0 ? "" :
    `<div class="panel" style="border:1px solid #b94646;background:#3a1818;color:#f6cccc;padding:12px;margin-bottom:12px;">
      <strong>JSON corrompido detectado</strong>
      <ul style="margin:6px 0 0 18px;padding:0;">
        ${alerts.map(a => `<li>${esc(a.path)}: ${esc(a.error)} <span class="small">(${esc(a.at)})</span></li>`).join("")}
      </ul>
    </div>`;
}

function renderKills(data){
  const kills = data.kills || {global: null, assets: []};
  const killsBox = $("killsBox");
  const blocks = [];
  if (kills.global) {
    blocks.push(`<div class="panel" style="border:2px solid #ef4444;background:#3a1010;color:#ffd9d9;padding:14px;margin-bottom:8px;">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
        <strong style="font-size:15px;">KILL SWITCH GLOBAL</strong>
        <button onclick="clearKill('global')" style="background:#ef4444;color:#fff;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-weight:600;">Resume Global</button>
      </div>
      <div style="margin-top:8px;font-size:13px;line-height:1.4;">${esc(kills.global.reason || "Sem motivo registrado")}</div>
    </div>`);
  }
  if ((kills.assets || []).length > 0) {
    const items = kills.assets.map(k => `<div style="border:1px solid #b58a2c;background:#2c2310;padding:10px 12px;border-radius:6px;margin-bottom:6px;color:#ffe7a3;">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
        <strong>${esc(k.asset)} <span class="small" style="opacity:.7">(mode=${esc(k.mode || "graceful")})</span></strong>
        <span class="small" style="opacity:.6;">expira ${esc(k.expires_at || "-")}</span>
        <button onclick="clearKill('asset','${esc(k.asset)}')" style="background:#b58a2c;color:#000;border:none;padding:4px 10px;border-radius:4px;cursor:pointer;font-weight:600;">Resume ${esc(k.asset)}</button>
      </div>
      <div style="margin-top:6px;font-size:12px;line-height:1.4;">${esc(k.reason || "")}</div>
    </div>`).join("");
    blocks.push(`<div class="panel" style="margin-bottom:8px;"><h2 style="margin-top:0">Kills por asset</h2>${items}</div>`);
  }
  killsBox.classList.toggle("hidden", blocks.length === 0);
  killsBox.innerHTML = blocks.join("");
  const totalKills = (kills.global ? 1 : 0) + (kills.assets || []).length;
  const paused = data.paused_strategies || {};
  const pausedCount = Object.keys(paused).length;
  $("tabRiskBadge").textContent = totalKills + pausedCount;
}

function renderPositionCards(data){
  const positions = data.paper || [];
  const open = positions.filter(p=>p.position).length;
  $("posCount").textContent = `${open} ${open===1?"aberta":"abertas"}`;
  $("assets").innerHTML = positions.length ? positions.map(a => {
    const dataAttrs = a.position
      ? `data-asset="${esc(a.asset)}" data-qty="${a.qty}" data-entry="${a.entry_price}" data-allocated="${a.allocated_usd}"`
      : '';
    return `
    <article class="asset" ${dataAttrs}>
      <div class="assetTop">
        <h3>${esc(a.asset)} ${a.position ? '<span class="livedot" title="atualizando preco a cada 2s"></span>' : ''}</h3>
        ${pill(a.position ? "LONG" : "SEM POS", a.position ? "ok" : "")}
      </div>
      <div class="assetGrid">
        <div class="metric"><span>Posicao</span><strong class="blue">$${fmt(a.allocated_usd)}</strong></div>
        <div class="metric"><span>PnL aberto</span><strong data-live="pnl" class="${cls(a.unrealized_pnl_usd)}">${sign(a.unrealized_pnl_usd)}$${fmt(a.unrealized_pnl_usd)}</strong></div>
        <div class="metric"><span>PnL total</span><strong class="${cls(a.pnl_usd)}">${sign(a.pnl_usd)}$${fmt(a.pnl_usd)}</strong></div>
      </div>
      ${a.position ? `<div class="small" style="margin-top:10px">Entrada $${fmt(a.entry_price,4)} · Atual <span data-live="price">$${fmt(a.live_price||a.entry_price,4)}</span> · Qty ${fmt(a.qty,6)}</div>` : `<div class="small" style="margin-top:10px">Aguardando sinal · ${a.trades} trades · WR ${fmt(a.winrate,1)}%</div>`}
    </article>`;
  }).join("") : `<div class="empty">Sem ativos no loop ainda.</div>`;
}

function renderRichTable(){
  if (!LATEST) return;
  const items = LATEST.unified_trades || [];
  const filter = state.filter;
  const q = (state.search || "").trim().toLowerCase();
  const filtered = items.filter(t => {
    if (filter !== "all" && t.status !== filter) return false;
    if (!q) return true;
    return [t.asset, t.strategy_full, t.strategy_key, t.timeframe, t.strategy_engine]
      .filter(Boolean)
      .some(v => String(v).toLowerCase().includes(q));
  });
  // Sort
  const col = state.sort.col;
  const dir = state.sort.dir === "asc" ? 1 : -1;
  filtered.sort((a, b) => {
    const va = a[col], vb = b[col];
    // null/undef sempre por ultimo
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
    return String(va).localeCompare(String(vb)) * dir;
  });
  // marca header ativo
  document.querySelectorAll("#richTradeTable th[data-sort]").forEach(th => {
    th.setAttribute("aria-sort", th.dataset.sort === col ? state.sort.dir : "none");
  });
  const body = $("richTradeBody");
  if (!filtered.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="16">Nenhum registro corresponde aos filtros.</td></tr>`;
    return;
  }
  body.innerHTML = filtered.map(t => {
    const sentido = t.side === "SHORT" ? "venda" : "compra";
    const sentidoLabel = sentido === "venda" ? "Venda" : "Compra";
    const pnlCls = cls(t.pnl_usd);
    const maxPos = (t.max_pos_usd != null) ? `<span class="${cls(t.max_pos_usd)}">${sign(t.max_pos_usd)}$${fmt(t.max_pos_usd)}</span>` : `<span class="dim">—</span>`;
    const maxNeg = (t.max_neg_usd != null) ? `<span class="${cls(t.max_neg_usd)}">${sign(t.max_neg_usd)}$${fmt(t.max_neg_usd)}</span>` : `<span class="dim">—</span>`;
    const compra = t.entry_price != null ? fmt(t.entry_price, 4) : '<span class="dim">—</span>';
    const venda = t.exit_price != null ? fmt(t.exit_price, 4) : '<span class="dim">—</span>';
    const atual = t.current_price != null ? fmt(t.current_price, 4) : '<span class="dim">—</span>';
    // Distancia ate take/stop em % (so faz sentido em posicao aberta com preco vivo)
    const cur = t.current_price;
    const distTake = (t.take_price != null && cur != null && cur > 0)
      ? ((t.take_price - cur) / cur * 100) : null;
    const distStop = (t.stop_price != null && cur != null && cur > 0)
      ? ((cur - t.stop_price) / cur * 100) : null;
    const take = t.take_price != null
      ? `${fmt(t.take_price, 4)} <span class="dim" style="font-size:10px">${distTake!=null?(distTake>=0?"+":"")+fmt(distTake,2)+"%":""}</span>`
      : '<span class="dim">—</span>';
    const stop = t.stop_price != null
      ? `${fmt(t.stop_price, 4)} <span class="dim" style="font-size:10px">${distStop!=null?"-"+fmt(Math.abs(distStop),2)+"%":""}</span>`
      : '<span class="dim">—</span>';
    const tfBadge = t.timeframe ? `<span class="tf">${esc(t.timeframe)}</span>` : '<span class="dim">—</span>';
    const stratHash = (t.strategy_key && t.strategy_key.split("#")[1]) ? "#" + t.strategy_key.split("#")[1] : "";
    const stratEngine = t.strategy_engine || (t.strategy_key && t.strategy_key.split(":")[1] ? t.strategy_key.split(":")[1].split("#")[0] : "");
    // Linhas abertas recebem data-* attrs pra polling Binance atualizar Atual/PnL/Max ao vivo
    const isOpen = t.status === "open";
    const liveAttrs = isOpen
      ? `data-asset="${esc(t.asset)}" data-qty="${t.qty}" data-entry="${t.entry_price}" data-size="${t.size_usd}"`
      : "";
    return `<tr data-trade-id="${esc(t.id)}" ${liveAttrs}>
      <td class="lft"><span class="ativo">${esc(t.asset || "—")}</span></td>
      <td>$${fmt(t.size_usd)}</td>
      <td><span class="sentido ${sentido}">${sentidoLabel}</span></td>
      <td>${compra}</td>
      <td>${venda}</td>
      <td ${isOpen ? 'data-live="atual"' : ""}>${atual}</td>
      <td>${take}</td>
      <td>${stop}</td>
      <td ${isOpen ? 'data-live="maxneg"' : ""}>${maxNeg}</td>
      <td ${isOpen ? 'data-live="pnl"' : ""}><span class="${pnlCls}">${sign(t.pnl_usd)}$${fmt(t.pnl_usd)}</span></td>
      <td ${isOpen ? 'data-live="maxpos"' : ""}>${maxPos}</td>
      <td>${tsLabel(t.opened_at)}</td>
      <td>${t.closed_at ? tsLabel(t.closed_at) : '<span class="dim">—</span>'}</td>
      <td ${isOpen ? 'data-live="dur" data-opened="'+esc(t.opened_at||'')+'"' : ""}>${durLabel(t.duration_s)}</td>
      <td class="lft estrat">${esc(stratEngine || "-")} <span class="hash" title="${esc(t.strategy_key||'')}">${esc(stratHash)}</span></td>
      <td>${tfBadge}</td>
    </tr>`;
  }).join("");
}

function renderRisk(data){
  const risk = [];
  // exposicao + caixa livre
  const o = data.overview || {};
  risk.push({title:"Exposicao", rows:[
    ["Em posicao", `$${fmt(o.allocated_usd)}`],
    ["Caixa livre", `$${fmt(o.cash_balance)}`],
    ["Posicoes abertas", String((data.today||{}).open_positions || 0)],
  ]});
  // limites por ativo (loss streaks etc)
  const positions = (data.paper || []);
  const lossStreaks = positions
    .map(p => ({asset: p.asset, ls: Number((p.risk||{}).consecutive_losses||0), wr: p.winrate, trades: p.trades}))
    .filter(x => x.ls > 0 || x.trades > 0);
  if (lossStreaks.length) {
    risk.push({title:"Por ativo", rows: lossStreaks.map(x => [
      x.asset,
      `WR ${fmt(x.wr,1)}% · ${x.trades}t · streak ${x.ls}`
    ])});
  }
  // dia
  const t = data.today || {};
  risk.push({title:"Hoje", rows:[
    ["Resultado realizado", `<span class="${cls(t.realized_today_usd)}">${sign(t.realized_today_usd)}$${fmt(t.realized_today_usd)}</span>`],
    ["Resultado aberto", `<span class="${cls(t.unrealized_open_usd)}">${sign(t.unrealized_open_usd)}$${fmt(t.unrealized_open_usd)}</span>`],
    ["Trades fechados", String(t.closed_today||0)],
    ["WR hoje", `${fmt(t.winrate_today,1)}%`],
  ]});
  $("riskGrid").innerHTML = risk.map(card => `
    <div class="risk-card">
      <h3>${esc(card.title)}</h3>
      ${card.rows.map(([k,v]) => `<div class="row"><span class="muted">${esc(k)}</span><strong>${v}</strong></div>`).join("")}
    </div>`).join("");
  // pausadas
  const paused = data.paused_strategies || {};
  const pkeys = Object.keys(paused);
  $("pausedMeta").textContent = `${pkeys.length} ${pkeys.length===1?"pausada":"pausadas"}`;
  $("pausedList").innerHTML = pkeys.length ? simpleTable(["Estrategia","Motivo"], pkeys.map(k => [
    `<span class="small" title="${esc(k)}">#${esc(k.split("#").pop()||k)}</span>`,
    esc(paused[k] || "-")
  ])) : `<div class="empty">Nenhuma estrategia pausada.</div>`;
}

function simpleTable(headers, rows){
  if (!rows.length) return `<div class="empty">Sem dados.</div>`;
  return `<table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map((c,i)=>`<td class="${i>2?'num':''}">${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

// Tab switching (persiste em localStorage)
function setTab(tab){
  state.tab = tab;
  document.querySelectorAll(".tabnav button").forEach(b => b.setAttribute("aria-selected", b.dataset.tab === tab ? "true" : "false"));
  document.querySelectorAll(".tabpanel").forEach(p => p.classList.toggle("active", p.dataset.tab === tab));
  persistState();
}
document.querySelectorAll(".tabnav button").forEach(b => b.addEventListener("click", () => setTab(b.dataset.tab)));

// Trade sub-filters
document.querySelectorAll("#tradeFilter button").forEach(b => b.addEventListener("click", () => {
  state.filter = b.dataset.filter;
  document.querySelectorAll("#tradeFilter button").forEach(x => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
  persistState();
  renderRichTable();
}));
$("tradeSearch").addEventListener("input", (e) => { state.search = e.target.value; persistState(); renderRichTable(); });
$("tradeReset").addEventListener("click", () => {
  state.filter = "all";
  state.search = "";
  state.sort = { col: "opened_at", dir: "desc" };
  $("tradeSearch").value = "";
  document.querySelectorAll("#tradeFilter button").forEach(x => x.setAttribute("aria-pressed", x.dataset.filter === "all" ? "true" : "false"));
  persistState();
  renderRichTable();
});

// Header clicks ordenam: 1o clique = desc, 2o = asc, 3o = limpa pra default
document.querySelectorAll("#richTradeTable th[data-sort]").forEach(th => {
  th.addEventListener("click", () => {
    const col = th.dataset.sort;
    if (state.sort.col !== col) {
      state.sort = { col, dir: "desc" };
    } else if (state.sort.dir === "desc") {
      state.sort.dir = "asc";
    } else {
      state.sort = { col: "opened_at", dir: "desc" };
    }
    persistState();
    renderRichTable();
  });
});

// Persistir aba/filtro/busca/sort em localStorage
function persistState(){
  try {
    localStorage.setItem("hnb_dash_ui", JSON.stringify({
      tab: state.tab, filter: state.filter, search: state.search, sort: state.sort
    }));
  } catch(_) {}
}
function restoreState(){
  try {
    const raw = localStorage.getItem("hnb_dash_ui");
    if (!raw) return;
    const s = JSON.parse(raw);
    if (s.tab) state.tab = s.tab;
    if (s.filter) state.filter = s.filter;
    if (typeof s.search === "string") state.search = s.search;
    if (s.sort && s.sort.col) state.sort = s.sort;
  } catch(_) {}
}
// ===== Drawer de detalhes =====
function openDrawer(tradeId){
  if (!LATEST) return;
  const t = (LATEST.unified_trades || []).find(x => x.id === tradeId);
  if (!t) return;
  document.querySelectorAll("#richTradeBody tr").forEach(r => r.classList.toggle("selected", r.dataset.tradeId === tradeId));
  $("drawerTitle").textContent = `${t.asset || "—"} · ${t.side || "LONG"}`;
  const stratEngine = t.strategy_engine || (t.strategy_key && t.strategy_key.split(":")[1] ? t.strategy_key.split(":")[1].split("#")[0] : "—");
  $("drawerSub").textContent = `${stratEngine}${t.strategy_key ? " · " + t.strategy_key : ""}${t.timeframe ? " · " + t.timeframe : ""}`;
  // Distancias %
  const cur = t.current_price;
  const distTake = (t.take_price != null && cur && cur > 0) ? ((t.take_price - cur) / cur * 100) : null;
  const distStop = (t.stop_price != null && cur && cur > 0) ? ((cur - t.stop_price) / cur * 100) : null;
  const cells = [
    ["Status",      t.status === "open" ? `<span class="pill ok">ABERTO</span>` : `<span class="pill">FECHADO</span>`],
    ["Tamanho",     `$${fmt(t.size_usd)}`],
    ["Qty",         fmt(t.qty, 6)],
    ["Compra",      t.entry_price != null ? `$${fmt(t.entry_price, 4)}` : "—"],
    ["Venda",       t.exit_price != null ? `$${fmt(t.exit_price, 4)}` : "—"],
    ["Atual",       cur != null ? `$${fmt(cur, 4)}` : "—"],
    ["Take",        t.take_price != null ? `$${fmt(t.take_price, 4)}` + (distTake != null ? ` <span class="dim">(${distTake>=0?"+":""}${fmt(distTake,2)}%)</span>` : "") : "—"],
    ["Stop / BE",   t.stop_price != null ? `$${fmt(t.stop_price, 4)}` + (distStop != null ? ` <span class="dim">(-${fmt(Math.abs(distStop),2)}%)</span>` : "") : "—"],
    ["PnL atual",   `<span class="${cls(t.pnl_usd)}">${sign(t.pnl_usd)}$${fmt(t.pnl_usd)} (${sign(t.pnl_pct)}${fmt(t.pnl_pct,2)}%)</span>`],
    ["Max.Pos",     t.max_pos_usd != null ? `<span class="${cls(t.max_pos_usd)}">${sign(t.max_pos_usd)}$${fmt(t.max_pos_usd)}</span>` : "—"],
    ["Max.Neg",     t.max_neg_usd != null ? `<span class="${cls(t.max_neg_usd)}">${sign(t.max_neg_usd)}$${fmt(t.max_neg_usd)}</span>` : "—"],
    ["Tempo",       durLabel(t.duration_s)],
    ["Abertura",    tsLabel(t.opened_at)],
    ["Fechamento",  t.closed_at ? tsLabel(t.closed_at) : "—"],
  ];
  let html = `<div class="grid2">${cells.map(([k,v]) => `<div class="cell"><span class="lbl">${esc(k)}</span><span class="v">${v}</span></div>`).join("")}</div>`;

  // Votos da entrada (se posicao aberta)
  const vs = t.vote_snapshot;
  if (vs && typeof vs === "object") {
    const votes = [];
    (vs.buy_votes || []).forEach(v => votes.push({k:v, tag:"buy"}));
    (vs.sell_votes || []).forEach(v => votes.push({k:v, tag:"sell"}));
    (vs.neutral_votes || []).forEach(v => votes.push({k:v, tag:"neutral"}));
    if (votes.length) {
      html += `<h4>Votos na entrada</h4><ul class="vote-list">` + votes.map(v =>
        `<li><span title="${esc(v.k)}">${esc((v.k||"").split("#").pop()||v.k)}</span><span class="tag ${v.tag}">${v.tag}</span></li>`
      ).join("") + `</ul>`;
    }
    // Tier config
    if (t.tier_config) {
      const tc = t.tier_config;
      html += `<h4>Tier config</h4><div class="grid2">` + [
        ["Tier", tc.tier ?? "—"],
        ["Position $", tc.position_usd != null ? `$${fmt(tc.position_usd)}` : "—"],
        ["SL $", tc.sl_usd != null ? `$${fmt(tc.sl_usd)}` : "—"],
        ["Trail activate $", tc.trail_activation_usd != null ? `$${fmt(tc.trail_activation_usd)}` : "—"],
        ["Trail floor $", tc.trail_floor_usd != null ? `$${fmt(tc.trail_floor_usd)}` : "—"],
        ["Trail giveback %", tc.trail_giveback_pct != null ? `${fmt(tc.trail_giveback_pct,1)}%` : "—"],
      ].map(([k,v]) => `<div class="cell"><span class="lbl">${esc(k)}</span><span class="v">${v}</span></div>`).join("") + `</div>`;
    }
    // Snapshot bruto colapsado
    html += `<h4>Snapshot completo</h4><pre class="json">${esc(JSON.stringify(vs, null, 2))}</pre>`;
  } else if (t.entry_votes && t.entry_votes.length) {
    html += `<h4>Estrategias na entrada</h4><ul class="vote-list">` +
      t.entry_votes.map(v => `<li><span title="${esc(v)}">${esc((v||"").split("#").pop()||v)}</span><span class="tag buy">buy</span></li>`).join("") +
      `</ul>`;
  }

  $("drawerBody").innerHTML = html;
  $("drawer").classList.add("open");
  $("drawer").setAttribute("aria-hidden", "false");
  $("drawerBackdrop").classList.add("open");
}
function closeDrawer(){
  $("drawer").classList.remove("open");
  $("drawer").setAttribute("aria-hidden", "true");
  $("drawerBackdrop").classList.remove("open");
  document.querySelectorAll("#richTradeBody tr").forEach(r => r.classList.remove("selected"));
}
$("drawerClose").addEventListener("click", closeDrawer);
$("drawerBackdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
$("richTradeBody").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-trade-id]");
  if (tr) openDrawer(tr.dataset.tradeId);
});

// ===== Export CSV =====
$("tradeExport").addEventListener("click", () => {
  if (!LATEST) return;
  const items = LATEST.unified_trades || [];
  const filter = state.filter;
  const q = (state.search || "").trim().toLowerCase();
  const filtered = items.filter(t => {
    if (filter !== "all" && t.status !== filter) return false;
    if (!q) return true;
    return [t.asset, t.strategy_full, t.strategy_key, t.timeframe, t.strategy_engine]
      .filter(Boolean).some(v => String(v).toLowerCase().includes(q));
  });
  const cols = ["status","asset","side","size_usd","qty","entry_price","exit_price","current_price",
    "take_price","stop_price","max_neg_usd","pnl_usd","pnl_pct","max_pos_usd",
    "opened_at","closed_at","duration_s","strategy_key","timeframe"];
  const escCsv = (v) => {
    if (v == null) return "";
    const s = String(v);
    return /[,\";\n]/.test(s) ? `"${s.replace(/"/g,'""')}"` : s;
  };
  const lines = [cols.join(",")];
  filtered.forEach(t => lines.push(cols.map(c => escCsv(t[c])).join(",")));
  const blob = new Blob([lines.join("\n")], {type:"text/csv;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const stamp = new Date().toISOString().slice(0,19).replace(/[-:T]/g,"");
  a.href = url;
  a.download = `trades_${state.filter}_${stamp}.csv`;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
});

restoreState();
// Aplicar estado restaurado no DOM (setTab/persistState ja estao definidos acima)
setTab(state.tab);
$("tradeSearch").value = state.search || "";
document.querySelectorAll("#tradeFilter button").forEach(x => x.setAttribute("aria-pressed", x.dataset.filter === state.filter ? "true" : "false"));

$("refreshBtn").onclick = load;
$("resetBtn").onclick = async () => {
  if (!confirm("Zerar paper e iniciar nova simulacao com banca global limpa?")) return;
  await fetch("/api/paper/reset", {method:"POST"});
  await load();
};

window.clearKill = async (scope, asset) => {
  const label = scope === "global" ? "GLOBAL" : asset;
  if (!confirm(`Remover kill switch ${label}? Bot voltara a operar ${scope === "global" ? "todos os pares" : "em " + asset}.`)) return;
  let url = `/api/kill/clear?scope=${encodeURIComponent(scope)}`;
  if (asset) url += `&asset=${encodeURIComponent(asset)}`;
  const r = await fetch(url, {method: "POST"});
  const j = await r.json();
  console.log("kill clear:", j);
  await load();
};

load();
setInterval(load, 5000);

// Polling de preco em tempo real direto da Binance (publico, sem auth).
// Atualiza Atual/PnL/Max.Pos/Max.Neg ao vivo: nos cards de Visao geral E
// nas linhas abertas da tabela rica (aba Trades).
const lastPnl = {};
const livePeak = {};   // por ativo: maior PnL ja visto na sessao
const liveTrough = {}; // por ativo: menor PnL ja visto na sessao
function fmtPrice(p){return `$${p.toLocaleString("pt-BR",{minimumFractionDigits:4,maximumFractionDigits:4})}`}
function fmtPnl(v){return `${v>=0?"+":""}$${v.toLocaleString("pt-BR",{minimumFractionDigits:2,maximumFractionDigits:2})}`}
function flashCell(el, prev, cur){
  if (prev === undefined || Math.abs(cur - prev) <= 0.001) return;
  const f = cur > prev ? "flash-up" : "flash-down";
  el.classList.remove("flash-up","flash-down");
  void el.offsetWidth;
  el.classList.add(f);
}
function durLabelJs(s){if(s==null)return "—";if(s<60)return s+"s";const m=Math.floor(s/60);if(m<60)return m+"m";const h=Math.floor(m/60);return h+"h "+(m%60)+"m"}

async function pollLivePrices(){
  const cards = document.querySelectorAll('.asset[data-asset]');
  const rows  = document.querySelectorAll('#richTradeBody tr[data-asset]');
  const all = [...cards, ...rows];
  if (!all.length) return;
  const symbols = Array.from(new Set(all.map(c => c.dataset.asset).filter(Boolean)));
  if (!symbols.length) return;
  const param = encodeURIComponent(JSON.stringify(symbols));
  let prices = {};
  try {
    const res = await fetch(`https://api.binance.com/api/v3/ticker/price?symbols=${param}`, {cache:"no-store"});
    const arr = await res.json();
    if (Array.isArray(arr)) arr.forEach(it => { prices[it.symbol] = parseFloat(it.price); });
  } catch (e) { return; }

  // Cards (Visao geral)
  cards.forEach(card => {
    const asset = card.dataset.asset;
    const qty = parseFloat(card.dataset.qty);
    const entry = parseFloat(card.dataset.entry);
    const price = prices[asset];
    if (!Number.isFinite(price) || !qty || !entry) return;
    const pnl = qty * (price - entry);
    const priceEl = card.querySelector('[data-live="price"]');
    const pnlEl = card.querySelector('[data-live="pnl"]');
    if (priceEl) priceEl.textContent = fmtPrice(price);
    if (pnlEl) {
      pnlEl.textContent = fmtPnl(pnl);
      pnlEl.className = pnl >= 0 ? "pos" : "neg";
      flashCell(pnlEl, lastPnl[asset], pnl);
    }
    lastPnl[asset] = pnl;
    livePeak[asset]   = livePeak[asset]   === undefined ? pnl : Math.max(livePeak[asset], pnl);
    liveTrough[asset] = liveTrough[asset] === undefined ? pnl : Math.min(liveTrough[asset], pnl);
  });

  // Linhas abertas da tabela rica (aba Trades)
  const nowSec = Math.floor(Date.now() / 1000);
  rows.forEach(row => {
    const asset = row.dataset.asset;
    const qty = parseFloat(row.dataset.qty);
    const entry = parseFloat(row.dataset.entry);
    const price = prices[asset];
    if (!Number.isFinite(price) || !qty || !entry) return;
    const pnl = qty * (price - entry);
    livePeak[asset]   = livePeak[asset]   === undefined ? pnl : Math.max(livePeak[asset], pnl);
    liveTrough[asset] = liveTrough[asset] === undefined ? pnl : Math.min(liveTrough[asset], pnl);
    const atualEl = row.querySelector('[data-live="atual"]');
    const pnlEl   = row.querySelector('[data-live="pnl"]');
    const maxPosEl= row.querySelector('[data-live="maxpos"]');
    const maxNegEl= row.querySelector('[data-live="maxneg"]');
    const durEl   = row.querySelector('[data-live="dur"]');
    if (atualEl) atualEl.textContent = price.toLocaleString("pt-BR",{minimumFractionDigits:4,maximumFractionDigits:4});
    if (pnlEl) {
      const cls = pnl >= 0 ? "pos" : "neg";
      pnlEl.innerHTML = `<span class="${cls}">${fmtPnl(pnl)}</span>`;
      flashCell(pnlEl, lastPnl[asset+"_row"], pnl);
      lastPnl[asset+"_row"] = pnl;
    }
    if (maxPosEl) {
      const v = livePeak[asset];
      const cls = v >= 0 ? "pos" : "neg";
      maxPosEl.innerHTML = `<span class="${cls}">${fmtPnl(v)}</span>`;
    }
    if (maxNegEl) {
      const v = liveTrough[asset];
      const cls = v >= 0 ? "pos" : "neg";
      maxNegEl.innerHTML = `<span class="${cls}">${fmtPnl(v)}</span>`;
    }
    if (durEl) {
      const opened = row.querySelector('[data-live="dur"]').dataset.opened;
      if (opened) {
        const t = Date.parse(opened);
        if (!Number.isNaN(t)) durEl.textContent = durLabelJs(nowSec - Math.floor(t/1000));
      }
    }
  });
}
setInterval(pollLivePrices, 2000);
// Primeira chamada apos primeiro render do load()
setTimeout(pollLivePrices, 800);
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
