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

def getUltimateOscillatorTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    period1: int = 7,
    period2: int = 14,
    period3: int = 28,
    weight1: float = 4.0,
    weight2: float = 2.0,
    weight3: float = 1.0,
    overbought: int = 70,
    oversold: int = 30,
    verbose: bool = True
):
    """
    Estratégia baseada em Ultimate Oscillator.
    
    O Ultimate Oscillator, desenvolvido por Larry Williams, utiliza médias de
    três períodos diferentes para reduzir a volatilidade e falsas divergências.
    
    Parâmetros:
    - period: Período geral para cálculos (mantido para compatibilidade)
    - period1: Primeiro período (curto) para média
    - period2: Segundo período (médio) para média
    - period3: Terceiro período (longo) para média
    - weight1: Peso para o primeiro período
    - weight2: Peso para o segundo período
    - weight3: Peso para o terceiro período
    - overbought: Nível que indica condição de sobrecompra
    - oversold: Nível que indica condição de sobrevenda
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low', 'close']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular True Range (TR) e Buying Pressure (BP)
    stock_data['prev_close'] = stock_data['close'].shift(1)
    
    # True Range = max(high, prev_close) - min(low, prev_close)
    stock_data['tr'] = stock_data.apply(
        lambda x: max(x['high'], x['prev_close']) - min(x['low'], x['prev_close']) 
        if not pd.isna(x['prev_close']) else x['high'] - x['low'],
        axis=1
    )
    
    # Buying Pressure = close - min(low, prev_close)
    stock_data['bp'] = stock_data.apply(
        lambda x: x['close'] - min(x['low'], x['prev_close'])
        if not pd.isna(x['prev_close']) else x['close'] - x['low'],
        axis=1
    )
    
    # Calcular as médias móveis para diferentes períodos
    # Average1 = Sum(BP, period1) / Sum(TR, period1)
    stock_data['avg1'] = stock_data['bp'].rolling(window=period1).sum() / stock_data['tr'].rolling(window=period1).sum()
    
    # Average2 = Sum(BP, period2) / Sum(TR, period2)
    stock_data['avg2'] = stock_data['bp'].rolling(window=period2).sum() / stock_data['tr'].rolling(window=period2).sum()
    
    # Average3 = Sum(BP, period3) / Sum(TR, period3)
    stock_data['avg3'] = stock_data['bp'].rolling(window=period3).sum() / stock_data['tr'].rolling(window=period3).sum()
    
    # Calcular o Ultimate Oscillator
    # UO = 100 * ((weight1 * Average1) + (weight2 * Average2) + (weight3 * Average3)) / (weight1 + weight2 + weight3)
    total_weight = weight1 + weight2 + weight3
    stock_data['uo'] = 100 * ((weight1 * stock_data['avg1'] + 
                              weight2 * stock_data['avg2'] + 
                              weight3 * stock_data['avg3']) / total_weight)
    
    # Extrair valores atuais
    current_uo = stock_data['uo'].iloc[-1]
    current_close = stock_data['close'].iloc[-1]
    
    # Valores anteriores para determinar tendências e divergências
    prev_uo = stock_data['uo'].iloc[-2] if len(stock_data) > 1 else current_uo
    
    # Verificar valores mais antigos para divergências
    if len(stock_data) >= period:
        lookback = period3 + 5  # Usar um período um pouco maior que o período longo
        
        # Encontrar os mínimos e máximos locais nos últimos 'lookback' períodos
        if lookback < len(stock_data):
            recent_prices = stock_data['close'].iloc[-lookback:]
            recent_uo = stock_data['uo'].iloc[-lookback:]
            
            # Verificar se existe divergência bullish: preço faz novos mínimos enquanto UO não
            price_min_idx = recent_prices.idxmin()
            uo_min_idx = recent_uo.idxmin()
            
            # Verificar divergência bullish
            bullish_divergence = False
            if price_min_idx > uo_min_idx:
                min_price = recent_prices.loc[price_min_idx]
                earlier_price = recent_prices.loc[uo_min_idx]
                min_uo = recent_uo.loc[uo_min_idx]
                current_uo_at_price_min = recent_uo.loc[price_min_idx]
                
                # Se o preço fez um novo mínimo, mas o UO não
                if min_price < earlier_price and current_uo_at_price_min > min_uo:
                    bullish_divergence = True
            
            # Verificar se existe divergência bearish: preço faz novos máximos enquanto UO não
            price_max_idx = recent_prices.idxmax()
            uo_max_idx = recent_uo.idxmax()
            
            # Verificar divergência bearish
            bearish_divergence = False
            if price_max_idx > uo_max_idx:
                max_price = recent_prices.loc[price_max_idx]
                earlier_price = recent_prices.loc[uo_max_idx]
                max_uo = recent_uo.loc[uo_max_idx]
                current_uo_at_price_max = recent_uo.loc[price_max_idx]
                
                # Se o preço fez um novo máximo, mas o UO não
                if max_price > earlier_price and current_uo_at_price_max < max_uo:
                    bearish_divergence = True
        else:
            bullish_divergence = False
            bearish_divergence = False
    else:
        bullish_divergence = False
        bearish_divergence = False
    
    # Determinar o estado atual do Ultimate Oscillator
    is_rising = current_uo > prev_uo
    is_overbought = current_uo > overbought
    is_oversold = current_uo < oversold
    
    # Gerar sinais de compra e venda segundo as regras de Larry Williams
    # Regra de compra:
    # 1. UO está abaixo de oversold (30)
    # 2. Uma divergência bullish é formada (o preço faz mínimos mais baixos, mas o UO não)
    # 3. UO cruza acima da leitura anterior
    
    # Regra de venda:
    # 1. UO está acima de overbought (70)
    # 2. Uma divergência bearish é formada (o preço faz máximos mais altos, mas o UO não)
    # 3. UO cruza abaixo da leitura anterior
    
    buy_signal = is_oversold and bullish_divergence and is_rising
    sell_signal = is_overbought and bearish_divergence and not is_rising
    
    # Sinais alternativos (simplificados)
    simple_buy_signal = (current_uo < oversold) and is_rising
    simple_sell_signal = (current_uo > overbought) and not is_rising
    
    # Adicionar sinais simplificados se não houver sinais principais
    if not buy_signal and not sell_signal:
        buy_signal = simple_buy_signal
        sell_signal = simple_sell_signal
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Ultimate Oscillator")
        print(f" | Período Curto: {period1}")
        print(f" | Período Médio: {period2}")
        print(f" | Período Longo: {period3}")
        print(f" | Pesos: {weight1}/{weight2}/{weight3}")
        print(f" | Nível de Sobrecompra: {overbought}")
        print(f" | Nível de Sobrevenda: {oversold}")
        print(f" | UO Atual: {current_uo:.2f}")
        print(f" | UO Anterior: {prev_uo:.2f}")
        print(f" | UO Crescendo: {is_rising}")
        print(f" | UO Sobrecomprado: {is_overbought}")
        print(f" | UO Sobrevendido: {is_oversold}")
        print(f" | Divergência Bullish: {bullish_divergence}")
        print(f" | Divergência Bearish: {bearish_divergence}")
        print(f" | Sinal Principal de Compra: {buy_signal and not simple_buy_signal}")
        print(f" | Sinal Principal de Venda: {sell_signal and not simple_sell_signal}")
        print(f" | Sinal Alternativo de Compra: {simple_buy_signal}")
        print(f" | Sinal Alternativo de Venda: {simple_sell_signal}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision