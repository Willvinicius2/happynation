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

def getChandeMomentumOscillatorTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    overbought: int = 50,
    oversold: int = -50,
    verbose: bool = True
):
    """
    Estratégia baseada em Chande Momentum Oscillator.
    
    O Chande Momentum Oscillator (CMO) é um indicador técnico que mede o momentum
    de preço de forma diferente do RSI. Ele oscila entre -100 e +100.
    
    Parâmetros:
    - period: Período para cálculo do CMO
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
    
    # Calcular variação diária
    stock_data['price_change'] = stock_data['close'].diff()
    
    # Separar as variações positivas e negativas
    stock_data['gain'] = np.where(stock_data['price_change'] > 0, stock_data['price_change'], 0)
    stock_data['loss'] = np.where(stock_data['price_change'] < 0, -stock_data['price_change'], 0)
    
    # Calcular a soma das variações positivas e negativas para o período
    stock_data['sum_gains'] = stock_data['gain'].rolling(window=period).sum()
    stock_data['sum_losses'] = stock_data['loss'].rolling(window=period).sum()
    
    # Calcular o CMO
    stock_data['cmo'] = 100 * ((stock_data['sum_gains'] - stock_data['sum_losses']) / 
                           (stock_data['sum_gains'] + stock_data['sum_losses']))
    
    # Preencher valores NaN
    stock_data['cmo'] = stock_data['cmo'].fillna(0)
    
    # Extrair valores atuais
    current_cmo = stock_data['cmo'].iloc[-1]
    prev_cmo = stock_data['cmo'].iloc[-2] if len(stock_data) > 1 else current_cmo
    
    # Determinar o estado atual do CMO
    is_overbought = current_cmo >= overbought
    is_oversold = current_cmo <= oversold
    is_rising = current_cmo > prev_cmo
    
    # Detectar cruzamento de zero
    zero_cross_up = (current_cmo > 0) and (prev_cmo < 0)
    zero_cross_down = (current_cmo < 0) and (prev_cmo > 0)
    
    # Detectar divergências
    if len(stock_data) >= period:
        price_rising = stock_data['close'].iloc[-1] > stock_data['close'].iloc[-period]
        cmo_rising = current_cmo > stock_data['cmo'].iloc[-period]
        
        # Divergência: preço sobe mas CMO cai = sinal de venda
        # Divergência: preço cai mas CMO sobe = sinal de compra
        divergence_sell = price_rising and not cmo_rising
        divergence_buy = not price_rising and cmo_rising
    else:
        divergence_sell = False
        divergence_buy = False
    
    # Gerar sinais de compra e venda
    buy_signal = is_oversold or zero_cross_up or divergence_buy
    sell_signal = is_overbought or zero_cross_down or divergence_sell
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Chande Momentum Oscillator")
        print(f" | Período: {period}")
        print(f" | CMO Atual: {current_cmo:.2f}")
        print(f" | CMO Anterior: {prev_cmo:.2f}")
        print(f" | Nível de Sobrecompra: {overbought}")
        print(f" | Nível de Sobrevenda: {oversold}")
        print(f" | Sobrecomprado: {is_overbought}")
        print(f" | Sobrevendido: {is_oversold}")
        print(f" | CMO Crescendo: {is_rising}")
        print(f" | Cruzamento de Zero (Para Cima): {zero_cross_up}")
        print(f" | Cruzamento de Zero (Para Baixo): {zero_cross_down}")
        print(f" | Divergência Compra: {divergence_buy}")
        print(f" | Divergência Venda: {divergence_sell}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision