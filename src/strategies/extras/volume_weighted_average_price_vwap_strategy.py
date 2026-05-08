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
    std_dev_multiplier: float = 1.0,
    reset_daily: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em Volume-Weighted Average Price (VWAP).
    
    Parâmetros:
    - period: Período para cálculo do indicador
    - std_dev_multiplier: Multiplicador para as bandas de desvio padrão
    - reset_daily: Resetar cálculos diariamente (padrão para VWAP)
    """
    stock_data = stock_data.copy()
    
    # Verificar se as colunas necessárias existem
    required_cols = ['high', 'low', 'close', 'volume', 'date']
    for col in required_cols:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna '{col}' não encontrada nos dados")
    
    # Converter a coluna de data para datetime se ainda não estiver
    if not pd.api.types.is_datetime64_any_dtype(stock_data['date']):
        stock_data['date'] = pd.to_datetime(stock_data['date'])
    
    # Adicionar coluna de dia para reset diário se necessário
    if reset_daily:
        stock_data['day'] = stock_data['date'].dt.date
    
    # Calcular preço típico (TP): (high + low + close) / 3
    stock_data['typical_price'] = (stock_data['high'] + stock_data['low'] + stock_data['close']) / 3
    
    # Calcular VWAP
    if reset_daily:
        # Resetar cálculos para cada dia
        stock_data['cum_tp_vol'] = stock_data.groupby('day')['typical_price'].apply(
            lambda x: x * stock_data.loc[x.index, 'volume']).cumsum()
        stock_data['cum_vol'] = stock_data.groupby('day')['volume'].cumsum()
        stock_data['vwap'] = stock_data['cum_tp_vol'] / stock_data['cum_vol']
    else:
        # Janela móvel para o período especificado
        rolling_tp_vol = (stock_data['typical_price'] * stock_data['volume']).rolling(window=period).sum()
        rolling_vol = stock_data['volume'].rolling(window=period).sum()
        stock_data['vwap'] = rolling_tp_vol / rolling_vol
    
    # Calcular bandas de desvio padrão ao redor do VWAP
    if reset_daily:
        # Cálculo do desvio padrão para cada dia
        stock_data['tp_vwap_diff'] = stock_data['typical_price'] - stock_data['vwap']
        stock_data['tp_vwap_diff_sq'] = stock_data['tp_vwap_diff'] ** 2
        
        stock_data['cum_diff_sq'] = stock_data.groupby('day')['tp_vwap_diff_sq'].cumsum()
        stock_data['std_dev'] = np.sqrt(stock_data['cum_diff_sq'] / stock_data['cum_vol'])
    else:
        # Janela móvel para o desvio padrão
        tp_vwap_diff = stock_data['typical_price'] - stock_data['vwap']
        stock_data['std_dev'] = tp_vwap_diff.rolling(window=period).std()
    
    # Calcular bandas superiores e inferiores
    stock_data['upper_band'] = stock_data['vwap'] + (stock_data['std_dev'] * std_dev_multiplier)
    stock_data['lower_band'] = stock_data['vwap'] - (stock_data['std_dev'] * std_dev_multiplier)
    
    # Gerar sinais de negociação
    stock_data['position'] = np.where(
        stock_data['close'] < stock_data['lower_band'], 1,  # Abaixo da banda inferior: sinal de compra
        np.where(stock_data['close'] > stock_data['upper_band'], -1,  # Acima da banda superior: sinal de venda
                 0)  # Entre as bandas: neutro
    )
    
    # Verificar a última posição
    last_close = stock_data['close'].iloc[-1] if not stock_data.empty else 0
    last_vwap = stock_data['vwap'].iloc[-1] if not stock_data.empty else 0
    last_upper = stock_data['upper_band'].iloc[-1] if not stock_data.empty else 0
    last_lower = stock_data['lower_band'].iloc[-1] if not stock_data.empty else 0
    last_position = stock_data['position'].iloc[-1] if not stock_data.empty else 0
    
    # Decisão de negociação
    # Comprar quando o preço está abaixo da banda inferior do VWAP
    # Vender quando o preço está acima da banda superior do VWAP
    if last_position == 1:
        trade_decision = True  # Comprar
    elif last_position == -1:
        trade_decision = False  # Vender
    elif last_close < last_vwap:  # Preço abaixo do VWAP mas não da banda inferior
        trade_decision = True  # Tendência a comprar, mas mais fraca
    elif last_close > last_vwap:  # Preço acima do VWAP mas não da banda superior
        trade_decision = False  # Tendência a vender, mas mais fraca
    else:
        trade_decision = None  # Neutro
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Volume-Weighted Average Price (VWAP)")
        print(f" | Período: {period}")
        print(f" | Multiplicador de Desvio Padrão: {std_dev_multiplier}")
        print(f" | Reset Diário: {'Sim' if reset_daily else 'Não'}")
        print(f" | Último Preço: {last_close:.2f}")
        print(f" | Último VWAP: {last_vwap:.2f}")
        print(f" | Banda Superior: {last_upper:.2f}")
        print(f" | Banda Inferior: {last_lower:.2f}")
        print(f" | Última Posição: {last_position}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision