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

def getSchaffTrendCycleTradeStrategy(
    stock_data: pd.DataFrame,
    stc_fast: int = 23,
    stc_slow: int = 50,
    stc_cycle: int = 10,
    stc_upper: int = 75,
    stc_lower: int = 25,
    use_close: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em Schaff Trend Cycle.
    
    Parâmetros:
    - stc_fast: Período rápido do MACD (padrão=23)
    - stc_slow: Período lento do MACD (padrão=50)
    - stc_cycle: Período do ciclo do Schaff (padrão=10)
    - stc_upper: Limite superior para sobrevenda (padrão=75)
    - stc_lower: Limite inferior para sobrecompra (padrão=25)
    - use_close: Usar preço de fechamento para cálculos
    """
    stock_data = stock_data.copy()
    
    # Verificar se a coluna de preço existe
    price_col = 'close' if use_close else 'open'
    if price_col not in stock_data.columns:
        raise ValueError(f"Coluna '{price_col}' não encontrada nos dados")
    
    # Verificar se há dados suficientes
    min_periods = max(stc_fast, stc_slow) + stc_cycle
    if len(stock_data) <= min_periods:
        return None  # Dados insuficientes para cálculo
    
    # Calcular componentes do MACD
    stock_data['ema_fast'] = stock_data[price_col].ewm(span=stc_fast, adjust=False).mean()
    stock_data['ema_slow'] = stock_data[price_col].ewm(span=stc_slow, adjust=False).mean()
    stock_data['macd'] = stock_data['ema_fast'] - stock_data['ema_slow']
    
    # Calcular máximos e mínimos do MACD ao longo do período do ciclo
    stock_data['macd_max'] = stock_data['macd'].rolling(window=stc_cycle).max()
    stock_data['macd_min'] = stock_data['macd'].rolling(window=stc_cycle).min()
    
    # Calcular o %K do MACD (similar ao Estocástico)
    stock_data['macd_k'] = 100 * (stock_data['macd'] - stock_data['macd_min']) / (stock_data['macd_max'] - stock_data['macd_min']).replace(0, 1)  # Evitar divisão por zero
    
    # Calcular o %D do MACD (primeira suavização)
    stock_data['macd_d'] = stock_data['macd_k'].ewm(span=stc_cycle, adjust=False).mean()
    
    # Calcular máximos e mínimos do %D ao longo do período do ciclo
    stock_data['macd_d_max'] = stock_data['macd_d'].rolling(window=stc_cycle).max()
    stock_data['macd_d_min'] = stock_data['macd_d'].rolling(window=stc_cycle).min()
    
    # Calcular o Schaff Trend Cycle (segunda aplicação do estocástico)
    stock_data['stc'] = 100 * (stock_data['macd_d'] - stock_data['macd_d_min']) / (stock_data['macd_d_max'] - stock_data['macd_d_min']).replace(0, 1)  # Evitar divisão por zero
    
    # Suavização final do STC
    stock_data['stc_smooth'] = stock_data['stc'].ewm(span=3, adjust=False).mean()
    
    # Gerar sinais de negociação baseados nos níveis do STC
    stock_data['signal'] = np.where(
        stock_data['stc_smooth'] < stc_lower, 1,  # Abaixo do limite inferior: sinal de compra (sobrevendido)
        np.where(stock_data['stc_smooth'] > stc_upper, -1,  # Acima do limite superior: sinal de venda (sobrecomprado)
                 0)  # Entre os limites: neutro
    )
    
    # Detectar cruzamentos dos limites
    stock_data['cross_lower'] = np.where(
        (stock_data['stc_smooth'].shift(1) <= stc_lower) & (stock_data['stc_smooth'] > stc_lower), 1, 0
    )
    
    stock_data['cross_upper'] = np.where(
        (stock_data['stc_smooth'].shift(1) >= stc_upper) & (stock_data['stc_smooth'] < stc_upper), 1, 0
    )
    
    # Verificar as condições atuais
    last_stc = stock_data['stc_smooth'].iloc[-1] if not stock_data.empty else 0
    last_signal = stock_data['signal'].iloc[-1] if not stock_data.empty else 0
    last_cross_lower = stock_data['cross_lower'].iloc[-1] if not stock_data.empty else 0
    last_cross_upper = stock_data['cross_upper'].iloc[-1] if not stock_data.empty else 0
    
    # Decisão de negociação
    if last_signal == 1 or last_cross_lower == 1:
        trade_decision = True  # Comprar quando STC está abaixo do limite inferior ou cruza para cima
    elif last_signal == -1 or last_cross_upper == 1:
        trade_decision = False  # Vender quando STC está acima do limite superior ou cruza para baixo
    elif last_stc < 50:
        trade_decision = True  # Tendência a comprar quando STC está abaixo de 50
    elif last_stc > 50:
        trade_decision = False  # Tendência a vender quando STC está acima de 50
    else:
        trade_decision = None  # Neutro
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Schaff Trend Cycle")
        print(f" | Período Fast: {stc_fast}")
        print(f" | Período Slow: {stc_slow}")
        print(f" | Período Cycle: {stc_cycle}")
        print(f" | Limite Superior: {stc_upper}")
        print(f" | Limite Inferior: {stc_lower}")
        print(f" | Último STC: {last_stc:.2f}")
        print(f" | Último Sinal: {last_signal}")
        print(f" | Cruzamento Inferior: {'Sim' if last_cross_lower == 1 else 'Não'}")
        print(f" | Cruzamento Superior: {'Sim' if last_cross_upper == 1 else 'Não'}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision