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

def getCmfTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    zero_cross_threshold: float = 0.05,
    verbose: bool = True
):
    """
    Estratégia baseada em CMF (Chaikin Money Flow).
    
    O CMF mede a quantidade de fluxo de dinheiro em um valor ao longo do tempo.
    Combina preço e volume para determinar se o valor está sob acumulação ou distribuição.
    
    Parâmetros:
    - period: Período para cálculo do CMF
    - zero_cross_threshold: Limiar para confirmar cruzamento de zero
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low', 'close', 'volume']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular o Money Flow Multiplier
    stock_data['mf_multiplier'] = ((stock_data['close'] - stock_data['low']) - 
                                  (stock_data['high'] - stock_data['close'])) / \
                                  (stock_data['high'] - stock_data['low'])
    
    # Lidar com casos onde high == low (evitar divisão por zero)
    stock_data['mf_multiplier'] = stock_data['mf_multiplier'].replace([np.inf, -np.inf], 0)
    stock_data['mf_multiplier'] = stock_data['mf_multiplier'].fillna(0)
    
    # Calcular o Money Flow Volume
    stock_data['mf_volume'] = stock_data['mf_multiplier'] * stock_data['volume']
    
    # Calcular o Chaikin Money Flow
    stock_data['cmf'] = stock_data['mf_volume'].rolling(window=period).sum() / \
                      stock_data['volume'].rolling(window=period).sum()
    
    # Extrair valores atuais
    current_cmf = stock_data['cmf'].iloc[-1]
    prev_cmf = stock_data['cmf'].iloc[-2] if len(stock_data) > 1 else current_cmf
    
    # Determinar o estado atual do CMF
    is_positive = current_cmf > 0
    is_rising = current_cmf > prev_cmf
    
    # Detectar cruzamento de zero
    zero_cross_up = (current_cmf > zero_cross_threshold) and (prev_cmf < -zero_cross_threshold)
    zero_cross_down = (current_cmf < -zero_cross_threshold) and (prev_cmf > zero_cross_threshold)
    
    # Detectar divergências
    if len(stock_data) >= period:
        price_rising = stock_data['close'].iloc[-1] > stock_data['close'].iloc[-period]
        cmf_rising = current_cmf > stock_data['cmf'].iloc[-period]
        
        # Divergência: preço sobe mas CMF cai = sinal de venda
        # Divergência: preço cai mas CMF sobe = sinal de compra
        divergence_sell = price_rising and not cmf_rising
        divergence_buy = not price_rising and cmf_rising
    else:
        divergence_sell = False
        divergence_buy = False
    
    # Gerar sinais de compra e venda
    buy_signal = zero_cross_up or (is_positive and is_rising) or divergence_buy
    sell_signal = zero_cross_down or (not is_positive and not is_rising) or divergence_sell
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: CMF")
        print(f" | Período: {period}")
        print(f" | CMF Atual: {current_cmf:.4f}")
        print(f" | CMF Anterior: {prev_cmf:.4f}")
        print(f" | CMF Positivo: {is_positive}")
        print(f" | CMF Crescendo: {is_rising}")
        print(f" | Cruzamento de Zero (Para Cima): {zero_cross_up}")
        print(f" | Cruzamento de Zero (Para Baixo): {zero_cross_down}")
        print(f" | Divergência Compra: {divergence_buy}")
        print(f" | Divergência Venda: {divergence_sell}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision