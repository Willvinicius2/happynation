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

def getZeroLagMovingAverageTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    signal_period: int = 9,
    threshold: float = 0.0,
    use_close: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em Zero-Lag Moving Average.
    
    Parâmetros:
    - period: Período para cálculo do indicador
    - signal_period: Período para o cálculo da linha de sinal
    - threshold: Limiar para decisão de compra/venda
    - use_close: Usar preço de fechamento para cálculos
    """
    stock_data = stock_data.copy()
    
    # Verificar se a coluna de preço existe
    price_col = 'close' if use_close else 'open'
    if price_col not in stock_data.columns:
        raise ValueError(f"Coluna '{price_col}' não encontrada nos dados")
    
    # Cálculo do Zero-Lag EMA (ZLEMA)
    # ZLEMA remove o lag do EMA usando uma combinação com valores defasados
    lag = (period - 1) // 2
    if len(stock_data) <= lag:
        return None  # Dados insuficientes para cálculo
    
    # Cálculo do ZLEMA
    # Fórmula: ZLEMA = EMA(2*price - price.shift(lag))
    stock_data['zlema_input'] = 2 * stock_data[price_col] - stock_data[price_col].shift(lag)
    stock_data['zlema'] = stock_data['zlema_input'].ewm(span=period, adjust=False).mean()
    
    # Cálculo da linha de sinal (média móvel do ZLEMA)
    stock_data['signal_line'] = stock_data['zlema'].rolling(window=signal_period).mean()
    
    # Gerar sinais de negociação
    stock_data['zlema_diff'] = stock_data['zlema'] - stock_data['signal_line']
    
    # Determinar a decisão de negociação com base no último valor
    last_diff = stock_data['zlema_diff'].iloc[-1] if not stock_data.empty else 0
    
    # Decisão de negociação
    # Comprar quando ZLEMA estiver acima da linha de sinal por mais que o threshold
    # Vender quando ZLEMA estiver abaixo da linha de sinal por mais que o threshold
    if last_diff > threshold:
        trade_decision = True  # Comprar
    elif last_diff < -threshold:
        trade_decision = False  # Vender
    else:
        trade_decision = None  # Neutro
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Zero-Lag Moving Average")
        print(f" | Período: {period}")
        print(f" | Período de Sinal: {signal_period}")
        print(f" | Threshold: {threshold}")
        print(f" | Último ZLEMA: {stock_data['zlema'].iloc[-1]:.2f}")
        print(f" | Última Linha de Sinal: {stock_data['signal_line'].iloc[-1]:.2f}")
        print(f" | Diferença: {last_diff:.2f}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision