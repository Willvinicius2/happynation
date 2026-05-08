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

def getALMATradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    sigma: float = 6.0,
    offset: float = 0.85,
    signal_period: int = 9,
    verbose: bool = True
):
    """
    Estratégia baseada em ALMA (Arnaud Legoux Moving Average).
    
    O ALMA utiliza uma distribuição gaussiana com offset para ponderar os preços,
    produzindo uma média móvel com menos lag e menos ruído.
    
    Parâmetros:
    - period: Período para cálculo do ALMA
    - sigma: Controla a suavidade do filtro (quanto maior, mais suave)
    - offset: Controla a localização da gaussiana (0.5-1.0, default 0.85)
    - signal_period: Período para a linha de sinal (média móvel do ALMA)
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos a coluna 'close'
    if 'close' not in stock_data.columns:
        # Tentar converter para minúsculas
        stock_data.columns = [col.lower() for col in stock_data.columns]
        if 'close' not in stock_data.columns:
            raise ValueError("Coluna 'close' não encontrada nos dados.")
    
    # Calcular o ALMA
    def calculate_alma(series, period, sigma, offset):
        # Verificar se temos dados suficientes
        if len(series) < period:
            return np.full_like(series, np.nan)
        
        # Inicializar o resultado com NaN
        result = np.full_like(series, np.nan)
        
        # Calcular os parâmetros
        m = np.floor(offset * (period - 1))
        s = period / sigma
        
        # Calcular os pesos para o ALMA
        weights = np.zeros(period)
        for i in range(period):
            weights[i] = np.exp(-((i - m) ** 2) / (2 * s * s))
        
        # Normalizar os pesos
        weights = weights / np.sum(weights)
        
        # Aplicar a média ponderada
        for i in range(period - 1, len(series)):
            window = series.iloc[i - period + 1 : i + 1].values
            result[i] = np.sum(window * weights)
        
        return result
    
    # Aplicar o ALMA aos preços de fechamento
    alma_values = calculate_alma(stock_data['close'], period, sigma, offset)
    stock_data['alma'] = alma_values
    
    # Calcular a linha de sinal (média móvel do ALMA)
    stock_data['alma_signal'] = stock_data['alma'].rolling(window=signal_period).mean()
    
    # Calcular a inclinação (tendência) do ALMA
    stock_data['alma_slope'] = stock_data['alma'].diff()
    
    # Extrair valores atuais
    current_close = stock_data['close'].iloc[-1]
    current_alma = stock_data['alma'].iloc[-1]
    current_signal = stock_data['alma_signal'].iloc[-1]
    current_slope = stock_data['alma_slope'].iloc[-1]
    
    # Valores anteriores para determinar cruzamentos
    prev_close = stock_data['close'].iloc[-2] if len(stock_data) > 1 else current_close
    prev_alma = stock_data['alma'].iloc[-2] if len(stock_data) > 1 else current_alma
    prev_signal = stock_data['alma_signal'].iloc[-2] if len(stock_data) > 1 else current_signal
    
    # Determinar o estado atual do ALMA
    is_uptrend = current_slope > 0
    
    # Detectar cruzamentos
    # 1. Preço cruza acima/abaixo do ALMA
    price_cross_up = (current_close > current_alma) and (prev_close <= prev_alma)
    price_cross_down = (current_close < current_alma) and (prev_close >= prev_alma)
    
    # 2. ALMA cruza acima/abaixo da linha de sinal
    alma_cross_up = (current_alma > current_signal) and (prev_alma <= prev_signal)
    alma_cross_down = (current_alma < current_signal) and (prev_alma >= prev_signal)
    
    # Gerar sinais de compra e venda
    # Comprar quando:
    # 1. Preço cruza acima do ALMA e estamos em tendência de alta
    # 2. ALMA cruza acima da linha de sinal
    
    # Vender quando:
    # 1. Preço cruza abaixo do ALMA e estamos em tendência de baixa
    # 2. ALMA cruza abaixo da linha de sinal
    
    buy_signal = (price_cross_up and is_uptrend) or alma_cross_up
    sell_signal = (price_cross_down and not is_uptrend) or alma_cross_down
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: ALMA")
        print(f" | Período: {period}")
        print(f" | Sigma: {sigma}")
        print(f" | Offset: {offset}")
        print(f" | Período do Sinal: {signal_period}")
        print(f" | Preço Atual: {current_close:.2f}")
        print(f" | ALMA: {current_alma:.2f}")
        print(f" | ALMA Signal: {current_signal:.2f}")
        print(f" | ALMA Slope: {current_slope:.4f}")
        print(f" | Tendência de Alta: {is_uptrend}")
        print(f" | Cruzamento do Preço (Para Cima): {price_cross_up}")
        print(f" | Cruzamento do Preço (Para Baixo): {price_cross_down}")
        print(f" | Cruzamento ALMA/Signal (Para Cima): {alma_cross_up}")
        print(f" | Cruzamento ALMA/Signal (Para Baixo): {alma_cross_down}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision