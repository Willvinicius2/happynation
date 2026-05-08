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

def getKeltnerChannelTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    atr_period: int = 10,
    multiplier: float = 2.0,
    verbose: bool = True
):
    """
    Estratégia baseada em Keltner Channel.
    
    O Keltner Channel é um indicador técnico composto por três linhas:
    - Linha central: EMA do preço de fechamento
    - Linha superior: Linha central + (multiplicador * ATR)
    - Linha inferior: Linha central - (multiplicador * ATR)
    
    Parâmetros:
    - period: Período para cálculo da EMA central
    - atr_period: Período para cálculo do ATR
    - multiplier: Multiplicador para determinar a largura do canal
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low', 'close']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular a EMA para a linha central
    stock_data['ema'] = stock_data['close'].ewm(span=period, adjust=False).mean()
    
    # Calcular o ATR
    # Primeiro, calcular o True Range (TR)
    stock_data['high_low'] = stock_data['high'] - stock_data['low']
    stock_data['high_close'] = np.abs(stock_data['high'] - stock_data['close'].shift(1))
    stock_data['low_close'] = np.abs(stock_data['low'] - stock_data['close'].shift(1))
    stock_data['tr'] = stock_data[['high_low', 'high_close', 'low_close']].max(axis=1)
    
    # Em seguida, calcular o ATR como média móvel do TR
    stock_data['atr'] = stock_data['tr'].rolling(window=atr_period).mean()
    
    # Calcular as bandas superior e inferior do Keltner Channel
    stock_data['upper_band'] = stock_data['ema'] + (multiplier * stock_data['atr'])
    stock_data['lower_band'] = stock_data['ema'] - (multiplier * stock_data['atr'])
    
    # Extrair valores atuais
    current_close = stock_data['close'].iloc[-1]
    current_ema = stock_data['ema'].iloc[-1]
    current_upper = stock_data['upper_band'].iloc[-1]
    current_lower = stock_data['lower_band'].iloc[-1]
    current_width = (current_upper - current_lower) / current_ema  # Largura normalizada
    
    # Determinar posição do preço em relação ao canal
    is_above_upper = current_close > current_upper
    is_below_lower = current_close < current_lower
    is_inside_channel = not is_above_upper and not is_below_lower
    
    # Verificar valores anteriores para detectar cruzamentos
    prev_close = stock_data['close'].iloc[-2] if len(stock_data) > 1 else current_close
    prev_upper = stock_data['upper_band'].iloc[-2] if len(stock_data) > 1 else current_upper
    prev_lower = stock_data['lower_band'].iloc[-2] if len(stock_data) > 1 else current_lower
    
    # Detectar cruzamentos com as bandas
    upper_cross_up = is_above_upper and (prev_close <= prev_upper)
    upper_cross_down = not is_above_upper and (prev_close > prev_upper)
    lower_cross_down = is_below_lower and (prev_close >= prev_lower)
    lower_cross_up = not is_below_lower and (prev_close < prev_lower)
    
    # Detectar a direção da EMA
    stock_data['ema_slope'] = stock_data['ema'].diff()
    current_ema_slope = stock_data['ema_slope'].iloc[-1]
    is_ema_rising = current_ema_slope > 0
    
    # Gerar sinais de compra e venda
    # Comprar: Preço cruza acima da banda inferior enquanto EMA está subindo
    # Vender: Preço cruza abaixo da banda superior enquanto EMA está descendo
    
    buy_signal = lower_cross_up and is_ema_rising
    sell_signal = upper_cross_down and not is_ema_rising
    
    # Sinais adicionais
    # Comprar forte: Preço cruza acima da banda superior e EMA está subindo
    # Vender forte: Preço cruza abaixo da banda inferior e EMA está descendo
    
    strong_buy_signal = upper_cross_up and is_ema_rising
    strong_sell_signal = lower_cross_down and not is_ema_rising
    
    # Decisão final
    trade_decision = None
    
    if buy_signal or strong_buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal or strong_sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Keltner Channel")
        print(f" | Período EMA: {period}")
        print(f" | Período ATR: {atr_period}")
        print(f" | Multiplicador: {multiplier}")
        print(f" | Preço Atual: {current_close:.2f}")
        print(f" | EMA: {current_ema:.2f}")
        print(f" | Banda Superior: {current_upper:.2f}")
        print(f" | Banda Inferior: {current_lower:.2f}")
        print(f" | Largura do Canal: {current_width:.4f}")
        print(f" | EMA Crescendo: {is_ema_rising}")
        print(f" | Preço Acima da Banda Superior: {is_above_upper}")
        print(f" | Preço Abaixo da Banda Inferior: {is_below_lower}")
        print(f" | Preço Dentro do Canal: {is_inside_channel}")
        print(f" | Cruzamento da Banda Superior (Para Cima): {upper_cross_up}")
        print(f" | Cruzamento da Banda Superior (Para Baixo): {upper_cross_down}")
        print(f" | Cruzamento da Banda Inferior (Para Cima): {lower_cross_up}")
        print(f" | Cruzamento da Banda Inferior (Para Baixo): {lower_cross_down}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision