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

def getKAMATradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    fast_efr: int = 2,
    slow_efr: int = 30,
    signal_period: int = 9,
    verbose: bool = True
):
    """
    Estratégia baseada em KAMA (Kaufman's Adaptive Moving Average).
    
    O KAMA se adapta automaticamente à volatilidade e ruído do mercado,
    movendo-se mais rapidamente quando o preço se move em tendência 
    e mais lentamente em mercados laterais ou voláteis.
    
    Parâmetros:
    - period: Período para cálculo do KAMA e do Efficiency Ratio
    - fast_efr: Período rápido para o Efficiency Ratio (geralmente 2)
    - slow_efr: Período lento para o Efficiency Ratio (geralmente 30)
    - signal_period: Período para o KAMA de sinal (para crossover)
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos a coluna 'close'
    if 'close' not in stock_data.columns:
        # Tentar converter para minúsculas
        stock_data.columns = [col.lower() for col in stock_data.columns]
        if 'close' not in stock_data.columns:
            raise ValueError("Coluna 'close' não encontrada nos dados.")
    
    # Calcular a mudança de preço direta
    stock_data['price_change'] = stock_data['close'].diff(1)
    
    # Calcular o "Efficiency Ratio" (ER)
    stock_data['direction'] = abs(stock_data['close'] - stock_data['close'].shift(period))
    stock_data['volatility'] = abs(stock_data['price_change']).rolling(window=period).sum()
    stock_data['efficiency_ratio'] = stock_data['direction'] / stock_data['volatility']
    
    # Substituir valores NaN e infinitos no ER
    stock_data['efficiency_ratio'] = stock_data['efficiency_ratio'].replace([np.inf, -np.inf], np.nan)
    stock_data['efficiency_ratio'] = stock_data['efficiency_ratio'].fillna(0)
    
    # Calcular as constantes suavizadas
    fast_sc = 2.0 / (fast_efr + 1.0)
    slow_sc = 2.0 / (slow_efr + 1.0)
    
    # Calcular o fator de suavização
    stock_data['smooth_factor'] = (stock_data['efficiency_ratio'] * (fast_sc - slow_sc) + slow_sc) ** 2
    
    # Inicializar o KAMA
    stock_data['kama'] = np.nan
    
    # Definir o primeiro valor KAMA
    # Normalmente, seria a média dos primeiros 'period' valores
    if len(stock_data) > period:
        stock_data.loc[stock_data.index[period-1], 'kama'] = stock_data.loc[stock_data.index[period-1], 'close']
    
    # Calcular o KAMA para os pontos restantes
    for i in range(period, len(stock_data)):
        prev_kama = stock_data.loc[stock_data.index[i-1], 'kama']
        if np.isnan(prev_kama):
            stock_data.loc[stock_data.index[i], 'kama'] = stock_data.loc[stock_data.index[i], 'close']
        else:
            current_sf = stock_data.loc[stock_data.index[i], 'smooth_factor']
            current_close = stock_data.loc[stock_data.index[i], 'close']
            stock_data.loc[stock_data.index[i], 'kama'] = prev_kama + current_sf * (current_close - prev_kama)
    
    # Calcular o KAMA de sinal (média móvel do KAMA)
    stock_data['kama_signal'] = stock_data['kama'].rolling(window=signal_period).mean()
    
    # Calcular a inclinação do KAMA para determinar a tendência
    stock_data['kama_slope'] = stock_data['kama'].diff()
    
    # Extrair valores atuais
    current_close = stock_data['close'].iloc[-1]
    current_kama = stock_data['kama'].iloc[-1]
    current_kama_signal = stock_data['kama_signal'].iloc[-1]
    current_kama_slope = stock_data['kama_slope'].iloc[-1]
    current_er = stock_data['efficiency_ratio'].iloc[-1]
    
    # Valores anteriores para determinar cruzamentos
    prev_close = stock_data['close'].iloc[-2] if len(stock_data) > 1 else current_close
    prev_kama = stock_data['kama'].iloc[-2] if len(stock_data) > 1 else current_kama
    prev_kama_signal = stock_data['kama_signal'].iloc[-2] if len(stock_data) > 1 else current_kama_signal
    
    # Determinar o estado atual do KAMA
    is_kama_rising = current_kama_slope > 0
    
    # Detectar cruzamento de preço e KAMA
    price_cross_up = (current_close > current_kama) and (prev_close <= prev_kama)
    price_cross_down = (current_close < current_kama) and (prev_close >= prev_kama)
    
    # Detectar cruzamento de KAMA e sua linha de sinal
    kama_cross_up = (current_kama > current_kama_signal) and (prev_kama <= prev_kama_signal)
    kama_cross_down = (current_kama < current_kama_signal) and (prev_kama >= prev_kama_signal)
    
    # Gerar sinais de compra e venda
    # Comprar: KAMA subindo e preço cruza acima do KAMA, ou KAMA cruza acima da linha de sinal
    # Vender: KAMA descendo e preço cruza abaixo do KAMA, ou KAMA cruza abaixo da linha de sinal
    
    buy_signal = (is_kama_rising and price_cross_up) or kama_cross_up
    sell_signal = (not is_kama_rising and price_cross_down) or kama_cross_down
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: KAMA")
        print(f" | Período: {period}")
        print(f" | Fast Efficiency Ratio: {fast_efr}")
        print(f" | Slow Efficiency Ratio: {slow_efr}")
        print(f" | Efficiency Ratio Atual: {current_er:.4f}")
        print(f" | Preço Atual: {current_close:.2f}")
        print(f" | KAMA: {current_kama:.2f}")
        print(f" | KAMA Signal: {current_kama_signal:.2f}")
        print(f" | KAMA Crescendo: {is_kama_rising}")
        print(f" | Cruzamento de Preço e KAMA (Para Cima): {price_cross_up}")
        print(f" | Cruzamento de Preço e KAMA (Para Baixo): {price_cross_down}")
        print(f" | Cruzamento de KAMA e Sinal (Para Cima): {kama_cross_up}")
        print(f" | Cruzamento de KAMA e Sinal (Para Baixo): {kama_cross_down}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision