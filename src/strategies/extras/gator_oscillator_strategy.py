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

def getGatorOscillatorTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    jaw_period: int = 13,
    teeth_period: int = 8,
    lips_period: int = 5,
    jaw_shift: int = 8,
    teeth_shift: int = 5,
    lips_shift: int = 3,
    verbose: bool = True
):
    """
    Estratégia baseada em Gator Oscillator.
    
    O Gator Oscillator, desenvolvido por Bill Williams, é um complemento ao
    indicador Alligator e mostra a convergência/divergência das três linhas
    do Alligator (Jaw, Teeth, Lips).
    
    Parâmetros:
    - period: Período geral para cálculos (mantido para compatibilidade)
    - jaw_period: Período para cálculo da linha Jaw (mandíbula do Alligator)
    - teeth_period: Período para cálculo da linha Teeth (dentes do Alligator)
    - lips_period: Período para cálculo da linha Lips (lábios do Alligator)
    - jaw_shift: Deslocamento para a linha Jaw
    - teeth_shift: Deslocamento para a linha Teeth
    - lips_shift: Deslocamento para a linha Lips
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular o preço médio
    stock_data['median_price'] = (stock_data['high'] + stock_data['low']) / 2
    
    # Calcular as linhas do Alligator (médias móveis suavizadas)
    # Jaw (Mandíbula) - Linha Azul - SMMA 13 deslocada 8 períodos para frente
    # Teeth (Dentes) - Linha Vermelha - SMMA 8 deslocada 5 períodos para frente
    # Lips (Lábios) - Linha Verde - SMMA 5 deslocada 3 períodos para frente
    
    # Implementar SMMA (Smoothed Moving Average)
    def calculate_smma(data, period):
        smma = np.zeros_like(data)
        smma[:period] = np.nan
        
        # Inicializar com a média dos primeiros 'period' elementos
        smma[period - 1] = np.mean(data[:period])
        
        # Calcular o SMMA para os elementos restantes
        for i in range(period, len(data)):
            smma[i] = (smma[i - 1] * (period - 1) + data[i]) / period
        
        return smma
    
    # Calcular SMMA para as linhas do Alligator
    stock_data['jaw'] = calculate_smma(stock_data['median_price'].values, jaw_period)
    stock_data['teeth'] = calculate_smma(stock_data['median_price'].values, teeth_period)
    stock_data['lips'] = calculate_smma(stock_data['median_price'].values, lips_period)
    
    # Aplicar os deslocamentos
    stock_data['jaw_shifted'] = stock_data['jaw'].shift(jaw_shift)
    stock_data['teeth_shifted'] = stock_data['teeth'].shift(teeth_shift)
    stock_data['lips_shifted'] = stock_data['lips'].shift(lips_shift)
    
    # Calcular o Gator Oscillator
    # Parte superior - distância entre Jaw e Teeth
    stock_data['gator_up'] = np.abs(stock_data['jaw_shifted'] - stock_data['teeth_shifted'])
    
    # Parte inferior - distância entre Teeth e Lips
    stock_data['gator_down'] = -np.abs(stock_data['teeth_shifted'] - stock_data['lips_shifted'])
    
    # Extrair valores atuais
    current_gator_up = stock_data['gator_up'].iloc[-1]
    prev_gator_up = stock_data['gator_up'].iloc[-2] if len(stock_data) > 1 else current_gator_up
    
    current_gator_down = stock_data['gator_down'].iloc[-1]
    prev_gator_down = stock_data['gator_down'].iloc[-2] if len(stock_data) > 1 else current_gator_down
    
    current_jaw = stock_data['jaw_shifted'].iloc[-1]
    current_teeth = stock_data['teeth_shifted'].iloc[-1]
    current_lips = stock_data['lips_shifted'].iloc[-1]
    
    # Determinar o estado atual do Gator
    is_gator_expanding = (current_gator_up > prev_gator_up) and (current_gator_down < prev_gator_down)
    is_gator_contracting = (current_gator_up < prev_gator_up) and (current_gator_down > prev_gator_down)
    
    # Estado do Alligator
    is_alligator_sleeping = (abs(current_jaw - current_teeth) < 0.0001) and (abs(current_teeth - current_lips) < 0.0001)
    is_alligator_eating = (current_lips > current_teeth) and (current_teeth > current_jaw)
    is_alligator_sated = (current_lips < current_teeth) and (current_teeth < current_jaw)
    
    # Mudanças de estado do Gator
    # Acordando - Gator começa a expandir após contração
    is_waking_up = is_gator_expanding and not is_gator_expanding and not is_alligator_sleeping
    
    # Gerar sinais de compra e venda
    # Comprar: Alligator acordando e Gator expandindo
    # Vender: Alligator saciado ou começando a dormir
    
    buy_signal = is_waking_up or (is_gator_expanding and is_alligator_eating)
    sell_signal = is_alligator_sated or (is_gator_contracting and is_alligator_eating)
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Gator Oscillator")
        print(f" | Gator Superior: {current_gator_up:.4f}")
        print(f" | Gator Inferior: {current_gator_down:.4f}")
        print(f" | Jaw (Mandíbula): {current_jaw:.4f}")
        print(f" | Teeth (Dentes): {current_teeth:.4f}")
        print(f" | Lips (Lábios): {current_lips:.4f}")
        print(f" | Gator Expandindo: {is_gator_expanding}")
        print(f" | Gator Contraindo: {is_gator_contracting}")
        print(f" | Alligator Dormindo: {is_alligator_sleeping}")
        print(f" | Alligator Comendo: {is_alligator_eating}")
        print(f" | Alligator Saciado: {is_alligator_sated}")
        print(f" | Acordando: {is_waking_up}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision