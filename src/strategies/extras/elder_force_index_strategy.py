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

def getElderForceIndexTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    short_period: int = 2,
    long_period: int = 13,
    verbose: bool = True
):
    """
    Estratégia baseada em Elder Force Index.
    
    O Force Index mede a força de cada movimento de preço com base na direção,
    magnitude e volume. Desenvolvido por Dr. Alexander Elder.
    
    Parâmetros:
    - period: Período geral para cálculos (mantido para compatibilidade)
    - short_period: Período para o EMA curto do Force Index
    - long_period: Período para o EMA longo do Force Index
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['close', 'volume']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular o Force Index básico
    stock_data['price_change'] = stock_data['close'].diff(1)
    stock_data['force_index_raw'] = stock_data['price_change'] * stock_data['volume']
    
    # Calcular o Force Index de curto e longo prazo (EMA)
    stock_data['force_index_short'] = stock_data['force_index_raw'].ewm(span=short_period, adjust=False).mean()
    stock_data['force_index_long'] = stock_data['force_index_raw'].ewm(span=long_period, adjust=False).mean()
    
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
    
    # Detectar divergências entre curto e longo prazo
    divergence_buy = (is_short_positive and not is_long_positive)
    divergence_sell = (not is_short_positive and is_long_positive)
    
    # Gerar sinais de compra e venda
    buy_signal = zero_cross_up_short or zero_cross_up_long or divergence_buy
    sell_signal = zero_cross_down_short or zero_cross_down_long or divergence_sell
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Elder Force Index")
        print(f" | Período Curto: {short_period}")
        print(f" | Período Longo: {long_period}")
        print(f" | Force Index Curto: {current_fi_short:.0f}")
        print(f" | Force Index Longo: {current_fi_long:.0f}")
        print(f" | FI Curto Positivo: {is_short_positive}")
        print(f" | FI Longo Positivo: {is_long_positive}")
        print(f" | Cruzamento Zero FI Curto (Para Cima): {zero_cross_up_short}")
        print(f" | Cruzamento Zero FI Curto (Para Baixo): {zero_cross_down_short}")
        print(f" | Cruzamento Zero FI Longo (Para Cima): {zero_cross_up_long}")
        print(f" | Cruzamento Zero FI Longo (Para Baixo): {zero_cross_down_long}")
        print(f" | Divergência Compra: {divergence_buy}")
        print(f" | Divergência Venda: {divergence_sell}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision