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

def getIchimokuCloudTradeStrategy(
    stock_data: pd.DataFrame,
    tenkan_period: int = 9,
    kijun_period: int = 26,
    senkou_span_b_period: int = 52,
    displacement: int = 26,
    require_confirmation: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em Ichimoku Cloud.
    
    Parâmetros:
    - tenkan_period: Período para o Tenkan-sen (Linha de Conversão)
    - kijun_period: Período para o Kijun-sen (Linha Base)
    - senkou_span_b_period: Período para o Senkou Span B (Segunda linha da nuvem)
    - displacement: Período de deslocamento para o Senkou Span (Nuvem)
    - require_confirmation: Exigir confirmação completa (preço e TK cross) para sinais
    """
    stock_data = stock_data.copy()
    
    # Verificar se as colunas necessárias existem
    required_cols = ['high', 'low', 'close']
    for col in required_cols:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna '{col}' não encontrada nos dados")
    
    # Verificar se há dados suficientes
    min_periods = max(tenkan_period, kijun_period, senkou_span_b_period) + displacement
    if len(stock_data) <= min_periods:
        return None  # Dados insuficientes para cálculo
    
    # Função para calcular a média dos altos/baixos de um período
    def donchian(high, low, period):
        return (high.rolling(window=period).max() + low.rolling(window=period).min()) / 2
    
    # Cálculo do Tenkan-sen (Linha de Conversão)
    stock_data['tenkan_sen'] = donchian(stock_data['high'], stock_data['low'], tenkan_period)
    
    # Cálculo do Kijun-sen (Linha Base)
    stock_data['kijun_sen'] = donchian(stock_data['high'], stock_data['low'], kijun_period)
    
    # Cálculo do Senkou Span A (Primeira linha da nuvem)
    stock_data['senkou_span_a'] = ((stock_data['tenkan_sen'] + stock_data['kijun_sen']) / 2).shift(displacement)
    
    # Cálculo do Senkou Span B (Segunda linha da nuvem)
    stock_data['senkou_span_b'] = donchian(stock_data['high'], stock_data['low'], senkou_span_b_period).shift(displacement)
    
    # Cálculo do Chikou Span (Linha de Atraso)
    stock_data['chikou_span'] = stock_data['close'].shift(-displacement)
    
    # Detectar cruzamento TK (Tenkan-sen x Kijun-sen)
    stock_data['tk_cross'] = np.where(
        (stock_data['tenkan_sen'].shift(1) <= stock_data['kijun_sen'].shift(1)) & 
        (stock_data['tenkan_sen'] > stock_data['kijun_sen']), 1,
        np.where(
            (stock_data['tenkan_sen'].shift(1) >= stock_data['kijun_sen'].shift(1)) & 
            (stock_data['tenkan_sen'] < stock_data['kijun_sen']), -1, 0
        )
    )
    
    # Identificar a posição do preço em relação à nuvem
    stock_data['price_above_cloud'] = np.where(
        stock_data['close'] > stock_data['senkou_span_a'], 1,
        np.where(stock_data['close'] < stock_data['senkou_span_a'], -1, 0)
    )
    
    stock_data['cloud_breakout'] = np.where(
        (stock_data['close'] > stock_data['senkou_span_a']) & 
        (stock_data['close'] > stock_data['senkou_span_b']), 1,
        np.where(
            (stock_data['close'] < stock_data['senkou_span_a']) & 
            (stock_data['close'] < stock_data['senkou_span_b']), -1, 0
        )
    )
    
    # Tendência da nuvem
    stock_data['cloud_trend'] = np.where(
        stock_data['senkou_span_a'] > stock_data['senkou_span_b'], 1,
        np.where(stock_data['senkou_span_a'] < stock_data['senkou_span_b'], -1, 0)
    )
    
    # Verificar as condições atuais
    last_close = stock_data['close'].iloc[-1] if not stock_data.empty else 0
    last_tenkan = stock_data['tenkan_sen'].iloc[-1] if not stock_data.empty else 0
    last_kijun = stock_data['kijun_sen'].iloc[-1] if not stock_data.empty else 0
    last_senkou_a = stock_data['senkou_span_a'].iloc[-1] if not stock_data.empty else 0
    last_senkou_b = stock_data['senkou_span_b'].iloc[-1] if not stock_data.empty else 0
    last_tk_cross = stock_data['tk_cross'].iloc[-1] if not stock_data.empty else 0
    last_cloud_breakout = stock_data['cloud_breakout'].iloc[-1] if not stock_data.empty else 0
    last_cloud_trend = stock_data['cloud_trend'].iloc[-1] if not stock_data.empty else 0
    
    # Decisão de negociação
    trade_decision = None
    
    # Determinar a decisão com base nos diferentes sinais do Ichimoku
    if require_confirmation:
        # Modo conservador: requer confirmação completa
        if last_tk_cross == 1 and last_cloud_breakout == 1 and last_cloud_trend == 1:
            trade_decision = True  # Comprar com confirmação completa
        elif last_tk_cross == -1 and last_cloud_breakout == -1 and last_cloud_trend == -1:
            trade_decision = False  # Vender com confirmação completa
    else:
        # Modo mais agressivo: qualquer sinal forte é suficiente
        if last_tk_cross == 1:
            trade_decision = True  # Comprar no cruzamento TK de alta
        elif last_tk_cross == -1:
            trade_decision = False  # Vender no cruzamento TK de baixa
        elif last_cloud_breakout == 1 and last_tenkan > last_kijun:
            trade_decision = True  # Comprar na quebra da nuvem para cima
        elif last_cloud_breakout == -1 and last_tenkan < last_kijun:
            trade_decision = False  # Vender na quebra da nuvem para baixo
        elif last_close > max(last_senkou_a, last_senkou_b) and last_tenkan > last_kijun:
            trade_decision = True  # Comprar quando acima da nuvem e Tenkan acima de Kijun
        elif last_close < min(last_senkou_a, last_senkou_b) and last_tenkan < last_kijun:
            trade_decision = False  # Vender quando abaixo da nuvem e Tenkan abaixo de Kijun
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Ichimoku Cloud")
        print(f" | Tenkan-sen Período: {tenkan_period}")
        print(f" | Kijun-sen Período: {kijun_period}")
        print(f" | Senkou Span B Período: {senkou_span_b_period}")
        print(f" | Deslocamento: {displacement}")
        print(f" | Tenkan-sen: {last_tenkan:.2f}")
        print(f" | Kijun-sen: {last_kijun:.2f}")
        print(f" | Senkou Span A: {last_senkou_a:.2f}")
        print(f" | Senkou Span B: {last_senkou_b:.2f}")
        print(f" | Último Preço: {last_close:.2f}")
        print(f" | TK Cross: {'+' if last_tk_cross == 1 else '-' if last_tk_cross == -1 else '0'}")
        print(f" | Cloud Breakout: {'+' if last_cloud_breakout == 1 else '-' if last_cloud_breakout == -1 else '0'}")
        print(f" | Cloud Trend: {'+' if last_cloud_trend == 1 else '-' if last_cloud_trend == -1 else '0'}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision