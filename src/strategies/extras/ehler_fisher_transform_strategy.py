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

def getEhlerFisherTransformTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    signal_period: int = 3,
    verbose: bool = True
):
    """
    Estratégia baseada em Ehler Fisher Transform.
    
    A variante de Ehlers do Fisher Transform é uma adaptação que aplica a transformação
    a um indicador de preço processado para gerar sinais de negociação mais claros.
    
    Parâmetros:
    - period: Período para cálculo do Ehler Fisher Transform
    - signal_period: Período para a linha de sinal (valor defasado)
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular o valor médio (midpoint) do período
    stock_data['price_mid'] = (stock_data['high'] + stock_data['low']) / 2
    
    # Iniciar valores
    stock_data['max_h'] = np.nan
    stock_data['min_l'] = np.nan
    
    # Preencher a primeira linha com valores iniciais para evitar NaN
    if len(stock_data) > 0:
        stock_data.loc[0, 'max_h'] = stock_data.loc[0, 'price_mid']
        stock_data.loc[0, 'min_l'] = stock_data.loc[0, 'price_mid']
    
    # Calcular máximos e mínimos com um método adaptativo (Ehlers)
    # Isso usa uma técnica de alisamento que é mais responsiva a mudanças recentes
    alpha = 2.0 / (period + 1.0)
    
    for i in range(1, len(stock_data)):
        # Atualizar máximo
        max_value = stock_data.loc[i-1, 'max_h']
        if stock_data.loc[i, 'price_mid'] > max_value:
            max_value = stock_data.loc[i, 'price_mid']
        else:
            max_value = max_value - alpha * (max_value - stock_data.loc[i, 'price_mid'])
        stock_data.loc[i, 'max_h'] = max_value
        
        # Atualizar mínimo
        min_value = stock_data.loc[i-1, 'min_l']
        if stock_data.loc[i, 'price_mid'] < min_value:
            min_value = stock_data.loc[i, 'price_mid']
        else:
            min_value = min_value + alpha * (stock_data.loc[i, 'price_mid'] - min_value)
        stock_data.loc[i, 'min_l'] = min_value
    
    # Normalizar os preços para o intervalo [-1, 1]
    stock_data['value'] = np.nan
    
    # Calcular o valor normalizado
    for i in range(1, len(stock_data)):
        price_range = stock_data.loc[i, 'max_h'] - stock_data.loc[i, 'min_l']
        if price_range == 0:
            stock_data.loc[i, 'value'] = 0
        else:
            # Fórmula de Ehlers para normalização
            stock_data.loc[i, 'value'] = 2 * ((stock_data.loc[i, 'price_mid'] - stock_data.loc[i, 'min_l']) / price_range - 0.5)
    
    # Suavizar o valor normalizado com uma média móvel
    stock_data['smooth_value'] = stock_data['value'].rolling(window=period).mean().fillna(0)
    
    # Limitar o valor a +/- 0.999 para evitar infinitos na transformação
    stock_data['smooth_value'] = np.clip(stock_data['smooth_value'], -0.999, 0.999)
    
    # Aplicar a transformação de Fisher
    stock_data['fisher'] = 0.5 * np.log((1 + stock_data['smooth_value']) / (1 - stock_data['smooth_value']))
    
    # Criar a linha de sinal (valor defasado)
    stock_data['fisher_signal'] = stock_data['fisher'].shift(signal_period)
    
    # Extrair valores atuais
    current_fisher = stock_data['fisher'].iloc[-1]
    prev_fisher = stock_data['fisher'].iloc[-2] if len(stock_data) > 1 else current_fisher
    
    current_signal = stock_data['fisher_signal'].iloc[-1]
    prev_signal = stock_data['fisher_signal'].iloc[-2] if len(stock_data) > 1 else current_signal
    
    # Determinar o estado atual do Fisher Transform
    is_positive = current_fisher > 0
    is_rising = current_fisher > prev_fisher
    
    # Detectar cruzamento de zero
    zero_cross_up = (current_fisher > 0) and (prev_fisher < 0)
    zero_cross_down = (current_fisher < 0) and (prev_fisher > 0)
    
    # Detectar cruzamento com a linha de sinal
    signal_cross_up = (current_fisher > current_signal) and (prev_fisher <= prev_signal)
    signal_cross_down = (current_fisher < current_signal) and (prev_fisher >= prev_signal)
    
    # Detectar extremos (valores acima de 1.5 ou abaixo de -1.5 são considerados extremos)
    extreme_high = current_fisher > 1.5 and not is_rising
    extreme_low = current_fisher < -1.5 and is_rising
    
    # Gerar sinais de compra e venda
    buy_signal = zero_cross_up or signal_cross_up or extreme_low
    sell_signal = zero_cross_down or signal_cross_down or extreme_high
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Ehler Fisher Transform")
        print(f" | Período: {period}")
        print(f" | Período do Sinal: {signal_period}")
        print(f" | Fisher Atual: {current_fisher:.4f}")
        print(f" | Fisher Anterior: {prev_fisher:.4f}")
        print(f" | Sinal Atual: {current_signal:.4f}")
        print(f" | Sinal Anterior: {prev_signal:.4f}")
        print(f" | Fisher Positivo: {is_positive}")
        print(f" | Fisher Crescendo: {is_rising}")
        print(f" | Cruzamento de Zero (Para Cima): {zero_cross_up}")
        print(f" | Cruzamento de Zero (Para Baixo): {zero_cross_down}")
        print(f" | Cruzamento do Sinal (Para Cima): {signal_cross_up}")
        print(f" | Cruzamento do Sinal (Para Baixo): {signal_cross_down}")
        print(f" | Extremo Superior: {extreme_high}")
        print(f" | Extremo Inferior: {extreme_low}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision