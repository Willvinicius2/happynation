import inspect
import numpy as np
import pandas as pd
from tinydb import TinyDB


def backtestRunner(
    stock_data: pd.DataFrame,
    strategy_function,
    strategy_instance=None,
    periods=900,
    initial_balance=1000,
    backtest_verbose=False,
    **strategy_kwargs,
):
    """
    Executa um backtest de qualquer estratégia (com ou sem 'self').
    - Se backtest_verbose=True, imprime detalhes de cada compra/venda e roda a função com verbose=True
      apenas naquele momento para exibir os prints da estratégia, sem alterar o estado definitivo.
    - Inclui a data do candle no qual BUY/SELL é sinalizado.

    Parâmetros:
      stock_data: DataFrame com preços históricos (deve conter coluna "open_time" do tipo datetime)
      strategy_function: função da estratégia (pode exigir 'self' ou não)
      strategy_instance: instância (ex: BinanceTraderBot) se a estratégia declara 'self'
      periods: número de períodos a usar (últimos N candles)
      initial_balance: capital inicial para o backtest
      backtest_verbose: se True, exibe prints ao acontecer BUY/SELL e invoca a estratégia em modo verbose
      strategy_kwargs: demais argumentos passados à estratégia (como fast_window, slow_window, etc.)
    """
    # 1) Prepara os dados (descarta NaN, mantém pelo menos `periods` candles + buffer)
    min_required_periods = strategy_kwargs.get("slow_window", 40) + 20
    stock_data = stock_data[-max(periods, min_required_periods) :].copy().reset_index(drop=True)
    stock_data.dropna(inplace=True)

    balance = initial_balance
    position = 0  # 0 = fora, 1 = comprado
    entry_price = 0
    last_signal = None
    trades = 0

    print(f"📊 Iniciando backtest da estratégia: {strategy_function.__name__}")
    print(f"🔹 Balanço inicial: ${balance:.2f}\n")

    # 2) Detecta se a função declara 'self'
    sig = inspect.signature(strategy_function)
    expects_self = "self" in sig.parameters

    # Se espera self mas não recebeu instância, erro
    if expects_self and strategy_instance is None:
        raise TypeError(
            f"A estratégia '{strategy_function.__name__}' declara 'self' mas você não forneceu `strategy_instance`.\n"
            f"Use: backtestRunner(..., strategy_function={strategy_function.__name__}, strategy_instance=<seu_objeto>, ...)"
        )

    # 3) Loop de backtest “tick a tick”
    for i in range(1, len(stock_data)):
        current_data = stock_data.iloc[: i + 1]
        candle_time = stock_data.iloc[i]["open_time"]

        # 3.1) Chama a estratégia em modo não-verbose (para decisão)
        if expects_self:
            signal = strategy_function(strategy_instance, current_data, verbose=False, **strategy_kwargs)
        else:
            signal = strategy_function(current_data, verbose=False, **strategy_kwargs)

        if signal is None:
            continue

        close_price = stock_data.iloc[i]["close_price"]

        # 3.2) Simula BUY
        if signal and position == 0 and last_signal != "buy":
            # PRIMEIRO: executa o verbose (se necessário), antes de modificar estado
            if backtest_verbose:
                print(f"🟢 [{i:04d}] BUY sinalizado em {close_price:.4f} | Data: {candle_time}")

                if expects_self:
                    # salva flags temporárias para não corromper o estado real
                    try:
                        state = strategy_instance.get_asset_state(strategy_instance.operation_code)
                        saved_lock = state["rsi_lock_50"]
                        saved_pos70 = state["position_rsi_70"]
                    except Exception:
                        saved_lock = None
                        saved_pos70 = None

                    # Roda em verbose, mas com estado ANTERIOR ao BUY
                    strategy_function(strategy_instance, current_data, verbose=True, **strategy_kwargs)

                    # Restaura flags no TinyDB (ou em memória)
                    if saved_lock is not None:
                        strategy_instance.update_asset_state(strategy_instance.operation_code, rsi_lock_50=saved_lock)
                    if saved_pos70 is not None:
                        strategy_instance.update_asset_state(strategy_instance.operation_code, position_rsi_70=saved_pos70)

                    # Garante que actual_trade_position ainda está como antes (False)
                    strategy_instance.actual_trade_position = False
                else:
                    strategy_function(current_data, verbose=True, **strategy_kwargs)

            # AGORA simula a compra, atualiza estado e registra trade
            position = 1
            entry_price = close_price
            last_signal = "buy"
            trades += 1
            if strategy_instance:
                strategy_instance.actual_trade_position = True

        # 3.3) Simula SELL
        elif not signal and position == 1 and last_signal != "sell":
            # PRIMEIRO: executa o verbose (se necessário), antes de modificar estado
            if backtest_verbose:
                print(f"🔴 [{i:04d}] SELL sinalizado em {close_price:.4f} | Data: {candle_time}", end="")
                profit = ((close_price - entry_price) / entry_price) * balance
                print(f" | Lucro parcial: {profit:.2f}")

                if expects_self:
                    try:
                        state = strategy_instance.get_asset_state(strategy_instance.operation_code)
                        saved_lock = state["rsi_lock_50"]
                        saved_pos70 = state["position_rsi_70"]
                    except Exception:
                        saved_lock = None
                        saved_pos70 = None

                    # Roda em verbose, com estado ANTERIOR ao SELL
                    strategy_function(strategy_instance, current_data, verbose=True, **strategy_kwargs)

                    # Restaura flags no TinyDB (ou em memória)
                    if saved_lock is not None:
                        strategy_instance.update_asset_state(strategy_instance.operation_code, rsi_lock_50=saved_lock)
                    if saved_pos70 is not None:
                        strategy_instance.update_asset_state(strategy_instance.operation_code, position_rsi_70=saved_pos70)

                    # Garante que actual_trade_position ainda está como antes (True)
                    strategy_instance.actual_trade_position = True
                else:
                    strategy_function(current_data, verbose=True, **strategy_kwargs)

            # AGORA simula a venda, atualiza estado e registra trade
            position = 0
            profit = ((close_price - entry_price) / entry_price) * balance
            balance += profit
            last_signal = "sell"
            trades += 1
            if strategy_instance:
                strategy_instance.actual_trade_position = False

        # 3.4) Se nem comprou nem vendeu, mantém posição atual

    # 4) Fecha posição final se necessário
    if position == 1:
        final_price = stock_data.iloc[-1]["close_price"]
        profit = ((final_price - entry_price) / entry_price) * balance
        balance += profit
        if backtest_verbose:
            last_time = stock_data.iloc[-1]["open_time"]
            print(f"\n🔔 Fechamento final da posição em {final_price:.4f} | Data: {last_time} | Lucro final: {profit:.2f}")

    profit_percentage = ((balance - initial_balance) / initial_balance) * 100

    print(f"🔹 Balanço final: ${balance:.2f}")
    print(f"📈 Lucro/prejuízo percentual: {profit_percentage:.2f}%")
    print(f"📊 Total de operações realizadas: {trades}")

    return profit_percentage
