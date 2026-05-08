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

def getAroonTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    bullish_threshold: int = 70,
    bearish_threshold: int = 30,
    crossover_signal: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em Aroon.
    
    Parâmetros:
    - period: Período para cálculo do indicador
    - bullish_threshold: Limite para identificar tendência de alta
    - bearish_threshold: Limite para identificar tendência de baixa
    - crossover_signal: Usar cruzamento como sinal adicional
    """
    stock_data = stock_data.copy()
    
    # Verificar se as colunas necessárias existem
    if 'high' not in stock_data.columns or 'low' not in stock_data.columns:
        raise ValueError("Colunas 'high' e 'low' são necessárias para o cálculo do Aroon")
    
    # Verificar se há dados suficientes
    if len(stock_data) <= period:
        return None  # Dados insuficientes para cálculo
    
    # Calcular Aroon Up e Aroon Down
    # Função para calcular índices dos máximos/mínimos em um período
    def calculate_aroon(data, period):
        high_idx = data['high'].rolling(window=period).apply(lambda x: x.argmax(), raw=True)
        low_idx = data['low'].rolling(window=period).apply(lambda x: x.argmin(), raw=True)
        
        # Calcular Aroon Up: ((period - períodos desde o máximo) / period) * 100
        aroon_up = ((period - high_idx - 1) / period) * 100
        
        # Calcular Aroon Down: ((period - períodos desde o mínimo) / period) * 100
        aroon_down = ((period - low_idx - 1) / period) * 100
        
        return aroon_up, aroon_down
    
    # Calcular Aroon Up e Aroon Down
    stock_data['aroon_up'], stock_data['aroon_down'] = calculate_aroon(stock_data, period)
    
    # Calcular Aroon Oscillator (Aroon Up - Aroon Down)
    stock_data['aroon_oscillator'] = stock_data['aroon_up'] - stock_data['aroon_down']
    
    # Gerar sinais baseados nos níveis do Aroon
    stock_data['level_signal'] = np.where(
        (stock_data['aroon_up'] > bullish_threshold) & (stock_data['aroon_down'] < bearish_threshold), 1,  # Bullish
        np.where(
            (stock_data['aroon_up'] < bearish_threshold) & (stock_data['aroon_down'] > bullish_threshold), -1,  # Bearish
            0  # Neutro
        )
    )
    
    # Detectar cruzamentos entre Aroon Up e Aroon Down
    stock_data['cross_signal'] = np.where(
        (stock_data['aroon_up'].shift(1) <= stock_data['aroon_down'].shift(1)) & 
        (stock_data['aroon_up'] > stock_data['aroon_down']), 1,  # Cruzamento para cima: sinal de compra
        np.where(
            (stock_data['aroon_up'].shift(1) >= stock_data['aroon_down'].shift(1)) & 
            (stock_data['aroon_up'] < stock_data['aroon_down']), -1,  # Cruzamento para baixo: sinal de venda
            0  # Sem cruzamento: neutro
        )
    )
    
    # Verificar as condições atuais
    last_aroon_up = stock_data['aroon_up'].iloc[-1] if not stock_data.empty else 0
    last_aroon_down = stock_data['aroon_down'].iloc[-1] if not stock_data.empty else 0
    last_oscillator = stock_data['aroon_oscillator'].iloc[-1] if not stock_data.empty else 0
    last_level_signal = stock_data['level_signal'].iloc[-1] if not stock_data.empty else 0
    last_cross_signal = stock_data['cross_signal'].iloc[-1] if not stock_data.empty else 0
    
    # Decisão de negociação
    # Prioridade: cruzamento > níveis > oscilador
    trade_decision = None
    
    if crossover_signal and last_cross_signal != 0:
        trade_decision = True if last_cross_signal == 1 else False
    elif last_level_signal != 0:
        trade_decision = True if last_level_signal == 1 else False
    elif last_oscillator > 0:
        trade_decision = True  # Tendência de alta
    elif last_oscillator < 0:
        trade_decision = False  # Tendência de baixa
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Aroon")
        print(f" | Período: {period}")
        print(f" | Limite Bullish: {bullish_threshold}")
        print(f" | Limite Bearish: {bearish_threshold}")
        print(f" | Usar Cruzamento: {'Sim' if crossover_signal else 'Não'}")
        print(f" | Aroon Up: {last_aroon_up:.2f}")
        print(f" | Aroon Down: {last_aroon_down:.2f}")
        print(f" | Aroon Oscillator: {last_oscillator:.2f}")
        print(f" | Sinal de Nível: {last_level_signal}")
        print(f" | Sinal de Cruzamento: {last_cross_signal}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision