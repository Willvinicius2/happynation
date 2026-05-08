import pandas as pd
import numpy as np
import os
import sys
from datetime import datetime

# Configuração de caminhos para importações
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Adicionar diretórios ao sys.path
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, SRC_DIR)

def getDonchianChannelTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    exit_period: int = 5,
    verbose: bool = True
):
    """
    Estratégia baseada em Donchian Channel.
    
    O Donchian Channel consiste em um canal superior, médio e inferior.
    - Canal superior: O preço mais alto do período
    - Canal inferior: O preço mais baixo do período
    - Canal médio: Média dos canais superior e inferior
    
    Parâmetros:
    - period: Período para cálculo do canal Donchian
    - exit_period: Período menor para o canal de saída
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low', 'close']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular o Canal Donchian
    stock_data['upper_band'] = stock_data['high'].rolling(window=period).max()
    stock_data['lower_band'] = stock_data['low'].rolling(window=period).min()
    stock_data['middle_band'] = (stock_data['upper_band'] + stock_data['lower_band']) / 2
    
    # Calcular canal de saída (para reduzir o risco de perda)
    stock_data['exit_upper'] = stock_data['high'].rolling(window=exit_period).max()
    stock_data['exit_lower'] = stock_data['low'].rolling(window=exit_period).min()
    
    # Extrair valores atuais
    current_close = stock_data['close'].iloc[-1]
    current_upper = stock_data['upper_band'].iloc[-1]
    current_lower = stock_data['lower_band'].iloc[-1]
    current_middle = stock_data['middle_band'].iloc[-1]
    
    current_exit_upper = stock_data['exit_upper'].iloc[-1]
    current_exit_lower = stock_data['exit_lower'].iloc[-1]
    
    # Verificar valores anteriores para detectar breakouts
    prev_close = stock_data['close'].iloc[-2] if len(stock_data) > 1 else current_close
    prev_upper = stock_data['upper_band'].iloc[-2] if len(stock_data) > 1 else current_upper
    prev_lower = stock_data['lower_band'].iloc[-2] if len(stock_data) > 1 else current_lower
    
    # Detectar breakouts
    breakout_up = (current_close > current_upper) and (prev_close <= prev_upper)
    breakout_down = (current_close < current_lower) and (prev_close >= prev_lower)
    
    # Detectar penetração no canal médio
    touch_middle_from_above = (current_close <= current_middle) and (prev_close > prev_upper)
    touch_middle_from_below = (current_close >= current_middle) and (prev_close < prev_lower)
    
    # Variáveis para o trailing stop
    trailing_stop_triggered = False
    
    # Gerar sinais de compra e venda
    buy_signal = breakout_up
    sell_signal = breakout_down
    
    # Decisão final
    trade_decision = None
    
    # Usar os breakouts para decisões
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    # Para uma estratégia trailing stop, considere:
    if current_close < current_exit_lower and not buy_signal:
        trade_decision = False  # Vender (trailing stop loss)
        trailing_stop_triggered = True
    elif current_close > current_exit_upper and not sell_signal:
        trade_decision = True  # Comprar (trailing stop gain)
        trailing_stop_triggered = True
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Donchian Channel")
        print(f" | Período: {period}")
        print(f" | Período de Saída: {exit_period}")
        print(f" | Preço Atual: {current_close:.2f}")
        print(f" | Banda Superior: {current_upper:.2f}")
        print(f" | Banda Média: {current_middle:.2f}")
        print(f" | Banda Inferior: {current_lower:.2f}")
        print(f" | Breakout Superior: {breakout_up}")
        print(f" | Breakout Inferior: {breakout_down}")
        print(f" | Toque na Banda Média (De Cima): {touch_middle_from_above}")
        print(f" | Toque na Banda Média (De Baixo): {touch_middle_from_below}")
        print(f" | Trailing Stop Acionado: {trailing_stop_triggered}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision