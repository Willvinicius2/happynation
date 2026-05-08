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

def getKeltnerChannelsTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    atr_period: int = 10,
    multiplier: float = 2.0,
    use_ema: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em Keltner Channels.
    
    Parâmetros:
    - period: Período para cálculo da média móvel central
    - atr_period: Período para cálculo do ATR (Average True Range)
    - multiplier: Multiplicador para definir a largura das bandas
    - use_ema: Usar EMA (True) ou SMA (False) para a linha central
    """
    stock_data = stock_data.copy()
    
    # Verificar se as colunas necessárias existem
    required_cols = ['high', 'low', 'close']
    for col in required_cols:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna '{col}' não encontrada nos dados")
    
    # Calcular True Range (TR)
    stock_data['tr1'] = abs(stock_data['high'] - stock_data['low'])
    stock_data['tr2'] = abs(stock_data['high'] - stock_data['close'].shift(1))
    stock_data['tr3'] = abs(stock_data['low'] - stock_data['close'].shift(1))
    stock_data['true_range'] = stock_data[['tr1', 'tr2', 'tr3']].max(axis=1)
    
    # Calcular Average True Range (ATR)
    if use_ema:
        stock_data['atr'] = stock_data['true_range'].ewm(span=atr_period, adjust=False).mean()
    else:
        stock_data['atr'] = stock_data['true_range'].rolling(window=atr_period).mean()
    
    # Calcular linha central (EMA ou SMA do preço de fechamento)
    if use_ema:
        stock_data['middle_line'] = stock_data['close'].ewm(span=period, adjust=False).mean()
    else:
        stock_data['middle_line'] = stock_data['close'].rolling(window=period).mean()
    
    # Calcular bandas superiores e inferiores
    stock_data['upper_band'] = stock_data['middle_line'] + (multiplier * stock_data['atr'])
    stock_data['lower_band'] = stock_data['middle_line'] - (multiplier * stock_data['atr'])
    
    # Gerar sinais de negociação
    stock_data['signal'] = np.where(
        stock_data['close'] < stock_data['lower_band'], 1,  # Abaixo da banda inferior: sinal de compra
        np.where(stock_data['close'] > stock_data['upper_band'], -1,  # Acima da banda superior: sinal de venda
                 0)  # Entre as bandas: neutro
    )
    
    # Verificar as condições atuais
    last_close = stock_data['close'].iloc[-1] if not stock_data.empty else 0
    last_middle = stock_data['middle_line'].iloc[-1] if not stock_data.empty else 0
    last_upper = stock_data['upper_band'].iloc[-1] if not stock_data.empty else 0
    last_lower = stock_data['lower_band'].iloc[-1] if not stock_data.empty else 0
    last_signal = stock_data['signal'].iloc[-1] if not stock_data.empty else 0
    
    # Decisão de negociação
    if last_signal == 1:
        trade_decision = True  # Comprar quando abaixo da banda inferior
    elif last_signal == -1:
        trade_decision = False  # Vender quando acima da banda superior
    elif last_close < last_middle:
        trade_decision = True  # Tendência a comprar quando abaixo da linha central
    elif last_close > last_middle:
        trade_decision = False  # Tendência a vender quando acima da linha central
    else:
        trade_decision = None  # Neutro
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Keltner Channels")
        print(f" | Período: {period}")
        print(f" | Período ATR: {atr_period}")
        print(f" | Multiplicador: {multiplier}")
        print(f" | Método: {'EMA' if use_ema else 'SMA'}")
        print(f" | Último Preço: {last_close:.2f}")
        print(f" | Linha Central: {last_middle:.2f}")
        print(f" | Banda Superior: {last_upper:.2f}")
        print(f" | Banda Inferior: {last_lower:.2f}")
        print(f" | Último Sinal: {last_signal}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision