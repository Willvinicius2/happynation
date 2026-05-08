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

def getTEMATradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    short_period: int = 7,
    long_period: int = 21,
    verbose: bool = True
):
    """
    Estratégia baseada em TEMA (Triple Exponential Moving Average).
    
    O TEMA reduz o lag associado a médias móveis tradicionais, tornando-o mais 
    sensível às mudanças recentes de preços.
    
    Parâmetros:
    - period: Período para cálculo do TEMA principal
    - short_period: Período para TEMA curto (para cruzamento)
    - long_period: Período para TEMA longo (para cruzamento)
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos a coluna 'close'
    if 'close' not in stock_data.columns:
        # Tentar converter para minúsculas
        stock_data.columns = [col.lower() for col in stock_data.columns]
        if 'close' not in stock_data.columns:
            raise ValueError("Coluna 'close' não encontrada nos dados.")
    
    # Função para calcular o TEMA
    def calculate_tema(data, period):
        # Primeiro EMA
        ema1 = data.ewm(span=period, adjust=False).mean()
        # Segundo EMA (EMA do primeiro EMA)
        ema2 = ema1.ewm(span=period, adjust=False).mean()
        # Terceiro EMA (EMA do segundo EMA)
        ema3 = ema2.ewm(span=period, adjust=False).mean()
        
        # TEMA = 3*EMA1 - 3*EMA2 + EMA3
        tema = 3 * ema1 - 3 * ema2 + ema3
        
        return tema
    
    # Calcular TEMAs com diferentes períodos
    stock_data['tema'] = calculate_tema(stock_data['close'], period)
    stock_data['tema_short'] = calculate_tema(stock_data['close'], short_period)
    stock_data['tema_long'] = calculate_tema(stock_data['close'], long_period)
    
    # Calcular a inclinação (slope) dos TEMAs para determinar tendência
    stock_data['tema_slope'] = stock_data['tema'].diff()
    stock_data['tema_short_slope'] = stock_data['tema_short'].diff()
    stock_data['tema_long_slope'] = stock_data['tema_long'].diff()
    
    # Extrair valores atuais
    current_close = stock_data['close'].iloc[-1]
    current_tema = stock_data['tema'].iloc[-1]
    current_tema_short = stock_data['tema_short'].iloc[-1]
    current_tema_long = stock_data['tema_long'].iloc[-1]
    current_tema_slope = stock_data['tema_slope'].iloc[-1]
    
    # Valores anteriores para determinar cruzamentos
    prev_close = stock_data['close'].iloc[-2] if len(stock_data) > 1 else current_close
    prev_tema = stock_data['tema'].iloc[-2] if len(stock_data) > 1 else current_tema
    prev_tema_short = stock_data['tema_short'].iloc[-2] if len(stock_data) > 1 else current_tema_short
    prev_tema_long = stock_data['tema_long'].iloc[-2] if len(stock_data) > 1 else current_tema_long
    
    # Determinar o estado atual dos TEMAs
    is_uptrend = current_tema_slope > 0
    short_above_long = current_tema_short > current_tema_long
    
    # Detectar cruzamentos
    # 1. Preço cruza acima/abaixo do TEMA
    price_cross_up = (current_close > current_tema) and (prev_close <= prev_tema)
    price_cross_down = (current_close < current_tema) and (prev_close >= prev_tema)
    
    # 2. TEMA curto cruza acima/abaixo do TEMA longo
    short_cross_up = (current_tema_short > current_tema_long) and (prev_tema_short <= prev_tema_long)
    short_cross_down = (current_tema_short < current_tema_long) and (prev_tema_short >= prev_tema_long)
    
    # Gerar sinais de compra e venda
    # Comprar quando:
    # 1. Preço cruza acima do TEMA principal em tendência de alta
    # 2. TEMA curto cruza acima do TEMA longo
    
    # Vender quando:
    # 1. Preço cruza abaixo do TEMA principal em tendência de baixa
    # 2. TEMA curto cruza abaixo do TEMA longo
    
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
        print(f"📊 Estratégia: TEMA")
        print(f" | Período Principal: {period}")
        print(f" | Período Curto: {short_period}")
        print(f" | Período Longo: {long_period}")
        print(f" | Preço Atual: {current_close:.2f}")
        print(f" | TEMA Principal: {current_tema:.2f}")
        print(f" | TEMA Curto: {current_tema_short:.2f}")
        print(f" | TEMA Longo: {current_tema_long:.2f}")
        print(f" | Inclinação TEMA: {current_tema_slope:.4f}")
        print(f" | Tendência de Alta: {is_uptrend}")
        print(f" | TEMA Curto > TEMA Longo: {short_above_long}")
        print(f" | Cruzamento do Preço (Para Cima): {price_cross_up}")
        print(f" | Cruzamento do Preço (Para Baixo): {price_cross_down}")
        print(f" | Cruzamento TEMA Curto/Longo (Para Cima): {short_cross_up}")
        print(f" | Cruzamento TEMA Curto/Longo (Para Baixo): {short_cross_down}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision