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

def getPSARTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    af_start: float = 0.02,
    af_increment: float = 0.02,
    af_max: float = 0.2,
    verbose: bool = True
):
    """
    Estratégia baseada em PSAR (Parabolic Stop and Reverse).
    
    O PSAR é um indicador de tendência desenvolvido por J. Welles Wilder Jr. que
    fornece pontos de entrada e saída bem como stop loss dinâmicos.
    
    Parâmetros:
    - period: Período para cálculos gerais (mantido para compatibilidade)
    - af_start: Fator de aceleração inicial
    - af_increment: Incremento do fator de aceleração
    - af_max: Fator de aceleração máximo
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low', 'close']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Inicializar colunas
    stock_data['psar'] = np.nan
    stock_data['trend'] = np.nan  # 1 para tendência de alta, -1 para tendência de baixa
    stock_data['af'] = af_start  # Fator de aceleração
    stock_data['ep'] = np.nan  # Extreme point
    
    # Determinar tendência inicial
    # Vamos usar a inclinação dos primeiros preços para determinar a tendência inicial
    if len(stock_data) > 1:
        if stock_data['close'].iloc[1] > stock_data['close'].iloc[0]:
            initial_trend = 1  # Tendência de alta
            stock_data.loc[1, 'psar'] = stock_data['low'].iloc[0]  # PSAR inicial abaixo do primeiro low
            stock_data.loc[1, 'ep'] = stock_data['high'].iloc[1]  # Primeiro EP é o high atual
        else:
            initial_trend = -1  # Tendência de baixa
            stock_data.loc[1, 'psar'] = stock_data['high'].iloc[0]  # PSAR inicial acima do primeiro high
            stock_data.loc[1, 'ep'] = stock_data['low'].iloc[1]  # Primeiro EP é o low atual
        
        stock_data.loc[1, 'trend'] = initial_trend
    
    # Calcular o PSAR para o restante dos dados
    for i in range(2, len(stock_data)):
        prev_psar = stock_data.loc[stock_data.index[i-1], 'psar']
        prev_trend = stock_data.loc[stock_data.index[i-1], 'trend']
        prev_af = stock_data.loc[stock_data.index[i-1], 'af']
        prev_ep = stock_data.loc[stock_data.index[i-1], 'ep']
        
        # Calcular o novo PSAR
        current_psar = prev_psar + prev_af * (prev_ep - prev_psar)
        
        # Tendência de alta
        if prev_trend == 1:
            # Limitar o PSAR pelos mínimos anteriores
            current_psar = min(current_psar, stock_data['low'].iloc[i-2], stock_data['low'].iloc[i-1])
            
            # Verificar se o PSAR é ultrapassado (tendência invertida)
            if current_psar > stock_data['low'].iloc[i]:
                current_trend = -1
                current_psar = max(stock_data['high'].iloc[i-2], stock_data['high'].iloc[i-1], stock_data['high'].iloc[i])
                current_ep = stock_data['low'].iloc[i]
                current_af = af_start
            else:
                current_trend = 1
                # Atualizar EP se tiver novo máximo
                if stock_data['high'].iloc[i] > prev_ep:
                    current_ep = stock_data['high'].iloc[i]
                    current_af = min(prev_af + af_increment, af_max)
                else:
                    current_ep = prev_ep
                    current_af = prev_af
        
        # Tendência de baixa
        else:
            # Limitar o PSAR pelos máximos anteriores
            current_psar = max(current_psar, stock_data['high'].iloc[i-2], stock_data['high'].iloc[i-1])
            
            # Verificar se o PSAR é ultrapassado (tendência invertida)
            if current_psar < stock_data['high'].iloc[i]:
                current_trend = 1
                current_psar = min(stock_data['low'].iloc[i-2], stock_data['low'].iloc[i-1], stock_data['low'].iloc[i])
                current_ep = stock_data['high'].iloc[i]
                current_af = af_start
            else:
                current_trend = -1
                # Atualizar EP se tiver novo mínimo
                if stock_data['low'].iloc[i] < prev_ep:
                    current_ep = stock_data['low'].iloc[i]
                    current_af = min(prev_af + af_increment, af_max)
                else:
                    current_ep = prev_ep
                    current_af = prev_af
        
        # Armazenar valores
        stock_data.loc[stock_data.index[i], 'psar'] = current_psar
        stock_data.loc[stock_data.index[i], 'trend'] = current_trend
        stock_data.loc[stock_data.index[i], 'af'] = current_af
        stock_data.loc[stock_data.index[i], 'ep'] = current_ep
    
    # Extrair valores atuais
    current_close = stock_data['close'].iloc[-1]
    current_psar = stock_data['psar'].iloc[-1]
    current_trend = stock_data['trend'].iloc[-1]
    
    # Determinar sinais de compra e venda
    # Verificar mudanças de tendência (cruzamentos do PSAR)
    trend_changes = stock_data['trend'].diff().fillna(0)
    buy_signal = trend_changes.iloc[-1] > 0  # Mudança de tendência para cima
    sell_signal = trend_changes.iloc[-1] < 0  # Mudança de tendência para baixo
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: PSAR (Parabolic Stop and Reverse)")
        print(f" | AF Inicial: {af_start}")
        print(f" | AF Incremento: {af_increment}")
        print(f" | AF Máximo: {af_max}")
        print(f" | Preço Atual: {current_close:.2f}")
        print(f" | PSAR Atual: {current_psar:.2f}")
        print(f" | Tendência Atual: {'Alta' if current_trend == 1 else 'Baixa'}")
        print(f" | Acima do PSAR: {current_close > current_psar}")
        print(f" | Abaixo do PSAR: {current_close < current_psar}")
        print(f" | Mudança para Tendência de Alta: {buy_signal}")
        print(f" | Mudança para Tendência de Baixa: {sell_signal}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision