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

def getFisherTransformTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    signal_period: int = 3,
    verbose: bool = True
):
    """
    Estratégia baseada em Fisher Transform.
    
    O Fisher Transform é um indicador técnico que normaliza os preços e os transforma
    em uma distribuição normal gaussiana, facilitando a identificação de pontos de reversão.
    
    Parâmetros:
    - period: Período para cálculo do Fisher Transform
    - signal_period: Período para a linha de sinal (média móvel do Fisher Transform)
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
    
    # Encontrar o máximo e mínimo para o período
    stock_data['period_high'] = stock_data['price_mid'].rolling(window=period).max()
    stock_data['period_low'] = stock_data['price_mid'].rolling(window=period).min()
    
    # Normalizar os preços para o intervalo [-1, 1]
    stock_data['value'] = np.nan
    
    # Evitar divisão por zero
    for i in range(period-1, len(stock_data)):
        price_range = stock_data['period_high'].iloc[i] - stock_data['period_low'].iloc[i]
        if price_range == 0:
            stock_data.loc[i, 'value'] = 0
        else:
            stock_data.loc[i, 'value'] = 2 * ((stock_data['price_mid'].iloc[i] - stock_data['period_low'].iloc[i]) / price_range - 0.5)
    
    # Calcular o Fisher Transform
    stock_data['fisher_input'] = stock_data['value'].rolling(window=period).mean()
    
    # Limitar o valor a +/- 0.999 para evitar infinitos na transformação
    stock_data['fisher_input'] = np.clip(stock_data['fisher_input'], -0.999, 0.999)
    
    # Aplicar a transformação de Fisher
    stock_data['fisher'] = 0.5 * np.log((1 + stock_data['fisher_input']) / (1 - stock_data['fisher_input']))
    
    # Calcular o sinal (média móvel do Fisher Transform)
    stock_data['fisher_signal'] = stock_data['fisher'].rolling(window=signal_period).mean()
    
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
    
    # Detectar extremos (valores acima de 2 ou abaixo de -2 são considerados extremos)
    extreme_high = current_fisher > 2.0 and not is_rising
    extreme_low = current_fisher < -2.0 and is_rising
    
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
        print(f"📊 Estratégia: Fisher Transform")
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