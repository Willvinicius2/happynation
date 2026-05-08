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

def getWMATradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    short_period: int = 7,
    long_period: int = 21,
    verbose: bool = True
):
    """
    Estratégia baseada em WMA (Weighted Moving Average).
    
    A WMA atribui pesos maiores para os preços mais recentes, o que a torna mais 
    sensível a movimentos de preço recentes do que uma SMA tradicional.
    
    Parâmetros:
    - period: Período para cálculo do WMA principal
    - short_period: Período para WMA curto (para cruzamento)
    - long_period: Período para WMA longo (para cruzamento)
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos a coluna 'close'
    if 'close' not in stock_data.columns:
        # Tentar converter para minúsculas
        stock_data.columns = [col.lower() for col in stock_data.columns]
        if 'close' not in stock_data.columns:
            raise ValueError("Coluna 'close' não encontrada nos dados.")
    
    # Função para calcular o WMA
    def calculate_wma(data, period):
        weights = np.arange(1, period + 1)
        wma = data.rolling(window=period).apply(
            lambda x: np.sum(weights * x) / np.sum(weights), raw=True
        )
        return wma
    
    # Calcular WMAs com diferentes períodos
    stock_data['wma'] = calculate_wma(stock_data['close'], period)
    stock_data['wma_short'] = calculate_wma(stock_data['close'], short_period)
    stock_data['wma_long'] = calculate_wma(stock_data['close'], long_period)
    
    # Calcular a inclinação (slope) das WMAs para determinar tendência
    stock_data['wma_slope'] = stock_data['wma'].diff()
    stock_data['wma_short_slope'] = stock_data['wma_short'].diff()
    stock_data['wma_long_slope'] = stock_data['wma_long'].diff()
    
    # Extrair valores atuais
    current_close = stock_data['close'].iloc[-1]
    current_wma = stock_data['wma'].iloc[-1]
    current_wma_short = stock_data['wma_short'].iloc[-1]
    current_wma_long = stock_data['wma_long'].iloc[-1]
    current_wma_slope = stock_data['wma_slope'].iloc[-1]
    
    # Valores anteriores para determinar cruzamentos
    prev_close = stock_data['close'].iloc[-2] if len(stock_data) > 1 else current_close
    prev_wma = stock_data['wma'].iloc[-2] if len(stock_data) > 1 else current_wma
    prev_wma_short = stock_data['wma_short'].iloc[-2] if len(stock_data) > 1 else current_wma_short
    prev_wma_long = stock_data['wma_long'].iloc[-2] if len(stock_data) > 1 else current_wma_long
    
    # Determinar o estado atual das WMAs
    is_uptrend = current_wma_slope > 0
    short_above_long = current_wma_short > current_wma_long
    
    # Detectar cruzamentos
    # 1. Preço cruza acima/abaixo da WMA
    price_cross_up = (current_close > current_wma) and (prev_close <= prev_wma)
    price_cross_down = (current_close < current_wma) and (prev_close >= prev_wma)
    
    # 2. WMA curta cruza acima/abaixo da WMA longa
    short_cross_up = (current_wma_short > current_wma_long) and (prev_wma_short <= prev_wma_long)
    short_cross_down = (current_wma_short < current_wma_long) and (prev_wma_short >= prev_wma_long)
    
    # Gerar sinais de compra e venda
    # Comprar quando:
    # 1. Preço cruza acima da WMA principal em tendência de alta
    # 2. WMA curta cruza acima da WMA longa
    
    # Vender quando:
    # 1. Preço cruza abaixo da WMA principal em tendência de baixa
    # 2. WMA curta cruza abaixo da WMA longa
    
    buy_signal = (price_cross_up and is_uptrend) or short_cross_up
    sell_signal = (price_cross_down and not is_uptrend) or short_cross_down
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: WMA")
        print(f" | Período Principal: {period}")
        print(f" | Período Curto: {short_period}")
        print(f" | Período Longo: {long_period}")
        print(f" | Preço Atual: {current_close:.2f}")
        print(f" | WMA Principal: {current_wma:.2f}")
        print(f" | WMA Curto: {current_wma_short:.2f}")
        print(f" | WMA Longo: {current_wma_long:.2f}")
        print(f" | Inclinação WMA: {current_wma_slope:.4f}")
        print(f" | Tendência de Alta: {is_uptrend}")
        print(f" | WMA Curto > WMA Longo: {short_above_long}")
        print(f" | Cruzamento do Preço (Para Cima): {price_cross_up}")
        print(f" | Cruzamento do Preço (Para Baixo): {price_cross_down}")
        print(f" | Cruzamento WMA Curto/Longo (Para Cima): {short_cross_up}")
        print(f" | Cruzamento WMA Curto/Longo (Para Baixo): {short_cross_down}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision