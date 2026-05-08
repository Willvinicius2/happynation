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

def getElderRayTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    ma_type: str = 'ema',
    bull_power_threshold: float = 0.0,
    bear_power_threshold: float = 0.0,
    verbose: bool = True
):
    """
    Estratégia baseada em Elder Ray.
    
    O Elder Ray, desenvolvido por Dr. Alexander Elder, consiste em dois indicadores:
    - Bull Power: mede a capacidade dos compradores de elevar o preço acima da média móvel
    - Bear Power: mede a capacidade dos vendedores de empurrar o preço abaixo da média móvel
    
    Parâmetros:
    - period: Período para cálculo da média móvel
    - ma_type: Tipo de média móvel ('sma' ou 'ema')
    - bull_power_threshold: Limiar para o Bull Power considerar um sinal
    - bear_power_threshold: Limiar para o Bear Power considerar um sinal
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low', 'close']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular a média móvel
    if ma_type.lower() == 'ema':
        stock_data['ma'] = stock_data['close'].ewm(span=period, adjust=False).mean()
    else:  # default to SMA
        stock_data['ma'] = stock_data['close'].rolling(window=period).mean()
    
    # Calcular o Bull Power e Bear Power
    stock_data['bull_power'] = stock_data['high'] - stock_data['ma']
    stock_data['bear_power'] = stock_data['low'] - stock_data['ma']
    
    # Extrair valores atuais
    current_ma = stock_data['ma'].iloc[-1]
    current_bull_power = stock_data['bull_power'].iloc[-1]
    current_bear_power = stock_data['bear_power'].iloc[-1]
    
    prev_bull_power = stock_data['bull_power'].iloc[-2] if len(stock_data) > 1 else current_bull_power
    prev_bear_power = stock_data['bear_power'].iloc[-2] if len(stock_data) > 1 else current_bear_power
    
    # Determinar tendência atual da média móvel
    ma_trend_up = stock_data['ma'].diff().iloc[-1] > 0
    
    # Determinar o estado atual do Bull e Bear Power
    is_bull_power_rising = current_bull_power > prev_bull_power
    is_bear_power_rising = current_bear_power > prev_bear_power
    
    # Gerar sinais de compra e venda com base nas regras Elder Ray
    
    # Regra de compra:
    # 1. A média móvel deve estar em tendência de alta (inclinação positiva)
    # 2. Bear Power deve ser negativo, mas aumentando (menos negativo) - ursos estão perdendo força
    # 3. Bull Power deve ser positivo - touros ainda têm força
    buy_signal = ma_trend_up and (current_bear_power < bear_power_threshold) and is_bear_power_rising and (current_bull_power > bull_power_threshold)
    
    # Regra de venda:
    # 1. A média móvel deve estar em tendência de baixa (inclinação negativa)
    # 2. Bull Power deve ser positivo, mas diminuindo - touros estão perdendo força
    # 3. Bear Power deve ser negativo - ursos ainda têm força
    sell_signal = not ma_trend_up and (current_bull_power > bull_power_threshold) and not is_bull_power_rising and (current_bear_power < bear_power_threshold)
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Elder Ray")
        print(f" | Período: {period}")
        print(f" | Tipo de Média Móvel: {ma_type.upper()}")
        print(f" | Média Móvel: {current_ma:.2f}")
        print(f" | Tendência da MA: {'Subindo' if ma_trend_up else 'Descendo'}")
        print(f" | Bull Power: {current_bull_power:.4f}")
        print(f" | Bull Power Anterior: {prev_bull_power:.4f}")
        print(f" | Bull Power Crescendo: {is_bull_power_rising}")
        print(f" | Bear Power: {current_bear_power:.4f}")
        print(f" | Bear Power Anterior: {prev_bear_power:.4f}")
        print(f" | Bear Power Crescendo: {is_bear_power_rising}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision