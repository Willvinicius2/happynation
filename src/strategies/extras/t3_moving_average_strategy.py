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

def getT3MovingAverageTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    volume_factor: float = 0.7,
    fast_period: int = 7,
    slow_period: int = 21,
    use_close: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em T3 Moving Average (Tillson T3).
    
    Parâmetros:
    - period: Período para cálculo do indicador
    - volume_factor: Fator de volume (0-1, padrão=0.7)
    - fast_period: Período para T3 rápida
    - slow_period: Período para T3 lenta
    - use_close: Usar preço de fechamento para cálculos
    """
    stock_data = stock_data.copy()
    
    # Verificar se a coluna de preço existe
    price_col = 'close' if use_close else 'open'
    if price_col not in stock_data.columns:
        raise ValueError(f"Coluna '{price_col}' não encontrada nos dados")
    
    # Verificar se há dados suficientes
    if len(stock_data) <= max(period, fast_period, slow_period) * 6:  # T3 requer mais dados (6 EMAs)
        return None  # Dados insuficientes para cálculo
    
    # Função para calcular T3 Moving Average
    def calculate_t3(data, period, vfactor):
        # Calcular o fator de suavização (c1)
        c1 = -vfactor * vfactor * vfactor
        
        # Calcular EMA 1
        e1 = data.ewm(span=period, adjust=False).mean()
        
        # Calcular EMA 2
        e2 = e1.ewm(span=period, adjust=False).mean()
        
        # Calcular EMA 3
        e3 = e2.ewm(span=period, adjust=False).mean()
        
        # Calcular EMA 4
        e4 = e3.ewm(span=period, adjust=False).mean()
        
        # Calcular EMA 5
        e5 = e4.ewm(span=period, adjust=False).mean()
        
        # Calcular EMA 6
        e6 = e5.ewm(span=period, adjust=False).mean()
        
        # Calcular T3 usando a fórmula de Tillson
        t3 = c1 * e6 + 3 * vfactor * c1 * e5 + 3 * vfactor * vfactor * c1 * e4 + vfactor * vfactor * vfactor * e3
        
        return t3
    
    # Calcular T3 principal
    stock_data['t3'] = calculate_t3(stock_data[price_col], period, volume_factor)
    
    # Calcular T3 rápida e lenta para sinais de cruzamento
    stock_data['fast_t3'] = calculate_t3(stock_data[price_col], fast_period, volume_factor)
    stock_data['slow_t3'] = calculate_t3(stock_data[price_col], slow_period, volume_factor)
    
    # Gerar sinais de negociação baseados no cruzamento do T3 rápido e lento
    stock_data['signal'] = np.where(
        stock_data['fast_t3'] > stock_data['slow_t3'], 1,
        np.where(stock_data['fast_t3'] < stock_data['slow_t3'], -1, 0)
    )
    
    # Verificar a inclinação do T3 (tendência)
    stock_data['t3_slope'] = stock_data['t3'].diff(3)  # Diferença com 3 períodos para suavizar
    
    # Detectar mudança na direção do sinal
    stock_data['signal_change'] = stock_data['signal'].diff()
    
    # Verificar o último sinal
    last_signal = stock_data['signal'].iloc[-1] if not stock_data.empty else 0
    last_signal_change = stock_data['signal_change'].iloc[-1] if not stock_data.empty else 0
    last_t3_slope = stock_data['t3_slope'].iloc[-1] if not stock_data.empty else 0
    
    # Decisão de negociação
    # Comprar quando T3 rápido cruza T3 lento para cima, ou quando a inclinação do T3 é positiva
    # Vender quando T3 rápido cruza T3 lento para baixo, ou quando a inclinação do T3 é negativa
    if last_signal_change > 0:  # Mudou de negativo para positivo (cruzamento de alta)
        trade_decision = True  # Comprar
    elif last_signal_change < 0:  # Mudou de positivo para negativo (cruzamento de baixa)
        trade_decision = False  # Vender
    elif last_signal > 0 and last_t3_slope > 0:  # Continuação de tendência de alta
        trade_decision = True  # Comprar
    elif last_signal < 0 and last_t3_slope < 0:  # Continuação de tendência de baixa
        trade_decision = False  # Vender
    else:
        trade_decision = None  # Neutro
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: T3 Moving Average")
        print(f" | Período: {period}")
        print(f" | Fator de Volume: {volume_factor}")
        print(f" | Período T3 Rápido: {fast_period}")
        print(f" | Período T3 Lento: {slow_period}")
        print(f" | Último T3: {stock_data['t3'].iloc[-1]:.2f}")
        print(f" | Último T3 Rápido: {stock_data['fast_t3'].iloc[-1]:.2f}")
        print(f" | Último T3 Lento: {stock_data['slow_t3'].iloc[-1]:.2f}")
        print(f" | Inclinação T3: {last_t3_slope:.4f}")
        print(f" | Último Sinal: {last_signal}")
        print(f" | Mudança de Sinal: {last_signal_change}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision