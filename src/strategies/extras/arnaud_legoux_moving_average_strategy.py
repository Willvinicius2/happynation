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

def getArnaudLegouxMovingAverageTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    sigma: float = 6.0,
    offset: float = 0.85,
    fast_period: int = 9,
    slow_period: int = 21,
    use_close: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em Arnaud Legoux Moving Average (ALMA).
    
    Parâmetros:
    - period: Período para cálculo do indicador
    - sigma: Controla a suavidade da curva (padrão=6)
    - offset: Controla a reatividade vs lag (0=mais lag, 1=mais reatividade)
    - fast_period: Período para ALMA rápida
    - slow_period: Período para ALMA lenta
    - use_close: Usar preço de fechamento para cálculos
    """
    stock_data = stock_data.copy()
    
    # Verificar se a coluna de preço existe
    price_col = 'close' if use_close else 'open'
    if price_col not in stock_data.columns:
        raise ValueError(f"Coluna '{price_col}' não encontrada nos dados")
    
    # Função para calcular ALMA
    def alma(series, period, sigma, offset):
        m = np.floor(offset * (period - 1))
        s = period / sigma
        weights = np.zeros(period)
        
        # Calcular pesos de acordo com a distribuição de Gauss
        for i in range(period):
            weights[i] = np.exp(-((i - m) ** 2) / (2 * s * s))
            
        # Normalizar pesos
        weights = weights / np.sum(weights)
        
        # Aplicar os pesos para calcular o ALMA
        result = np.zeros(len(series))
        result[:] = np.NaN
        
        for i in range(period - 1, len(series)):
            window = series.iloc[i - period + 1:i + 1].values
            result[i] = np.sum(window * weights[::-1])  # Inverter pesos para corresponder ao janelamento correto
            
        return pd.Series(result, index=series.index)
    
    # Calcular ALMA principal
    stock_data['alma'] = alma(stock_data[price_col], period, sigma, offset)
    
    # Calcular ALMA rápida e lenta para sinais de cruzamento
    stock_data['fast_alma'] = alma(stock_data[price_col], fast_period, sigma, offset)
    stock_data['slow_alma'] = alma(stock_data[price_col], slow_period, sigma, offset)
    
    # Gerar sinais de negociação
    stock_data['signal'] = np.where(
        stock_data['fast_alma'] > stock_data['slow_alma'], 1,
        np.where(stock_data['fast_alma'] < stock_data['slow_alma'], -1, 0)
    )
    
    # Detectar mudança na direção do sinal
    stock_data['signal_change'] = stock_data['signal'].diff()
    
    # Verificar o último sinal
    last_signal = stock_data['signal'].iloc[-1] if not stock_data.empty else 0
    last_signal_change = stock_data['signal_change'].iloc[-1] if not stock_data.empty else 0
    last_price = stock_data[price_col].iloc[-1] if not stock_data.empty else 0
    last_alma = stock_data['alma'].iloc[-1] if not stock_data.empty else 0
    
    # Decisão de negociação
    # Comprar quando o preço cruza o ALMA para cima, ou quando ALMA rápido cruza ALMA lento para cima
    # Vender quando o preço cruza o ALMA para baixo, ou quando ALMA rápido cruza ALMA lento para baixo
    if last_signal_change > 0:  # Mudou de negativo para positivo (cruzamento de alta)
        trade_decision = True  # Comprar
    elif last_signal_change < 0:  # Mudou de positivo para negativo (cruzamento de baixa)
        trade_decision = False  # Vender
    elif last_signal > 0 and last_price > last_alma:  # Continuação de tendência de alta
        trade_decision = True  # Comprar
    elif last_signal < 0 and last_price < last_alma:  # Continuação de tendência de baixa
        trade_decision = False  # Vender
    else:
        trade_decision = None  # Neutro
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Arnaud Legoux Moving Average")
        print(f" | Período: {period}")
        print(f" | Sigma: {sigma}")
        print(f" | Offset: {offset}")
        print(f" | Período ALMA Rápido: {fast_period}")
        print(f" | Período ALMA Lento: {slow_period}")
        print(f" | Último ALMA: {last_alma:.2f}")
        print(f" | Último ALMA Rápido: {stock_data['fast_alma'].iloc[-1]:.2f}")
        print(f" | Último ALMA Lento: {stock_data['slow_alma'].iloc[-1]:.2f}")
        print(f" | Último Preço: {last_price:.2f}")
        print(f" | Último Sinal: {last_signal}")
        print(f" | Mudança de Sinal: {last_signal_change}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision