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

def getOBVTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    signal_period: int = 9,
    verbose: bool = True
):
    """
    Estratégia baseada em OBV (On-Balance Volume).
    
    O OBV é um indicador cumulativo de volume que adiciona volume em dias de alta 
    e subtrai volume em dias de baixa. Busca relacionar mudanças de preço e volume.
    
    Esta estratégia utiliza a média móvel do OBV para gerar sinais de compra/venda.
    
    Parâmetros:
    - period: Período para cálculo da média móvel do OBV
    - signal_period: Período para cálculo da linha de sinal do OBV
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['close', 'volume']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular OBV
    stock_data['price_change'] = stock_data['close'].diff()
    stock_data['obv'] = 0
    
    # Inicialização do OBV
    stock_data.loc[1:, 'obv'] = np.nan
    
    # Para o primeiro dia, OBV é igual ao volume
    if len(stock_data) > 0:
        stock_data.loc[0, 'obv'] = stock_data.loc[0, 'volume']
    
    # Cálculo do OBV para os dias seguintes
    for i in range(1, len(stock_data)):
        if stock_data.loc[i, 'price_change'] > 0:
            stock_data.loc[i, 'obv'] = stock_data.loc[i-1, 'obv'] + stock_data.loc[i, 'volume']
        elif stock_data.loc[i, 'price_change'] < 0:
            stock_data.loc[i, 'obv'] = stock_data.loc[i-1, 'obv'] - stock_data.loc[i, 'volume']
        else:
            stock_data.loc[i, 'obv'] = stock_data.loc[i-1, 'obv']
    
    # Calcular média móvel do OBV
    stock_data['obv_ma'] = stock_data['obv'].rolling(window=period).mean()
    
    # Calcular linha de sinal do OBV
    stock_data['obv_signal'] = stock_data['obv'].rolling(window=signal_period).mean()
    
    # Calcular histograma OBV
    stock_data['obv_histogram'] = stock_data['obv'] - stock_data['obv_signal']
    
    # Extrair os valores atuais
    current_obv = stock_data['obv'].iloc[-1]
    current_obv_ma = stock_data['obv_ma'].iloc[-1]
    current_obv_signal = stock_data['obv_signal'].iloc[-1]
    current_histogram = stock_data['obv_histogram'].iloc[-1]
    previous_histogram = stock_data['obv_histogram'].iloc[-2] if len(stock_data) > 1 else 0
    
    # Gerar sinais de compra e venda
    cross_above = (current_obv > current_obv_signal) and (stock_data['obv'].iloc[-2] <= stock_data['obv_signal'].iloc[-2])
    cross_below = (current_obv < current_obv_signal) and (stock_data['obv'].iloc[-2] >= stock_data['obv_signal'].iloc[-2])
    
    # Tendência de divergência entre preço e OBV
    price_trend_up = stock_data['close'].iloc[-1] > stock_data['close'].iloc[-period]
    obv_trend_up = current_obv > stock_data['obv'].iloc[-period]
    
    # Divergência: preço sobe mas OBV cai = sinal de venda
    # Divergência: preço cai mas OBV sobe = sinal de compra
    divergence_sell = price_trend_up and not obv_trend_up
    divergence_buy = not price_trend_up and obv_trend_up
    
    # Decisão final
    trade_decision = None
    
    if cross_above or divergence_buy:
        trade_decision = True  # Comprar
    elif cross_below or divergence_sell:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: OBV")
        print(f" | OBV Atual: {current_obv:.0f}")
        print(f" | MM OBV ({period}): {current_obv_ma:.0f}")
        print(f" | Sinal OBV ({signal_period}): {current_obv_signal:.0f}")
        print(f" | Histograma: {current_histogram:.0f}")
        print(f" | Cruzamento Ascendente: {cross_above}")
        print(f" | Cruzamento Descendente: {cross_below}")
        print(f" | Divergência Compra: {divergence_buy}")
        print(f" | Divergência Venda: {divergence_sell}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision