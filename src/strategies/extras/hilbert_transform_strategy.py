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

def getHilbertTransformTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    verbose: bool = True
):
    """
    Estratégia baseada em Hilbert Transform.
    
    Parâmetros:
    - period: Período para cálculo do indicador
    """
    stock_data = stock_data.copy()
    
    # Verificar se as colunas necessárias existem
    if 'close' not in stock_data.columns:
        raise ValueError("Coluna 'close' é necessária para o cálculo do Hilbert Transform")
    
    # Implementação simplificada do Hilbert Transform
    # Nota: O HT real é complexo e geralmente implementado em bibliotecas como TA-Lib
    
    # Calculando componentes simulados do HT
    
    # 1. Média dos preços (aproximação da tendência)
    stock_data['ht_trend'] = stock_data['close'].rolling(window=period).mean()
    
    # 2. Componente oscilatório (aproximação do sine wave)
    price_diff = stock_data['close'].diff()
    stock_data['ht_sine'] = np.sin(np.arange(len(stock_data)) * 2 * np.pi / period)
    stock_data['ht_lead_sine'] = np.cos(np.arange(len(stock_data)) * 2 * np.pi / period)
    
    # 3. Detecção de ciclo (simplificada)
    stock_data['inphase'] = stock_data['close'] * stock_data['ht_sine']
    stock_data['quadrature'] = stock_data['close'] * stock_data['ht_lead_sine']
    
    # Suavização dos componentes
    stock_data['smooth_inphase'] = stock_data['inphase'].rolling(window=period).mean()
    stock_data['smooth_quadrature'] = stock_data['quadrature'].rolling(window=period).mean()
    
    # Detecção de modo instantâneo
    stock_data['prev_inphase'] = stock_data['smooth_inphase'].shift(1)
    stock_data['prev_quadrature'] = stock_data['smooth_quadrature'].shift(1)
    
    # Gerando sinais
    stock_data['ht_signal'] = np.where(
        (stock_data['smooth_inphase'] > 0) & (stock_data['prev_inphase'] <= 0), 1,
        np.where((stock_data['smooth_inphase'] < 0) & (stock_data['prev_inphase'] >= 0), -1, 0)
    )
    
    # Gerar tendência (aproximada)
    stock_data['ht_trend_signal'] = np.where(
        stock_data['close'] > stock_data['ht_trend'], 1,
        np.where(stock_data['close'] < stock_data['ht_trend'], -1, 0)
    )
    
    # Verificar as condições atuais
    last_ht_signal = stock_data['ht_signal'].iloc[-1] if not stock_data.empty else 0
    last_ht_trend = stock_data['ht_trend_signal'].iloc[-1] if not stock_data.empty else 0
    
    # Decisão de negociação
    if last_ht_signal == 1 and last_ht_trend == 1:
        trade_decision = True  # Forte sinal de compra
    elif last_ht_signal == -1 and last_ht_trend == -1:
        trade_decision = False  # Forte sinal de venda
    elif last_ht_signal == 1 or last_ht_trend == 1:
        trade_decision = True  # Sinal moderado de compra
    elif last_ht_signal == -1 or last_ht_trend == -1:
        trade_decision = False  # Sinal moderado de venda
    else:
        trade_decision = None  # Neutro
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Hilbert Transform")
        print(f" | Período: {period}")
        print(f" | Sinal HT: {last_ht_signal}")
        print(f" | Sinal Tendência: {last_ht_trend}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision