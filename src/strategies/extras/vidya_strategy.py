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

def getVIDYATradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    fast_period: int = 9,
    slow_period: int = 21,
    chande_period: int = 10,
    use_close: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em VIDYA (Variable Index Dynamic Average).
    
    Parâmetros:
    - period: Período para cálculo do indicador principal
    - fast_period: Período para VIDYA rápida
    - slow_period: Período para VIDYA lenta
    - chande_period: Período para cálculo do Chande Momentum Oscillator
    - use_close: Usar preço de fechamento para cálculos
    """
    stock_data = stock_data.copy()
    
    # Verificar se a coluna de preço existe
    price_col = 'close' if use_close else 'open'
    if price_col not in stock_data.columns:
        raise ValueError(f"Coluna '{price_col}' não encontrada nos dados")
    
    # Verificar se há dados suficientes
    min_periods = max(period, fast_period, slow_period, chande_period)
    if len(stock_data) <= min_periods:
        return None  # Dados insuficientes para cálculo
    
    # Função para calcular o Chande Momentum Oscillator (CMO)
    def calculate_cmo(data, period):
        # Mudança no preço
        price_change = data.diff(1)
        
        # Somas de mudanças positivas e negativas
        su = price_change.copy()
        sd = price_change.copy()
        
        su[su < 0] = 0  # Manter apenas valores positivos
        sd[sd > 0] = 0  # Manter apenas valores negativos
        sd = abs(sd)    # Converter para valores positivos
        
        # Média móvel das somas
        su_roll = su.rolling(window=period).sum()
        sd_roll = sd.rolling(window=period).sum()
        
        # Calcular CMO: 100 * ((su - sd) / (su + sd))
        cmo = 100 * ((su_roll - sd_roll) / (su_roll + sd_roll))
        
        return cmo
    
    # Calcular CMO para definir a volatilidade
    stock_data['cmo'] = calculate_cmo(stock_data[price_col], chande_period)
    
    # Função para calcular VIDYA
    def calculate_vidya(data, price_col, cmo_col, period):
        # Calcular o fator de suavização baseado no CMO
        # SC = 2 / (period + 1) é o fator de suavização tradicional do EMA
        sc = 2 / (period + 1)
        
        # O CMO normalizado para o range 0-1
        k = abs(data[cmo_col]) / 100
        
        # Inicializar VIDYA como o primeiro valor válido de preço
        vidya = np.zeros(len(data))
        vidya[:] = np.nan
        
        # Encontrar o primeiro índice válido (onde CMO não é NaN)
        first_valid = data[cmo_col].first_valid_index()
        if first_valid is not None:
            idx = data.index.get_loc(first_valid)
            # Inicializar VIDYA com o primeiro valor de preço
            vidya[idx] = data[price_col].iloc[idx]
            
            # Calcular VIDYA para os valores restantes
            for i in range(idx + 1, len(data)):
                # Fator de suavização ajustado pela volatilidade (k)
                alpha = sc * k.iloc[i]
                # VIDYA(i) = VIDYA(i-1) + α * (Price(i) - VIDYA(i-1))
                vidya[i] = vidya[i-1] + alpha * (data[price_col].iloc[i] - vidya[i-1])
        
        return pd.Series(vidya, index=data.index)
    
    # Calcular VIDYA principal
    stock_data['vidya'] = calculate_vidya(stock_data, price_col, 'cmo', period)
    
    # Calcular VIDYA rápida e lenta para sinais de cruzamento
    stock_data['fast_vidya'] = calculate_vidya(stock_data, price_col, 'cmo', fast_period)
    stock_data['slow_vidya'] = calculate_vidya(stock_data, price_col, 'cmo', slow_period)
    
    # Gerar sinais de negociação baseado no cruzamento de VIDYA rápida e lenta
    stock_data['signal'] = np.where(
        stock_data['fast_vidya'] > stock_data['slow_vidya'], 1,
        np.where(stock_data['fast_vidya'] < stock_data['slow_vidya'], -1, 0)
    )
    
    # Calcular tendência de preço em relação ao VIDYA
    stock_data['price_trend'] = np.where(
        stock_data[price_col] > stock_data['vidya'], 1,
        np.where(stock_data[price_col] < stock_data['vidya'], -1, 0)
    )
    
    # Detectar mudança na direção do sinal
    stock_data['signal_change'] = stock_data['signal'].diff()
    
    # Verificar o último sinal
    last_signal = stock_data['signal'].iloc[-1] if not stock_data.empty else 0
    last_signal_change = stock_data['signal_change'].iloc[-1] if not stock_data.empty else 0
    last_price_trend = stock_data['price_trend'].iloc[-1] if not stock_data.empty else 0
    
    # Decisão de negociação
    # Comprar quando VIDYA rápido cruza VIDYA lento para cima, ou quando o preço está acima do VIDYA
    # Vender quando VIDYA rápido cruza VIDYA lento para baixo, ou quando o preço está abaixo do VIDYA
    if last_signal_change > 0:  # Mudou de negativo para positivo (cruzamento de alta)
        trade_decision = True  # Comprar
    elif last_signal_change < 0:  # Mudou de positivo para negativo (cruzamento de baixa)
        trade_decision = False  # Vender
    elif last_signal > 0 and last_price_trend > 0:  # Continuação de tendência de alta
        trade_decision = True  # Comprar
    elif last_signal < 0 and last_price_trend < 0:  # Continuação de tendência de baixa
        trade_decision = False  # Vender
    else:
        trade_decision = None  # Neutro
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: VIDYA")
        print(f" | Período: {period}")
        print(f" | Período VIDYA Rápido: {fast_period}")
        print(f" | Período VIDYA Lento: {slow_period}")
        print(f" | Período CMO: {chande_period}")
        print(f" | Último VIDYA: {stock_data['vidya'].iloc[-1]:.2f}")
        print(f" | Último VIDYA Rápido: {stock_data['fast_vidya'].iloc[-1]:.2f}")
        print(f" | Último VIDYA Lento: {stock_data['slow_vidya'].iloc[-1]:.2f}")
        print(f" | Último Preço: {stock_data[price_col].iloc[-1]:.2f}")
        print(f" | Último Sinal: {last_signal}")
        print(f" | Mudança de Sinal: {last_signal_change}")
        print(f" | Tendência de Preço: {last_price_trend}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision