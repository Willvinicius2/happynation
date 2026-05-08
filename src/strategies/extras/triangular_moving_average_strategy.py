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

def getTriangularMovingAverageTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    fast_period: int = 7,
    slow_period: int = 21,
    use_close: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em Triangular Moving Average.
    
    Parâmetros:
    - period: Período para cálculo do indicador
    - fast_period: Período para TMA rápida
    - slow_period: Período para TMA lenta
    - use_close: Usar preço de fechamento para cálculos
    """
    stock_data = stock_data.copy()
    
    # Verificar se a coluna de preço existe
    price_col = 'close' if use_close else 'open'
    if price_col not in stock_data.columns:
        raise ValueError(f"Coluna '{price_col}' não encontrada nos dados")
    
    # Verificar se há dados suficientes
    if len(stock_data) < max(period, fast_period, slow_period):
        return None  # Dados insuficientes para cálculo
    
    # Cálculo do Triangular Moving Average (TMA)
    # TMA é uma média móvel dupla, ou seja, uma SMA de uma SMA
    # Primeiro, calculamos a SMA
    stock_data['sma'] = stock_data[price_col].rolling(window=period).mean()
    
    # Em seguida, calculamos a SMA da SMA para obter o TMA
    stock_data['tma'] = stock_data['sma'].rolling(window=period).mean()
    
    # Calcular TMA rápida e lenta para sinais de cruzamento
    stock_data['fast_sma'] = stock_data[price_col].rolling(window=fast_period).mean()
    stock_data['fast_tma'] = stock_data['fast_sma'].rolling(window=fast_period).mean()
    
    stock_data['slow_sma'] = stock_data[price_col].rolling(window=slow_period).mean()
    stock_data['slow_tma'] = stock_data['slow_sma'].rolling(window=slow_period).mean()
    
    # Gerar sinais de negociação baseados no cruzamento do TMA rápido e lento
    # Quando o TMA rápido cruza o TMA lento para cima, é um sinal de compra
    # Quando o TMA rápido cruza o TMA lento para baixo, é um sinal de venda
    stock_data['signal'] = np.where(
        stock_data['fast_tma'] > stock_data['slow_tma'], 1,
        np.where(stock_data['fast_tma'] < stock_data['slow_tma'], -1, 0)
    )
    
    # Detectar mudança na direção do sinal
    stock_data['signal_change'] = stock_data['signal'].diff()
    
    # Verificar o último sinal
    last_signal = stock_data['signal'].iloc[-1] if not stock_data.empty else 0
    last_signal_change = stock_data['signal_change'].iloc[-1] if not stock_data.empty else 0
    
    # Decisão de negociação
    # Comprar quando o sinal é 1 (TMA rápido acima do TMA lento)
    # Vender quando o sinal é -1 (TMA rápido abaixo do TMA lento)
    # Dá mais importância para sinais recentes (mudança de sinal)
    if last_signal_change > 0:  # Mudou de negativo para positivo
        trade_decision = True  # Comprar
    elif last_signal_change < 0:  # Mudou de positivo para negativo
        trade_decision = False  # Vender
    elif last_signal > 0:  # Continuação de tendência de alta
        trade_decision = True  # Comprar
    elif last_signal < 0:  # Continuação de tendência de baixa
        trade_decision = False  # Vender
    else:
        trade_decision = None  # Neutro
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Triangular Moving Average")
        print(f" | Período: {period}")
        print(f" | Período TMA Rápido: {fast_period}")
        print(f" | Período TMA Lento: {slow_period}")
        print(f" | Último TMA: {stock_data['tma'].iloc[-1]:.2f}")
        print(f" | Último TMA Rápido: {stock_data['fast_tma'].iloc[-1]:.2f}")
        print(f" | Último TMA Lento: {stock_data['slow_tma'].iloc[-1]:.2f}")
        print(f" | Último Sinal: {last_signal}")
        print(f" | Mudança de Sinal: {last_signal_change}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision