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
from fastapi.staticfiles import StaticFiles

from src.modules.PaperBalanceLogger import reset_clean_simulation
from src.modules.PortfolioConfig import load_portfolio


app = FastAPI(title="HAPPYNATIONBOT Trade Dashboard")

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
PAPER_DIR = ROOT / "src" / "logs" / "paper_balance"
PAPER_STATE = PAPER_DIR / "state.json"
TRADES_CSV = PAPER_DIR / "trades.csv"
ACTIVE_STRATEGIES = ROOT / "src" / "logs" / "research" / "active_strategies.json"
TERMINAL_LOG = ROOT / "src" / "logs" / "terminal.log"
TRADING_BOT_LOG = ROOT / "src" / "logs" / "trading_bot.log"
BOT_LOCK = ROOT / "src" / "logs" / ".bot.lock"
MAIN_FILE = ROOT / "src" / "main.py"
PORTFOLIO_FILE = ROOT / "src" / "config" / "portfolio.yaml"

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
    if PORTFOLIO_FILE.exists():
        try:
            return {asset.symbol for asset in load_portfolio(PORTFOLIO_FILE).assets if asset.traded_quantity != 0.0 or asset.notional_usd > 0}
        except Exception:
            pass
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


DASHBOARD_HTML_PATH = ROOT / "src" / "static" / "trade_dashboard.html"


def _load_dashboard_html() -> str:
    return DASHBOARD_HTML_PATH.read_text(encoding="utf-8")


DASHBOARD_HTML = _load_dashboard_html()


if __name__ == "__main__":
    main()
