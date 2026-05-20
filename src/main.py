"""HAPPYNATIONBOT — entrypoint principal (multi-timeframe + portfolio expandido).

Ciclo:
1) Carrega portfolio.yaml (10 ativos top liquidez)
2) Resolve lotSize via exchangeInfo
3) Inicializa MarketDataHub (WebSocket multiplex, todos os TFs)
4) Spawn 1 thread por ativo, cada uma roda BinanceTraderBot.execute() em loop
5) Hub alimenta candles ao vivo; cada estrategia roda no seu TF nativo
"""
import sys
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path

# Log com rotacao: 5MB por arquivo, mantem 5 backups (= 25MB max).
Path("src/logs").mkdir(parents=True, exist_ok=True)
_log_handler = RotatingFileHandler(
    "src/logs/trading_bot.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_log_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])

# Lock inter-processo: previne duas instancias.
from src.modules.InstanceLock import acquire_or_die  # noqa: E402
acquire_or_die()

# Redireciona prints para o log (sem poluir o dashboard no terminal)
from src.modules.TerminalLogger import TerminalLogger
_log_only = TerminalLogger()
sys.stdout = _log_only
sys.stderr = _log_only

import src.modules.TerminalDashboard as Dashboard
Dashboard.start()


# ------------------------------------------------------------------

import threading
import time
from datetime import datetime
from binance.client import Client

from src.modules.BinanceTraderBot import BinanceTraderBot
from src.modules.BinanceClient import BinanceClient
from src.modules.PaperBalanceLogger import mark_to_market
from src.models.stock_start_model import StockStartModel
from src.modules.MarketDataHub import MarketDataHub, set_hub
from src.modules.PortfolioConfig import load_portfolio, resolve_lot_sizes

from src.strategies.moving_average_antecipation import getMovingAverageAntecipationTradeStrategy
from src.strategies.moving_average import getMovingAverageTradeStrategy
from src.strategies.multi_strategy import MultiStrategy

from dotenv import load_dotenv
load_dotenv()


# fmt: off
# -------------------------------------------------------------------------------------------------
# 🟢🟢🟢 CONFIGURACOES 🟢🟢🟢

# ESTRATEGIA PRINCIPAL: MultiStrategy + Aether-only deploys
# Com hub, cada estrategia Aether roda no _timeframe que ela declarou (1m..1d).
# bot_timeframe define o TF base do ciclo (usado para resample fallback).
MAIN_STRATEGY = MultiStrategy
MAIN_STRATEGY_ARGS = {
    "champion_args": {
        "take_profit_pct": 0.9,
        "stop_loss_pct": 2.0,
        "rsi_min": 40.0,
        "rsi_max": 64.0,
        "pullback_pct": 0.7,
        "max_hold_bars": 24,
    },
    "min_buy_votes": 2,
    "min_sell_votes": 1,
    "min_buy_weight": 2.0,
    "min_sell_weight": 1.0,
    "bot_timeframe": "15m",  # TF base do ciclo (usado tambem como fallback de resample)
    "entry_mode": "normal",
    "include_scalp": False,
}

# Fallback (legado, desativado por padrao)
FALLBACK_ENABLED = False
FALLBACK_STRATEGY = getMovingAverageTradeStrategy
FALLBACK_STRATEGY_ARGS = {"fast_window": 7, "slow_window": 40}

# AJUSTES TECNICOS
ACCEPTABLE_LOSS_PERCENTAGE = 2
STOP_LOSS_PERCENTAGE = 2

# Protecao USD-based.
# Fallback alinhado ao tier 3 (StrategyTiers.py): se o snapshot do tier nao
# vier por algum motivo, ainda caimos em valores coerentes com posicao $50.
# O tier override (_apply_tier_before_buy) sobrepoe estes valores antes de cada
# compra com base no rank da estrategia.
STOP_LOSS_USD = 1.0
TRAIL_ACTIVATION_USD = 0.5
TRAIL_FLOOR_USD = 0.3
TRAIL_GIVEBACK_PCT = 25.0

TRAILING_STOP_ENABLED = False
TRAILING_STOP_PERCENTAGE = 5

TP_AT_PERCENTAGE = []
TP_AMOUNT_PERCENTAGE = []

PAUSE_AFTER_TAKE_PROFIT = False
PAUSE_AFTER_STOP_LOSS = False
PAUSE_AFTER_TRAILING_STOP = False

EXECUTE_MARKET_ORDERS = True

# TF base do ciclo (intervalo do candle "principal" pro stock_data legado).
# Com hub, todos os outros TFs sao puxados em paralelo via WebSocket.
CANDLE_PERIOD = Client.KLINE_INTERVAL_15MINUTE

# Ciclo rapido: SL/trail/kill switch reagem em 15s. Strategy decision tambem
# roda toda iteracao mas e barato (le tf_data ja em cache no hub).
TEMPO_ENTRE_TRADES = 15
DELAY_ENTRE_ORDENS = 15 * 60

THREAD_LOCK = True

# 🔴🔴🔴 FIM CONFIGURACOES 🔴🔴🔴
# -------------------------------------------------------------------------------------------------


# ------------------------------------------------------------------
# Carrega portfolio do YAML

PORTFOLIO_YAML = Path("src/config/portfolio.yaml")
if not PORTFOLIO_YAML.exists():
    raise SystemExit(f"[CONFIG] portfolio.yaml ausente: {PORTFOLIO_YAML}")

portfolio_cfg = load_portfolio(PORTFOLIO_YAML)
print(f"[CONFIG] portfolio carregado: {len(portfolio_cfg.assets)} ativos, TFs default {portfolio_cfg.default_timeframes}")


# ------------------------------------------------------------------
# Inicializa cliente Binance (mesmas regras do BinanceTraderBot)

api_key = os.getenv("BINANCE_API_KEY")
secret_key = os.getenv("BINANCE_SECRET_KEY")
offline_mode = os.getenv("OFFLINE_MODE", "false").lower() == "true"
keys_invalid = (
    not api_key or not secret_key
    or "Cole" in (api_key or "") or "Cole" in (secret_key or "")
)
hub_disabled = os.getenv("MARKET_DATA_HUB_DISABLED", "false").lower() == "true"

# Cliente para REST (backfill, exchangeInfo, ticker)
if offline_mode:
    print("[OFFLINE] modo sintetico — hub desabilitado")
    rest_client = None
    hub_disabled = True
elif keys_invalid:
    print("[LIVE DATA] sem API key — REST publico apenas")
    rest_client = BinanceClient("", "", sync=False, ping=True, sync_interval=30000, verbose=False)
else:
    rest_client = BinanceClient(api_key, secret_key, sync=True, sync_interval=30000, verbose=False)


# Resolve lotSize via exchangeInfo (atualiza traded_quantity por ativo).
if rest_client is not None:
    try:
        resolve_lot_sizes(portfolio_cfg, rest_client)
    except Exception as exc:
        logging.error(f"[PORTFOLIO] resolve_lot_sizes falhou: {exc}", exc_info=True)


# ------------------------------------------------------------------
# Sobe MarketDataHub

market_data_hub = None
if not hub_disabled and rest_client is not None:
    try:
        db_path = Path("src/logs/market_data.db")
        market_data_hub = MarketDataHub(
            client=rest_client,
            api_key=api_key if not keys_invalid else None,
            api_secret=secret_key if not keys_invalid else None,
            db_path=db_path,
            buffer_size=portfolio_cfg.buffer_size,
        )
        # Subscribe todos os ativos com seus TFs
        for asset in portfolio_cfg.assets:
            market_data_hub.subscribe(asset.symbol, asset.timeframes)
        market_data_hub.start()
        set_hub(market_data_hub)
        print(f"[HUB] iniciado: {len(portfolio_cfg.assets)} ativos, "
              f"~{sum(len(a.timeframes) for a in portfolio_cfg.assets)} streams")
        # Espera o backfill terminar antes de soltar as threads de trading,
        # senao primeiros ciclos teriam stock_data vazio.
        time.sleep(3)
    except Exception as exc:
        logging.error(f"[HUB] inicializacao falhou: {exc}", exc_info=True)
        market_data_hub = None
        print(f"[HUB] FALHOU — caindo para REST polling: {exc}")
else:
    print("[HUB] desativado — usando REST polling legado")


# ------------------------------------------------------------------
# Constroi StockStartModel por ativo a partir do portfolio_cfg

CHAMPIONS_BASE = {
    "take_profit_pct": 99.0,
    "stop_loss_pct": 2.0,
    "rsi_min": 40.0,
    "rsi_max": 64.0,
    "pullback_pct": 0.6,
    "max_hold_bars": 48,
}


def MULTI_ARGS():
    return {
        "champion_args": dict(CHAMPIONS_BASE),
        # Fase de teste: 1 voto basta pra disparar compra. Em producao subir
        # pra 2 (consenso) quando quiser filtrar mais sinal isolado.
        "min_buy_votes": 1,
        "min_sell_votes": 1,
        "min_buy_weight": 1.0,
        "min_sell_weight": 1.0,
        "bot_timeframe": "15m",
        "entry_mode": "normal",
        "include_scalp": False,
    }


stocks_traded_list: list[StockStartModel] = []
asset_meta: dict[str, dict] = {}  # symbol -> {asset_cfg, timeframes}
for asset in portfolio_cfg.assets:
    if asset.traded_quantity <= 0:
        logging.warning(f"[PORTFOLIO] {asset.symbol}: traded_quantity=0 (skip)")
        continue
    model = StockStartModel(
        baseStock=asset.base,
        quoteStock=asset.quote,
        tradedQuantity=asset.traded_quantity,
        mainStrategy=MAIN_STRATEGY,
        mainStrategyArgs=MULTI_ARGS(),
        fallbackStrategy=FALLBACK_STRATEGY,
        fallbackStrategyArgs=FALLBACK_STRATEGY_ARGS,
        candlePeriod=CANDLE_PERIOD,
        stopLossPercentage=STOP_LOSS_PERCENTAGE,
        tempoEntreTrades=TEMPO_ENTRE_TRADES,
        delayEntreOrdens=DELAY_ENTRE_ORDENS,
        acceptableLossPercentage=ACCEPTABLE_LOSS_PERCENTAGE,
        fallBackActivated=FALLBACK_ENABLED,
        takeProfitAtPercentage=TP_AT_PERCENTAGE,
        takeProfitAmountPercentage=TP_AMOUNT_PERCENTAGE,
        executeMarketOrders=EXECUTE_MARKET_ORDERS,
        trailingStopPercent=TRAILING_STOP_PERCENTAGE,
        trailingStopEnabled=TRAILING_STOP_ENABLED,
        pauseAfterTakeProfit=PAUSE_AFTER_TAKE_PROFIT,
        pauseAfterStopLoss=PAUSE_AFTER_STOP_LOSS,
        pauseAfterTrailingStop=PAUSE_AFTER_TRAILING_STOP,
        stopLossUsd=STOP_LOSS_USD,
        trailActivationUsd=TRAIL_ACTIVATION_USD,
        trailFloorUsd=TRAIL_FLOOR_USD,
        trailGivebackPct=TRAIL_GIVEBACK_PCT,
    )
    stocks_traded_list.append(model)
    asset_meta[asset.symbol] = {
        "timeframes": list(asset.timeframes),
        "notional_usd": asset.notional_usd,
    }

print(f"[PORTFOLIO] ativos prontos pra trade: {len(stocks_traded_list)}")


# ------------------------------------------------------------------
# Loop de trading por ativo (mesmo modelo de threads de antes)

thread_lock = threading.Lock()

HEARTBEATS: dict[str, float] = {}
HEARTBEAT_STALE_SECONDS = int(os.getenv("HEARTBEAT_STALE_SECONDS", "1800"))


def trader_loop(stockStart: StockStartModel):
    try:
        with open("src/logs/thread_debug.log", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now()}] trader_loop started for {stockStart.baseStock}\n")
    except Exception:
        pass
    try:
        _trader_loop_impl(stockStart)
    except Exception as e:
        with open("src/logs/thread_debug.log", "a", encoding="utf-8") as f:
            import traceback
            f.write(f"[{datetime.now()}] TRADER_LOOP CRASHED: {e}\n")
            f.write(traceback.format_exc() + "\n")


def _trader_loop_impl(stockStart: StockStartModel):
    symbol = f"{stockStart.baseStock}{stockStart.quoteStock}"
    tfs = (asset_meta.get(symbol) or {}).get("timeframes")
    MaTrader = BinanceTraderBot(
        base_stock=stockStart.baseStock,
        quote_stock=stockStart.quoteStock,
        traded_quantity=stockStart.tradedQuantity,
        traded_percentage=stockStart.tradedPercentage,
        candle_period=stockStart.candlePeriod,
        time_to_trade=stockStart.tempoEntreTrades,
        delay_after_order=stockStart.delayEntreOrdens,
        acceptable_loss_percentage=stockStart.acceptableLossPercentage,
        stop_loss_percentage=stockStart.stopLossPercentage,
        fallback_activated=stockStart.fallBackActivated,
        take_profit_at_percentage=stockStart.takeProfitAtPercentage,
        take_profit_amount_percentage=stockStart.takeProfitAmountPercentage,
        main_strategy=stockStart.mainStrategy,
        main_strategy_args=stockStart.mainStrategyArgs,
        fallback_strategy=stockStart.fallbackStrategy,
        fallback_strategy_args=stockStart.fallbackStrategyArgs,
        execute_market_orders=stockStart.executeMarketOrders,
        trailing_stop_percent=stockStart.trailingStopPercent,
        trailing_stop_enabled=stockStart.trailingStopEnabled,
        pause_after_take_profit=stockStart.pauseAfterTakeProfit,
        pause_after_stop_loss=stockStart.pauseAfterStopLoss,
        pause_after_trailing_stop=stockStart.pauseAfterTrailingStop,
        stop_loss_usd=stockStart.stopLossUsd,
        trail_activation_usd=stockStart.trailActivationUsd,
        trail_floor_usd=stockStart.trailFloorUsd,
        trail_giveback_pct=stockStart.trailGivebackPct,
        market_data_hub=market_data_hub,
        strategy_timeframes=tfs,
    )

    total_executed: int = 1

    while True:
        try:
            HEARTBEATS[stockStart.baseStock] = time.time()
        except Exception:
            pass

        # Kill switch: liquidacao FORA do thread_lock
        try:
            from src.modules.PaperBalanceLogger import kill_switch_active
            killed, kill_reason = kill_switch_active()
        except Exception:
            killed, kill_reason = False, ""
        if killed and MaTrader.actual_trade_position:
            MaTrader._force_sell_bypass_stale = True
            try:
                logging.warning(f"KILL SWITCH liquidando {MaTrader.operation_code}: {kill_reason}")
                liquidated = False
                last_err = None
                for attempt in range(3):
                    try:
                        MaTrader.cancelAllOrders()
                    except Exception as e_cancel:
                        last_err = e_cancel
                        logging.error(f"KILL cancelAllOrders falhou {MaTrader.operation_code} tent {attempt+1}: {e_cancel}")
                    try:
                        MaTrader.sellMarketOrder()
                    except Exception as e_sell:
                        last_err = e_sell
                        logging.error(f"KILL sellMarketOrder falhou {MaTrader.operation_code} tent {attempt+1}: {e_sell}")
                    try:
                        if not MaTrader.paper_trading:
                            MaTrader.updateAllData()
                    except Exception as e_refresh:
                        logging.warning(f"KILL refresh saldo falhou {MaTrader.operation_code}: {e_refresh}")
                    if not MaTrader.actual_trade_position:
                        liquidated = True
                        break
                    backoff = min(5 * (attempt + 1), 15)
                    logging.warning(f"KILL {MaTrader.operation_code} ainda aberto apos tentativa {attempt+1}, retry em {backoff}s")
                    time.sleep(backoff)
                if not liquidated:
                    crit = f"KILL SWITCH FALHOU em {MaTrader.operation_code} apos 3 tentativas. Ultima exc: {last_err}"
                    logging.critical(crit)
                    Dashboard.update("_status", {"msg": crit})
                else:
                    logging.warning(f"KILL SWITCH ok em {MaTrader.operation_code}")
            finally:
                MaTrader._force_sell_bypass_stale = False
            time.sleep(MaTrader.time_to_trade)
            continue

        try:
            if THREAD_LOCK:
                with thread_lock:
                    MaTrader.execute()
                    total_executed += 1
            else:
                MaTrader.execute()
                total_executed += 1
        except Exception as e:
            logging.error(f"execute() crashou em {MaTrader.operation_code}: {e}", exc_info=True)
            time.sleep(30)
            continue

        try:
            price = float(MaTrader.stock_data["close_price"].iloc[-1]) if MaTrader.stock_data is not None else None
            if MaTrader.paper_trading and price is not None:
                mark_to_market(MaTrader.operation_code, price)
            buy_price = MaTrader.last_buy_price or None
            pnl = ((price - buy_price) / buy_price * 100) if (buy_price and price and MaTrader.actual_trade_position) else None
            sl_price = (buy_price * (1 - MaTrader.stop_loss_percentage)) if buy_price else None

            Dashboard.update(MaTrader.operation_code, {
                "position": MaTrader.actual_trade_position,
                "price": price,
                "buy_price": buy_price if MaTrader.actual_trade_position else None,
                "pnl_pct": pnl,
                "stop_loss": sl_price if MaTrader.actual_trade_position else None,
                "trailing_stop": MaTrader.trailing_stop_price if MaTrader.trailing_is_running else None,
                "decision": "Comprar" if MaTrader.last_trade_decision else ("Vender" if MaTrader.last_trade_decision is False else "Aguardando"),
                "cycle": total_executed,
                "paper_trading": MaTrader.paper_trading,
                "offline_mode": MaTrader.paper_trading,
            })
            Dashboard.render()
        except Exception as e:
            logging.warning(f"Dashboard/mark_to_market falhou em {MaTrader.operation_code}: {e}")

        time.sleep(MaTrader.time_to_sleep)


threads_by_asset: dict[str, threading.Thread] = {}
asset_by_base: dict[str, StockStartModel] = {s.baseStock: s for s in stocks_traded_list}


def _spawn_trader_thread(asset_model: StockStartModel) -> threading.Thread:
    th = threading.Thread(target=trader_loop, args=(asset_model,), daemon=True)
    th.start()
    return th


for asset in stocks_traded_list:
    threads_by_asset[asset.baseStock] = _spawn_trader_thread(asset)

threads = list(threads_by_asset.values())

Dashboard.update("_status", {"msg": f"Threads iniciadas ({len(threads)} ativos)"})


RESPAWN_COOLDOWN_SECONDS = int(os.getenv("RESPAWN_COOLDOWN_SECONDS", "300"))
_last_respawn_at: dict[str, float] = {}


def _heartbeat_watcher():
    while True:
        time.sleep(60)
        now = time.time()
        # Hub diagnostic
        if market_data_hub is not None:
            try:
                health = market_data_hub.stream_health()
                if health.get("stale_buffers"):
                    logging.warning(f"[HUB] {len(health['stale_buffers'])} buffers stale")
            except Exception:
                pass
        for base, model in asset_by_base.items():
            last = HEARTBEATS.get(base)
            stale = (now - last) if last is not None else None
            th = threads_by_asset.get(base)
            dead = th is not None and not th.is_alive()
            silent = stale is not None and stale > HEARTBEAT_STALE_SECONDS
            if not (dead or silent):
                continue
            reason = "thread morreu" if dead else f"sem heartbeat ha {int(stale)}s"
            msg = f"[HEALTH] {base}: {reason} (limite {HEARTBEAT_STALE_SECONDS}s)."
            logging.error(msg)
            Dashboard.update("_status", {"msg": msg})
            if not dead:
                continue
            last_respawn = _last_respawn_at.get(base, 0.0)
            if now - last_respawn < RESPAWN_COOLDOWN_SECONDS:
                logging.warning(f"[HEALTH] {base}: respawn em cooldown, ignorando.")
                continue
            try:
                logging.warning(f"[HEALTH] {base}: respawn da thread.")
                threads_by_asset[base] = _spawn_trader_thread(model)
                _last_respawn_at[base] = now
                Dashboard.update("_status", {"msg": f"[HEALTH] {base} respawn ok"})
            except Exception as e:
                crit = f"[HEALTH] {base}: respawn falhou: {e}"
                logging.critical(crit)
                Dashboard.update("_status", {"msg": crit})


_watcher = threading.Thread(target=_heartbeat_watcher, daemon=True)
_watcher.start()


def _config_reload_watcher():
    flag = Path(".env.reload")
    while True:
        time.sleep(30)
        if flag.exists():
            try:
                from src.modules.PaperBalanceLogger import reload_config_from_env
                cfg = reload_config_from_env()
                logging.info(f"[CONFIG RELOAD] {cfg}")
                Dashboard.update("_status", {"msg": "Config recarregada do .env"})
                flag.unlink(missing_ok=True)
            except Exception as e:
                logging.error(f"reload config falhou: {e}")


threading.Thread(target=_config_reload_watcher, daemon=True).start()


try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\nPrograma encerrado pelo usuario.")
    if market_data_hub is not None:
        try:
            market_data_hub.stop()
        except Exception:
            pass

# fmt: on
