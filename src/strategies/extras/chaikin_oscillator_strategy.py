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

def getChaikinOscillatorTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    fast_period: int = 3,
    slow_period: int = 10,
    verbose: bool = True
):
    """
    Estratégia baseada em Chaikin Oscillator.
    
    O Chaikin Oscillator é derivado da Accumulation/Distribution Line (ADL) e é
    calculado como a diferença entre a EMA rápida e a EMA lenta da ADL.
    
    Parâmetros:
    - period: Período geral para cálculos (mantido para compatibilidade)
    - fast_period: Período para a EMA rápida da ADL
    - slow_period: Período para a EMA lenta da ADL
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low', 'close', 'volume']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular o Money Flow Multiplier
    stock_data['mf_multiplier'] = ((stock_data['close'] - stock_data['low']) - 
                                  (stock_data['high'] - stock_data['close'])) / \
                                  (stock_data['high'] - stock_data['low'])
    
    # Lidar com casos onde high == low (evitar divisão por zero)
    stock_data['mf_multiplier'] = stock_data['mf_multiplier'].replace([np.inf, -np.inf], 0)
    stock_data['mf_multiplier'] = stock_data['mf_multiplier'].fillna(0)
    
    # Calcular o Money Flow Volume
    stock_data['mf_volume'] = stock_data['mf_multiplier'] * stock_data['volume']
    
    # Calcular a Accumulation/Distribution Line (ADL)
    stock_data['adl'] = stock_data['mf_volume'].cumsum()
    
    # Calcular EMA rápida e lenta da ADL
    stock_data['adl_ema_fast'] = stock_data['adl'].ewm(span=fast_period, adjust=False).mean()
    stock_data['adl_ema_slow'] = stock_data['adl'].ewm(span=slow_period, adjust=False).mean()
    
    # Calcular o Chaikin Oscillator
    stock_data['chaikin_osc'] = stock_data['adl_ema_fast'] - stock_data['adl_ema_slow']
    
    # Extrair valores atuais
    current_chaikin = stock_data['chaikin_osc'].iloc[-1]
    prev_chaikin = stock_data['chaikin_osc'].iloc[-2] if len(stock_data) > 1 else current_chaikin
    
    # Determinar o estado atual do Chaikin Oscillator
    is_positive = current_chaikin > 0
    is_rising = current_chaikin > prev_chaikin
    
    # Detectar cruzamento de zero
    zero_cross_up = (current_chaikin > 0) and (prev_chaikin < 0)
    zero_cross_down = (current_chaikin < 0) and (prev_chaikin > 0)
    
    # Detectar divergências
    if len(stock_data) >= period:
        price_rising = stock_data['close'].iloc[-1] > stock_data['close'].iloc[-period]
        chaikin_rising = current_chaikin > stock_data['chaikin_osc'].iloc[-period]
        
        # Divergência: preço sobe mas Chaikin cai = sinal de venda
        # Divergência: preço cai mas Chaikin sobe = sinal de compra
        divergence_sell = price_rising and not chaikin_rising
        divergence_buy = not price_rising and chaikin_rising
    else:
        divergence_sell = False
        divergence_buy = False
    
    # Gerar sinais de compra e venda
    buy_signal = zero_cross_up or (is_positive and is_rising) or divergence_buy
    sell_signal = zero_cross_down or (not is_positive and not is_rising) or divergence_sell
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Chaikin Oscillator")
        print(f" | Período EMA Rápida: {fast_period}")
        print(f" | Período EMA Lenta: {slow_period}")
        print(f" | Chaikin Atual: {current_chaikin:.4f}")
        print(f" | Chaikin Anterior: {prev_chaikin:.4f}")
        print(f" | Chaikin Positivo: {is_positive}")
        print(f" | Chaikin Crescendo: {is_rising}")
        print(f" | Cruzamento de Zero (Para Cima): {zero_cross_up}")
        print(f" | Cruzamento de Zero (Para Baixo): {zero_cross_down}")
        print(f" | Divergência Compra: {divergence_buy}")
        print(f" | Divergência Venda: {divergence_sell}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision