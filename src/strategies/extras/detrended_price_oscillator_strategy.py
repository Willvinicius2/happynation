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

def getDetrendedPriceOscillatorTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    ma_type: str = 'sma',
    verbose: bool = True
):
    """
    Estratégia baseada em Detrended Price Oscillator.
    
    O Detrended Price Oscillator (DPO) ajuda a remover tendências de longo prazo
    para identificar ciclos no preço. Não é um oscilador comum pois não está
    relacionado ao momentum ou à velocidade da mudança de preço.
    
    Parâmetros:
    - period: Período para cálculo do DPO
    - ma_type: Tipo de média móvel ('sma' ou 'ema')
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos a coluna 'close'
    if 'close' not in stock_data.columns:
        # Tentar converter para minúsculas
        stock_data.columns = [col.lower() for col in stock_data.columns]
        if 'close' not in stock_data.columns:
            raise ValueError("Coluna 'close' não encontrada nos dados.")
    
    # Calcular a média móvel
    if ma_type.lower() == 'ema':
        stock_data['ma'] = stock_data['close'].ewm(span=period, adjust=False).mean()
    else:  # default to SMA
        stock_data['ma'] = stock_data['close'].rolling(window=period).mean()
    
    # Calcular o DPO (Detrended Price Oscillator)
    # O DPO é calculado como o preço de fechamento menos a média móvel deslocada por (período / 2 + 1)
    shift_period = int(period / 2 + 1)
    stock_data['dpo'] = stock_data['close'] - stock_data['ma'].shift(shift_period)
    
    # Calcular média móvel do DPO para suavizar
    stock_data['dpo_ma'] = stock_data['dpo'].rolling(window=period//2).mean()
    
    # Extrair valores atuais
    current_dpo = stock_data['dpo'].iloc[-1]
    prev_dpo = stock_data['dpo'].iloc[-2] if len(stock_data) > 1 else current_dpo
    current_dpo_ma = stock_data['dpo_ma'].iloc[-1]
    
    # Determinar o estado atual do DPO
    is_positive = current_dpo > 0
    is_rising = current_dpo > prev_dpo
    
    # Detectar cruzamento de zero
    zero_cross_up = (current_dpo > 0) and (prev_dpo < 0)
    zero_cross_down = (current_dpo < 0) and (prev_dpo > 0)
    
    # Detectar cruzamento de média móvel
    ma_cross_up = (current_dpo > current_dpo_ma) and (stock_data['dpo'].iloc[-2] <= stock_data['dpo_ma'].iloc[-2])
    ma_cross_down = (current_dpo < current_dpo_ma) and (stock_data['dpo'].iloc[-2] >= stock_data['dpo_ma'].iloc[-2])
    
    # Gerar sinais de compra e venda
    buy_signal = zero_cross_up or ma_cross_up
    sell_signal = zero_cross_down or ma_cross_down
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Detrended Price Oscillator")
        print(f" | Período: {period}")
        print(f" | Tipo de Média Móvel: {ma_type.upper()}")
        print(f" | DPO Atual: {current_dpo:.4f}")
        print(f" | DPO Anterior: {prev_dpo:.4f}")
        print(f" | Media Móvel do DPO: {current_dpo_ma:.4f}")
        print(f" | DPO Positivo: {is_positive}")
        print(f" | DPO Crescendo: {is_rising}")
        print(f" | Cruzamento de Zero (Para Cima): {zero_cross_up}")
        print(f" | Cruzamento de Zero (Para Baixo): {zero_cross_down}")
        print(f" | Cruzamento da MA (Para Cima): {ma_cross_up}")
        print(f" | Cruzamento da MA (Para Baixo): {ma_cross_down}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision