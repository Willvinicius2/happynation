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

def getAcceleratorOscillatorTradeStrategy(
    stock_data: pd.DataFrame,
    sma_period: int = 5,
    ao_period_fast: int = 5,
    ao_period_slow: int = 34,
    signal_lookback: int = 3,
    use_zero_cross: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em Accelerator Oscillator (AC).
    
    Parâmetros:
    - sma_period: Período para cálculo da média dos preços (SMA)
    - ao_period_fast: Período rápido para cálculo do Awesome Oscillator
    - ao_period_slow: Período lento para cálculo do Awesome Oscillator
    - signal_lookback: Períodos para análise de tendência do AC
    - use_zero_cross: Usar cruzamento do zero como sinal adicional
    """
    stock_data = stock_data.copy()
    
    # Verificar se as colunas necessárias existem
    required_cols = ['high', 'low']
    for col in required_cols:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna '{col}' não encontrada nos dados")
    
    # Verificar se há dados suficientes
    min_periods = max(sma_period, ao_period_fast, ao_period_slow) + signal_lookback
    if len(stock_data) <= min_periods:
        return None  # Dados insuficientes para cálculo
    
    # Cálculo do preço típico (média do high e low)
    stock_data['median_price'] = (stock_data['high'] + stock_data['low']) / 2
    
    # Cálculo do Awesome Oscillator (AO)
    # 1. SMA do preço típico para período rápido
    stock_data['ao_fast'] = stock_data['median_price'].rolling(window=ao_period_fast).mean()
    
    # 2. SMA do preço típico para período lento
    stock_data['ao_slow'] = stock_data['median_price'].rolling(window=ao_period_slow).mean()
    
    # 3. Awesome Oscillator = SMA Rápido - SMA Lento
    stock_data['awesome_oscillator'] = stock_data['ao_fast'] - stock_data['ao_slow']
    
    # Cálculo do Accelerator Oscillator (AC)
    # AC = AO - SMA(AO, sma_period)
    stock_data['ao_sma'] = stock_data['awesome_oscillator'].rolling(window=sma_period).mean()
    stock_data['accelerator_oscillator'] = stock_data['awesome_oscillator'] - stock_data['ao_sma']
    
    # Detectar mudanças de sinal do AC
    stock_data['ac_sign'] = np.sign(stock_data['accelerator_oscillator'])
    stock_data['ac_sign_change'] = stock_data['ac_sign'].diff()
    
    # Verificar sequência de barras (análise de tendência)
    stock_data['ac_increasing'] = np.where(
        stock_data['accelerator_oscillator'] > stock_data['accelerator_oscillator'].shift(1), 1, 0
    )
    
    # Contar barras consecutivas na mesma direção
    for i in range(2, signal_lookback + 1):
        stock_data[f'ac_increased_{i}'] = np.where(
            stock_data['ac_increasing'].shift(i-1) == 1, 1, 0
        )
    
    # Verificar padrão de aceleração (2 barras consecutivas na mesma direção)
    stock_data['ac_trend_pattern'] = np.where(
        stock_data['accelerator_oscillator'] > 0, 
        stock_data[['ac_increasing'] + [f'ac_increased_{i}' for i in range(2, signal_lookback+1)]].sum(axis=1),
        -stock_data[['ac_increasing'] + [f'ac_increased_{i}' for i in range(2, signal_lookback+1)]].sum(axis=1)
    )
    
    # Detectar cruzamentos do zero
    if use_zero_cross:
        stock_data['zero_cross'] = np.where(
            (stock_data['accelerator_oscillator'].shift(1) <= 0) & (stock_data['accelerator_oscillator'] > 0), 1,
            np.where(
                (stock_data['accelerator_oscillator'].shift(1) >= 0) & (stock_data['accelerator_oscillator'] < 0), -1, 0
            )
        )
    
    # Verificar as condições atuais
    last_ac = stock_data['accelerator_oscillator'].iloc[-1] if not stock_data.empty else 0
    last_ac_trend = stock_data['ac_trend_pattern'].iloc[-1] if not stock_data.empty else 0
    last_zero_cross = stock_data['zero_cross'].iloc[-1] if use_zero_cross and not stock_data.empty else 0
    last_sign_change = stock_data['ac_sign_change'].iloc[-1] if not stock_data.empty else 0
    
    # Decisão de negociação
    trade_decision = None
    
    # Prioridade: cruzamento do zero > mudança de sinal > tendência > valor atual
    if use_zero_cross and last_zero_cross != 0:
        trade_decision = True if last_zero_cross == 1 else False
    elif last_sign_change != 0:
        trade_decision = True if last_sign_change == 2 else False if last_sign_change == -2 else None
    elif last_ac_trend > 0:
        trade_decision = True  # Padrão de aceleração positiva
    elif last_ac_trend < 0:
        trade_decision = False  # Padrão de aceleração negativa
    elif last_ac > 0:
        trade_decision = True  # AC positivo
    elif last_ac < 0:
        trade_decision = False  # AC negativo
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Accelerator Oscillator")
        print(f" | Período SMA: {sma_period}")
        print(f" | Período AO Rápido: {ao_period_fast}")
        print(f" | Período AO Lento: {ao_period_slow}")
        print(f" | Períodos de Análise: {signal_lookback}")
        print(f" | Usar Cruzamento Zero: {'Sim' if use_zero_cross else 'Não'}")
        print(f" | Último AC: {last_ac:.6f}")
        print(f" | Padrão de Tendência: {last_ac_trend}")
        if use_zero_cross:
            print(f" | Cruzamento do Zero: {last_zero_cross}")
        print(f" | Mudança de Sinal: {last_sign_change}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision