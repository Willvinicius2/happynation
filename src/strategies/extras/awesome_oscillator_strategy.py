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

def getAwesomeOscillatorTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    fast_period: int = 5,
    slow_period: int = 34,
    verbose: bool = True
):
    """
    Estratégia baseada em Awesome Oscillator.
    
    O Awesome Oscillator (AO) é um indicador de momentum que mostra a diferença entre
    uma média móvel simples rápida e uma média móvel simples lenta do ponto médio.
    
    Parâmetros:
    - period: Período geral para cálculos (mantido para compatibilidade)
    - fast_period: Período para a média móvel rápida
    - slow_period: Período para a média móvel lenta
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular o ponto médio dos preços
    stock_data['midpoint'] = (stock_data['high'] + stock_data['low']) / 2
    
    # Calcular as médias móveis rápida e lenta do ponto médio
    stock_data['ao_fast'] = stock_data['midpoint'].rolling(window=fast_period).mean()
    stock_data['ao_slow'] = stock_data['midpoint'].rolling(window=slow_period).mean()
    
    # Calcular o Awesome Oscillator
    stock_data['ao'] = stock_data['ao_fast'] - stock_data['ao_slow']
    
    # Armazenar os valores anteriores do AO para detectar cruzamentos e padrões
    stock_data['ao_prev'] = stock_data['ao'].shift(1)
    stock_data['ao_prev2'] = stock_data['ao'].shift(2)
    
    # Calcular a diferença entre valores consecutivos do AO
    stock_data['ao_diff'] = stock_data['ao'] - stock_data['ao_prev']
    stock_data['ao_diff_prev'] = stock_data['ao_prev'] - stock_data['ao_prev2']
    
    # Extrair valores atuais
    current_ao = stock_data['ao'].iloc[-1]
    prev_ao = stock_data['ao_prev'].iloc[-1]
    prev2_ao = stock_data['ao_prev2'].iloc[-1]
    
    # Determinar o estado atual do AO
    is_positive = current_ao > 0
    is_rising = current_ao > prev_ao
    
    # Detectar cruzamento de zero
    zero_cross_up = (current_ao > 0) and (prev_ao < 0)
    zero_cross_down = (current_ao < 0) and (prev_ao > 0)
    
    # Detectar padrões de Saucer
    saucer_buy = (current_ao > 0) and (current_ao > prev_ao) and (prev_ao < prev2_ao)
    saucer_sell = (current_ao < 0) and (current_ao < prev_ao) and (prev_ao > prev2_ao)
    
    # Detectar padrão de Twin Peaks
    # (requer mais dados históricos, simplificado aqui)
    twin_peaks_buy = False
    twin_peaks_sell = False
    
    if len(stock_data) > 5:
        # Verificar últimos 5 valores para um padrão simplificado de Twin Peaks
        last_5_ao = stock_data['ao'].iloc[-5:].values
        last_5_diff = np.diff(last_5_ao)
        
        # Padrão simplificado: dois picos negativos com o segundo menos negativo
        if current_ao < 0 and np.sum(last_5_diff > 0) >= 3:
            twin_peaks_buy = True
        
        # Padrão simplificado: dois picos positivos com o segundo menos positivo
        if current_ao > 0 and np.sum(last_5_diff < 0) >= 3:
            twin_peaks_sell = True
    
    # Gerar sinais de compra e venda
    buy_signal = zero_cross_up or saucer_buy or twin_peaks_buy
    sell_signal = zero_cross_down or saucer_sell or twin_peaks_sell
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Awesome Oscillator")
        print(f" | AO Atual: {current_ao:.4f}")
        print(f" | AO Anterior: {prev_ao:.4f}")
        print(f" | Positivo: {is_positive}")
        print(f" | Crescendo: {is_rising}")
        print(f" | Cruzamento de Zero (Para Cima): {zero_cross_up}")
        print(f" | Cruzamento de Zero (Para Baixo): {zero_cross_down}")
        print(f" | Padrão Saucer (Compra): {saucer_buy}")
        print(f" | Padrão Saucer (Venda): {saucer_sell}")
        print(f" | Padrão Twin Peaks (Compra): {twin_peaks_buy}")
        print(f" | Padrão Twin Peaks (Venda): {twin_peaks_sell}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision