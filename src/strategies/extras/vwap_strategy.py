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

def getVolumeWeightedAveragePriceTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    std_dev_multiplier: float = 1.5,
    reset_daily: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em VWAP (Volume-Weighted Average Price).
    
    O VWAP é um indicador técnico que calcula o preço médio de um ativo ponderado
    pelo volume durante um período específico, geralmente um dia de negociação.
    
    Parâmetros:
    - period: Período para cálculo (mantido para compatibilidade)
    - std_dev_multiplier: Multiplicador para as bandas de desvio padrão
    - reset_daily: Se True, reseta os cálculos a cada dia de negociação
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low', 'close', 'volume']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Verificar se temos a data para separar dias
    date_column = None
    for col in ['date', 'datetime', 'timestamp']:
        if col in stock_data.columns:
            date_column = col
            break
    
    # Calcular o preço típico (high + low + close) / 3
    stock_data['typical_price'] = (stock_data['high'] + stock_data['low'] + stock_data['close']) / 3
    
    # Calcular o valor negociado (preço típico * volume)
    stock_data['tp_volume'] = stock_data['typical_price'] * stock_data['volume']
    
    if reset_daily and date_column:
        # Extrair apenas a data (sem hora) para agrupar por dia
        if pd.api.types.is_datetime64_any_dtype(stock_data[date_column]):
            stock_data['day'] = stock_data[date_column].dt.date
        else:
            # Tentar converter para datetime
            try:
                stock_data['day'] = pd.to_datetime(stock_data[date_column]).dt.date
            except:
                # Se não conseguir converter, usar o dia do index (se for datetime)
                if pd.api.types.is_datetime64_any_dtype(stock_data.index):
                    stock_data['day'] = stock_data.index.date
                else:
                    # Se tudo falhar, não resetar diariamente
                    reset_daily = False
    
    # Calcular o VWAP
    if reset_daily and 'day' in stock_data.columns:
        # Resetar os cálculos por dia
        stock_data['cum_tp_volume'] = stock_data.groupby('day')['tp_volume'].cumsum()
        stock_data['cum_volume'] = stock_data.groupby('day')['volume'].cumsum()
    else:
        # Acumular continuamente
        stock_data['cum_tp_volume'] = stock_data['tp_volume'].cumsum()
        stock_data['cum_volume'] = stock_data['volume'].cumsum()
    
    # Evitar divisão por zero
    stock_data['vwap'] = np.where(
        stock_data['cum_volume'] > 0,
        stock_data['cum_tp_volume'] / stock_data['cum_volume'],
        stock_data['typical_price']
    )
    
    # Calcular desvio do preço em relação ao VWAP
    stock_data['price_dev'] = stock_data['typical_price'] - stock_data['vwap']
    
    # Calcular desvio padrão do preço em relação ao VWAP
    if reset_daily and 'day' in stock_data.columns:
        stock_data['std_dev'] = stock_data.groupby('day')['price_dev'].rolling(window=period).std().reset_index(level=0, drop=True)
    else:
        stock_data['std_dev'] = stock_data['price_dev'].rolling(window=period).std()
    
    # Preencher valores NaN
    stock_data['std_dev'] = stock_data['std_dev'].fillna(0)
    
    # Calcular bandas de desvio padrão
    stock_data['upper_band'] = stock_data['vwap'] + (stock_data['std_dev'] * std_dev_multiplier)
    stock_data['lower_band'] = stock_data['vwap'] - (stock_data['std_dev'] * std_dev_multiplier)
    
    # Extrair valores atuais
    current_close = stock_data['close'].iloc[-1]
    current_vwap = stock_data['vwap'].iloc[-1]
    current_upper = stock_data['upper_band'].iloc[-1]
    current_lower = stock_data['lower_band'].iloc[-1]
    
    # Determinar posição do preço em relação ao VWAP e às bandas
    is_above_vwap = current_close > current_vwap
    is_above_upper = current_close > current_upper
    is_below_lower = current_close < current_lower
    
    # Verificar valores anteriores para detectar cruzamentos
    prev_close = stock_data['close'].iloc[-2] if len(stock_data) > 1 else current_close
    prev_vwap = stock_data['vwap'].iloc[-2] if len(stock_data) > 1 else current_vwap
    prev_upper = stock_data['upper_band'].iloc[-2] if len(stock_data) > 1 else current_upper
    prev_lower = stock_data['lower_band'].iloc[-2] if len(stock_data) > 1 else current_lower
    
    # Detectar cruzamentos
    vwap_cross_up = (current_close > current_vwap) and (prev_close <= prev_vwap)
    vwap_cross_down = (current_close < current_vwap) and (prev_close >= prev_vwap)
    upper_cross_up = (current_close > current_upper) and (prev_close <= prev_upper)
    upper_cross_down = (current_close < current_upper) and (prev_close >= prev_upper)
    lower_cross_up = (current_close > current_lower) and (prev_close <= prev_lower)
    lower_cross_down = (current_close < current_lower) and (prev_close >= prev_lower)
    
    # Detectar retorno ao VWAP após ter cruzado as bandas
    return_to_vwap_from_above = vwap_cross_down and (stock_data['close'].shift(1) > stock_data['upper_band'].shift(1)).any()
    return_to_vwap_from_below = vwap_cross_up and (stock_data['close'].shift(1) < stock_data['lower_band'].shift(1)).any()
    
    # Gerar sinais de compra e venda
    # Comprar:
    # 1. Preço cruza acima do VWAP (sinal de força)
    # 2. Preço está abaixo da banda inferior e começa a subir (retorno à média)
    # 3. Preço retorna ao VWAP de baixo (após ter estado abaixo da banda inferior)
    
    # Vender:
    # 1. Preço cruza abaixo do VWAP (sinal de fraqueza)
    # 2. Preço está acima da banda superior e começa a cair (retorno à média)
    # 3. Preço retorna ao VWAP de cima (após ter estado acima da banda superior)
    
    buy_signal = vwap_cross_up or (is_below_lower and lower_cross_up) or return_to_vwap_from_below
    sell_signal = vwap_cross_down or (is_above_upper and upper_cross_down) or return_to_vwap_from_above
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: VWAP (Volume-Weighted Average Price)")
        print(f" | Período: {period}")
        print(f" | Multiplicador de Desvio: {std_dev_multiplier}")
        print(f" | Reset Diário: {reset_daily}")
        print(f" | Preço Atual: {current_close:.2f}")
        print(f" | VWAP: {current_vwap:.2f}")
        print(f" | Banda Superior: {current_upper:.2f}")
        print(f" | Banda Inferior: {current_lower:.2f}")
        print(f" | Preço Acima do VWAP: {is_above_vwap}")
        print(f" | Preço Acima da Banda Superior: {is_above_upper}")
        print(f" | Preço Abaixo da Banda Inferior: {is_below_lower}")
        print(f" | Cruzamento do VWAP (Para Cima): {vwap_cross_up}")
        print(f" | Cruzamento do VWAP (Para Baixo): {vwap_cross_down}")
        print(f" | Retorno ao VWAP de Cima: {return_to_vwap_from_above}")
        print(f" | Retorno ao VWAP de Baixo: {return_to_vwap_from_below}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision