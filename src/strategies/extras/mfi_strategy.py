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

def getMfiTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    overbought: int = 80,
    oversold: int = 20,
    verbose: bool = True
):
    """
    Estratégia baseada em MFI (Money Flow Index).
    
    O MFI é um oscilador que utiliza tanto o preço quanto o volume para medir
    a pressão de compra e venda. Também é conhecido como RSI ponderado pelo volume.
    
    Parâmetros:
    - period: Período para cálculo do MFI
    - overbought: Nível que indica condição de sobrecompra
    - oversold: Nível que indica condição de sobrevenda
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low', 'close', 'volume']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular o preço típico
    stock_data['typical_price'] = (stock_data['high'] + stock_data['low'] + stock_data['close']) / 3
    
    # Calcular o fluxo de dinheiro (money flow)
    stock_data['money_flow'] = stock_data['typical_price'] * stock_data['volume']
    
    # Calcular a mudança do preço típico
    stock_data['price_change'] = stock_data['typical_price'].diff()
    
    # Separar fluxo de dinheiro positivo e negativo
    stock_data['positive_flow'] = np.where(stock_data['price_change'] > 0, stock_data['money_flow'], 0)
    stock_data['negative_flow'] = np.where(stock_data['price_change'] < 0, stock_data['money_flow'], 0)
    
    # Calcular a soma dos fluxos positivos e negativos para o período
    stock_data['positive_flow_sum'] = stock_data['positive_flow'].rolling(window=period).sum()
    stock_data['negative_flow_sum'] = stock_data['negative_flow'].rolling(window=period).sum()
    
    # Calcular o Money Ratio
    stock_data['money_ratio'] = stock_data['positive_flow_sum'] / stock_data['negative_flow_sum']
    
    # Calcular o MFI
    stock_data['mfi'] = 100 - (100 / (1 + stock_data['money_ratio']))
    
    # Extrair valores atuais
    current_mfi = stock_data['mfi'].iloc[-1]
    prev_mfi = stock_data['mfi'].iloc[-2] if len(stock_data) > 1 else current_mfi
    
    # Determinar o estado atual do MFI
    is_overbought = current_mfi >= overbought
    is_oversold = current_mfi <= oversold
    is_rising = current_mfi > prev_mfi
    
    # Gerar sinais de compra e venda
    buy_signal = is_oversold and is_rising
    sell_signal = is_overbought and not is_rising
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: MFI")
        print(f" | Período: {period}")
        print(f" | MFI Atual: {current_mfi:.2f}")
        print(f" | MFI Anterior: {prev_mfi:.2f}")
        print(f" | Nível de Sobrecompra: {overbought}")
        print(f" | Nível de Sobrevenda: {oversold}")
        print(f" | Sobrecomprado: {is_overbought}")
        print(f" | Sobrevendido: {is_oversold}")
        print(f" | Crescendo: {is_rising}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision