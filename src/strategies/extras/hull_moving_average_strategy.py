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

def getHullMovingAverageTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    fast_period: int = 9,
    verbose: bool = True
):
    """
    Estratégia baseada em Hull Moving Average.
    
    A Hull Moving Average (HMA) foi desenvolvida por Alan Hull para reduzir o lag
    associado às médias móveis tradicionais, mantendo a suavidade do gráfico.
    
    Parâmetros:
    - period: Período principal para cálculo do HMA
    - fast_period: Período menor para o HMA rápido (para crossover)
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos a coluna 'close'
    if 'close' not in stock_data.columns:
        # Tentar converter para minúsculas
        stock_data.columns = [col.lower() for col in stock_data.columns]
        if 'close' not in stock_data.columns:
            raise ValueError("Coluna 'close' não encontrada nos dados.")
    
    # Função para calcular o HMA
    def calculate_hma(data, n):
        # Calcular as duas WMAs (Weighted Moving Average)
        wma_n = data.rolling(window=n, min_periods=n).apply(
            lambda x: sum([(i+1) * x[i] for i in range(len(x))]) / sum([(i+1) for i in range(len(x))]), raw=True)
        
        wma_n_half = data.rolling(window=n//2, min_periods=n//2).apply(
            lambda x: sum([(i+1) * x[i] for i in range(len(x))]) / sum([(i+1) for i in range(len(x))]), raw=True)
        
        # Calcular o raw HMA
        raw_hma = 2 * wma_n_half - wma_n
        
        # Calcular o HMA final
        sqrt_n = int(np.sqrt(n))
        hma = raw_hma.rolling(window=sqrt_n, min_periods=sqrt_n).apply(
            lambda x: sum([(i+1) * x[i] for i in range(len(x))]) / sum([(i+1) for i in range(len(x))]), raw=True)
        
        return hma
    
    # Calcular o HMA principal e o HMA rápido
    stock_data['hma'] = calculate_hma(stock_data['close'], period)
    stock_data['hma_fast'] = calculate_hma(stock_data['close'], fast_period)
    
    # Calcular a direção do HMA (slope)
    stock_data['hma_slope'] = stock_data['hma'].diff()
    stock_data['hma_fast_slope'] = stock_data['hma_fast'].diff()
    
    # Extrair valores atuais
    current_close = stock_data['close'].iloc[-1]
    current_hma = stock_data['hma'].iloc[-1]
    current_hma_fast = stock_data['hma_fast'].iloc[-1]
    current_hma_slope = stock_data['hma_slope'].iloc[-1]
    current_hma_fast_slope = stock_data['hma_fast_slope'].iloc[-1]
    
    # Valores anteriores para determinar cruzamentos
    prev_close = stock_data['close'].iloc[-2] if len(stock_data) > 1 else current_close
    prev_hma = stock_data['hma'].iloc[-2] if len(stock_data) > 1 else current_hma
    prev_hma_fast = stock_data['hma_fast'].iloc[-2] if len(stock_data) > 1 else current_hma_fast
    
    # Determinar o estado atual do HMA
    is_hma_rising = current_hma_slope > 0
    is_hma_fast_rising = current_hma_fast_slope > 0
    
    # Detectar cruzamento de preço e HMA
    price_cross_up = (current_close > current_hma) and (prev_close <= prev_hma)
    price_cross_down = (current_close < current_hma) and (prev_close >= prev_hma)
    
    # Detectar cruzamento de HMA rápido e HMA lento
    hma_cross_up = (current_hma_fast > current_hma) and (prev_hma_fast <= prev_hma)
    hma_cross_down = (current_hma_fast < current_hma) and (prev_hma_fast >= prev_hma)
    
    # Gerar sinais de compra e venda
    # Comprar: HMA subindo e preço cruza acima do HMA, ou HMA rápido cruza acima do HMA lento
    # Vender: HMA descendo e preço cruza abaixo do HMA, ou HMA rápido cruza abaixo do HMA lento
    
    buy_signal = (is_hma_rising and price_cross_up) or hma_cross_up
    sell_signal = (not is_hma_rising and price_cross_down) or hma_cross_down
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Hull Moving Average")
        print(f" | Período: {period}")
        print(f" | Período Rápido: {fast_period}")
        print(f" | Preço Atual: {current_close:.2f}")
        print(f" | HMA: {current_hma:.2f}")
        print(f" | HMA Rápido: {current_hma_fast:.2f}")
        print(f" | HMA Crescendo: {is_hma_rising}")
        print(f" | HMA Rápido Crescendo: {is_hma_fast_rising}")
        print(f" | Cruzamento de Preço e HMA (Para Cima): {price_cross_up}")
        print(f" | Cruzamento de Preço e HMA (Para Baixo): {price_cross_down}")
        print(f" | Cruzamento de HMA Rápido e HMA (Para Cima): {hma_cross_up}")
        print(f" | Cruzamento de HMA Rápido e HMA (Para Baixo): {hma_cross_down}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision