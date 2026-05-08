"""
Script para gerar relatório completo de backtests
Testa múltiplas estratégias e gera um sumário formatado
"""
import sys
from binance.client import Client
from src.modules.BinanceTraderBot import BinanceTraderBot
from src.backtesting import backtestRunner

# Configuração de encoding
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Importa estratégias
from src.strategies.chilo_rsi import ChiloRSIStrategy
from src.strategies.moving_average import getMovingAverageTradeStrategy
from src.strategies.rsi_strategy import getRsiTradeStrategy
from src.strategies.vortex_strategy import getVortexTradeStrategy
from src.strategies.high_win_rate_scalp import HighWinRateScalpStrategy

BASE_STOCK = "JUP"
QUOTE_STOCK = "USDT"
INITIAL_BALANCE = 1000
CANDLE_PERIOD = Client.KLINE_INTERVAL_1HOUR
CLANDES_RODADOS = 24 * 90  # 90 dias

# Setup
trader = BinanceTraderBot(
    base_stock=BASE_STOCK,
    quote_stock=QUOTE_STOCK,
    traded_quantity=0,
    traded_percentage=100,
    candle_period=CANDLE_PERIOD,
)
trader.updateAllData()

# Lista de estratégias para testar
strategies = [
    {
        "name": "ChiloRSIStrategy",
        "function": ChiloRSIStrategy,
        "args": {"rsi_reentry": 65.0},
        "instance": trader
    },
    {
        "name": "Moving Average",
        "function": getMovingAverageTradeStrategy,
        "args": {"fast_window": 7, "slow_window": 40},
        "instance": None
    },
    {
        "name": "RSI Strategy",
        "function": getRsiTradeStrategy,
        "args": {"low": 30, "high": 70},
        "instance": None
    },
    {
        "name": "Vortex Strategy",
        "function": getVortexTradeStrategy,
        "args": {},
        "instance": None
    },
    {
        "name": "HighWinRateScalp",
        "function": HighWinRateScalpStrategy,
        "args": {
            "take_profit_pct": 0.5,
            "stop_loss_pct": 3.0,
            "rsi_min": 38.0,
            "rsi_max": 62.0,
            "pullback_pct": 0.2,
            "max_hold_bars": 72,
        },
        "instance": trader
    },
]

print("=" * 70)
print(f"BACKTEST REPORT - {BASE_STOCK}/{QUOTE_STOCK}")
print(f"Período: {CLANDES_RODADOS} candles (1h)")
print(f"Capital inicial: ${INITIAL_BALANCE}")
print("=" * 70)

results = []

for strategy in strategies:
    print(f"\n▶ Testando: {strategy['name']}")
    print("-" * 70)

    try:
        if strategy['instance']:
            strategy['instance'].operation_code = BASE_STOCK

        profit_pct = backtestRunner(
            stock_data=trader.stock_data,
            strategy_function=strategy['function'],
            strategy_instance=strategy['instance'],
            periods=CLANDES_RODADOS,
            initial_balance=INITIAL_BALANCE,
            backtest_verbose=False,
            **strategy['args']
        )

        results.append({
            'name': strategy['name'],
            'profit': profit_pct,
            'status': 'OK'
        })

    except Exception as e:
        print(f"❌ Erro: {str(e)}")
        results.append({
            'name': strategy['name'],
            'profit': 0,
            'status': f'ERRO: {str(e)[:50]}'
        })

# Relatório final
print("\n" + "=" * 70)
print("RESUMO DOS RESULTADOS")
print("=" * 70)
print(f"{'Estratégia':<25} {'Retorno':<15} {'Status':<30}")
print("-" * 70)

for result in results:
    profit_str = f"{result['profit']:+.2f}%" if result['status'] == 'OK' else "N/A"
    status_str = result['status']
    print(f"{result['name']:<25} {profit_str:<15} {status_str:<30}")

print("=" * 70)

# Melhor estratégia
best = max([r for r in results if r['status'] == 'OK'], key=lambda x: x['profit'], default=None)
if best:
    print(f"\n🏆 Melhor estratégia: {best['name']} ({best['profit']:+.2f}%)")
else:
    print("\n⚠️ Nenhuma estratégia teve sucesso no teste")

print("\nRelatório gerado com sucesso!")
