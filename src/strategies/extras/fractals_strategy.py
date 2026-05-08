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

def getFractalsTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    window_size: int = 5,
    confirmation_bars: int = 2,
    verbose: bool = True
):
    """
    Estratégia baseada em Fractals.
    
    Os Fractals, desenvolvidos por Bill Williams, são indicadores que identificam
    topos e fundos locais no preço. Um fractal é identificado quando um conjunto
    de 5 barras forma um padrão específico.
    
    Parâmetros:
    - period: Período geral para cálculos (mantido para compatibilidade)
    - window_size: Tamanho da janela para identificar fractais (normalmente 5)
    - confirmation_bars: Número de barras para confirmar um fractal
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low', 'close']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Adicionar colunas para identificar fractals
    stock_data['fractal_high'] = False
    stock_data['fractal_low'] = False
    
    # Identificar fractals
    for i in range(window_size // 2, len(stock_data) - window_size // 2):
        # Verificar se o ponto central é o mais alto na janela (fractal de alta)
        if stock_data['high'].iloc[i] > max(stock_data['high'].iloc[i - window_size // 2:i]) and \
           stock_data['high'].iloc[i] > max(stock_data['high'].iloc[i + 1:i + window_size // 2 + 1]):
            stock_data.loc[stock_data.index[i], 'fractal_high'] = True
        
        # Verificar se o ponto central é o mais baixo na janela (fractal de baixa)
        if stock_data['low'].iloc[i] < min(stock_data['low'].iloc[i - window_size // 2:i]) and \
           stock_data['low'].iloc[i] < min(stock_data['low'].iloc[i + 1:i + window_size // 2 + 1]):
            stock_data.loc[stock_data.index[i], 'fractal_low'] = True
    
    # Identificar os valores dos fractals mais recentes
    last_high_fractal_idx = stock_data[stock_data['fractal_high']].index[-1] if any(stock_data['fractal_high']) else None
    last_low_fractal_idx = stock_data[stock_data['fractal_low']].index[-1] if any(stock_data['fractal_low']) else None
    
    last_high_fractal = stock_data.loc[last_high_fractal_idx, 'high'] if last_high_fractal_idx else None
    last_low_fractal = stock_data.loc[last_low_fractal_idx, 'low'] if last_low_fractal_idx else None
    
    # Verificar se temos confirmação
    high_fractal_confirmed = False
    low_fractal_confirmed = False
    
    if last_high_fractal_idx is not None:
        high_idx_pos = stock_data.index.get_loc(last_high_fractal_idx)
        if high_idx_pos + confirmation_bars < len(stock_data):
            high_fractal_confirmed = True
    
    if last_low_fractal_idx is not None:
        low_idx_pos = stock_data.index.get_loc(last_low_fractal_idx)
        if low_idx_pos + confirmation_bars < len(stock_data):
            low_fractal_confirmed = True
    
    # Extrair valor atual
    current_close = stock_data['close'].iloc[-1]
    
    # Calcular uma média móvel para determinar a tendência
    stock_data['ma'] = stock_data['close'].rolling(window=period).mean()
    current_ma = stock_data['ma'].iloc[-1]
    
    # Determinar a tendência
    is_uptrend = current_close > current_ma
    
    # Gerar sinais de compra e venda
    # Comprar: Preço acima da média móvel e quebra do último fractal de alta confirmado
    # Vender: Preço abaixo da média móvel e quebra do último fractal de baixa confirmado
    
    buy_signal = False
    sell_signal = False
    
    if high_fractal_confirmed and is_uptrend and last_high_fractal is not None and current_close > last_high_fractal:
        buy_signal = True
    
    if low_fractal_confirmed and not is_uptrend and last_low_fractal is not None and current_close < last_low_fractal:
        sell_signal = True
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Fractals")
        print(f" | Tamanho da Janela: {window_size}")
        print(f" | Barras para Confirmação: {confirmation_bars}")
        print(f" | Preço Atual: {current_close:.2f}")
        print(f" | Média Móvel ({period}): {current_ma:.2f}")
        print(f" | Tendência de Alta: {is_uptrend}")
        print(f" | Último Fractal de Alta: {last_high_fractal:.2f if last_high_fractal else 'N/A'}")
        print(f" | Último Fractal de Baixa: {last_low_fractal:.2f if last_low_fractal else 'N/A'}")
        print(f" | Fractal de Alta Confirmado: {high_fractal_confirmed}")
        print(f" | Fractal de Baixa Confirmado: {low_fractal_confirmed}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision