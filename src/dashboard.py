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
                "live_price": live_price,
                "live_decision": l.get("decision"),
                "live_cycle": l.get("cycle"),
                "live_updated_at": l.get("updated_at"),
                "risk": p.get("risk") if isinstance(p.get("risk"), dict) else {},
            }
        )
    return rows


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
    return {
        "overview": _overview(paper_rows, strategies, trades, meta),
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
    uvicorn.run("src.dashboard:app", host="127.0.0.1", port=port, reload=False, log_level="error")


DASHBOARD_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HAPPYNATIONBOT Trade Monitor</title>
<style>
:root{--bg:#0b0d10;--panel:#11151a;--panel2:#161c23;--line:#252e38;--text:#edf2f7;--muted:#7f8a98;--green:#55d68b;--red:#ff6b6b;--amber:#f2c45c;--blue:#6bb7ff;--chip:#1e2530;--gold:#ffd166}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,Segoe UI,Arial,sans-serif}button{font:inherit}
.wrap{max-width:1280px;margin:0 auto;padding:24px 28px 60px}
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
    <div class="toolbar"><span id="updated">-</span><button id="resetBtn">Resetar banca</button><button id="refreshBtn">Atualizar</button></div>
  </header>

  <section id="alerts"></section>
  <section id="killsBox" class="hidden"></section>

  <!-- HERO: 4 numeros essenciais -->
  <section class="hero" id="hero"></section>

  <section class="grid">
    <div class="stack">
      <div class="panel"><h2>Posicoes <span class="meta" id="posCount">0 abertas</span></h2><div class="cards" id="assets"></div></div>
      <div class="panel"><h2>Historico de trades <span class="meta" id="tradesCount">0</span></h2><div id="trades"></div></div>
    </div>
    <div class="stack">
      <div class="panel" id="stratSummary"></div>
      <div class="panel"><h2>Ranking de estrategias <span class="meta" id="rankMeta">por PnL realizado</span></h2><div id="ranking"></div></div>
      <details class="events-wrap"><summary>Eventos recentes</summary><div class="events" id="events"></div></details>
    </div>
  </section>
</div>
<script>
const $ = (id) => document.getElementById(id);
const fmt = (v,d=2) => Number(v || 0).toLocaleString("pt-BR",{minimumFractionDigits:d,maximumFractionDigits:d});
const pct = (v) => `${Number(v || 0) >= 0 ? "+" : ""}${fmt(v,2)}%`;
const cls = (v) => Number(v || 0) >= 0 ? "pos" : "neg";
function esc(v){return String(v ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;")}
function pill(text, type=""){return `<span class="pill ${type}">${esc(text)}</span>`}

async function load(){
  const res = await fetch("/api/monitor?trade_limit=120",{cache:"no-store"});
  const data = await res.json();
  render(data);
}

function render(data){
  const o = data.overview || {};
  const bs = data.bot_status || {};
  const badge = $("botBadge");
  badge.className = "bot-badge " + (bs.color || "");
  const ageStr = bs.log_age_seconds !== null && bs.log_age_seconds !== undefined
    ? ` ${bs.log_age_seconds < 60 ? bs.log_age_seconds + "s" : Math.round(bs.log_age_seconds/60) + "min"}`
    : "";
  badge.textContent = `${bs.label || "?"}${ageStr}`;
  badge.title = `BOT alive=${bs.alive} pid=${bs.pid || "-"} log_age=${bs.log_age_seconds || "?"}s`;
  $("updated").textContent = `Atualizado ${o.updated_at || "-"}`;
  const alerts = data.json_alerts || [];
  $("alerts").innerHTML = alerts.length === 0 ? "" :
    `<div class="panel" style="border:1px solid #b94646;background:#3a1818;color:#f6cccc;padding:12px;margin-bottom:12px;">
      <strong>JSON corrompido detectado</strong>
      <ul style="margin:6px 0 0 18px;padding:0;">
        ${alerts.map(a => `<li>${esc(a.path)}: ${esc(a.error)} <span class="small">(${esc(a.at)})</span></li>`).join("")}
      </ul>
    </div>`;

  // Kill switches: global vermelho-critico, per-asset amarelo-aviso
  const kills = data.kills || {global: null, assets: []};
  const killsBox = $("killsBox");
  const blocks = [];
  if (kills.global) {
    blocks.push(`<div class="panel" style="border:2px solid #ef4444;background:#3a1010;color:#ffd9d9;padding:14px;margin-bottom:8px;">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
        <strong style="font-size:15px;">🔴 KILL SWITCH GLOBAL</strong>
        <button onclick="clearKill('global')" style="background:#ef4444;color:#fff;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-weight:600;">Resume Global</button>
      </div>
      <div style="margin-top:8px;font-size:13px;line-height:1.4;">${esc(kills.global.reason || "Sem motivo registrado")}</div>
    </div>`);
  }
  if ((kills.assets || []).length > 0) {
    const items = kills.assets.map(k => `<div style="border:1px solid #b58a2c;background:#2c2310;padding:10px 12px;border-radius:6px;margin-bottom:6px;color:#ffe7a3;">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">
        <strong>🟡 ${esc(k.asset)} <span class="small" style="opacity:.7">(mode=${esc(k.mode || "graceful")})</span></strong>
        <span class="small" style="opacity:.6;">expira ${esc(k.expires_at || "-")}</span>
        <button onclick="clearKill('asset','${esc(k.asset)}')" style="background:#b58a2c;color:#000;border:none;padding:4px 10px;border-radius:4px;cursor:pointer;font-weight:600;">Resume ${esc(k.asset)}</button>
      </div>
      <div style="margin-top:6px;font-size:12px;line-height:1.4;">${esc(k.reason || "")}</div>
    </div>`).join("");
    blocks.push(`<div class="panel" style="margin-bottom:8px;"><h2 style="margin-top:0">Kills por asset</h2>${items}</div>`);
  }
  killsBox.classList.toggle("hidden", blocks.length === 0);
  killsBox.innerHTML = blocks.join("");
  // HERO: 4 numeros essenciais
  $("hero").innerHTML = [
    {lbl:"Patrimonio",   big:`$${fmt(o.balance)}`, sub:`Inicial $${fmt(o.initial)}`, c:cls(o.pnl_usd)},
    {lbl:"PnL",          big:`${(o.pnl_usd||0)>=0?"+":""}$${fmt(o.pnl_usd)}`, sub:pct(o.pnl_pct), c:cls(o.pnl_usd)},
    {lbl:"Em posicao",   big:`$${fmt(o.allocated_usd)}`, sub:`Livre $${fmt(o.cash_balance)}`, c:"blue"},
    {lbl:"Trades / WR",  big:`${o.trades||0}`, sub:`WR ${fmt(o.winrate,1)}%`, c:""}
  ].map(x=>`<div class="cell"><div class="lbl">${x.lbl}</div><div class="big ${x.c}">${x.big}</div><div class="sub">${x.sub}</div></div>`).join("");

  // Stats de estrategias (usado no painel resumo + ranking)
  const stratActive = (data.active_strategies || []).length;
  const ranking = data.tier_ranking || {};
  const tCounts = Object.values(ranking);
  const t1 = tCounts.filter(t=>t===1).length;
  const t2 = tCounts.filter(t=>t===2).length;
  const t3 = stratActive - t1 - t2;

  // POSICOES — cards com data-* para live polling de preco
  const positions = (data.paper || []);
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
        <div class="metric"><span>PnL aberto</span><strong data-live="pnl" class="${cls(a.unrealized_pnl_usd)}">${(a.unrealized_pnl_usd||0)>=0?"+":""}$${fmt(a.unrealized_pnl_usd)}</strong></div>
        <div class="metric"><span>PnL total</span><strong class="${cls(a.pnl_usd)}">${(a.pnl_usd||0)>=0?"+":""}$${fmt(a.pnl_usd)}</strong></div>
      </div>
      ${a.position ? `<div class="small" style="margin-top:10px">Entrada $${fmt(a.entry_price,4)} · Atual <span data-live="price">$${fmt(a.live_price||a.entry_price,4)}</span> · Qty ${fmt(a.qty,6)}</div>` : `<div class="small" style="margin-top:10px">Aguardando sinal · ${a.trades} trades · WR ${fmt(a.winrate,1)}%</div>`}
    </article>`;
  }).join("") : `<div class="empty">Sem ativos no loop ainda.</div>`;

  // TRADES — so ENTRADAS (BUY). Pareia cada BUY com o SELL correspondente
  // pra mostrar resultado da operacao (aberta vs fechada com pnl).
  const trades = data.trades || [];
  // trades vem em ordem reversa (mais novo primeiro). Pra cada BUY, o SELL
  // dela esta em algum indice MENOR (mais recente).
  const buys = trades.map((t,i) => ({...t, _idx:i})).filter(t => t.side === "BUY");
  $("tradesCount").textContent = `${buys.length} ${buys.length===1?"entrada":"entradas"}`;
  $("trades").innerHTML = buys.length ? table(["Hora","Ativo","Tier · Estrategia","Valor","Resultado"], buys.slice(0,15).map(t => {
    const price = Number(t.price);
    const qty = Number(t.qty);
    const notional = (Number.isFinite(price) && Number.isFinite(qty)) ? price * qty : null;
    const time = String(t.timestamp || "").slice(11,19) || "-";
    const stratFull = String(t.strategy || "");
    const m = stratFull.match(/Aether:[\w]+#[a-f0-9]+/);
    const stratKey = m ? m[0] : stratFull.split("(").pop().replace(")","");
    const stratHash = stratKey.split("#").pop() || "-";
    const tier = ranking[stratKey] || 3;
    // Procura SELL correspondente (indice menor = mais recente)
    let exit = null;
    for (let i = t._idx - 1; i >= 0; i--) {
      if (trades[i].asset === t.asset && trades[i].side === "SELL") { exit = trades[i]; break; }
    }
    let resultadoCell;
    if (!exit) {
      resultadoCell = pill("ABERTA","ok");
    } else {
      const pnl = Number(exit.pnl_usd || 0);
      const pnlPct = Number(exit.pnl_pct || 0);
      resultadoCell = `<span class="${cls(pnl)}">${pnl>=0?"+":""}$${fmt(pnl)}</span> <span class="small">(${fmt(pnlPct,2)}%)</span>`;
    }
    return [
      `<span class="small">${esc(time)}</span>`,
      esc(t.asset),
      `<span class="tier t${tier}">T${tier}</span> <span class="small" title="${esc(stratKey)}">#${esc(stratHash)}</span>`,
      notional !== null ? `$${fmt(notional,2)}` : "-",
      resultadoCell
    ];
  })) : `<div class="empty">Nenhuma entrada ainda.</div>`;

  // ESTRATEGIAS — so resumo numerico. Detalhes vao na tabela "Ranking".
  const stats = data.strategy_stats || {};
  const stratWithTrades = Object.values(stats).filter(s => Number(s.trades||0) > 0).length;
  $("stratSummary").innerHTML = `
    <h2>Estrategias rodando <span class="meta">${stratWithTrades} com historico</span></h2>
    <div class="strat-summary">
      <div class="total"><div class="n">${stratActive}</div><div class="lbl">total ativas no loop</div></div>
      <div class="breakdown">
        <div class="tcell t1"><span class="label">Tier 1</span><span class="value">${t1}</span></div>
        <div class="tcell t2"><span class="label">Tier 2</span><span class="value">${t2}</span></div>
        <div class="tcell t3"><span class="label">Tier 3</span><span class="value">${t3}</span></div>
      </div>
    </div>`;

  // RANKING — so estrategias que ja entraram em trade, ordenadas por PnL.
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
  $("ranking").innerHTML = rankRows.length ? table(["#","Tier","Estrategia","Trades","WR","PnL"], rankRows.map((r,i) => [
    `<strong style="color:var(--muted);font-family:ui-monospace,monospace">${String(i+1).padStart(2,"0")}</strong>`,
    `<span class="tier t${r.tier}">T${r.tier}</span>`,
    `<span class="small" title="${esc(r.name)}">#${esc(r.shortName)}</span>`,
    r.trades,
    `${fmt(r.wr,0)}%`,
    `<span class="${cls(r.pnl)}">${r.pnl>=0?"+":""}$${fmt(r.pnl)}</span>`
  ])) : `<div class="empty">Nenhuma estrategia entrou em trade ainda.</div>`;

  $("events").innerHTML = (data.events || []).slice(0,30).map(e=>`<div class="event">${esc(e)}</div>`).join("") || `<div class="empty">Nenhum evento recente.</div>`;
}

function table(headers, rows){
  if (!rows.length) return `<div class="empty">Sem dados.</div>`;
  return `<table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map((c,i)=>`<td class="${i>3?'num':''}">${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

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
// Atualiza so PnL aberto e preco atual nas posicoes abertas a cada 2s.
const lastPnl = {};
async function pollLivePrices(){
  const cards = document.querySelectorAll('.asset[data-asset]');
  if (!cards.length) return;
  const symbols = Array.from(cards).map(c => c.dataset.asset);
  if (!symbols.length) return;
  // Endpoint batch: /api/v3/ticker/price?symbols=["BTCUSDT","ETHUSDT"]
  const param = encodeURIComponent(JSON.stringify(symbols));
  let prices = {};
  try {
    const res = await fetch(`https://api.binance.com/api/v3/ticker/price?symbols=${param}`, {cache:"no-store"});
    const arr = await res.json();
    if (Array.isArray(arr)) arr.forEach(it => { prices[it.symbol] = parseFloat(it.price); });
  } catch (e) { return; }
  cards.forEach(card => {
    const asset = card.dataset.asset;
    const qty = parseFloat(card.dataset.qty);
    const entry = parseFloat(card.dataset.entry);
    const price = prices[asset];
    if (!Number.isFinite(price) || !qty || !entry) return;
    const pnl = qty * (price - entry);
    const pnlEl = card.querySelector('[data-live="pnl"]');
    const priceEl = card.querySelector('[data-live="price"]');
    if (priceEl) priceEl.textContent = `$${price.toLocaleString("pt-BR",{minimumFractionDigits:4,maximumFractionDigits:4})}`;
    if (pnlEl) {
      const sign = pnl >= 0 ? "+" : "";
      const cls = pnl >= 0 ? "pos" : "neg";
      pnlEl.textContent = `${sign}$${pnl.toLocaleString("pt-BR",{minimumFractionDigits:2,maximumFractionDigits:2})}`;
      pnlEl.className = cls;
      // Flash sutil quando muda
      const prev = lastPnl[asset];
      if (prev !== undefined && Math.abs(pnl - prev) > 0.001) {
        const flash = pnl > prev ? "flash-up" : "flash-down";
        pnlEl.classList.remove("flash-up","flash-down");
        void pnlEl.offsetWidth;
        pnlEl.classList.add(flash);
      }
      lastPnl[asset] = pnl;
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
