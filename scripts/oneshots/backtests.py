from binance.client import Client
from src.modules.BinanceTraderBot import BinanceTraderBot
from src.backtesting import backtestRunner

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
else:
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.detach())

sys.stdout.write("\ufeff")


import src.strategies.chilo_rsi as chilo_mod

BASE_STOCK = "JUP"
QUOTE_STOCK = "USDT"
INITIAL_BALANCE = 1000
CANDLE_PERIOD = Client.KLINE_INTERVAL_1HOUR
CLANDES_RODADOS = 24 * 90

devTrader = BinanceTraderBot(
    base_stock=BASE_STOCK,
    quote_stock=QUOTE_STOCK,
    traded_quantity=0,
    traded_percentage=100,
    candle_period=CANDLE_PERIOD,
)
devTrader.updateAllData()


print(f"\n{BASE_STOCK} - CHILO RSI - {str(CANDLE_PERIOD)}")
chilo_mod.USE_IN_MEMORY_ONLY = True
backtestRunner(
    stock_data=devTrader.stock_data,
    strategy_function=chilo_mod.ChiloRSIStrategy,
    strategy_instance=devTrader,
    periods=CLANDES_RODADOS,
    initial_balance=INITIAL_BALANCE,
    backtest_verbose=False,
)
chilo_mod.USE_IN_MEMORY_ONLY = False
print("\n\n")
