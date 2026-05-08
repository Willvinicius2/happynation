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

def getATRTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    atr_threshold: float = 1.5,
    lookback_period: int = 5,
    verbose: bool = True
):
    """
    Estratégia baseada em ATR (Average True Range).
    
    O ATR é um indicador de volatilidade que mostra quão amplamente um ativo está 
    negociando. Este indicador não fornece direção, apenas volatilidade.
    
    Nesta estratégia, compramos quando há um aumento significativo no ATR 
    (aumento de volatilidade) seguido por um movimento de preço para cima, 
    indicando força de movimento, e vendemos quando o ATR cai significativamente,
    indicando uma possível estabilização ou reversão do movimento.
    
    Parâmetros:
    - period: Período para cálculo do indicador ATR
    - atr_threshold: Limite percentual de variação do ATR para gerar sinais
    - lookback_period: Período para verificar a tendência recente do preço
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low', 'close']
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    # Calcular o ATR
    # Primeiro, calcular o True Range (TR)
    stock_data['high_low'] = stock_data['high'] - stock_data['low']
    stock_data['high_close'] = np.abs(stock_data['high'] - stock_data['close'].shift(1))
    stock_data['low_close'] = np.abs(stock_data['low'] - stock_data['close'].shift(1))
    stock_data['tr'] = stock_data[['high_low', 'high_close', 'low_close']].max(axis=1)
    
    # Em seguida, calcular o ATR como média móvel do TR
    stock_data['atr'] = stock_data['tr'].rolling(window=period).mean()
    
    # Calcular a variação percentual do ATR
    stock_data['atr_change'] = stock_data['atr'].pct_change(periods=1) * 100
    
    # Calcular a direção recente do preço
    stock_data['price_direction'] = stock_data['close'].diff(periods=lookback_period)
    
    # Determinar os sinais de compra e venda
    # Compra: ATR aumenta acima do limite e preço está subindo
    # Venda: ATR diminui abaixo do limite negativo
    stock_data['buy_signal'] = (stock_data['atr_change'] > atr_threshold) & (stock_data['price_direction'] > 0)
    stock_data['sell_signal'] = (stock_data['atr_change'] < -atr_threshold)
    
    # Obter o último sinal
    last_buy_signal = stock_data['buy_signal'].iloc[-1]
    last_sell_signal = stock_data['sell_signal'].iloc[-1]
    
    # Determinar a decisão de negociação
    trade_decision = None
    if last_buy_signal:
        trade_decision = True  # Comprar
    elif last_sell_signal:
        trade_decision = False  # Vender
    
    # Adicionar valores para visualização no log
    current_atr = stock_data['atr'].iloc[-1]
    current_atr_change = stock_data['atr_change'].iloc[-1]
    price_trend = "Subindo" if stock_data['price_direction'].iloc[-1] > 0 else "Descendo"
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: ATR")
        print(f" | Período: {period}")
        print(f" | ATR Atual: {current_atr:.4f}")
        print(f" | Variação ATR: {current_atr_change:.2f}%")
        print(f" | Tendência de Preço: {price_trend}")
        print(f" | Limite ATR: ±{atr_threshold:.2f}%")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision