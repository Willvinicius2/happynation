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

def getPPOTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
    verbose: bool = True
):
    """
    Estratégia baseada em PPO (Percentage Price Oscillator).
    
    O PPO é semelhante ao MACD, mas calcula a diferença percentual entre duas EMAs,
    o que o torna mais comparável entre diferentes valores mobiliários.
    
    Parâmetros:
    - period: Período geral para cálculos (mantido para compatibilidade)
    - fast_period: Período para a EMA rápida
    - slow_period: Período para a EMA lenta
    - signal_period: Período para a linha de sinal
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos a coluna 'close'
    if 'close' not in stock_data.columns:
        # Tentar converter para minúsculas
        stock_data.columns = [col.lower() for col in stock_data.columns]
        if 'close' not in stock_data.columns:
            raise ValueError("Coluna 'close' não encontrada nos dados.")
    
    # Calcular EMA rápida e lenta
    stock_data['ema_fast'] = stock_data['close'].ewm(span=fast_period, adjust=False).mean()
    stock_data['ema_slow'] = stock_data['close'].ewm(span=slow_period, adjust=False).mean()
    
    # Calcular PPO [(EMA_Rápida - EMA_Lenta) / EMA_Lenta * 100]
    stock_data['ppo'] = ((stock_data['ema_fast'] - stock_data['ema_slow']) / stock_data['ema_slow']) * 100
    
    # Calcular linha de sinal (EMA do PPO)
    stock_data['ppo_signal'] = stock_data['ppo'].ewm(span=signal_period, adjust=False).mean()
    
    # Calcular histograma PPO
    stock_data['ppo_histogram'] = stock_data['ppo'] - stock_data['ppo_signal']
    
    # Extrair valores atuais
    current_ppo = stock_data['ppo'].iloc[-1]
    current_signal = stock_data['ppo_signal'].iloc[-1]
    current_histogram = stock_data['ppo_histogram'].iloc[-1]
    
    # Valores anteriores para determinar cruzamentos e divergências
    prev_ppo = stock_data['ppo'].iloc[-2] if len(stock_data) > 1 else current_ppo
    prev_signal = stock_data['ppo_signal'].iloc[-2] if len(stock_data) > 1 else current_signal
    prev_histogram = stock_data['ppo_histogram'].iloc[-2] if len(stock_data) > 1 else current_histogram
    
    # Verificar valores mais antigos para divergências
    if len(stock_data) >= period:
        period_ago_close = stock_data['close'].iloc[-period]
        period_ago_ppo = stock_data['ppo'].iloc[-period]
        
        # Divergência: preço sobe mas PPO cai = sinal de venda
        # Divergência: preço cai mas PPO sobe = sinal de compra
        price_up = stock_data['close'].iloc[-1] > period_ago_close
        ppo_up = current_ppo > period_ago_ppo
        
        divergence_buy = not price_up and ppo_up
        divergence_sell = price_up and not ppo_up
    else:
        divergence_buy = False
        divergence_sell = False
    
    # Determinar o estado atual do PPO
    is_positive = current_ppo > 0
    is_rising = current_ppo > prev_ppo
    
    # Detectar cruzamento de zero
    zero_cross_up = (current_ppo > 0) and (prev_ppo < 0)
    zero_cross_down = (current_ppo < 0) and (prev_ppo > 0)
    
    # Detectar cruzamento com a linha de sinal
    signal_cross_up = (current_ppo > current_signal) and (prev_ppo <= prev_signal)
    signal_cross_down = (current_ppo < current_signal) and (prev_ppo >= prev_signal)
    
    # Verificar mudança de direção do histograma
    histogram_reversal_up = (current_histogram > 0) and (prev_histogram < 0)
    histogram_reversal_down = (current_histogram < 0) and (prev_histogram > 0)
    
    # Gerar sinais de compra e venda
    # Comprar:
    # 1. PPO cruza acima da linha de sinal
    # 2. PPO cruza acima de zero
    # 3. Reversão do histograma para positivo
    # 4. Divergência de compra
    
    # Vender:
    # 1. PPO cruza abaixo da linha de sinal
    # 2. PPO cruza abaixo de zero
    # 3. Reversão do histograma para negativo
    # 4. Divergência de venda
    
    buy_signal = signal_cross_up or zero_cross_up or histogram_reversal_up or divergence_buy
    sell_signal = signal_cross_down or zero_cross_down or histogram_reversal_down or divergence_sell
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: PPO (Percentage Price Oscillator)")
        print(f" | Período EMA Rápida: {fast_period}")
        print(f" | Período EMA Lenta: {slow_period}")
        print(f" | Período do Sinal: {signal_period}")
        print(f" | PPO Atual: {current_ppo:.4f}%")
        print(f" | Sinal Atual: {current_signal:.4f}%")
        print(f" | Histograma Atual: {current_histogram:.4f}%")
        print(f" | PPO Positivo: {is_positive}")
        print(f" | PPO Crescendo: {is_rising}")
        print(f" | Cruzamento de Zero (Para Cima): {zero_cross_up}")
        print(f" | Cruzamento de Zero (Para Baixo): {zero_cross_down}")
        print(f" | Cruzamento de Sinal (Para Cima): {signal_cross_up}")
        print(f" | Cruzamento de Sinal (Para Baixo): {signal_cross_down}")
        print(f" | Reversão do Histograma (Para Cima): {histogram_reversal_up}")
        print(f" | Reversão do Histograma (Para Baixo): {histogram_reversal_down}")
        print(f" | Divergência Compra: {divergence_buy}")
        print(f" | Divergência Venda: {divergence_sell}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision