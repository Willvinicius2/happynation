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

def getForceIndexTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    short_period: int = 2,
    verbose: bool = True
):
    """
    Estratégia baseada em Force Index.
    
    O Force Index mede a força por trás dos movimentos de preço utilizando
    preço e volume. É útil para confirmar tendências e identificar reversões.
    
    Parâmetros:
    - period: Período para o EMA do Force Index (longo prazo)
    - short_period: Período para o EMA do Force Index (curto prazo)
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['close', 'volume']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular a variação do preço
    stock_data['price_change'] = stock_data['close'].diff()
    
    # Calcular o Force Index
    stock_data['force_index'] = stock_data['price_change'] * stock_data['volume']
    
    # Calcular o Force Index de curto prazo (EMA)
    stock_data['force_index_short'] = stock_data['force_index'].ewm(span=short_period, adjust=False).mean()
    
    # Calcular o Force Index de longo prazo (EMA)
    stock_data['force_index_long'] = stock_data['force_index'].ewm(span=period, adjust=False).mean()
    
    # Extrair valores atuais
    current_fi_short = stock_data['force_index_short'].iloc[-1]
    prev_fi_short = stock_data['force_index_short'].iloc[-2] if len(stock_data) > 1 else current_fi_short
    
    current_fi_long = stock_data['force_index_long'].iloc[-1]
    prev_fi_long = stock_data['force_index_long'].iloc[-2] if len(stock_data) > 1 else current_fi_long
    
    # Determinar o estado atual do Force Index
    is_short_positive = current_fi_short > 0
    is_long_positive = current_fi_long > 0
    
    # Detectar cruzamento de zero
    zero_cross_up_short = (current_fi_short > 0) and (prev_fi_short < 0)
    zero_cross_down_short = (current_fi_short < 0) and (prev_fi_short > 0)
    
    zero_cross_up_long = (current_fi_long > 0) and (prev_fi_long < 0)
    zero_cross_down_long = (current_fi_long < 0) and (prev_fi_long > 0)
    
    # Calcular tendência de curto prazo da média móvel
    stock_data['ema_13'] = stock_data['close'].ewm(span=13, adjust=False).mean()
    current_ema = stock_data['ema_13'].iloc[-1]
    prev_ema = stock_data['ema_13'].iloc[-2] if len(stock_data) > 1 else current_ema
    ema_trend_up = current_ema > prev_ema
    
    # Gerar sinais de compra e venda
    # Em tendência de alta (EMA subindo):
    # - Comprar: quando FI de curto prazo cruza zero de baixo para cima
    # - Vender: quando FI de longo prazo cruza zero de cima para baixo
    # Em tendência de baixa (EMA descendo):
    # - Comprar: quando FI de longo prazo cruza zero de baixo para cima
    # - Vender: quando FI de curto prazo cruza zero de cima para baixo
    
    if ema_trend_up:
        buy_signal = zero_cross_up_short
        sell_signal = zero_cross_down_long
    else:
        buy_signal = zero_cross_up_long
        sell_signal = zero_cross_down_short
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Force Index")
        print(f" | Período Curto: {short_period}")
        print(f" | Período Longo: {period}")
        print(f" | Force Index Curto: {current_fi_short:.0f}")
        print(f" | Force Index Longo: {current_fi_long:.0f}")
        print(f" | FI Curto Positivo: {is_short_positive}")
        print(f" | FI Longo Positivo: {is_long_positive}")
        print(f" | Tendência EMA: {'Subindo' if ema_trend_up else 'Descendo'}")
        print(f" | Cruzamento Zero FI Curto (Para Cima): {zero_cross_up_short}")
        print(f" | Cruzamento Zero FI Curto (Para Baixo): {zero_cross_down_short}")
        print(f" | Cruzamento Zero FI Longo (Para Cima): {zero_cross_up_long}")
        print(f" | Cruzamento Zero FI Longo (Para Baixo): {zero_cross_down_long}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision