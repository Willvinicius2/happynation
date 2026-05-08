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

def getDonchianChannelsTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    breakout_mode: bool = True,
    use_midline: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em Donchian Channels.
    
    Parâmetros:
    - period: Período para cálculo do indicador
    - breakout_mode: Usar modo de breakout (True) ou modo de reversão (False)
    - use_midline: Usar linha do meio para sinais adicionais
    """
    stock_data = stock_data.copy()
    
    # Verificar se as colunas necessárias existem
    if 'high' not in stock_data.columns or 'low' not in stock_data.columns or 'close' not in stock_data.columns:
        raise ValueError("Colunas 'high', 'low' e 'close' são necessárias para o cálculo do Donchian Channels")
    
    # Verificar se há dados suficientes
    if len(stock_data) <= period:
        return None  # Dados insuficientes para cálculo
    
    # Calcular Donchian Channels
    stock_data['upper_band'] = stock_data['high'].rolling(window=period).max()
    stock_data['lower_band'] = stock_data['low'].rolling(window=period).min()
    stock_data['middle_band'] = (stock_data['upper_band'] + stock_data['lower_band']) / 2
    
    # Cálculo para detectar novos máximos/mínimos no período
    stock_data['new_high'] = stock_data['high'] >= stock_data['upper_band'].shift(1)
    stock_data['new_low'] = stock_data['low'] <= stock_data['lower_band'].shift(1)
    
    # Gerar sinais de negociação baseados na estratégia selecionada
    if breakout_mode:
        # Modo de breakout: comprar em novos máximos, vender em novos mínimos
        stock_data['signal'] = np.where(
            stock_data['new_high'], 1,
            np.where(stock_data['new_low'], -1, 0)
        )
    else:
        # Modo de reversão: comprar em mínimos, vender em máximos (contrário ao breakout)
        stock_data['signal'] = np.where(
            stock_data['close'] <= stock_data['lower_band'], 1,
            np.where(stock_data['close'] >= stock_data['upper_band'], -1, 0)
        )
    
    # Sinais adicionais usando a linha do meio se ativado
    if use_midline:
        # Detectar cruzamentos da linha do meio
        stock_data['mid_cross'] = np.where(
            (stock_data['close'].shift(1) < stock_data['middle_band'].shift(1)) & 
            (stock_data['close'] > stock_data['middle_band']), 1,
            np.where(
                (stock_data['close'].shift(1) > stock_data['middle_band'].shift(1)) & 
                (stock_data['close'] < stock_data['middle_band']), -1, 0
            )
        )
    
    # Verificar as condições atuais
    last_close = stock_data['close'].iloc[-1] if not stock_data.empty else 0
    last_upper = stock_data['upper_band'].iloc[-1] if not stock_data.empty else 0
    last_lower = stock_data['lower_band'].iloc[-1] if not stock_data.empty else 0
    last_middle = stock_data['middle_band'].iloc[-1] if not stock_data.empty else 0
    last_signal = stock_data['signal'].iloc[-1] if not stock_data.empty else 0
    last_mid_cross = stock_data['mid_cross'].iloc[-1] if use_midline and not stock_data.empty else 0
    
    # Decisão de negociação
    if last_signal == 1:
        trade_decision = True  # Comprar quando sinal primário indica compra
    elif last_signal == -1:
        trade_decision = False  # Vender quando sinal primário indica venda
    elif use_midline and last_mid_cross == 1:
        trade_decision = True  # Comprar quando há cruzamento positivo da linha do meio
    elif use_midline and last_mid_cross == -1:
        trade_decision = False  # Vender quando há cruzamento negativo da linha do meio
    elif use_midline and last_close > last_middle:
        trade_decision = True  # Tendência a comprar quando acima da linha do meio
    elif use_midline and last_close < last_middle:
        trade_decision = False  # Tendência a vender quando abaixo da linha do meio
    else:
        trade_decision = None  # Neutro
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Donchian Channels")
        print(f" | Período: {period}")
        print(f" | Modo: {'Breakout' if breakout_mode else 'Reversão'}")
        print(f" | Uso da Linha Média: {'Sim' if use_midline else 'Não'}")
        print(f" | Último Preço: {last_close:.2f}")
        print(f" | Banda Superior: {last_upper:.2f}")
        print(f" | Banda Inferior: {last_lower:.2f}")
        print(f" | Banda Média: {last_middle:.2f}")
        print(f" | Último Sinal: {last_signal}")
        if use_midline:
            print(f" | Cruzamento da Linha Média: {last_mid_cross}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision