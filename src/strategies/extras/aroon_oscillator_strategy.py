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

def getAroonOscillatorTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    overbought: int = 50,
    oversold: int = -50,
    zero_cross_signal: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em Aroon Oscillator.
    
    Parâmetros:
    - period: Período para cálculo do indicador
    - overbought: Nível de sobrecompra
    - oversold: Nível de sobrevenda
    - zero_cross_signal: Usar cruzamento do zero como sinal adicional
    """
    stock_data = stock_data.copy()
    
    # Verificar se as colunas necessárias existem
    if 'high' not in stock_data.columns or 'low' not in stock_data.columns:
        raise ValueError("Colunas 'high' e 'low' são necessárias para o cálculo do Aroon Oscillator")
    
    # Função para calcular Aroon
    def calculate_aroon(data, period):
        high_idx = data['high'].rolling(window=period).apply(lambda x: x.argmax(), raw=True)
        low_idx = data['low'].rolling(window=period).apply(lambda x: x.argmin(), raw=True)
        
        aroon_up = 100 * (period - high_idx) / period
        aroon_down = 100 * (period - low_idx) / period
        
        return aroon_up, aroon_down
    
    # Calcular Aroon Up e Aroon Down
    stock_data['aroon_up'], stock_data['aroon_down'] = calculate_aroon(stock_data, period)
    
    # Calcular Aroon Oscillator
    stock_data['aroon_oscillator'] = stock_data['aroon_up'] - stock_data['aroon_down']
    
    # Detectar cruzamento do zero
    stock_data['zero_cross'] = np.where(
        (stock_data['aroon_oscillator'].shift(1) <= 0) & (stock_data['aroon_oscillator'] > 0), 1,
        np.where(
            (stock_data['aroon_oscillator'].shift(1) >= 0) & (stock_data['aroon_oscillator'] < 0), -1, 0
        )
    )
    
    # Verificar os valores atuais
    last_oscillator = stock_data['aroon_oscillator'].iloc[-1] if not stock_data.empty else 0
    last_zero_cross = stock_data['zero_cross'].iloc[-1] if not stock_data.empty else 0
    
    # Decisão de negociação
    trade_decision = None
    
    # Usar cruzamento do zero como sinal prioritário se habilitado
    if zero_cross_signal and last_zero_cross != 0:
        trade_decision = True if last_zero_cross == 1 else False
    # Caso contrário, usar níveis de sobrecompra/sobrevenda
    elif last_oscillator > overbought:
        trade_decision = True  # Comprar em sobrecompra (forte momento de alta)
    elif last_oscillator < oversold:
        trade_decision = False  # Vender em sobrevenda (forte momento de baixa)
    # Tendência com base no valor do oscilador
    elif last_oscillator > 0:
        trade_decision = True  # Tendência de alta
    elif last_oscillator < 0:
        trade_decision = False  # Tendência de baixa
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Aroon Oscillator")
        print(f" | Período: {period}")
        print(f" | Nível de Sobrecompra: {overbought}")
        print(f" | Nível de Sobrevenda: {oversold}")
        print(f" | Aroon Up: {stock_data['aroon_up'].iloc[-1]:.2f}")
        print(f" | Aroon Down: {stock_data['aroon_down'].iloc[-1]:.2f}")
        print(f" | Aroon Oscillator: {last_oscillator:.2f}")
        print(f" | Cruzamento do Zero: {'Positivo' if last_zero_cross == 1 else 'Negativo' if last_zero_cross == -1 else 'Nenhum'}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision