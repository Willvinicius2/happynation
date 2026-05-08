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

def getMovingAverageEnvelopeTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    envelope_percentage: float = 2.5,
    ma_type: str = 'sma',
    verbose: bool = True
):
    """
    Estratégia baseada em Moving Average Envelope.
    
    Os envelopes de média móvel consistem em bandas colocadas acima e abaixo 
    de uma média móvel, a uma distância fixa (percentual).
    
    Parâmetros:
    - period: Período para cálculo da média móvel
    - envelope_percentage: Percentual para determinar as bandas superior e inferior
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
    
    # Calcular as bandas superior e inferior do envelope
    envelope_factor = envelope_percentage / 100.0
    stock_data['upper_envelope'] = stock_data['ma'] * (1 + envelope_factor)
    stock_data['lower_envelope'] = stock_data['ma'] * (1 - envelope_factor)
    
    # Extrair valores atuais
    current_close = stock_data['close'].iloc[-1]
    current_ma = stock_data['ma'].iloc[-1]
    current_upper = stock_data['upper_envelope'].iloc[-1]
    current_lower = stock_data['lower_envelope'].iloc[-1]
    
    # Determinar posição do preço em relação ao envelope
    is_above_upper = current_close > current_upper
    is_below_lower = current_close < current_lower
    is_inside_envelope = not is_above_upper and not is_below_lower
    
    # Verificar valores anteriores para detectar cruzamentos
    prev_close = stock_data['close'].iloc[-2] if len(stock_data) > 1 else current_close
    prev_upper = stock_data['upper_envelope'].iloc[-2] if len(stock_data) > 1 else current_upper
    prev_lower = stock_data['lower_envelope'].iloc[-2] if len(stock_data) > 1 else current_lower
    
    # Detectar cruzamentos com as bandas do envelope
    upper_cross_up = (current_close > current_upper) and (prev_close <= prev_upper)
    upper_cross_down = (current_close < current_upper) and (prev_close >= prev_upper)
    lower_cross_down = (current_close < current_lower) and (prev_close >= prev_lower)
    lower_cross_up = (current_close > current_lower) and (prev_close <= prev_lower)
    
    # Detectar a direção da média móvel
    stock_data['ma_slope'] = stock_data['ma'].diff()
    current_ma_slope = stock_data['ma_slope'].iloc[-1]
    is_ma_rising = current_ma_slope > 0
    
    # Gerar sinais de compra e venda
    # Comprar: Preço cruza acima da banda inferior enquanto média móvel está subindo
    # Vender: Preço cruza abaixo da banda superior enquanto média móvel está descendo
    
    buy_signal = lower_cross_up and is_ma_rising
    sell_signal = upper_cross_down and not is_ma_rising
    
    # Estratégia de retorno à média (mean reversion)
    # Comprar quando o preço está abaixo da banda inferior (sobrevendido)
    # Vender quando o preço está acima da banda superior (sobrecomprado)
    
    mean_reversion_buy = is_below_lower
    mean_reversion_sell = is_above_upper
    
    # Decisão final
    trade_decision = None
    
    if buy_signal or mean_reversion_buy:
        trade_decision = True  # Comprar
    elif sell_signal or mean_reversion_sell:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Moving Average Envelope")
        print(f" | Período: {period}")
        print(f" | Tipo de Média Móvel: {ma_type.upper()}")
        print(f" | Percentual do Envelope: {envelope_percentage}%")
        print(f" | Preço Atual: {current_close:.2f}")
        print(f" | Média Móvel: {current_ma:.2f}")
        print(f" | Banda Superior: {current_upper:.2f}")
        print(f" | Banda Inferior: {current_lower:.2f}")
        print(f" | MA Crescendo: {is_ma_rising}")
        print(f" | Preço Acima da Banda Superior: {is_above_upper}")
        print(f" | Preço Abaixo da Banda Inferior: {is_below_lower}")
        print(f" | Preço Dentro do Envelope: {is_inside_envelope}")
        print(f" | Cruzamento da Banda Superior (Para Cima): {upper_cross_up}")
        print(f" | Cruzamento da Banda Superior (Para Baixo): {upper_cross_down}")
        print(f" | Cruzamento da Banda Inferior (Para Cima): {lower_cross_up}")
        print(f" | Cruzamento da Banda Inferior (Para Baixo): {lower_cross_down}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision