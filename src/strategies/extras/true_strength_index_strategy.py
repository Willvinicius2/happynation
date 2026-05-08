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

def getTrueStrengthIndexTradeStrategy(
    stock_data: pd.DataFrame,
    r_period: int = 25,
    s_period: int = 13,
    signal_period: int = 7,
    overbought: float = 25.0,
    oversold: float = -25.0,
    use_close: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em True Strength Index.
    
    Parâmetros:
    - r_period: Período para a primeira suavização (padrão=25)
    - s_period: Período para a segunda suavização (padrão=13)
    - signal_period: Período para a linha de sinal (padrão=7)
    - overbought: Nível de sobrecompra (padrão=25)
    - oversold: Nível de sobrevenda (padrão=-25)
    - use_close: Usar preço de fechamento para cálculos
    """
    stock_data = stock_data.copy()
    
    # Verificar se a coluna de preço existe
    price_col = 'close' if use_close else 'open'
    if price_col not in stock_data.columns:
        raise ValueError(f"Coluna '{price_col}' não encontrada nos dados")
    
    # Verificar se há dados suficientes
    min_periods = r_period + s_period
    if len(stock_data) <= min_periods:
        return None  # Dados insuficientes para cálculo
    
    # Calcular mudanças de preço
    stock_data['price_change'] = stock_data[price_col].diff()
    
    # Primeira suavização da mudança de preço (EMA do period r_period)
    stock_data['pc_ema_r'] = stock_data['price_change'].ewm(span=r_period, adjust=False).mean()
    
    # Segunda suavização (EMA do resultado anterior de period s_period)
    stock_data['pc_ema_r_s'] = stock_data['pc_ema_r'].ewm(span=s_period, adjust=False).mean()
    
    # Primeira suavização do valor absoluto da mudança de preço
    stock_data['abs_pc_ema_r'] = stock_data['price_change'].abs().ewm(span=r_period, adjust=False).mean()
    
    # Segunda suavização do valor absoluto
    stock_data['abs_pc_ema_r_s'] = stock_data['abs_pc_ema_r'].ewm(span=s_period, adjust=False).mean()
    
    # Calcular TSI
    stock_data['tsi'] = 100 * (stock_data['pc_ema_r_s'] / stock_data['abs_pc_ema_r_s'])
    
    # Calcular linha de sinal (EMA do TSI)
    stock_data['tsi_signal'] = stock_data['tsi'].ewm(span=signal_period, adjust=False).mean()
    
    # Calcular histograma (diferença entre TSI e sua linha de sinal)
    stock_data['tsi_histogram'] = stock_data['tsi'] - stock_data['tsi_signal']
    
    # Gerar sinais de negociação baseados nos níveis do TSI
    stock_data['level_signal'] = np.where(
        stock_data['tsi'] < oversold, 1,  # Abaixo do nível de sobrevenda: sinal de compra
        np.where(stock_data['tsi'] > overbought, -1,  # Acima do nível de sobrecompra: sinal de venda
                 0)  # Entre os níveis: neutro
    )
    
    # Detectar cruzamentos entre TSI e sua linha de sinal
    stock_data['cross_signal'] = np.where(
        (stock_data['tsi'].shift(1) <= stock_data['tsi_signal'].shift(1)) & 
        (stock_data['tsi'] > stock_data['tsi_signal']), 1,  # Cruzamento para cima: sinal de compra
        np.where(
            (stock_data['tsi'].shift(1) >= stock_data['tsi_signal'].shift(1)) & 
            (stock_data['tsi'] < stock_data['tsi_signal']), -1,  # Cruzamento para baixo: sinal de venda
            0  # Sem cruzamento: neutro
        )
    )
    
    # Detectar cruzamentos do zero
    stock_data['zero_cross'] = np.where(
        (stock_data['tsi'].shift(1) <= 0) & (stock_data['tsi'] > 0), 1,  # Cruzamento para cima do zero: sinal de compra
        np.where(
            (stock_data['tsi'].shift(1) >= 0) & (stock_data['tsi'] < 0), -1,  # Cruzamento para baixo do zero: sinal de venda
            0  # Sem cruzamento: neutro
        )
    )
    
    # Verificar as condições atuais
    last_tsi = stock_data['tsi'].iloc[-1] if not stock_data.empty else 0
    last_signal = stock_data['tsi_signal'].iloc[-1] if not stock_data.empty else 0
    last_histogram = stock_data['tsi_histogram'].iloc[-1] if not stock_data.empty else 0
    last_level_signal = stock_data['level_signal'].iloc[-1] if not stock_data.empty else 0
    last_cross_signal = stock_data['cross_signal'].iloc[-1] if not stock_data.empty else 0
    last_zero_cross = stock_data['zero_cross'].iloc[-1] if not stock_data.empty else 0
    
    # Decisão de negociação
    # Prioridade: nível de sobrevenda/sobrecompra > cruzamento de sinal > cruzamento de zero > valor atual
    if last_level_signal == 1:
        trade_decision = True  # Comprar quando TSI está abaixo do nível de sobrevenda
    elif last_level_signal == -1:
        trade_decision = False  # Vender quando TSI está acima do nível de sobrecompra
    elif last_cross_signal == 1:
        trade_decision = True  # Comprar no cruzamento para cima do sinal
    elif last_cross_signal == -1:
        trade_decision = False  # Vender no cruzamento para baixo do sinal
    elif last_zero_cross == 1:
        trade_decision = True  # Comprar no cruzamento para cima do zero
    elif last_zero_cross == -1:
        trade_decision = False  # Vender no cruzamento para baixo do zero
    elif last_tsi > 0 and last_histogram > 0:
        trade_decision = True  # Tendência a comprar quando TSI e histograma são positivos
    elif last_tsi < 0 and last_histogram < 0:
        trade_decision = False  # Tendência a vender quando TSI e histograma são negativos
    else:
        trade_decision = None  # Neutro
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: True Strength Index")
        print(f" | Período R: {r_period}")
        print(f" | Período S: {s_period}")
        print(f" | Período de Sinal: {signal_period}")
        print(f" | Nível de Sobrecompra: {overbought}")
        print(f" | Nível de Sobrevenda: {oversold}")
        print(f" | Último TSI: {last_tsi:.2f}")
        print(f" | Último Sinal: {last_signal:.2f}")
        print(f" | Último Histograma: {last_histogram:.2f}")
        print(f" | Sinal de Nível: {last_level_signal}")
        print(f" | Sinal de Cruzamento: {last_cross_signal}")
        print(f" | Cruzamento do Zero: {last_zero_cross}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision