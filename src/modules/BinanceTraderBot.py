# fmt: off
import os
import time
from datetime import datetime
import logging

from dotenv import load_dotenv
import pandas as pd

from tinydb import TinyDB, Query
from binance.client import Client
from binance.enums import *
from binance.enums import SIDE_SELL, ORDER_TYPE_STOP_LOSS_LIMIT

from binance.exceptions import BinanceAPIException

from src.modules.BinanceClient import BinanceClient
from src.modules.binance_credentials import looks_like_real_binance_credentials
from src.modules.TraderOrder import TraderOrder
from src.modules.Logger import *
from src.modules.TelegramNotifier import notificar_compra, notificar_venda, notificar_stop_loss, notificar_trailing_stop, notificar_take_profit
from src.modules.order_math import (
    adjust_to_step as _adjust_to_step,
    validate_position_size as _validate_position_size,
)

from src.modules.StrategyRunner import StrategyRunner
from src.modules.PaperBalanceLogger import (
    can_open_new_position,
    get_open_position_qty,
    log_buy as paper_log_buy,
    log_sell as paper_log_sell,
    plan_paper_buy,
)


from src.strategies.moving_average_antecipation import getMovingAverageAntecipationTradeStrategy
from src.strategies.moving_average import getMovingAverageTradeStrategy

from src.indicators import Indicators
# fmt: on


load_dotenv()
api_key = os.getenv("BINANCE_API_KEY")
secret_key = os.getenv("BINANCE_SECRET_KEY")


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().strip('"').strip("'").lower() in {"1", "true", "yes", "y", "sim", "on"}


# ------------------------------------------------------------------
# Bases de Dados
DB_TRAILING = "src/database/trailing_db.json"  # arquivo JSON onde TinyDB irá persistir os estados
DB_TRAILING_TABLE_NAME = "trailing"

# Lock global — evita corrida de IDs no TinyDB quando 5 threads gravam no mesmo arquivo.
import threading
_TRAILING_DB_LOCK = threading.Lock()

# Circuit breaker global de rate limit: quando uma thread leva 429/418, todas pausam.
RATE_LIMIT_BACKOFF_UNTIL: float = 0.0
_RATE_LIMIT_LOCK = threading.Lock()


def _trip_rate_limit(seconds: float = 300.0) -> None:
    global RATE_LIMIT_BACKOFF_UNTIL
    with _RATE_LIMIT_LOCK:
        target = time.time() + seconds
        if target > RATE_LIMIT_BACKOFF_UNTIL:
            RATE_LIMIT_BACKOFF_UNTIL = target


def _rate_limit_remaining() -> float:
    return max(0.0, RATE_LIMIT_BACKOFF_UNTIL - time.time())


# ------------------------------------------------------------------
# Classe Principal
class BinanceTraderBot:

    # --------------------------------------------------------------
    tick_size: float
    step_size: float

    # Construtor
    def __init__(
        self,
        base_stock,
        quote_stock,
        traded_quantity,
        traded_percentage,
        candle_period,
        time_to_trade=30 * 60,
        delay_after_order=60 * 60,
        acceptable_loss_percentage=0.5,
        stop_loss_percentage=3.5,
        fallback_activated=True,
        take_profit_at_percentage=None,
        take_profit_amount_percentage=None,
        main_strategy=None,
        main_strategy_args=None,
        fallback_strategy=None,
        fallback_strategy_args=None,
        execute_market_orders=False,
        trailing_stop_percent=100,
        trailing_stop_enabled=False,
        pause_after_take_profit=False,
        pause_after_stop_loss=False,
        pause_after_trailing_stop=False,
        # Protecao USD-based (sobrepoe % quando > 0):
        # - stop_loss_usd: SL fixo em USD (ex: 10 = sai com -$10)
        # - trail_activation_usd: lucro em USD pra ativar trailing
        # - trail_floor_usd: lucro minimo em USD que o trailing trava
        # - trail_giveback_pct: % do peak que o trail pode devolver
        stop_loss_usd=0.0,
        trail_activation_usd=0.0,
        trail_floor_usd=0.0,
        trail_giveback_pct=25.0,
        # MarketDataHub (multi-TF live). Quando setado, getStockData/getFinalDecisionStrategy
        # leem candles do hub ao inves de REST polling. strategy_timeframes lista os TFs
        # que o bot deve consultar no hub (ex: ["1m","5m","15m","1h","4h","1d"]).
        market_data_hub=None,
        strategy_timeframes=None,
    ):

        print("------------------------------------------------")
        print("🤖 Robo Trader iniciando...")

        # fmt: off

        self.base_stock = base_stock  # Código base da stock negociada (ex: 'BTC')
        self.quote_stock = quote_stock # Código da moeda cotada (ex: 'USDT')
        self.operation_code = f'{base_stock}{quote_stock}' # Código negociado/moeda (ex:'BTCBRL')
        self.traded_quantity = traded_quantity  # Quantidade incial que será operada
        self.traded_percentage = traded_percentage  # Porcentagem do total da carteira, que será negociada        
        self.candle_period = candle_period  # Período levado em consideração para operação (ex: 15min)        

        self.fallback_activated = fallback_activated  # Define se a estratégia de Fallback será usada (ela pode entrar comprada em mercados subindo)
        self.acceptable_loss_percentage = acceptable_loss_percentage / 100 # % Máxima que o bot aceita perder quando vender
        self.stop_loss_percentage = stop_loss_percentage / 100 # % Máxima de loss que ele aceita, em caso de não vender na ordem limitada

        self.take_profit_at_percentage = list(take_profit_at_percentage or []) # Quanto de valorização para pegar lucro. (Array exemplo: [2, 5, 10])
        self.take_profit_amount_percentage = list(take_profit_amount_percentage or []) # Quanto da quantidade tira de lucro. (Array exemplo: [25, 25, 40])

        self.main_strategy = main_strategy # Estratégia principal
        self.main_strategy_args = dict(main_strategy_args or {}) # (opcional) Argumentos da estratégia principal
        self.fallback_strategy = fallback_strategy # (opcional) Estratégia de Fallback
        self.fallback_strategy_args = dict(fallback_strategy_args or {}) # (opcional) Argumentos da estratégia de fallback
        
        # Configurações de tempos de espera
        self.time_to_trade = time_to_trade
        self.delay_after_order = delay_after_order
        self.time_to_sleep = time_to_trade

        offline_mode = os.getenv("OFFLINE_MODE", "false").lower() == "true"
        keys_invalid = not looks_like_real_binance_credentials(api_key, secret_key)
        self.live_data_only = keys_invalid and not offline_mode

        if offline_mode:
            from src.modules.MockBinanceClient import MockBinanceClient
            self.client_binance = MockBinanceClient(base_stock=base_stock, quote_stock=quote_stock)
            print("[OFFLINE] Dados sinteticos ativos - zero API key necessaria.")
        elif self.live_data_only:
            self.client_binance = BinanceClient(
                "", "", sync=False, ping=True, sync_interval=30000, verbose=False
            )
            print("[LIVE DATA] Precos reais da Binance (endpoints publicos) - sem API key.")
        else:
            self.client_binance = BinanceClient(
                api_key, secret_key, sync=True, sync_interval=30000, verbose=False
            )  # Inicia o client da Binance

        self.setStepSizeAndTickSize() # Seta o time_step e step_size da classe (só precisa executar 1x)

        # Dados limpos antes do updateAllData chamada em execute()
        self.account_data = None
        self.last_stock_account_balance = 0.0
        self.actual_trade_position = None #
        self.stock_data = None
        self.open_orders = []
        self.last_trade_decision = None  # Última decisão de posição (False = Vender | True = Comprar)
        self.last_buy_price = 0.0 # Último valor de ordem de COMPRA executado
        self.last_sell_price = 0.0 # Último valor de ordem de VENDA executada
        self.take_profit_index = 0       
        self.partial_quantity_discount = 0  # Valor que já foi executado e que será descontado da quantidade, caso uma ordem não seja completamente executada

        # 1.5
        self.execute_market_orders = execute_market_orders  # Define se o bot irá executar ordens de mercado (se False, ele executa ordens limitadas)
        
        self.pause_bot_state = False  # Define se o bot está pausado
        self.pause_after_take_profit = pause_after_take_profit      # Define se o bot deve pausar após atingir o take profit
        self.pause_after_stop_loss = pause_after_stop_loss          # Define se o bot deve pausar após atingir o stop loss
        self.pause_after_trailing_stop = pause_after_trailing_stop  # Define se o bot deve pausar após atingir o trailing stop

        # Protecao USD-based.
        self.stop_loss_usd = float(stop_loss_usd or 0.0)
        self.trail_activation_usd = float(trail_activation_usd or 0.0)
        self.trail_floor_usd = float(trail_floor_usd or 0.0)
        self.trail_giveback_pct = max(0.0, min(100.0, float(trail_giveback_pct or 0.0)))
        self.trail_usd_active = False  # ja passou pelo gatilho de ativacao?
        self.peak_pnl_usd = 0.0        # maior lucro USD ja visto na posicao corrente

        # Trailing Stop - Configuração e Estado
        self.trailing_stop_percent = max(trailing_stop_percent / 100, 0.0001)  # Porcentagem abaixo da máxima comprada que vende
        self.trailing_stop_enabled = trailing_stop_enabled  # Configuração do usuário: quer usar trailing stop?
        self.trailing_is_running = False  # Estado atual: trailing stop está funcionando agora?
        self.db = TinyDB(DB_TRAILING)
        self.trailing_table = self.db.table(DB_TRAILING_TABLE_NAME)
        self.max_price_since_buy = 0.0
        self.trailing_stop_price = 0.0
        self.last_buy_price_for_trailing = 0.0
        self.last_buy_time = None  # Timestamp da última compra executada
        self.load_trailing_state() # Carrega o estado do trailing caso exista

        # MarketDataHub: candles ao vivo via WebSocket multi-TF
        self.market_data_hub = market_data_hub
        self.strategy_timeframes = list(strategy_timeframes) if strategy_timeframes else None

        # Paper Trading. Fail-safe: sem configuracao explicita, fica em paper.
        self.paper_trading = _env_truthy("PAPER_TRADING", True)
        self.paper_last_buy_price = 0.0
        self.paper_last_sell_price = 0.0
        if self.paper_trading:
            print("[PAPER] Modo paper trading ativo - nenhuma ordem real sera executada.")

    # fmt: on

    def _strategy_name(self):
        """Nome canonico da estrategia principal.

        Prioridade:
          1. atributo `__strategy_name__` da funcao (override explicito).
          2. nome curto derivado de __name__ (mantido por compatibilidade
             com historico em paper_balance/state.json).
        """
        fn = getattr(self, "main_strategy", None)
        if fn is None:
            return "-"
        explicit = getattr(fn, "__strategy_name__", None)
        if explicit:
            return str(explicit)
        name = getattr(fn, "__name__", "-")
        if name.startswith("adapted_get"):
            name = name[len("adapted_get"):]
        elif name.startswith("get"):
            name = name[3:]
        if name.endswith("TradeStrategy"):
            name = name[:-len("TradeStrategy")]
        return name or "-"

    def _fake_order(self, side, quantity, price=None, order_type="LIMIT"):
        current_price = price if price else float(self.stock_data["close_price"].iloc[-1])
        qty = float(quantity) if quantity else float(self.traded_quantity)
        ts = int(time.time() * 1000)
        return {
            "symbol": self.operation_code,
            "orderId": ts,
            "orderListId": -1,
            "clientOrderId": f"paper_{ts}",
            "transactTime": ts,
            "price": str(current_price),
            "origQty": str(qty),
            "executedQty": str(qty),
            "cummulativeQuoteQty": str(qty * current_price),
            "status": "FILLED",
            "timeInForce": "GTC",
            "type": order_type,
            "side": side,
            "time": ts,
            "fills": [],
        }

    # Atualiza todos os dados da conta
    # Função importante, sempre incrementar ela, em caso de novos gets
    def updateAllData(
        self,
        verbose=False,
    ):
        try:
            # Dados atualizados do usuário e sua carteira
            self.account_data = self.getUpdatedAccountData()
            # Balanço atual do ativo na carteira
            self.last_stock_account_balance = self.getLastStockAccountBalance()
            # Posição atual (False = Vendido | True = Comprado)
            self.actual_trade_position = self.getActualTradePosition()
            # Atualiza dados usados nos modelos
            self.stock_data = self.getStockData()
            # Retorna uma lista com todas as ordens abertas
            self.open_orders = self.getOpenOrders()
            # Salva o último valor de compra executado com sucesso
            self.last_buy_price = self.getLastBuyPrice(verbose)
            # Salva o último valor de venda executado com sucesso
            self.last_sell_price = self.getLastSellPrice(verbose)
            # Se a posição atual for vendida, ele reseta o index do take profit
            if self.actual_trade_position == False:
                self.take_profit_index = 0
            # Reseta o trailing stop se a posição for vendida
            if self.actual_trade_position == False:
                self.reset_trailing_state()
            return True

        except BinanceAPIException as e:
            print(f"Erro na atualização de dados: {e}")
            logging.warning(f"BinanceAPIException em updateAllData {self.operation_code}: {e}")
        except (ConnectionError, TimeoutError, OSError) as e:
            print(f"Erro de rede em updateAllData {self.operation_code}: {e}")
            logging.warning(f"Network em updateAllData {self.operation_code}: {e}")
        except Exception as e:
            print(f"Erro inesperado em updateAllData {self.operation_code}: {e}")
            logging.error(f"updateAllData {self.operation_code}: {e}", exc_info=True)
        return False

    # ------------------------------------------------------------------
    # GETS Principais

    # Busca infos atualizada da conta Binance
    def getUpdatedAccountData(self):
        if self.live_data_only:
            return {"balances": [{"asset": self.base_stock, "free": "0", "locked": "0"}]}
        return self.client_binance.get_account()  # Busca infos da conta

    # Busca o último balanço da conta, na stock escolhida.
    def getLastStockAccountBalance(self):
        if self.paper_trading:
            qty = get_open_position_qty(self.operation_code)
            return qty if qty > 0 else 0.0
        in_wallet_amount = 0.0
        for stock in self.account_data["balances"]:
            if stock["asset"] == self.base_stock:
                free = float(stock["free"])
                locked = float(stock["locked"])
                in_wallet_amount = free + locked
                break
        return float(in_wallet_amount)

    # Checa se a posição atual é comprado ou vendido
    def getActualTradePosition(self):
        """
        Determina a posição atual (comprado ou vendido).

        Em REAL: nao basta haver saldo > step_size — qualquer poeira de compra antiga
        passaria por "comprado". Exige saldo >= traded_quantity * DUST_THRESHOLD_PCT (env)
        para considerar posicao do bot. Default 50%: pelo menos metade da posicao tipica.

        Em PAPER: confia em get_open_position_qty (paper_balance/state.json).
        """
        try:
            if self.paper_trading:
                return self.last_stock_account_balance > 0
            try:
                dust_pct = float(os.getenv("DUST_THRESHOLD_PCT", 0.50))
            except (TypeError, ValueError):
                dust_pct = 0.50
            min_qty = max(self.step_size, float(self.traded_quantity) * dust_pct)
            return self.last_stock_account_balance >= min_qty

        except Exception as e:
            print(f"Erro ao determinar a posição atual para {self.operation_code}: {e}")
            return False  # Retorna como vendido por padrão em caso de erro

    # Mapeia candle_period (Binance interval string) para TF canonico do hub.
    _CANDLE_PERIOD_TO_TF = {
        Client.KLINE_INTERVAL_1MINUTE: "1m",
        Client.KLINE_INTERVAL_3MINUTE: "3m",
        Client.KLINE_INTERVAL_5MINUTE: "5m",
        Client.KLINE_INTERVAL_15MINUTE: "15m",
        Client.KLINE_INTERVAL_30MINUTE: "30m",
        Client.KLINE_INTERVAL_1HOUR: "1h",
        Client.KLINE_INTERVAL_2HOUR: "2h",
        Client.KLINE_INTERVAL_4HOUR: "4h",
        Client.KLINE_INTERVAL_6HOUR: "6h",
        Client.KLINE_INTERVAL_8HOUR: "8h",
        Client.KLINE_INTERVAL_12HOUR: "12h",
        Client.KLINE_INTERVAL_1DAY: "1d",
    }

    def _bot_tf_canonical(self) -> str:
        """TF canonico do bot (ex: '15m') derivado do candle_period."""
        return self._CANDLE_PERIOD_TO_TF.get(self.candle_period, "15m")

    # Busca os dados do ativo no periodo
    def getStockData(
        self,
    ):
        # Hub-first: se hub esta disponivel, usa o buffer ao vivo do TF base do bot.
        # Sem fallback REST aqui — o hub ja faz backfill+stream e nunca fica sem dado
        # (so fica vazio nos primeiros segundos do boot, antes do backfill terminar).
        if self.market_data_hub is not None:
            tf = self._bot_tf_canonical()
            df = self.market_data_hub.get_dataframe(self.operation_code, tf, include_live=True)
            if df is not None and not df.empty:
                self._stock_data_stale = False
                # Schema legado do bot: close_price/open_time/open_price/high_price/low_price/volume
                # com open_time como datetime UTC (pd.to_datetime unit=ms). Hub ja entrega assim.
                cols = ["close_price", "open_time", "open_price", "high_price", "low_price", "volume"]
                out = df[cols].copy()
                return out
            else:
                # Hub sem dado ainda — informa stale e devolve ultimo conhecido.
                self._stock_data_stale = True
                if self.stock_data is not None:
                    return self.stock_data
                # nao tem nada -> cai pro REST como fallback no boot

        # Circuit breaker global de rate limit: se outra thread tomou 429, espera todas.
        remaining = _rate_limit_remaining()
        if remaining > 0:
            print(f"[RATE LIMIT] Pausa global ativa: aguardando {remaining:.0f}s ({self.operation_code}).", flush=True)
            time.sleep(min(remaining, 60))
            if self.stock_data is not None:
                return self.stock_data

        # Retry com backoff exponencial: rede instavel nao deve derrubar a thread.
        candles = None
        last_exc = None
        for attempt in range(4):
            try:
                candles = self.client_binance.get_klines(
                    symbol=self.operation_code,
                    interval=self.candle_period,
                    limit=1000,
                )
                # Sucesso: marca dados como frescos
                self._stock_data_stale = False
                break
            except BinanceAPIException as exc:
                last_exc = exc
                code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                if code in (429, 418, -1003) or "Too many requests" in str(exc) or "banned" in str(exc).lower():
                    # Trip global: 5 minutos default, 30 min se IP banido (418).
                    backoff = 1800 if code == 418 else 300
                    _trip_rate_limit(backoff)
                    print(f"[RATE LIMIT] {self.operation_code}: {exc}. Pausa global {backoff}s.", flush=True)
                    logging.error(f"RATE LIMIT trip {self.operation_code}: {exc}")
                    break
                wait = min(2 ** attempt, 8)
                print(f"[NET] get_klines {self.operation_code} falhou (tentativa {attempt + 1}/4): {exc}. Aguardando {wait}s.", flush=True)
                logging.warning(f"get_klines retry {attempt + 1}/4 {self.operation_code}: {exc}")
                time.sleep(wait)
            except (ConnectionError, TimeoutError, OSError) as exc:
                last_exc = exc
                wait = min(2 ** attempt, 8)
                print(f"[NET] get_klines {self.operation_code} falhou (tentativa {attempt + 1}/4): {exc}. Aguardando {wait}s.", flush=True)
                logging.warning(f"get_klines retry {attempt + 1}/4 {self.operation_code}: {exc}")
                time.sleep(wait)
        if candles is None:
            # Apos 4 tentativas, marca dados como velhos e retorna ultimo conhecido.
            self._stock_data_stale = True
            print(f"[NET] get_klines {self.operation_code} falhou em todas as tentativas. Dados velhos.", flush=True)
            logging.error(f"get_klines exausted {self.operation_code}: {last_exc}")
            if self.stock_data is not None:
                return self.stock_data
            raise last_exc if last_exc else RuntimeError("get_klines retornou None sem excecao")

        # Transforma um um DataFrame Pandas
        prices = pd.DataFrame(candles)

        # Renomea as colunas baseada na Documentação da Binance
        prices.columns = [
            "open_time",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
            "close_time",
            "quote_asset_volume",
            "number_of_trades",
            "taker_buy_base_asset_volume",
            "taker_buy_quote_asset_volume",
            "-",
        ]

        # Pega apenas os indicadores que queremos para esse modelo
        prices = prices[
            [
                "close_price",
                "open_time",
                "open_price",
                "high_price",
                "low_price",
                "volume",
            ]
        ]

        # Converte as colunas para o tipo numérico
        prices["close_price"] = pd.to_numeric(
            prices["close_price"],
            errors="coerce",
        )
        prices["open_price"] = pd.to_numeric(
            prices["open_price"],
            errors="coerce",
        )
        prices["high_price"] = pd.to_numeric(
            prices["high_price"],
            errors="coerce",
        )
        prices["low_price"] = pd.to_numeric(
            prices["low_price"],
            errors="coerce",
        )
        prices["volume"] = pd.to_numeric(
            prices["volume"],
            errors="coerce",
        )

        # Corrige o tempo de fechamento
        prices["open_time"] = pd.to_datetime(
            prices["open_time"],
            unit="ms",
            utc=True,  # já cria como tz-aware UTC
        )
        # Se quiser converter para outro timezone para exibição, use tz_convert
        # prices["open_time"] = prices["open_time"].dt.tz_convert("America/Sao_Paulo")
        # CÁLCULOS PRÉVIOS...

        return prices

    # Retorna o preço da última ordem de compra executada para o ativo configurado.
    # Retorna 0.0 se nenhuma ordem de compra foi encontrada.
    def getLastBuyPrice(
        self,
        verbose=False,
    ):
        if self.paper_trading:
            # Se o bot foi reiniciado com posicao aberta, paper_last_buy_price
            # esta zerado em memoria. Recupera do state.json (entry_price).
            if not self.paper_last_buy_price:
                try:
                    from src.modules.PaperBalanceLogger import _load_state
                    state = _load_state()
                    pos = (state.get(self.operation_code) or {}).get("position") or {}
                    entry = float(pos.get("entry_price") or 0.0)
                    if entry > 0:
                        self.paper_last_buy_price = entry
                except Exception:
                    pass
            return self.paper_last_buy_price
        try:
            # Obtém o histórico de ordens do par configurado
            all_orders = self.client_binance.get_all_orders(
                symbol=self.operation_code,
                limit=100,
            )

            # Filtra apenas as ordens de compra executadas (FILLED)
            executed_buy_orders = [order for order in all_orders if order["side"] == "BUY" and order["status"] == "FILLED"]

            if executed_buy_orders:
                # Ordena as ordens por tempo (timestamp) para obter a mais recente
                last_executed_order = sorted(
                    executed_buy_orders,
                    key=lambda x: x["time"],
                    reverse=True,
                )[0]

                # print(f'ÚLTIMA EXECUTADA: {last_executed_order}')

                # Retorna o preço da última ordem de compra executada
                last_buy_price = float(last_executed_order["cummulativeQuoteQty"]) / float(last_executed_order["executedQty"])
                # Corrige o timestamp para a chave correta
                datetime_transact = datetime.utcfromtimestamp(last_executed_order["time"] / 1000).strftime("(%H:%M:%S) %d-%m-%Y")
                if verbose:
                    print(f"\nÚltima ordem de COMPRA executada para {self.operation_code}:")
                    print(
                        f" - Data: {datetime_transact} | Preço: {self.adjust_to_step(last_buy_price,self.tick_size, as_string=True)} | Qnt.: {self.adjust_to_step(float(last_executed_order['origQty']), self.step_size, as_string=True)}"
                    )

                return last_buy_price
            else:
                if verbose:
                    print(f"\nNão há ordens de COMPRA executadas para {self.operation_code}.")
                return 0.0

        except Exception as e:
            if verbose:
                print(f"Erro ao verificar a última ordem de COMPRA executada para {self.operation_code}: {e}")
            return 0.0

    # Retorna o preço da última ordem de venda executada para o ativo configurado.
    # Retorna 0.0 se nenhuma ordem de venda foi encontrada.
    def getLastSellPrice(
        self,
        verbose=False,
    ):
        if self.paper_trading:
            return self.paper_last_sell_price
        try:
            # Obtém o histórico de ordens do par configurado
            all_orders = self.client_binance.get_all_orders(
                symbol=self.operation_code,
                limit=100,
            )

            # Filtra apenas as ordens de venda executadas (FILLED)
            executed_sell_orders = [order for order in all_orders if order["side"] == "SELL" and order["status"] == "FILLED"]

            if executed_sell_orders:
                # Ordena as ordens por tempo (timestamp) para obter a mais recente
                last_executed_order = sorted(
                    executed_sell_orders,
                    key=lambda x: x["time"],
                    reverse=True,
                )[0]

                # Retorna o preço da última ordem de venda executada
                last_sell_price = float(last_executed_order["cummulativeQuoteQty"]) / float(last_executed_order["executedQty"])

                # Corrige o timestamp para a chave correta
                datetime_transact = datetime.utcfromtimestamp(last_executed_order["time"] / 1000).strftime("(%H:%M:%S) %d-%m-%Y")

                if verbose:
                    print(f"Última ordem de VENDA executada para {self.operation_code}:")
                    print(
                        f" - Data: {datetime_transact} | Preço: {self.adjust_to_step(last_sell_price,self.tick_size, as_string=True)} | Qnt.: {self.adjust_to_step(float(last_executed_order['origQty']), self.step_size, as_string=True)}"
                    )
                return last_sell_price
            else:
                if verbose:
                    print(f"\nNão há ordens de VENDA executadas para {self.operation_code}.")
                return 0.0

        except Exception as e:
            if verbose:
                print(f"Erro ao verificar a última ordem de VENDA executada para {self.operation_code}: {e}")
            return 0.0

    def getTimestamp(self):
        """
        Retorna o timestamp ajustado com base no desvio de tempo entre o sistema local e o servidor da Binance.
        Re-sincroniza a cada 30 minutos para evitar drift acumulado.
        """
        try:
            now = time.time()
            need_sync = (
                not hasattr(self, "time_offset")
                or self.time_offset is None
                or not hasattr(self, "_time_offset_sync_at")
                or (now - getattr(self, "_time_offset_sync_at", 0)) > 1800
            )
            if need_sync:
                server_time = self.client_binance.get_server_time()["serverTime"]
                local_time = int(now * 1000)
                self.time_offset = server_time - local_time
                self._time_offset_sync_at = now

            # Retorna o timestamp ajustado
            adjusted_timestamp = int(time.time() * 1000) + self.time_offset
            return adjusted_timestamp

        except Exception as e:
            print(f"Erro ao ajustar o timestamp: {e}")
            # Retorna o timestamp local em caso de falha, mas não é recomendado para chamadas críticas
            return int(time.time() * 1000)

    # --------------------------------------------------------------
    # SETs

    # Seta o step_size (para quantidade) e tick_size (para preço) do ativo operado, só precisa ser executado 1x
    def setStepSizeAndTickSize(self):
        # Obter informações do símbolo para respeitar os filtros
        symbol_info = self.client_binance.get_symbol_info(self.operation_code)
        price_filter = next(f for f in symbol_info["filters"] if f["filterType"] == "PRICE_FILTER")
        self.tick_size = float(price_filter["tickSize"])

        lot_size_filter = next(f for f in symbol_info["filters"] if f["filterType"] == "LOT_SIZE")
        self.step_size = float(lot_size_filter["stepSize"])

    """
    Ajusta o valor para o múltiplo mais próximo do passo definido, lidando com problemas de precisão
    e garantindo que o resultado não seja retornado em notação científica.

    Parameters:
        value (float): O valor a ser ajustado.
        step (float): O incremento mínimo permitido.
        as_string (bool): Define se o valor ajustado será retornado como string. Padrão é True.

    Returns:
        str|float: O valor ajustado no formato especificado.
    """

    def adjust_to_step(
        self,
        value,
        step,
        as_string=False,
    ):
        return _adjust_to_step(value, step, as_string=as_string)

    # --------------------------------------------------------------
    # PRINTS

    # Printa toda a carteira
    def printWallet(self):
        for stock in self.account_data["balances"]:
            if float(stock["free"]) > 0:
                print(stock)

    # Printa o ativo definido na classe
    def printStock(self):
        for stock in self.account_data["balances"]:
            if stock["asset"] == self.base_stock:
                print(stock)

    def printBrl(self):
        for stock in self.account_data["balances"]:
            if stock["asset"] == "BRL":
                print(stock)

    # Printa todas ordens abertas
    def printOpenOrders(self):
        # Log das ordens abertas
        if self.open_orders:
            print("-------------------------")
            print(f"Ordens abertas para {self.operation_code}:")
            for order in self.open_orders:
                to_print = (
                    f"----"
                    f"\nID {order['orderId']}:"
                    f"\n - Status: {getOrderStatus(order['status'])}"
                    f"\n - Side: {order['side']}"
                    f"\n - Ativo: {order['symbol']}"
                    f"\n - Preço: {order['price']}"
                    f"\n - Quantidade Original: {order['origQty']}"
                    f"\n - Quantidade Executada: {order['executedQty']}"
                    f"\n - Tipo: {order['type']}"
                )
                print(to_print)
            print("-------------------------")

        else:
            print(f"Não há ordens abertas para {self.operation_code}.")

    # --------------------------------------------------------------
    # GETs auxiliares

    # Retorna toda a carteira
    def getWallet(self):
        for stock in self.account_data["balances"]:
            if float(stock["free"]) > 0:
                return stock

    # Retorna todo o ativo definido na classe
    def getStock(self):
        for stock in self.account_data["balances"]:
            if stock["asset"] == self.base_stock:
                return stock

    def getPriceChangePercentage(self, initial_price, close_price):
        if initial_price == 0:
            raise ValueError("O initial_price não pode ser zero.")

        percentual_change = ((close_price - initial_price) / initial_price) * 100
        return percentual_change

    def riskGuardAllowsBuy(self):
        # Dados velhos (fallback do retry) nao podem abrir nova posicao.
        if getattr(self, "_stock_data_stale", False):
            msg = f"[RISK GUARD] Compra bloqueada em {self.operation_code}: dados velhos (rede)."
            print(f"\n{msg}", flush=True)
            logging.warning(msg)
            return False
        allowed, reason = can_open_new_position(self.operation_code, self._strategy_name())
        if not allowed:
            msg = f"[RISK GUARD] Compra bloqueada em {self.operation_code}: {reason}."
            print(f"\n{msg}", flush=True)
            logging.warning(msg)
        return allowed

    # --------------------------------------------------------------
    # FUNÇÕES DE COMPRA

    _CANDLE_PERIOD_SECONDS = {
        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
        "12h": 43200, "1d": 86400, "3d": 259200, "1w": 604800,
    }

    def _data_is_fresh(self, max_age_multiplier: float = 2.5) -> tuple[bool, str]:
        """Bloqueia ordens reais se o ultimo candle estiver stale.

        Stale = retry de get_klines exauriu (_stock_data_stale=True)
        ou ultimo open_time mais antigo que max_age_multiplier x candle_period.
        """
        if getattr(self, "_stock_data_stale", False):
            return False, "stock_data marcado stale (get_klines exausto)"
        if self.stock_data is None or len(self.stock_data) == 0:
            return False, "stock_data vazio"
        try:
            last_open_ms = float(self.stock_data["open_time"].iloc[-1])
        except Exception as exc:
            return False, f"open_time invalido: {exc}"
        period_seconds = self._CANDLE_PERIOD_SECONDS.get(str(self.candle_period), 900)
        max_age_seconds = period_seconds * max_age_multiplier
        now_ms = time.time() * 1000.0
        age_seconds = (now_ms - last_open_ms) / 1000.0
        if age_seconds > max_age_seconds:
            return False, (
                f"candle stale: idade {age_seconds:.0f}s > limite "
                f"{max_age_seconds:.0f}s ({self.candle_period})"
            )
        return True, ""

    def _block_if_stale(self, side: str) -> bool:
        """Retorna True se deve bloquear ordem real por dados stale."""
        ok, reason = self._data_is_fresh()
        if ok:
            return False
        msg = f"[REAL] {side} bloqueada em {self.operation_code}: {reason}"
        print(msg, flush=True)
        logging.critical(msg)
        return True

    def _validate_real_position_size(self, quantity: float, price: float) -> tuple[bool, float, str]:
        """Em real, ajusta `quantity` para nao estourar PAPER_MAX_POSITION_USD.

        Se notional > MAX_POSITION_USD, reduz quantity proporcionalmente.
        Retorna (ok, quantity_ajustada, motivo).
        """
        try:
            from src.modules.PaperBalanceLogger import MAX_POSITION_USD, MIN_POSITION_USD
        except ImportError:
            return True, quantity, ""
        return _validate_position_size(
            quantity=quantity,
            price=price,
            max_position_usd=MAX_POSITION_USD,
            min_position_usd=MIN_POSITION_USD,
        )

    # Compra a ação a MERCADO
    def _real_order_allowed(self, side: str) -> bool:
        """Trava dura para qualquer ordem real."""
        if _env_truthy("REAL_TRADING_CONFIRM", False):
            return True
        msg = (
            f"[REAL] Ordem {side} bloqueada em {self.operation_code}: "
            "REAL_TRADING_CONFIRM=true obrigatorio para operar dinheiro real."
        )
        print(msg, flush=True)
        logging.critical(msg)
        return False

    def _apply_tier_before_buy(self):
        """Aplica tier de risco/posicao do snapshot atual antes da compra.

        Atualiza self.stop_loss_usd, trail_activation_usd, trail_floor_usd e
        guarda em self._tier_*_pct os ratios (SL/posicao, trail_act/posicao,
        trail_floor/posicao) para que `_rescale_protection_to_actual_notional`
        possa reescalonar quando `plan_paper_buy` reduzir o notional.

        Retorna o cap de notional (override_max_usd) pro plan_paper_buy, ou
        None se nao houver tier_config no snapshot.
        """
        snap = getattr(self, "last_strategy_snapshot", None) or {}
        tier_cfg = snap.get("tier_config")
        if not isinstance(tier_cfg, dict):
            # Fallback: nao temos ratios do tier. _rescale nao reescalona.
            self._tier_sl_pct = None
            self._tier_trail_act_pct = None
            self._tier_trail_floor_pct = None
            logging.info(
                f"[TIER FALLBACK] {self.operation_code}: sem tier_config no snapshot. "
                f"usando STOP_LOSS_USD=${self.stop_loss_usd:.2f}, "
                f"trail_act=${self.trail_activation_usd:.2f}, "
                f"floor=${self.trail_floor_usd:.2f} (config estatica)."
            )
            return None
        try:
            position_usd = float(tier_cfg.get("position_usd") or 0.0)
            sl_usd_tier = float(tier_cfg.get("sl_usd") or self.stop_loss_usd)
            trail_act_tier = float(tier_cfg.get("trail_activation_usd") or self.trail_activation_usd)
            trail_floor_tier = float(tier_cfg.get("trail_floor_usd") or self.trail_floor_usd)
            if position_usd > 0:
                self._tier_sl_pct = sl_usd_tier / position_usd
                self._tier_trail_act_pct = trail_act_tier / position_usd
                self._tier_trail_floor_pct = trail_floor_tier / position_usd
            else:
                self._tier_sl_pct = None
                self._tier_trail_act_pct = None
                self._tier_trail_floor_pct = None
            self.stop_loss_usd = sl_usd_tier
            self.trail_activation_usd = trail_act_tier
            self.trail_floor_usd = trail_floor_tier
            self.trail_giveback_pct = float(tier_cfg.get("trail_giveback_pct") or self.trail_giveback_pct)
            tier = tier_cfg.get("tier")
            msg = (f"[TIER {tier}] posicao=${position_usd:.0f} | SL=${self.stop_loss_usd:.2f} | "
                   f"trail_act=+${self.trail_activation_usd:.2f} | floor=+${self.trail_floor_usd:.2f}")
            print(msg, flush=True)
            logging.info(msg)
            return position_usd if position_usd > 0 else None
        except Exception as exc:
            logging.warning(f"_apply_tier_before_buy falhou em {self.operation_code}: {exc}")
            return None

    def _rescale_protection_to_actual_notional(self, actual_notional: float) -> None:
        """Reescalona SL/trail USD pro notional efetivamente aprovado.

        plan_paper_buy pode reduzir a quantidade por caixa, exposicao ou
        reserva. Sem rescale, SL absoluto vira % maior do que o tier
        calibrou (ex: tier 1 com SL=$2 calibrado pra posicao $100 vira -8%
        se a posicao final for $25). Aqui mantemos o ratio SL/posicao.

        Sem ratio do tier (fallback do main), nao reescalona.
        """
        sl_pct = getattr(self, "_tier_sl_pct", None)
        if sl_pct is None or actual_notional <= 0:
            return
        new_sl = actual_notional * sl_pct
        new_act = actual_notional * (getattr(self, "_tier_trail_act_pct", None) or 0.0)
        new_floor = actual_notional * (getattr(self, "_tier_trail_floor_pct", None) or 0.0)
        if abs(new_sl - self.stop_loss_usd) > 0.01:
            msg = (f"[TIER RESCALE] {self.operation_code} notional ${actual_notional:.2f}: "
                   f"SL ${self.stop_loss_usd:.2f} -> ${new_sl:.2f} | "
                   f"trail_act ${self.trail_activation_usd:.2f} -> ${new_act:.2f} | "
                   f"floor ${self.trail_floor_usd:.2f} -> ${new_floor:.2f}")
            logging.info(msg)
        self.stop_loss_usd = new_sl
        self.trail_activation_usd = new_act
        self.trail_floor_usd = new_floor

    # Compra a ação a MERCADO
    def buyMarketOrder(self, quantity=None):
        if self.paper_trading:
            override_max_usd = self._apply_tier_before_buy()
            requested_qty = quantity or (self.traded_quantity - self.partial_quantity_discount)
            price = float(self.stock_data["close_price"].iloc[-1])
            allowed, planned_qty, reason = plan_paper_buy(
                self.operation_code,
                self._strategy_name(),
                price,
                float(requested_qty),
                override_max_usd=override_max_usd,
            )
            if not allowed:
                msg = f"[PAPER] COMPRA bloqueada em {self.operation_code}: {reason}"
                print(f"\n{msg}")
                logging.warning(msg)
                return False
            qty = planned_qty
            self._rescale_protection_to_actual_notional(float(price) * float(qty))
            self.paper_last_buy_price = price
            self.last_stock_account_balance = float(qty)
            self.actual_trade_position = True
            self.last_buy_time = pd.Timestamp.utcnow()
            self.save_trailing_state()
            fake = self._fake_order("BUY", qty, price, order_type="MARKET")
            print(f"\n[PAPER] COMPRA a mercado simulada: {qty} @ {price:.4f}")
            if reason:
                print(f"[PAPER] {reason}")
            createLogOrder(fake)
            paper_log_buy(
                self.operation_code,
                self._strategy_name(),
                price,
                float(qty),
                metadata=getattr(self, "last_strategy_snapshot", None),
            )
            notificar_compra(self.operation_code, float(qty), price, "MARKET [PAPER]")
            return fake
        try:
            if quantity == None:  # Se não definida, ele compra a traded_quantity - o que já foi executado
                quantity = self.adjust_to_step(
                    self.traded_quantity - self.partial_quantity_discount,
                    self.step_size,
                    as_string=True,
                )
            else:  # Se não, ele ajusta o valor passado
                quantity = self.adjust_to_step(
                    quantity,
                    self.step_size,
                    as_string=True,
                )
            if not self._real_order_allowed("BUY"):
                return False
            if self._block_if_stale("COMPRA MARKET"):
                return False

            # Em real, valida notional vs MAX_POSITION_USD antes de enviar.
            try:
                current_price = float(self.stock_data["close_price"].iloc[-1])
                ok, adjusted_qty, reason = self._validate_real_position_size(float(quantity), current_price)
                if not ok:
                    msg = f"[REAL] COMPRA bloqueada em {self.operation_code}: {reason}"
                    print(msg, flush=True)
                    logging.warning(msg)
                    return False
                if reason:
                    print(f"[REAL] {reason}", flush=True)
                    quantity = self.adjust_to_step(adjusted_qty, self.step_size, as_string=True)
            except Exception as e:
                logging.warning(f"validate_real_position_size falhou: {e}")

            order_buy = self.client_binance.create_order(
                symbol=self.operation_code,
                side=SIDE_BUY,  # Compra
                type=ORDER_TYPE_MARKET,  # Ordem de Mercado
                quantity=quantity,
            )

            # Registra o timestamp da compra para o trailing stop
            executed_qty = float(order_buy.get("executedQty", 0)) if order_buy else 0.0
            if executed_qty > 0:
                self.last_buy_time = pd.Timestamp.utcnow()  # UTC
                self.save_trailing_state()

            self.actual_trade_position = True  # Define posição como comprada
            createLogOrder(order_buy)  # Cria um log
            print(f"\nOrdem de COMPRA a mercado enviada com sucesso:")
            print(order_buy)
            notificar_compra(self.operation_code, float(order_buy.get("executedQty", 0)), float(order_buy.get("fills", [{}])[0].get("price", 0)) if order_buy.get("fills") else 0, "MARKET")
            return order_buy  # Retorna a ordem

        except Exception as e:
            logging.error(f"Erro ao executar ordem de compra a mercado: {e}")
            print(f"\nErro ao executar ordem de compra a mercado: {e}")
            return False

    # Compra por um preço máximo (Ordem Limitada)
    # [NOVA] Define o valor usando RSI e Volume Médio
    def buyLimitedOrder(
        self,
        price=0,
    ):
        if self.paper_trading:
            override_max_usd = self._apply_tier_before_buy()
            current_price = float(self.stock_data["close_price"].iloc[-1])
            requested_qty = self.traded_quantity - self.partial_quantity_discount
            allowed, planned_qty, reason = plan_paper_buy(
                self.operation_code,
                self._strategy_name(),
                current_price,
                float(requested_qty),
                override_max_usd=override_max_usd,
            )
            if not allowed:
                msg = f"[PAPER] COMPRA bloqueada em {self.operation_code}: {reason}"
                print(f"\n{msg}")
                logging.warning(msg)
                return False
            qty = planned_qty
            self._rescale_protection_to_actual_notional(float(current_price) * float(qty))
            self.paper_last_buy_price = current_price
            self.last_stock_account_balance = float(qty)
            self.actual_trade_position = True
            self.last_buy_time = pd.Timestamp.utcnow()
            self.save_trailing_state()
            fake = self._fake_order("BUY", qty, current_price)
            print(f"\n[PAPER] COMPRA limitada simulada: {qty} @ {current_price:.4f}")
            if reason:
                print(f"[PAPER] {reason}")
            createLogOrder(fake)
            paper_log_buy(
                self.operation_code,
                self._strategy_name(),
                current_price,
                float(qty),
                metadata=getattr(self, "last_strategy_snapshot", None),
            )
            notificar_compra(self.operation_code, float(qty), current_price, "LIMIT [PAPER]")
            return fake
        if not self._real_order_allowed("BUY"):
            return False
        if self._block_if_stale("COMPRA LIMIT"):
            return False
        close_price = self.stock_data["close_price"].iloc[-1]
        volume = self.stock_data["volume"].iloc[-1]  # Volume atual do mercado
        avg_volume = self.stock_data["volume"].rolling(window=20).mean().iloc[-1]  # Média de volume
        # Cacheia RSI por ciclo: evita recalculo redundante quando estrategia ja calculou.
        rsi = getattr(self, "_cycle_rsi", None)
        if rsi is None:
            rsi = Indicators.getRSI(series=self.stock_data["close_price"])
            self._cycle_rsi = rsi

        if price == 0:
            if rsi < 30:  # Mercado sobrevendido
                limit_price = close_price - (0.002 * close_price)  # Tenta comprar um pouco mais abaixo
            elif volume < avg_volume:  # Volume baixo (mercado lateral)
                limit_price = close_price + (0.002 * close_price)  # Ajuste pequeno acima
            else:  # Volume alto (mercado volátil)
                limit_price = close_price + (0.005 * close_price)  # Ajuste maior acima (caso suba muito rápido)
        else:
            limit_price = price

        # Ajustar o preço limite para o tickSize permitido
        limit_price = self.adjust_to_step(
            limit_price,
            self.tick_size,
            as_string=True,
        )

        # Ajustar a quantidade para o stepSize permitido
        quantity = self.adjust_to_step(
            self.traded_quantity - self.partial_quantity_discount,
            self.step_size,
            as_string=True,
        )

        # Log de informações
        print(f"Enviando ordem limitada de COMPRA para {self.operation_code}:")
        print(f" - RSI: {rsi}")
        print(f" - Quantidade: {quantity}")
        print(f" - Close Price: {close_price}")
        print(f" - Preço Limite: {limit_price}")

        # Enviar ordem limitada de COMPRA
        try:
            order_buy = self.client_binance.create_order(
                symbol=self.operation_code,
                side=SIDE_BUY,  # Compra
                type=ORDER_TYPE_LIMIT,  # Ordem Limitada
                timeInForce="GTC",  # Good 'Til Canceled (Ordem válida até ser cancelada)
                quantity=quantity,
                price=limit_price,
            )

            # Registra estado de compra apenas se a ordem executou alguma quantidade.
            executed_qty = float(order_buy.get("executedQty", 0)) if order_buy else 0.0
            if executed_qty > 0:
                self.last_buy_time = pd.Timestamp.utcnow()  # UTC
                self.save_trailing_state()

            self.actual_trade_position = executed_qty > 0
            print(f"\nOrdem COMPRA limitada enviada com sucesso:")
            if order_buy is not None:
                createLogOrder(order_buy)  # Cria um log
                if executed_qty > 0:
                    preco_exec = float(order_buy.get("fills", [{}])[0].get("price", 0)) if order_buy.get("fills") else float(order_buy.get("price", 0))
                    notificar_compra(self.operation_code, executed_qty, preco_exec, "LIMIT")

            return order_buy  # Retorna a ordem enviada
        except Exception as e:
            logging.error(f"Erro ao enviar ordem limitada de COMPRA: {e}")
            print(f"\nErro ao enviar ordem limitada de COMPRA: {e}")
            return False

    # --------------------------------------------------------------
    # FUNÇÕES DE VENDA

    # Vende a ação a MERCADO
    def sellMarketOrder(self, quantity=None):
        if self.paper_trading:
            price = float(self.stock_data["close_price"].iloc[-1])
            qty = quantity or self.last_stock_account_balance
            self.paper_last_sell_price = price
            self.actual_trade_position = False
            fake = self._fake_order("SELL", qty, price, order_type="MARKET")
            print(f"\n[PAPER] VENDA a mercado simulada: {qty} @ {price:.4f}")
            createLogOrder(fake)
            paper_log_sell(self.operation_code, price, float(qty))
            notificar_venda(self.operation_code, float(qty), price, self.paper_last_buy_price or price, "MARKET [PAPER]")
            return fake
        try:
            if self.actual_trade_position:  # Se a posição for comprada

                if quantity == None:  # Se não definida, ele vende tudo na carteira
                    quantity = self.adjust_to_step(
                        self.last_stock_account_balance,
                        self.step_size,
                        as_string=True,
                    )
                else:  # Se não, ele ajusta o valor passado
                    quantity = self.adjust_to_step(
                        quantity,
                        self.step_size,
                        as_string=True,
                    )

                if not self._real_order_allowed("SELL"):
                    return False
                if not getattr(self, "_force_sell_bypass_stale", False):
                    if self._block_if_stale("VENDA MARKET"):
                        return False
                order_sell = self.client_binance.create_order(
                    symbol=self.operation_code,
                    side=SIDE_SELL,  # Venda
                    type=ORDER_TYPE_MARKET,  # Ordem de Mercado
                    quantity=quantity,
                )

                self.actual_trade_position = False  # Define posição como vendida
                createLogOrder(order_sell)  # Cria um log
                print(f"\nOrdem de VENDA a mercado enviada com sucesso:")
                preco_venda = float(order_sell.get("fills", [{}])[0].get("price", 0)) if order_sell.get("fills") else 0
                notificar_venda(self.operation_code, float(order_sell.get("executedQty", 0)), preco_venda, self.last_buy_price, "MARKET")
                return order_sell  # Retorna a ordem

            else:  # Se a posição já está vendida
                logging.warning("Erro ao vender: Posição já vendida.")
                print("\nErro ao vender: Posição já vendida.")
                return False

        except Exception as e:
            logging.error(f"Erro ao executar ordem de venda a mercado: {e}")
            print(f"\nErro ao executar ordem de venda a mercado: {e}")
            return False

    # Venda por um preço mínimo (Ordem Limitada)
    # [NOVA] Define o valor usando RSI e Volume Médio
    def sellLimitedOrder(
        self,
        price=0,
    ):
        if self.paper_trading:
            current_price = float(self.stock_data["close_price"].iloc[-1])
            qty = self.last_stock_account_balance
            self.paper_last_sell_price = current_price
            self.actual_trade_position = False
            fake = self._fake_order("SELL", qty, current_price)
            print(f"\n[PAPER] VENDA limitada simulada: {qty} @ {current_price:.4f}")
            createLogOrder(fake)
            paper_log_sell(self.operation_code, current_price, float(qty))
            notificar_venda(self.operation_code, float(qty), current_price, self.paper_last_buy_price or current_price, "LIMIT [PAPER]")
            return fake
        if not self._real_order_allowed("SELL"):
            return False
        if not getattr(self, "_force_sell_bypass_stale", False):
            if self._block_if_stale("VENDA LIMIT"):
                return False
        close_price = self.stock_data["close_price"].iloc[-1]
        volume = self.stock_data["volume"].iloc[-1]  # Volume atual do mercado
        avg_volume = self.stock_data["volume"].rolling(window=20).mean().iloc[-1]  # Média de volume
        rsi = Indicators.getRSI(series=self.stock_data["close_price"])

        if price == 0:
            if rsi > 70:  # Mercado sobrecomprado
                limit_price = close_price + (0.002 * close_price)  # Tenta vender um pouco acima
            elif volume < avg_volume:  # Volume baixo (mercado lateral)
                limit_price = close_price - (0.002 * close_price)  # Ajuste pequeno abaixo
            else:  # Volume alto (mercado volátil)
                limit_price = close_price - (0.005 * close_price)  # Ajuste maior abaixo (caso caia muito rápido)

            # Garantir que o preço limite seja maior que o mínimo aceitável
            # limit_price = max(limit_price, self.getMinimumPriceToSell())
            if limit_price < (self.last_buy_price * (1 - self.acceptable_loss_percentage)):
                print(f"\nAjuste de venda aceitável ({self.acceptable_loss_percentage*100}%):")
                print(f" - De: {limit_price:.4f}")
                # limit_price = (self.last_buy_price*(1-self.acceptable_loss_percentage))
                limit_price = self.getMinimumPriceToSell()
                print(f" - Para: {limit_price}")
        else:
            limit_price = price

        # Ajustar o preço limite para o tickSize permitido
        limit_price = self.adjust_to_step(
            limit_price,
            self.tick_size,
            as_string=True,
        )

        # Ajustar a quantidade para o stepSize permitido
        quantity = self.adjust_to_step(
            self.last_stock_account_balance,
            self.step_size,
            as_string=True,
        )

        # Log de informações
        print(f"\nEnviando ordem limitada de VENDA para {self.operation_code}:")
        print(f" - RSI: {rsi}")
        print(f" - Quantidade: {quantity}")
        print(f" - Close Price: {close_price}")
        print(f" - Preço Limite: {limit_price}")

        # Enviar ordem limitada de VENDA
        try:
            # Por algum motivo, fazer direto por aqui resolveu um bug de mudança de preço
            # Depois vou testar novamente.
            order_sell = self.client_binance.create_order(
                symbol=self.operation_code,
                side=SIDE_SELL,  # Venda
                type=ORDER_TYPE_LIMIT,  # Ordem Limitada
                timeInForce="GTC",  # Good 'Til Canceled (Ordem válida até ser cancelada)
                quantity=quantity,
                price=limit_price,
            )

            executed_qty = float(order_sell.get("executedQty", 0)) if order_sell else 0.0
            # Posicao so muda quando ordem foi totalmente executada.
            # Parcial/PENDING: continua "comprado" e relog na proxima execucao.
            if order_sell and order_sell.get("status") == "FILLED" and executed_qty >= float(quantity):
                self.actual_trade_position = False
            print(f"\nOrdem VENDA limitada enviada com sucesso:")
            # print(order_sell)
            if order_sell is not None:
                createLogOrder(order_sell)  # Cria um log
                if executed_qty > 0:
                    preco_exec = float(order_sell.get("fills", [{}])[0].get("price", 0)) if order_sell.get("fills") else float(order_sell.get("price", 0))
                    notificar_venda(self.operation_code, executed_qty, preco_exec, self.last_buy_price, "LIMIT")
            return order_sell  # Retorna a ordem enviada
        except Exception as e:
            logging.error(f"Erro ao enviar ordem limitada de VENDA: {e}")
            print(f"\nErro ao enviar ordem limitada de VENDA: {e}")
            return False

    # --------------------------------------------------------------
    # ORDENS E SUAS ATUALIZAÇÕES

    # Verifica as ordens ativas do ativo atual configurado
    def getOpenOrders(self):
        if self.live_data_only or self.paper_trading:
            return []
        open_orders = self.client_binance.get_open_orders(symbol=self.operation_code)

        return open_orders

    # Cancela uma ordem a partir do seu ID
    def cancelOrderById(
        self,
        order_id,
    ):
        self.client_binance.cancel_order(
            symbol=self.operation_code,
            orderId=order_id,
        )

    # Cancela todas ordens abertas. Retorna True se TODAS canceladas, False se alguma falhou.
    def cancelAllOrders(self) -> bool:
        if not self.open_orders:
            return True
        all_ok = True
        for order in self.open_orders:
            try:
                self.client_binance.cancel_order(
                    symbol=self.operation_code,
                    orderId=order["orderId"],
                )
                print(f"❌ Ordem {order['orderId']} cancelada.", flush=True)
            except Exception as e:
                # Se a ordem ja foi FILLED, Binance retorna -2011. Tratar como nao-falha.
                err_str = str(e)
                if "-2011" in err_str or "Unknown order" in err_str:
                    print(f"⚠ Ordem {order['orderId']} ja nao existia (provavelmente FILLED).", flush=True)
                else:
                    print(f"Erro ao cancelar ordem {order['orderId']}: {e}", flush=True)
                    logging.warning(f"cancelOrder falhou {self.operation_code}/{order.get('orderId')}: {e}")
                    all_ok = False
        return all_ok

    # Verifica se há alguma ordem de COMPRA aberta
    # Se a ordem foi parcialmente executada, ele salva o valor
    # executado na variável self.partial_quantity_discount, para que
    # este valor seja descontado nas execuções seguintes.
    # Se foi parcialmente executado, ela também salva o valor que foi executado
    # na variável self.last_buy_price
    def hasOpenBuyOrder(self):
        """
        Verifica se há uma ordem de compra aberta para o ativo configurado.
        Se houver:
            - Salva a quantidade já executada em self.partial_quantity_discount.
            - Salva o maior preço parcialmente executado em self.last_buy_price.
        """
        # Inicializa as variáveis de desconto e maior preço como 0
        self.partial_quantity_discount = 0.0
        if self.live_data_only or self.paper_trading:
            return False
        try:

            # Obtém todas as ordens abertas para o par
            open_orders = self.client_binance.get_open_orders(symbol=self.operation_code)

            # Filtra as ordens de compra (SIDE_BUY)
            buy_orders = [order for order in open_orders if order["side"] == "BUY"]

            if buy_orders:
                self.last_buy_price = 0.0

                print(f"\nOrdens de compra abertas para {self.operation_code}:")
                for order in buy_orders:
                    executed_qty = float(order["executedQty"])  # Quantidade já executada
                    price = float(order["price"])  # Preço da ordem

                    print(
                        f" - ID da Ordem: {order['orderId']}, Preço: {price}, Qnt.: {order['origQty']}, Qnt. Executada: {executed_qty}"
                    )

                    # Atualiza a quantidade parcial executada
                    self.partial_quantity_discount += executed_qty

                    # Atualiza o maior preço parcialmente executado
                    if executed_qty > 0 and price > self.last_buy_price:
                        self.last_buy_price = price

                print(f" - Quantidade parcial executada no total: {self.partial_quantity_discount}")
                print(f" - Maior preço parcialmente executado: {self.last_buy_price}")
                return True
            else:
                print(f"\n - Não há ordens de compra abertas para {self.operation_code}.")
                return False

        except Exception as e:
            print(f"Erro ao verificar ordens abertas para {self.operation_code}: {e}")
            return False

    # Verifica se há uma ordem de VENDA aberta para o ativo configurado.
    # Se houver, salva a quantidade já executada na variável self.partial_quantity_discount.
    def hasOpenSellOrder(self):
        # Inicializa a variável de desconto como 0
        self.partial_quantity_discount = 0.0
        if self.live_data_only or self.paper_trading:
            return False
        try:

            # Obtém todas as ordens abertas para o par
            open_orders = self.client_binance.get_open_orders(symbol=self.operation_code)

            # Filtra as ordens de venda (SIDE_SELL)
            sell_orders = [order for order in open_orders if order["side"] == "SELL"]

            if sell_orders:
                print(f"\nOrdens de venda abertas para {self.operation_code}:")
                for order in sell_orders:
                    executed_qty = float(order["executedQty"])  # Quantidade já executada
                    print(
                        f" - ID da Ordem: {order['orderId']}, Preço: {order['price']}, Qnt.: {order['origQty']}, Qnt. Executada: {executed_qty}"
                    )

                    # Atualiza a quantidade parcial executada
                    self.partial_quantity_discount += executed_qty

                print(f" - Quantidade parcial executada no total: {self.partial_quantity_discount}")
                return True
            else:
                print(f" - Não há ordens de venda abertas para {self.operation_code}.")
                return False

        except Exception as e:
            print(f"Erro ao verificar ordens abertas para {self.operation_code}: {e}")
            return False

    # --------------------------------------------------------------
    # MÉTODOS TRAILING STOP
    # Salva o estado do trailing stop no banco de dados

    def save_trailing_state(self):
        Trailing = Query()
        # Lock inter-processo (filelock) + lock intra-processo + atomic flush.
        from src.modules.FileLock import file_lock
        lock_path = DB_TRAILING + ".lock"
        payload = {
            "operation_code": self.operation_code,
            "max_price_since_buy": self.max_price_since_buy,
            "trailing_stop_price": self.trailing_stop_price,
            "last_buy_price_for_trailing": getattr(self, "last_buy_price_for_trailing", 0.0),
            "trailing_is_running": self.trailing_is_running,  # Estado real do trailing
            "last_buy_time": self.last_buy_time.isoformat() if self.last_buy_time is not None else None,
        }
        with file_lock(lock_path, timeout=10.0), _TRAILING_DB_LOCK:
            try:
                self.trailing_table.upsert(payload, Trailing.operation_code == self.operation_code)
            except ValueError:
                # "Document with ID N already exists": cache do TinyDB dessincronizou
                # com o arquivo (outro processo/thread escreveu). Limpa cache, remove o
                # registro antigo deste operation_code e re-insere. Idempotente.
                try:
                    self.trailing_table.clear_cache()
                except Exception:
                    pass
                try:
                    self.trailing_table.remove(Trailing.operation_code == self.operation_code)
                except Exception:
                    pass
                self.trailing_table.insert(payload)
            # fsync no arquivo TinyDB para reduzir janela de perda em kill -9.
            try:
                self.db.storage.flush()  # type: ignore[attr-defined]
            except Exception:
                pass

    def load_trailing_state(self):
        Trailing = Query()
        from src.modules.FileLock import file_lock
        lock_path = DB_TRAILING + ".lock"
        # Inter-processo + intra-processo: garante leitura consistente
        # quando outro bot acabou de gravar (reload, restart, etc).
        with file_lock(lock_path, timeout=10.0), _TRAILING_DB_LOCK:
            record = self.trailing_table.get(Trailing.operation_code == self.operation_code)
        if record:
            self.max_price_since_buy = record.get("max_price_since_buy", 0.0)
            self.trailing_stop_price = record.get("trailing_stop_price", 0.0)
            self.last_buy_price_for_trailing = record.get("last_buy_price_for_trailing", 0.0)
            self.trailing_is_running = record.get("trailing_is_running", False)  # Carrega estado real
            # Carrega last_buy_time se existir
            last_buy_time_str = record.get("last_buy_time", None)
            if last_buy_time_str:
                try:
                    self.last_buy_time = pd.Timestamp(last_buy_time_str).tz_localize("UTC") if last_buy_time_str else None
                except:
                    self.last_buy_time = None
            else:
                self.last_buy_time = None
        else:
            self.max_price_since_buy = 0.0
            self.trailing_stop_price = 0.0
            self.last_buy_price_for_trailing = 0.0
            self.trailing_is_running = False  # Estado inicial: não está rodando
            self.last_buy_time = None

    def reset_trailing_state(self):
        Trailing = Query()
        from src.modules.FileLock import file_lock
        lock_path = DB_TRAILING + ".lock"
        with file_lock(lock_path, timeout=10.0), _TRAILING_DB_LOCK:
            self.trailing_table.remove(Trailing.operation_code == self.operation_code)
        self.max_price_since_buy = 0.0
        self.trailing_stop_price = 0.0
        self.last_buy_price_for_trailing = 0.0
        self.trailing_is_running = False  # Reset do estado
        self.last_buy_time = None
        # Reset do trailing USD-based.
        self.trail_usd_active = False
        self.peak_pnl_usd = 0.0

    # --------------------------------------------------------------
    # ESTRATÉGIAS DE DECISÃO

    # Função que executa estratégias implementadas e retorna a decisão final
    def getFinalDecisionStrategy(self):
        # Se hub disponivel, monta o dict {tf: df} de TFs declarados pra MultiStrategy
        # poder despachar cada estrategia no TF nativo (sem resample).
        tf_data = None
        if self.market_data_hub is not None:
            try:
                tfs_wanted = self.strategy_timeframes or list(
                    self.market_data_hub.get_dataframes(self.operation_code).keys()
                )
                tf_data = {}
                for tf in tfs_wanted:
                    df = self.market_data_hub.get_dataframe(
                        self.operation_code, tf, include_live=True,
                    )
                    if df is None or df.empty:
                        continue
                    # Normaliza pro schema do bot
                    cols = ["close_price", "open_time", "open_price", "high_price", "low_price", "volume"]
                    tf_data[tf] = df[cols].copy()
                if not tf_data:
                    tf_data = None
            except Exception as exc:
                logging.warning(f"[HUB] tf_data falhou {self.operation_code}: {exc}")
                tf_data = None

        final_decision = StrategyRunner.execute(
            self,
            stock_data=self.stock_data,
            main_strategy=self.main_strategy,
            main_strategy_args=self.main_strategy_args,
            fallback_strategy=self.fallback_strategy,
            fallback_strategy_args=self.fallback_strategy_args,
            tf_data=tf_data,
        )

        return final_decision

    # Define o valor mínimo para vender, baseado no acceptable_loss_percentage
    def getMinimumPriceToSell(self):
        return self.last_buy_price * (1 - self.acceptable_loss_percentage)

    def _atr_pct(self, period: int = 14) -> float | None:
        """ATR percentual sobre os ultimos `period` candles. Retorna None se nao houver dados."""
        try:
            df = self.stock_data
            if df is None or len(df) < period + 1:
                return None
            high = df["high_price"]
            low = df["low_price"]
            prev_close = df["close_price"].shift(1)
            tr1 = high - low
            tr2 = (high - prev_close).abs()
            tr3 = (low - prev_close).abs()
            tr = tr1.combine(tr2, max).combine(tr3, max)
            atr = tr.rolling(period, min_periods=period).mean().iloc[-1]
            close = float(df["close_price"].iloc[-1])
            if close <= 0 or pd.isna(atr):
                return None
            return float(atr) / close * 100.0
        except Exception:
            return None

    def getDynamicStopPct(self) -> float:
        """Stop em % do preco baseado em ATR.

        - Multiplicador padrao: ATR_STOP_MULT (env, default 2.0).
        - Pisos/tetos: ATR_STOP_FLOOR_PCT (default 0.5%) e ATR_STOP_CAP_PCT (default 4.0%).
        - Se ATR nao puder ser calculado, cai para `self.stop_loss_percentage` (config estatica).
        """
        try:
            mult = float(os.getenv("ATR_STOP_MULT", 2.0))
            floor_pct = float(os.getenv("ATR_STOP_FLOOR_PCT", 0.5))
            cap_pct = float(os.getenv("ATR_STOP_CAP_PCT", 4.0))
        except (TypeError, ValueError):
            mult, floor_pct, cap_pct = 2.0, 0.5, 4.0

        atr_pct = self._atr_pct(period=14)
        if atr_pct is None:
            return float(self.stop_loss_percentage)
        dyn_pct = max(min(atr_pct * mult, cap_pct), floor_pct) / 100.0
        return dyn_pct

    # Estratégia de venda por "Stop Loss"
    def stopLossTrigger(self):
        close_price = self.stock_data["close_price"].iloc[-1]
        weighted_price = self.stock_data["close_price"].iloc[-2]  # Preço ponderado pelo candle anterior

        # ---- SL em USD (prioritario quando configurado) ----
        if self.stop_loss_usd > 0 and self.actual_trade_position and self.last_buy_price > 0:
            qty = float(self.last_stock_account_balance or 0.0)
            notional = qty * float(close_price)
            pnl_usd = qty * (float(close_price) - float(self.last_buy_price))
            # Buffer pra perda real nao estourar o limite por causa do slippage
            # de saida e da fee de venda. Default 0.25% do notional.
            try:
                slip_buffer_pct = float(os.getenv("SL_USD_SLIP_BUFFER_PCT", 0.25))
            except (TypeError, ValueError):
                slip_buffer_pct = 0.25
            slip_buffer_usd = notional * (slip_buffer_pct / 100.0)
            effective_limit_usd = max(0.0, float(self.stop_loss_usd) - slip_buffer_usd)
            stop_threshold = -effective_limit_usd
            print(f'\n - Preço atual: {close_price}')
            print(f" - PnL USD: ${pnl_usd:+.2f} | SL USD: ${stop_threshold:.2f} "
                  f"(limite ${self.stop_loss_usd:.2f} - buffer ${slip_buffer_usd:.2f})")
            if pnl_usd <= stop_threshold:
                print(f"🔴 Ativando STOP LOSS USD (${pnl_usd:+.2f} <= ${stop_threshold:.2f})...", flush=True)
                notificar_stop_loss(self.operation_code, self.last_stock_account_balance, float(close_price), self.last_buy_price)
                self.cancelAllOrders()
                time.sleep(2)
                self.sellMarketOrder()
                return True
            return False

        # ---- SL em % (modo legado) ----
        # Modo do stop ATR:
        #  - "tight"    : usa o MENOR(estatico, ATR) — sai antes em vol baixa, protege ganhos.
        #  - "loose"    : usa o MAIOR(estatico, ATR) — da espaco em vol alta, evita whipsaw.
        #  - "atr_only" : usa so ATR.
        #  - "off"      : ignora ATR.
        atr_mode = os.getenv("ATR_STOP_MODE", "tight").lower()
        if atr_mode == "off":
            effective_stop_pct = self.stop_loss_percentage
        else:
            atr_stop_pct = self.getDynamicStopPct()
            if atr_mode == "atr_only":
                effective_stop_pct = atr_stop_pct
            elif atr_mode == "loose":
                effective_stop_pct = max(self.stop_loss_percentage, atr_stop_pct)
            else:  # tight (default)
                effective_stop_pct = min(self.stop_loss_percentage, atr_stop_pct) if atr_stop_pct > 0 else self.stop_loss_percentage
        stop_loss_price = self.last_buy_price * (1 - effective_stop_pct)

        print(f'\n - Preço atual: {self.stock_data["close_price"].iloc[-1]}')
        print(f" - Preço mínimo para vender: {self.getMinimumPriceToSell()}")
        print(f" - Stop Loss em: {stop_loss_price:.4f} (-{self.stop_loss_percentage*100:.2f}%)")

        if close_price < stop_loss_price and weighted_price < stop_loss_price and self.actual_trade_position == True:
            print("🔴 Ativando STOP LOSS...", flush=True)
            notificar_stop_loss(self.operation_code, self.last_stock_account_balance, float(close_price), self.last_buy_price)
            self.cancelAllOrders()
            time.sleep(2)
            self.sellMarketOrder()
            return True
        return False

    # Estratégia de venda por "Take Profit"
    def takeProfitTrigger(self):
        """
        Verifica se o preço atual atingiu uma meta de take profit e, se sim,
        realiza uma venda parcial da carteira de acordo com os percentuais definidos.
        Retorna True se a venda for executada, caso contrário, retorna False.
        """

        try:
            # Obtém o preço de fechamento mais recente
            close_price = self.stock_data["close_price"].iloc[-1]

            # Calcula a variação percentual do preço
            price_percentage_variation = self.getPriceChangePercentage(initial_price=self.last_buy_price, close_price=close_price)

            if price_percentage_variation >= 0:
                print(f"\n📈 Variação atual: {price_percentage_variation:.2f}%\n")
            else:
                print(f"\n📉 Variação atual: {price_percentage_variation:.2f}%\n")

            # Verifica se o índice atual está dentro do tamanho da lista de take profit
            if self.take_profit_index < len(self.take_profit_at_percentage):
                tp_percentage = self.take_profit_at_percentage[self.take_profit_index]
                tp_amount = self.take_profit_amount_percentage[self.take_profit_index]

                print(f" - Próxima meta Take Profit: {tp_percentage}% (Venda de: {tp_amount}%)\n")

                # Condição para ativação do take profit
                if (
                    self.actual_trade_position  # Só executa se estiver comprado
                    and tp_percentage > 0  # Apenas se o TP for maior que 0
                    and round(price_percentage_variation, 2) >= round(tp_percentage, 2)  # Se atingiu a meta de lucro
                ):
                    # Define a quantidade a ser vendida proporcionalmente
                    quantity_to_sell = self.last_stock_account_balance * (tp_amount / 100)

                    # Verifica se há uma quantidade válida para vender
                    if quantity_to_sell > 0:
                        log = (
                            f"🎯 Meta de Take Profit atingida! ({tp_percentage}% lucro)\n"
                            f" - Vendendo {tp_amount}% da carteira...\n"
                            f" - Preço atual: {close_price:.4f}\n"
                            f" - Quantidade vendida: {quantity_to_sell:.6f} {self.base_stock}"
                        )

                        print(log)
                        logging.info(log)

                        # Tenta executar a venda
                        order_result = self.sellMarketOrder(quantity=quantity_to_sell)

                        # Verifica se a ordem foi executada com sucesso
                        if order_result and "status" in order_result and order_result["status"] == "FILLED":
                            self.take_profit_index += 1
                            preco_exec = float(order_result.get("fills", [{}])[0].get("price", close_price)) if order_result.get("fills") else float(close_price)
                            notificar_take_profit(self.operation_code, quantity_to_sell, preco_exec, self.last_buy_price, self.take_profit_index, tp_percentage)
                            print(f"✅ Take Profit {tp_percentage}% realizado com sucesso! Avançando para a próxima meta.")
                            return True  # 🚀 Retorna True indicando que o take profit foi executado

                        else:
                            print(f"❌ Falha ao executar a ordem de venda. Tentando novamente na próxima rodada.")
                            return False  # Falhou na venda, retorna False

                    else:
                        print("⚠️ Quantidade de venda inválida. Take profit não executado.")
                        return False  # Retorna False pois não conseguiu executar a venda

            else:
                print("ℹ️  Todas as metas de take profit já foram atingidas.")
                return False  # Retorna False se todas as metas já foram atingidas

        except Exception as e:
            logging.error(f"Erro no take profit: {e}")
            print(f"❌ Erro no take profit: {e}")
            return False  # Retorna False se houver erro

    # Estratégia de venda por "Trailing Stop USD"
    # Modo USD: ativa apos +trail_activation_usd; trava floor em +trail_floor_usd;
    # acima do floor, devolve no maximo trail_giveback_pct% do peak.
    def trailingStopUsdTrigger(self):
        try:
            if not self.actual_trade_position:
                # Reset de estado quando posicao foi vendida.
                self.trail_usd_active = False
                self.peak_pnl_usd = 0.0
                return False

            close_price = float(self.stock_data["close_price"].iloc[-1])
            qty = float(self.last_stock_account_balance or 0.0)
            entry = float(self.last_buy_price or 0.0)
            if qty <= 0 or entry <= 0:
                return False

            pnl_usd = qty * (close_price - entry)
            if pnl_usd > self.peak_pnl_usd:
                self.peak_pnl_usd = pnl_usd

            # Ativacao do trailing.
            if (not self.trail_usd_active) and self.peak_pnl_usd >= self.trail_activation_usd:
                self.trail_usd_active = True
                msg = (f"\n[TRAIL$] Ativado em peak ${self.peak_pnl_usd:+.2f} "
                       f"(threshold ${self.trail_activation_usd:+.2f}). "
                       f"Floor minimo ${self.trail_floor_usd:+.2f}, giveback {self.trail_giveback_pct:.0f}%.")
                print(msg)
                logging.info(msg)

            if not self.trail_usd_active:
                # Antes da ativacao quem protege e o stop_loss_usd.
                print(f" - [TRAIL$] aguardando peak ≥ ${self.trail_activation_usd:+.2f} "
                      f"(atual peak ${self.peak_pnl_usd:+.2f}, pnl ${pnl_usd:+.2f})")
                return False

            # Giveback adaptativo: vol baixa (BTC) usa giveback menor, vol alta
            # (DOGE/SOL) usa maior. ATR_TRAIL_GIVEBACK_K (default 5.0) escala
            # ATR% -> giveback%. Clamp em [TRAIL_GIVEBACK_MIN, TRAIL_GIVEBACK_MAX].
            # Ex: ATR=2% e k=5 -> 10% giveback. Sem ATR cai no fixo do tier.
            try:
                k = float(os.getenv("ATR_TRAIL_GIVEBACK_K", 5.0))
                gmin = float(os.getenv("TRAIL_GIVEBACK_MIN_PCT", 15.0))
                gmax = float(os.getenv("TRAIL_GIVEBACK_MAX_PCT", 40.0))
            except (TypeError, ValueError):
                k, gmin, gmax = 5.0, 15.0, 40.0
            atr_pct = self._atr_pct(period=14)
            if atr_pct is not None and atr_pct > 0:
                effective_giveback_pct = max(min(atr_pct * k, gmax), gmin)
            else:
                effective_giveback_pct = self.trail_giveback_pct
            give = effective_giveback_pct / 100.0
            giveback_floor = self.peak_pnl_usd * (1.0 - give)
            # Ladder de protecao escalonada: piso minimo cresce com peak%.
            # Combina com giveback via max(): sempre vence o piso mais protetor.
            try:
                from src.modules.StrategyTiers import compute_ladder_floor_usd
                notional = float(entry) * float(qty)
                ladder_floor_usd, ladder_locked_pct = compute_ladder_floor_usd(self.peak_pnl_usd, notional)
            except Exception:
                ladder_floor_usd, ladder_locked_pct = 0.0, 0.0
            dynamic_floor = max(self.trail_floor_usd, giveback_floor, ladder_floor_usd)
            ladder_msg = f" | ladder {ladder_locked_pct:.0f}% (${ladder_floor_usd:+.2f})" if ladder_locked_pct > 0 else ""
            print(f" - [TRAIL$] peak ${self.peak_pnl_usd:+.2f} | pnl ${pnl_usd:+.2f} | "
                  f"floor ${dynamic_floor:+.2f} | giveback {effective_giveback_pct:.1f}%{ladder_msg}")

            if pnl_usd <= dynamic_floor:
                msg = (f"\n[TRAIL$] Stop atingido: pnl ${pnl_usd:+.2f} <= floor ${dynamic_floor:+.2f} "
                       f"(peak ${self.peak_pnl_usd:+.2f}). Vendendo...")
                print(msg)
                logging.warning(msg)
                self.cancelAllOrders()
                time.sleep(2)
                preco_atual = close_price
                notificar_trailing_stop(self.operation_code, self.last_stock_account_balance, preco_atual, self.last_buy_price)
                self.sellMarketOrder()
                self.trail_usd_active = False
                self.peak_pnl_usd = 0.0
                return True
            return False
        except Exception as e:
            print(f"\n[TRAIL$][ERRO] {e}")
            logging.error(f"[TRAIL$][ERRO] {e}")
            return False

    # Estratégia de venda por "Trailing Stop"
    def trailingStopTrigger(self):
        """
        Ativa ou atualiza o trailing stop enquanto a posição está comprada.
        Se o preço cair e atingir o trailing stop, vende a mercado.
        Utiliza o preço máximo (high) atingido desde a compra.
        """
        try:
            # Modo USD tem prioridade quando configurado.
            if self.trail_activation_usd > 0:
                return self.trailingStopUsdTrigger()

            # Verifica se o usuário quer usar trailing stop
            if not self.trailing_stop_enabled:
                return False

            if not self.actual_trade_position:  # Só faz trailing se estiver comprado
                self.trailing_is_running = False
                self.max_price_since_buy = 0.0
                self.trailing_stop_price = 0.0
                self.last_buy_price_for_trailing = 0.0
                self.last_buy_time = None
                self.save_trailing_state()
                print("[TRAILING] Não está comprado. Trailing stop resetado.")
                logging.info("[TRAILING] Não está comprado. Trailing stop resetado.")
                return False

            close_price = self.stock_data["close_price"].iloc[-1]
            last_buy_price = self.last_buy_price

            # Calcula o high_price desde a última compra
            if self.last_buy_time is not None:
                # Filtra dados desde a última compra
                prices_since_buy = self.stock_data[self.stock_data["open_time"] >= self.last_buy_time]
                if not prices_since_buy.empty:
                    high_since_buy = prices_since_buy["high_price"].max()
                else:
                    # Fallback: usa preço atual se não há dados desde a compra
                    high_since_buy = max(close_price, self.last_buy_price)
            else:
                # Fallback: usa o máximo entre preço atual e preço de compra
                high_since_buy = max(close_price, self.last_buy_price)

            # Inicialização do trailing após compra ou ressincronização
            if (not self.trailing_is_running) or (self.last_buy_price != self.last_buy_price_for_trailing):
                self.max_price_since_buy = high_since_buy
                self.trailing_stop_price = self.adjust_to_step(
                    self.max_price_since_buy * (1 - self.trailing_stop_percent), self.tick_size, as_string=False
                )
                self.trailing_is_running = True  # Agora está rodando
                self.last_buy_price_for_trailing = self.last_buy_price
                self.save_trailing_state()
                msg = f"\n[TRAILING] Iniciado em {self.max_price_since_buy:.4f}. Stop em {self.trailing_stop_price:.4f} (-{self.trailing_stop_percent*100:.2f}%)"
                print(msg)
                logging.info(msg)
                return False

            # Atualiza a máxima desde a compra
            if high_since_buy > self.max_price_since_buy:
                self.max_price_since_buy = high_since_buy
                self.trailing_stop_price = self.adjust_to_step(
                    self.max_price_since_buy * (1 - self.trailing_stop_percent), self.tick_size, as_string=False
                )
                self.save_trailing_state()
                msg = f"\n[TRAILING] Novo topo: {self.max_price_since_buy:.4f}. Novo stop: {self.trailing_stop_price:.4f} (-{self.trailing_stop_percent*100:.2f}%)"
                print(msg)
                logging.info(msg)

            # Atualiza o stop se o percentual mudou, mesmo sem novo topo.
            # Compara com tolerancia de 1 tick para nao gravar TinyDB a cada ciclo
            # quando arredondamento gera diferencas microscopicas.
            new_trailing_stop_price = self.adjust_to_step(
                self.max_price_since_buy * (1 - self.trailing_stop_percent), self.tick_size, as_string=False
            )
            if abs(self.trailing_stop_price - new_trailing_stop_price) > self.tick_size / 2:
                self.trailing_stop_price = new_trailing_stop_price
                self.save_trailing_state()
                msg = f"\n[TRAILING] Percentual alterado. Novo stop: {self.trailing_stop_price:.4f} (-{self.trailing_stop_percent*100:.2f}%)"
                print(msg)
                logging.info(msg)

            # Se cair e atingir o trailing stop, vende
            if close_price <= self.trailing_stop_price:
                msg = f"\n[TRAILING] Stop atingido: {close_price:.4f} <= {self.trailing_stop_price:.4f} (-{self.trailing_stop_percent*100:.2f}%). Vendendo..."
                print(msg)
                logging.warning(msg)
                self.cancelAllOrders()
                time.sleep(2)
                preco_atual = float(self.stock_data["close_price"].iloc[-1]) if self.stock_data is not None else 0
                notificar_trailing_stop(self.operation_code, self.last_stock_account_balance, preco_atual, self.last_buy_price)
                result = self.sellMarketOrder()
                self.trailing_is_running = False  # Para de rodar após venda
                self.last_buy_time = None  # Reset timestamp
                self.save_trailing_state()
                self.reset_trailing_state()  # Limpa estado após a venda total
                logging.info(f"\n[TRAILING] Venda executada: {result}")
                return True
            else:
                msg = f" - Trailing stop em: {self.trailing_stop_price:.4f} (-{self.trailing_stop_percent*100:.2f}%)"
                print(msg)
                logging.info(msg)
                return False  # Não ativou o trailing stop ainda
        except Exception as e:
            print(f"\n[TRAILING][ERRO] {e}")
            logging.error(f"\n[TRAILING][ERRO] {e}")
            return False

    # --------------------------------------------------------------

    # Não usada por enquanto
    def create_order(
        self,
        _symbol,
        _side,
        _type,
        _quantity,
        _timeInForce=None,
        _limit_price=None,
        _stop_price=None,
    ):
        order_buy = TraderOrder.create_order(
            self.client_binance,
            _symbol=_symbol,
            _side=_side,  # Compra
            _type=_type,  # Ordem Limitada
            _timeInForce=_timeInForce,  # Good 'Til Canceled (Ordem válida até ser cancelada)
            _quantity=_quantity,
            _limit_price=_limit_price,
            _stop_price=_stop_price,
        )

        return order_buy

    # --------------------------------------------------------------
    # EXECUTE

    # Função principal e a única que deve ser execuda em loop, quando o
    # robô estiver funcionando normalmente
    def execute(
        self,
    ):
        print("------------------------------------------------", flush=True)
        print(f'🟢 Executado {datetime.now().strftime("(%H:%M:%S) %d-%m-%Y")}\n', flush=True)

        # Reseta caches do ciclo (RSI etc.).
        self._cycle_rsi = None

        # Atualiza todos os dados
        data_ok = self.updateAllData(verbose=True)
        if (
            not data_ok
            or self.stock_data is None
            or getattr(self.stock_data, "empty", True)
            or "close_price" not in self.stock_data
        ):
            print(f"[DATA] Sem candles validos para {self.operation_code}; pulando ciclo.", flush=True)
            logging.warning(f"Sem candles validos para {self.operation_code}; ciclo ignorado")
            self.time_to_sleep = self.time_to_trade
            return

        print("\n-------")
        print("Detalhes:")
        print(f' - Posição atual: {"Comprado" if self.actual_trade_position else "Vendido"}')
        print(f" - Balanço atual: {self.last_stock_account_balance:.4f} ({self.base_stock})")

        # ---------
        # Estratégias sentinelas de saída

        # Kill switch:
        # - GLOBAL (KILL_SWITCH na raiz) -> emergencia: cancela ordens e liquida MARKET
        # - PER-ASSET (src/logs/kill_switch/<ASSET>.json) -> bloqueia novas, MANTEM posicao
        #   ate proximo sinal natural / stop / take profit (sem slippage forcado).
        try:
            from src.modules.PaperBalanceLogger import (
                _global_kill_active,
                _asset_kill_active,
            )
            global_killed, global_reason = _global_kill_active()
            asset_killed, asset_reason, asset_data = _asset_kill_active(self.operation_code)
        except Exception:
            global_killed, global_reason = False, ""
            asset_killed, asset_reason, asset_data = False, "", {}

        if global_killed:
            print(f"\n🔴 KILL SWITCH GLOBAL: {global_reason}")
            logging.warning(f"KILL SWITCH GLOBAL em {self.operation_code}: {global_reason}")
            if self.actual_trade_position:
                print(f"   Forcando liquidacao de {self.operation_code}...")
                try:
                    self.cancelAllOrders()
                    time.sleep(1)
                    self.sellMarketOrder()
                except Exception as e:
                    logging.error(f"Falha ao liquidar via kill switch global: {e}")
            self.time_to_sleep = self.time_to_trade
            return

        if asset_killed:
            mode = (asset_data.get("mode") or "graceful").lower()
            print(f"\n🟡 KILL ASSET ({self.operation_code}, mode={mode}): {asset_reason}")
            logging.warning(f"KILL ASSET {self.operation_code} mode={mode}: {asset_reason}")
            if mode == "market" and self.actual_trade_position:
                # Modo agressivo opcional (configuravel via JSON do kill): liquida MARKET.
                print(f"   mode=market -> liquidando {self.operation_code}...")
                try:
                    self.cancelAllOrders()
                    time.sleep(1)
                    self.sellMarketOrder()
                except Exception as e:
                    logging.error(f"Falha ao liquidar via kill asset: {e}")
                self.time_to_sleep = self.time_to_trade
                return
            # Modo padrao "graceful": NAO mexe na posicao. Bloqueia abertura de
            # novas operacoes no asset abortando antes do strategy decision.
            self.time_to_sleep = self.time_to_trade
            return

        # Paused Bot
        if self.pause_bot_state:
            print("\n🔴 Bot pausado. Aguardando reinício...\n")
            return

        # Stop Loss
        # Se perder mais que o stop loss aceitável, ele sai à mercado, independente.
        if self.stopLossTrigger():
            print("\n🟢 STOP LOSS finalizado.\n")
            if self.pause_after_stop_loss:
                print("\n⏸️  Pausando bot após STOP LOSS.\n")
                self.pause_bot_state = True
            return

        # TRAILING STOP
        if self.actual_trade_position == True and self.trailingStopTrigger():
            print("\n🟢 TRAILING STOP finalizado.\n")
            if self.pause_after_trailing_stop:
                print("\n⏸️  Pausando bot após TRAILING STOP.\n")
                self.pause_bot_state = True
            return

        # Take Profit
        if self.actual_trade_position == True and self.takeProfitTrigger():
            print("\n🟢 TAKE PROFIT finalizado.\n")
            if self.pause_after_take_profit:
                print("\n⏸️  Pausando bot após TAKE PROFIT.\n")
                self.pause_bot_state = True
            return

        # ---------
        # Calcula a melhor estratégia para a decisão final
        self.last_trade_decision = self.getFinalDecisionStrategy()

        # ---------
        # Verifica ordens anteriores abertas
        if self.last_trade_decision == True:  # Se a decisão for COMPRA
            if self.hasOpenBuyOrder():
                if not self.cancelAllOrders():
                    # Falha em cancelar = nao podemos criar ordem nova (risco de duplicar exposicao).
                    print("⚠ Cancelamento parcial: pulando criacao de nova ordem.", flush=True)
                    self.time_to_sleep = self.time_to_trade
                    return
                time.sleep(2)

        if self.last_trade_decision == False:  # Se a decisão for VENDA
            if self.hasOpenSellOrder():
                if not self.cancelAllOrders():
                    print("⚠ Cancelamento parcial: pulando criacao de nova ordem.", flush=True)
                    self.time_to_sleep = self.time_to_trade
                    return
                time.sleep(2)

        # ---------
        print("\n--------------")
        if self.last_trade_decision is True:
            decision_label = "Comprar" if not self.actual_trade_position else "Manter comprado"
        elif self.last_trade_decision is False:
            decision_label = "Vender" if self.actual_trade_position else "Manter vendido"
        else:
            decision_label = "Inconclusiva"
        print(f"🔎 Decisão Final: {decision_label}")

        # ---------
        # Se a posição for vendida (false) e a decisão for de compra (true), compra o ativo
        if self.actual_trade_position == False and self.last_trade_decision == True:
            if not self.riskGuardAllowsBuy():
                print("--------------")
                print("Acao final: Manter vendido por protecao de risco")
                print("--------------")
                self.time_to_sleep = self.time_to_trade
                return
            print(f"🏁 Ação final: Comprar {'a Mercado' if self.execute_market_orders else 'a Limite'}")
            print("--------------")
            print(f"\nCarteira em {self.base_stock} [ANTES]:")
            self.printStock()

            if self.execute_market_orders == True:
                self.buyMarketOrder()
            else:
                self.buyLimitedOrder()

            time.sleep(2)
            self.updateAllData()
            print(f"Carteira em {self.base_stock} [DEPOIS]:")
            self.printStock()
            self.time_to_sleep = self.delay_after_order

        # Se a posição for comprada (true) e a decisão for de venda (false), vende o ativo
        elif self.actual_trade_position == True and self.last_trade_decision == False:
            print(f"🏁 Ação final: Vender {'a Mercado' if self.execute_market_orders else 'a Limite'}")
            print("--------------")
            print(f"\nCarteira em {self.base_stock} [ANTES]:")
            self.printStock()

            if self.execute_market_orders == True:
                self.sellMarketOrder()
            else:
                self.sellLimitedOrder()

            time.sleep(2)
            self.updateAllData()
            print(f"\nCarteira em {self.base_stock} [DEPOIS]:")
            self.printStock()
            self.time_to_sleep = self.delay_after_order

        else:
            print(f'🏁 Ação final: Manter posição ({"Comprado" if self.actual_trade_position else "Vendido"})')
            print("--------------")
            self.time_to_sleep = self.time_to_trade

        print("------------------------------------------------")
