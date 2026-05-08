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

def getWilliamsAlligatorTradeStrategy(
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
    Estratégia baseada em Williams Alligator.
    
    O Alligator é um indicador técnico desenvolvido por Bill Williams que usa
    três médias móveis suavizadas deslocadas para identificar tendências.
    
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
    required_columns = ['high', 'low', 'close']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular o preço médio
    stock_data['median_price'] = (stock_data['high'] + stock_data['low']) / 2
    
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
    
    # Calcular as três linhas do Alligator:
    # Jaw (Mandíbula) - Linha Azul: SMMA de 13 períodos deslocada 8 barras para frente
    # Teeth (Dentes) - Linha Vermelha: SMMA de 8 períodos deslocada 5 barras para frente
    # Lips (Lábios) - Linha Verde: SMMA de 5 períodos deslocada 3 barras para frente
    
    # Calcular SMMAs
    smma_jaw = calculate_smma(stock_data['median_price'].values, jaw_period)
    smma_teeth = calculate_smma(stock_data['median_price'].values, teeth_period)
    smma_lips = calculate_smma(stock_data['median_price'].values, lips_period)
    
    # Criar as colunas do Alligator
    stock_data['jaw'] = pd.Series(smma_jaw, index=stock_data.index)
    stock_data['teeth'] = pd.Series(smma_teeth, index=stock_data.index)
    stock_data['lips'] = pd.Series(smma_lips, index=stock_data.index)
    
    # Aplicar os deslocamentos (shift)
    stock_data['jaw_shifted'] = stock_data['jaw'].shift(-jaw_shift)  # Futuro
    stock_data['teeth_shifted'] = stock_data['teeth'].shift(-teeth_shift)  # Futuro
    stock_data['lips_shifted'] = stock_data['lips'].shift(-lips_shift)  # Futuro
    
    # Extrair valores atuais
    current_close = stock_data['close'].iloc[-1]
    current_jaw = stock_data['jaw_shifted'].iloc[-1]
    current_teeth = stock_data['teeth_shifted'].iloc[-1]
    current_lips = stock_data['lips_shifted'].iloc[-1]
    
    # Verificar valores anteriores para detectar cruzamentos
    prev_close = stock_data['close'].iloc[-2] if len(stock_data) > 1 else current_close
    prev_jaw = stock_data['jaw_shifted'].iloc[-2] if len(stock_data) > 1 else current_jaw
    prev_teeth = stock_data['teeth_shifted'].iloc[-2] if len(stock_data) > 1 else current_teeth
    prev_lips = stock_data['lips_shifted'].iloc[-2] if len(stock_data) > 1 else current_lips
    
    # Determinar os estados do Alligator
    
    # 1. Alligator Sleeping (dormindo)
    # Quando as três linhas estão entrelaçadas ou muito próximas
    lines_entangled = (abs(current_jaw - current_teeth) < 0.0001) and (abs(current_teeth - current_lips) < 0.0001)
    
    # 2. Alligator Awakening (acordando)
    # Quando as linhas começam a se separar após um período de entrelaçamento
    was_entangled = (abs(prev_jaw - prev_teeth) < 0.0001) and (abs(prev_teeth - prev_lips) < 0.0001)
    is_separating = not lines_entangled and was_entangled
    
    # 3. Alligator Eating (comendo)
    # Quando as três linhas estão ordenadas corretamente para uma tendência
    # Tendência de alta: Lips > Teeth > Jaw
    # Tendência de baixa: Lips < Teeth < Jaw
    uptrend_eating = (current_lips > current_teeth) and (current_teeth > current_jaw)
    downtrend_eating = (current_lips < current_teeth) and (current_teeth < current_jaw)
    
    # 4. Alligator Sated (saciado)
    # Quando as linhas começam a se aproximar novamente após um período de "alimentação"
    was_eating_up = (prev_lips > prev_teeth) and (prev_teeth > prev_jaw)
    was_eating_down = (prev_lips < prev_teeth) and (prev_teeth < prev_jaw)
    is_converging = (was_eating_up and not uptrend_eating) or (was_eating_down and not downtrend_eating)
    
    # Verificar a posição do preço em relação às linhas do Alligator
    price_above_all = current_close > max(current_jaw, current_teeth, current_lips)
    price_below_all = current_close < min(current_jaw, current_teeth, current_lips)
    
    # Detectar cruzamentos
    cross_above_lips = (current_close > current_lips) and (prev_close <= prev_lips)
    cross_below_lips = (current_close < current_lips) and (prev_close >= prev_lips)
    
    # Gerar sinais de compra e venda segundo Bill Williams
    
    # Comprar (Primeira mordida do Alligator):
    # 1. O Alligator está dormindo ou começando a acordar (linhas entrelaçadas ou começando a separar)
    # 2. O preço cruza acima da linha Lips (lábios) - a primeira linha a ser cruzada
    # 3. As linhas estão começando a se separar na ordem correta para uma tendência de alta
    
    # Vender (Primeira mordida do Alligator):
    # 1. O Alligator está dormindo ou começando a acordar
    # 2. O preço cruza abaixo da linha Lips
    # 3. As linhas estão começando a se separar na ordem correta para uma tendência de baixa
    
    # Sinais de compra e venda
    first_bite_buy = (lines_entangled or is_separating) and cross_above_lips and current_lips >= current_teeth
    first_bite_sell = (lines_entangled or is_separating) and cross_below_lips and current_lips <= current_teeth
    
    # Comprar (Durante a alimentação):
    # 1. O Alligator está comendo (tendência de alta estabelecida)
    # 2. O preço está acima de todas as linhas
    
    # Vender (Durante a alimentação):
    # 1. O Alligator está comendo (tendência de baixa estabelecida)
    # 2. O preço está abaixo de todas as linhas
    
    eating_buy = uptrend_eating and price_above_all
    eating_sell = downtrend_eating and price_below_all
    
    # Comprar:
    # 1. Primeira mordida do Alligator na tendência de alta
    # 2. Durante a alimentação na tendência de alta
    
    # Vender:
    # 1. Primeira mordida do Alligator na tendência de baixa
    # 2. Durante a alimentação na tendência de baixa
    # 3. Alligator está saciado após tendência de alta
    
    buy_signal = first_bite_buy or eating_buy
    sell_signal = first_bite_sell or eating_sell or (is_converging and was_eating_up)
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Williams Alligator")
        print(f" | Períodos (Jaw/Teeth/Lips): {jaw_period}/{teeth_period}/{lips_period}")
        print(f" | Deslocamentos (Jaw/Teeth/Lips): {jaw_shift}/{teeth_shift}/{lips_shift}")
        print(f" | Preço Atual: {current_close:.2f}")
        print(f" | Jaw (Mandíbula): {current_jaw:.4f}")
        print(f" | Teeth (Dentes): {current_teeth:.4f}")
        print(f" | Lips (Lábios): {current_lips:.4f}")
        print(f" | Alligator Dormindo: {lines_entangled}")
        print(f" | Alligator Acordando: {is_separating}")
        print(f" | Alligator Comendo (Alta): {uptrend_eating}")
        print(f" | Alligator Comendo (Baixa): {downtrend_eating}")
        print(f" | Alligator Saciado: {is_converging}")
        print(f" | Preço Acima de Todas as Linhas: {price_above_all}")
        print(f" | Preço Abaixo de Todas as Linhas: {price_below_all}")
        print(f" | Cruzamento Acima dos Lips: {cross_above_lips}")
        print(f" | Cruzamento Abaixo dos Lips: {cross_below_lips}")
        print(f" | Primeira Mordida (Compra): {first_bite_buy}")
        print(f" | Primeira Mordida (Venda): {first_bite_sell}")
        print(f" | Alimentação (Compra): {eating_buy}")
        print(f" | Alimentação (Venda): {eating_sell}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision