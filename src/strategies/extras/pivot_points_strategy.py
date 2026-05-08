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

def getPivotPointsTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    method: str = 'standard',
    verbose: bool = True
):
    """
    Estratégia baseada em Pivot Points.
    
    Os Pivot Points são níveis de suporte e resistência calculados com base nos preços
    do período anterior e são usados para identificar potenciais reversões e breakouts.
    
    Parâmetros:
    - period: Período para cálculo (mantido para compatibilidade)
    - method: Método de cálculo ('standard', 'fibonacci', 'woodie', 'camarilla', 'demark')
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low', 'close', 'open']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            if col == 'open':  # 'open' pode ser opcional em alguns casos
                stock_data['open'] = stock_data['close'].shift(1)
            else:
                raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Adicionar informações sobre o período anterior
    stock_data['prev_high'] = stock_data['high'].shift(1)
    stock_data['prev_low'] = stock_data['low'].shift(1)
    stock_data['prev_close'] = stock_data['close'].shift(1)
    stock_data['prev_open'] = stock_data['open'].shift(1)
    
    # Calcular os Pivot Points com base no método selecionado
    # 1. Standard Pivot Points
    if method.lower() == 'standard':
        # Pivot Point (PP) = (High + Low + Close) / 3
        stock_data['pp'] = (stock_data['prev_high'] + stock_data['prev_low'] + stock_data['prev_close']) / 3
        
        # Primeira resistência e suporte
        stock_data['r1'] = 2 * stock_data['pp'] - stock_data['prev_low']
        stock_data['s1'] = 2 * stock_data['pp'] - stock_data['prev_high']
        
        # Segunda resistência e suporte
        stock_data['r2'] = stock_data['pp'] + (stock_data['prev_high'] - stock_data['prev_low'])
        stock_data['s2'] = stock_data['pp'] - (stock_data['prev_high'] - stock_data['prev_low'])
        
        # Terceira resistência e suporte
        stock_data['r3'] = stock_data['r1'] + (stock_data['prev_high'] - stock_data['prev_low'])
        stock_data['s3'] = stock_data['s1'] - (stock_data['prev_high'] - stock_data['prev_low'])
    
    # 2. Fibonacci Pivot Points
    elif method.lower() == 'fibonacci':
        stock_data['pp'] = (stock_data['prev_high'] + stock_data['prev_low'] + stock_data['prev_close']) / 3
        
        range_hl = stock_data['prev_high'] - stock_data['prev_low']
        
        # Resistências (usando níveis de Fibonacci)
        stock_data['r1'] = stock_data['pp'] + 0.382 * range_hl
        stock_data['r2'] = stock_data['pp'] + 0.618 * range_hl
        stock_data['r3'] = stock_data['pp'] + 1.000 * range_hl
        
        # Suportes (usando níveis de Fibonacci)
        stock_data['s1'] = stock_data['pp'] - 0.382 * range_hl
        stock_data['s2'] = stock_data['pp'] - 0.618 * range_hl
        stock_data['s3'] = stock_data['pp'] - 1.000 * range_hl
    
    # 3. Woodie's Pivot Points
    elif method.lower() == 'woodie':
        # Woodie dá mais peso ao preço de fechamento
        stock_data['pp'] = (stock_data['prev_high'] + stock_data['prev_low'] + 2 * stock_data['prev_close']) / 4
        
        # Resistências e suportes
        stock_data['r1'] = 2 * stock_data['pp'] - stock_data['prev_low']
        stock_data['s1'] = 2 * stock_data['pp'] - stock_data['prev_high']
        
        stock_data['r2'] = stock_data['pp'] + (stock_data['prev_high'] - stock_data['prev_low'])
        stock_data['s2'] = stock_data['pp'] - (stock_data['prev_high'] - stock_data['prev_low'])
        
        stock_data['r3'] = stock_data['pp'] + 2 * (stock_data['prev_high'] - stock_data['prev_low'])
        stock_data['s3'] = stock_data['pp'] - 2 * (stock_data['prev_high'] - stock_data['prev_low'])
    
    # 4. Camarilla Pivot Points
    elif method.lower() == 'camarilla':
        stock_data['pp'] = (stock_data['prev_high'] + stock_data['prev_low'] + stock_data['prev_close']) / 3
        
        range_hl = stock_data['prev_high'] - stock_data['prev_low']
        
        # Resistências (usando multiplicadores específicos)
        stock_data['r1'] = stock_data['prev_close'] + range_hl * 1.1 / 12
        stock_data['r2'] = stock_data['prev_close'] + range_hl * 1.1 / 6
        stock_data['r3'] = stock_data['prev_close'] + range_hl * 1.1 / 4
        
        # Suportes (usando multiplicadores específicos)
        stock_data['s1'] = stock_data['prev_close'] - range_hl * 1.1 / 12
        stock_data['s2'] = stock_data['prev_close'] - range_hl * 1.1 / 6
        stock_data['s3'] = stock_data['prev_close'] - range_hl * 1.1 / 4
    
    # 5. DeMark's Pivot Points
    elif method.lower() == 'demark':
        # Condições para determinar o valor X
        cond1 = stock_data['prev_close'] > stock_data['prev_open']
        cond2 = stock_data['prev_close'] < stock_data['prev_open']
        cond3 = stock_data['prev_close'] == stock_data['prev_open']
        
        # Calcular X com base nas condições
        stock_data['x'] = np.where(cond1, stock_data['prev_high'] * 2 + stock_data['prev_low'] + stock_data['prev_close'],
                        np.where(cond2, stock_data['prev_high'] + stock_data['prev_low'] * 2 + stock_data['prev_close'],
                                stock_data['prev_high'] + stock_data['prev_low'] + stock_data['prev_close'] * 2))
        
        # Pivot Point DeMark
        stock_data['pp'] = stock_data['x'] / 4
        
        # Resistência e suporte
        stock_data['r1'] = stock_data['x'] / 2 - stock_data['prev_low']
        stock_data['s1'] = stock_data['x'] / 2 - stock_data['prev_high']
        
        # Adicionar R2, R3, S2, S3 como NaN para manter a consistência com outros métodos
        stock_data['r2'] = np.nan
        stock_data['r3'] = np.nan
        stock_data['s2'] = np.nan
        stock_data['s3'] = np.nan
    
    else:
        raise ValueError(f"Método '{method}' não reconhecido. Use 'standard', 'fibonacci', 'woodie', 'camarilla' ou 'demark'.")
    
    # Extrair valores atuais
    current_close = stock_data['close'].iloc[-1]
    current_pp = stock_data['pp'].iloc[-1]
    current_r1 = stock_data['r1'].iloc[-1]
    current_r2 = stock_data['r2'].iloc[-1]
    current_r3 = stock_data['r3'].iloc[-1]
    current_s1 = stock_data['s1'].iloc[-1]
    current_s2 = stock_data['s2'].iloc[-1]
    current_s3 = stock_data['s3'].iloc[-1]
    
    # Determinar posição do preço em relação aos níveis de pivot
    is_above_pp = current_close > current_pp
    is_above_r1 = current_close > current_r1
    is_above_r2 = current_close > current_r2
    is_above_r3 = current_close > current_r3
    is_below_s1 = current_close < current_s1
    is_below_s2 = current_close < current_s2
    is_below_s3 = current_close < current_s3
    
    # Verificar valores anteriores para detectar cruzamentos
    prev_close = stock_data['close'].iloc[-2] if len(stock_data) > 1 else current_close
    prev_pp = stock_data['pp'].iloc[-2] if len(stock_data) > 1 else current_pp
    prev_r1 = stock_data['r1'].iloc[-2] if len(stock_data) > 1 else current_r1
    prev_s1 = stock_data['s1'].iloc[-2] if len(stock_data) > 1 else current_s1
    
    # Detectar cruzamentos
    pp_cross_up = (current_close > current_pp) and (prev_close <= prev_pp)
    pp_cross_down = (current_close < current_pp) and (prev_close >= prev_pp)
    r1_cross_up = (current_close > current_r1) and (prev_close <= prev_r1)
    s1_cross_down = (current_close < current_s1) and (prev_close >= prev_s1)
    
    # Determinar a tendência de curto prazo com base nos pivots
    uptrend = is_above_pp and pp_cross_up
    downtrend = not is_above_pp and pp_cross_down
    
    # Gerar sinais de compra e venda
    # Comprar:
    # 1. Preço cruza acima de PP em tendência de alta
    # 2. Preço testa S1 como suporte e volta a subir
    
    # Vender:
    # 1. Preço cruza abaixo de PP em tendência de baixa
    # 2. Preço testa R1 como resistência e volta a cair
    
    buy_signal = pp_cross_up or (is_below_s1 and not s1_cross_down)
    sell_signal = pp_cross_down or (is_above_r1 and not r1_cross_up)
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Pivot Points")
        print(f" | Método: {method.capitalize()}")
        print(f" | Preço Atual: {current_close:.2f}")
        print(f" | Pivot Point (PP): {current_pp:.2f}")
        print(f" | Resistência 1 (R1): {current_r1:.2f}")
        print(f" | Resistência 2 (R2): {current_r2:.2f}" if not np.isnan(current_r2) else " | Resistência 2 (R2): N/A")
        print(f" | Resistência 3 (R3): {current_r3:.2f}" if not np.isnan(current_r3) else " | Resistência 3 (R3): N/A")
        print(f" | Suporte 1 (S1): {current_s1:.2f}")
        print(f" | Suporte 2 (S2): {current_s2:.2f}" if not np.isnan(current_s2) else " | Suporte 2 (S2): N/A")
        print(f" | Suporte 3 (S3): {current_s3:.2f}" if not np.isnan(current_s3) else " | Suporte 3 (S3): N/A")
        print(f" | Preço Acima do PP: {is_above_pp}")
        print(f" | Preço Acima de R1: {is_above_r1}")
        print(f" | Preço Abaixo de S1: {is_below_s1}")
        print(f" | Cruzamento do PP (Para Cima): {pp_cross_up}")
        print(f" | Cruzamento do PP (Para Baixo): {pp_cross_down}")
        print(f" | Tendência de Alta: {uptrend}")
        print(f" | Tendência de Baixa: {downtrend}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision