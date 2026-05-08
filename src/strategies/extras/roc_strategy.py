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

def getROCTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    signal_period: int = 9,
    overbought: int = 10,
    oversold: int = -10,
    verbose: bool = True
):
    """
    Estratégia baseada em ROC (Rate of Change).
    
    O ROC mede a variação percentual do preço em relação a um período anterior,
    indicando a força de uma tendência e potenciais reversões.
    
    Parâmetros:
    - period: Período para cálculo do ROC
    - signal_period: Período para cálculo da média móvel do ROC
    - overbought: Nível que indica condição de sobrecompra
    - oversold: Nível que indica condição de sobrevenda
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos a coluna 'close'
    if 'close' not in stock_data.columns:
        # Tentar converter para minúsculas
        stock_data.columns = [col.lower() for col in stock_data.columns]
        if 'close' not in stock_data.columns:
            raise ValueError("Coluna 'close' não encontrada nos dados.")
    
    # Calcular o ROC
    # ROC = ((Preço atual / Preço n períodos atrás) - 1) * 100
    stock_data['roc'] = ((stock_data['close'] / stock_data['close'].shift(period)) - 1) * 100
    
    # Calcular média móvel do ROC (linha de sinal)
    stock_data['roc_signal'] = stock_data['roc'].rolling(window=signal_period).mean()
    
    # Calcular histograma (ROC - Sinal)
    stock_data['roc_histogram'] = stock_data['roc'] - stock_data['roc_signal']
    
    # Extrair valores atuais
    current_roc = stock_data['roc'].iloc[-1]
    current_signal = stock_data['roc_signal'].iloc[-1]
    current_histogram = stock_data['roc_histogram'].iloc[-1]
    
    # Valores anteriores para determinar cruzamentos
    prev_roc = stock_data['roc'].iloc[-2] if len(stock_data) > 1 else current_roc
    prev_signal = stock_data['roc_signal'].iloc[-2] if len(stock_data) > 1 else current_signal
    prev_histogram = stock_data['roc_histogram'].iloc[-2] if len(stock_data) > 1 else current_histogram
    
    # Verificar valores mais antigos para divergências
    if len(stock_data) >= period:
        period_ago_close = stock_data['close'].iloc[-period]
        period_ago_roc = stock_data['roc'].iloc[-period]
        
        # Divergência: preço sobe mas ROC cai = sinal de venda
        # Divergência: preço cai mas ROC sobe = sinal de compra
        price_up = stock_data['close'].iloc[-1] > period_ago_close
        roc_up = current_roc > period_ago_roc
        
        divergence_buy = not price_up and roc_up
        divergence_sell = price_up and not roc_up
    else:
        divergence_buy = False
        divergence_sell = False
    
    # Determinar o estado atual do ROC
    is_positive = current_roc > 0
    is_rising = current_roc > prev_roc
    is_overbought = current_roc > overbought
    is_oversold = current_roc < oversold
    
    # Detectar cruzamento de zero
    zero_cross_up = (current_roc > 0) and (prev_roc < 0)
    zero_cross_down = (current_roc < 0) and (prev_roc > 0)
    
    # Detectar cruzamento com a linha de sinal
    signal_cross_up = (current_roc > current_signal) and (prev_roc <= prev_signal)
    signal_cross_down = (current_roc < current_signal) and (prev_roc >= prev_signal)
    
    # Verificar mudança de direção do histograma
    histogram_reversal_up = (current_histogram > 0) and (prev_histogram < 0)
    histogram_reversal_down = (current_histogram < 0) and (prev_histogram > 0)
    
    # Gerar sinais de compra e venda
    # Comprar:
    # 1. ROC cruza acima da linha de sinal
    # 2. ROC cruza acima de zero
    # 3. ROC atinge níveis de sobrevenda e começa a subir
    # 4. Divergência de compra
    
    # Vender:
    # 1. ROC cruza abaixo da linha de sinal
    # 2. ROC cruza abaixo de zero
    # 3. ROC atinge níveis de sobrecompra e começa a cair
    # 4. Divergência de venda
    
    buy_signal = signal_cross_up or zero_cross_up or (is_oversold and is_rising) or divergence_buy
    sell_signal = signal_cross_down or zero_cross_down or (is_overbought and not is_rising) or divergence_sell
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: ROC (Rate of Change)")
        print(f" | Período: {period}")
        print(f" | Período do Sinal: {signal_period}")
        print(f" | Nível de Sobrecompra: {overbought}")
        print(f" | Nível de Sobrevenda: {oversold}")
        print(f" | ROC Atual: {current_roc:.2f}%")
        print(f" | Sinal Atual: {current_signal:.2f}%")
        print(f" | Histograma Atual: {current_histogram:.2f}%")
        print(f" | ROC Positivo: {is_positive}")
        print(f" | ROC Crescendo: {is_rising}")
        print(f" | ROC Sobrecomprado: {is_overbought}")
        print(f" | ROC Sobrevendido: {is_oversold}")
        print(f" | Cruzamento de Zero (Para Cima): {zero_cross_up}")
        print(f" | Cruzamento de Zero (Para Baixo): {zero_cross_down}")
        print(f" | Cruzamento de Sinal (Para Cima): {signal_cross_up}")
        print(f" | Cruzamento de Sinal (Para Baixo): {signal_cross_down}")
        print(f" | Divergência Compra: {divergence_buy}")
        print(f" | Divergência Venda: {divergence_sell}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision