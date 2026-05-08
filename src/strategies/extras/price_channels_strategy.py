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

def getPriceChannelsTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    confirm_periods: int = 2,
    verbose: bool = True
):
    """
    Estratégia baseada em Price Channels.
    
    Os Price Channels identificam o range de preços alto e baixo durante um
    determinado período, formando canais de suporte e resistência.
    
    Parâmetros:
    - period: Período para cálculo dos canais de preço
    - confirm_periods: Número de períodos para confirmar um breakout
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low', 'close']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular o Price Channel
    stock_data['upper_channel'] = stock_data['high'].rolling(window=period).max()
    stock_data['lower_channel'] = stock_data['low'].rolling(window=period).min()
    stock_data['mid_channel'] = (stock_data['upper_channel'] + stock_data['lower_channel']) / 2
    
    # Calcular a largura do canal
    stock_data['channel_width'] = stock_data['upper_channel'] - stock_data['lower_channel']
    stock_data['channel_width_pct'] = stock_data['channel_width'] / stock_data['mid_channel'] * 100
    
    # Extrair valores atuais
    current_close = stock_data['close'].iloc[-1]
    current_upper = stock_data['upper_channel'].iloc[-1]
    current_lower = stock_data['lower_channel'].iloc[-1]
    current_mid = stock_data['mid_channel'].iloc[-1]
    current_width = stock_data['channel_width'].iloc[-1]
    current_width_pct = stock_data['channel_width_pct'].iloc[-1]
    
    # Determinar posição do preço em relação ao canal
    is_above_upper = current_close > current_upper
    is_below_lower = current_close < current_lower
    is_inside_channel = not is_above_upper and not is_below_lower
    
    # Verificar valores anteriores para detectar breakouts
    # Um breakout ocorre quando o preço se move acima do canal superior ou abaixo do canal inferior
    
    # Iniciar contadores para confirmação
    confirm_upper_breakout = 0
    confirm_lower_breakout = 0
    
    # Verificar os últimos períodos para confirmar breakouts
    for i in range(1, confirm_periods + 1):
        if i < len(stock_data):
            if stock_data['close'].iloc[-i] > stock_data['upper_channel'].iloc[-i]:
                confirm_upper_breakout += 1
            elif stock_data['close'].iloc[-i] < stock_data['lower_channel'].iloc[-i]:
                confirm_lower_breakout += 1
    
    # Confirmar breakouts
    is_upper_breakout = confirm_upper_breakout >= confirm_periods
    is_lower_breakout = confirm_lower_breakout >= confirm_periods
    
    # Verificar se o canal está expandindo ou contraindo
    if len(stock_data) > 1:
        prev_width = stock_data['channel_width'].iloc[-2]
        is_expanding = current_width > prev_width
        is_contracting = current_width < prev_width
    else:
        is_expanding = False
        is_contracting = False
    
    # Gerar sinais de compra e venda
    # Comprar: Breakout confirmado acima do canal superior
    # Vender: Breakout confirmado abaixo do canal inferior
    # Alternativa: Usar a estratégia de reversão à média (mean reversion)
    # Comprar: Preço se aproxima do canal inferior
    # Vender: Preço se aproxima do canal superior
    
    buy_signal = is_upper_breakout
    sell_signal = is_lower_breakout
    
    # Estratégia de reversão à média (ativar com uma flag se necessário)
    use_mean_reversion = False
    
    if use_mean_reversion:
        # Inversão da lógica para reversão à média
        buy_signal = is_below_lower or (is_inside_channel and current_close < current_mid)
        sell_signal = is_above_upper or (is_inside_channel and current_close > current_mid)
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Price Channels")
        print(f" | Período: {period}")
        print(f" | Períodos para Confirmação: {confirm_periods}")
        print(f" | Preço Atual: {current_close:.2f}")
        print(f" | Canal Superior: {current_upper:.2f}")
        print(f" | Canal Médio: {current_mid:.2f}")
        print(f" | Canal Inferior: {current_lower:.2f}")
        print(f" | Largura do Canal: {current_width:.2f}")
        print(f" | Largura do Canal (%): {current_width_pct:.2f}%")
        print(f" | Preço Acima do Canal Superior: {is_above_upper}")
        print(f" | Preço Abaixo do Canal Inferior: {is_below_lower}")
        print(f" | Preço Dentro do Canal: {is_inside_channel}")
        print(f" | Breakout Superior Confirmado: {is_upper_breakout} ({confirm_upper_breakout}/{confirm_periods})")
        print(f" | Breakout Inferior Confirmado: {is_lower_breakout} ({confirm_lower_breakout}/{confirm_periods})")
        print(f" | Canal Expandindo: {is_expanding}")
        print(f" | Canal Contraindo: {is_contracting}")
        print(f" | Estratégia de Reversão à Média: {use_mean_reversion}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision