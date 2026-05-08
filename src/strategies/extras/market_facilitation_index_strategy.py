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

def getMarketFacilitationIndexTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    verbose: bool = True
):
    """
    Estratégia baseada em Market Facilitation Index (MFI).
    
    O MFI, desenvolvido por Bill Williams, mede a eficiência de variação de preço
    por unidade de volume. Ele identifica diferentes estados do mercado com base
    na variação do MFI e do volume.
    
    Parâmetros:
    - period: Período para cálculo de médias móveis para suavização
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos os dados necessários
    required_columns = ['high', 'low', 'volume']
    
    # Converter nomes de colunas para minúsculas se necessário
    stock_data.columns = [col.lower() for col in stock_data.columns]
    
    for col in required_columns:
        if col not in stock_data.columns:
            raise ValueError(f"Coluna {col} não encontrada nos dados.")
    
    # Calcular o Market Facilitation Index (MFI)
    stock_data['mfi'] = (stock_data['high'] - stock_data['low']) / stock_data['volume']
    
    # Substituir valores infinitos ou NaN (se divisão por zero no volume)
    stock_data['mfi'] = stock_data['mfi'].replace([np.inf, -np.inf], np.nan)
    stock_data['mfi'] = stock_data['mfi'].fillna(method='ffill')  # Forward fill
    
    # Suavizar o MFI com uma média móvel
    stock_data['mfi_smooth'] = stock_data['mfi'].rolling(window=period).mean()
    
    # Calcular mudanças no MFI e no volume em relação ao período anterior
    stock_data['mfi_change'] = stock_data['mfi_smooth'].diff()
    stock_data['volume_change'] = stock_data['volume'].diff()
    
    # Determinar os estados do mercado segundo Bill Williams
    # 1. Green - MFI aumentando e Volume aumentando (força do mercado)
    # 2. Fade - MFI diminuindo e Volume diminuindo (exaustão do mercado)
    # 3. Fake - MFI aumentando e Volume diminuindo (movimento falso)
    # 4. Squat - MFI diminuindo e Volume aumentando (bloqueio na tendência)
    
    stock_data['green'] = (stock_data['mfi_change'] > 0) & (stock_data['volume_change'] > 0)
    stock_data['fade'] = (stock_data['mfi_change'] < 0) & (stock_data['volume_change'] < 0)
    stock_data['fake'] = (stock_data['mfi_change'] > 0) & (stock_data['volume_change'] < 0)
    stock_data['squat'] = (stock_data['mfi_change'] < 0) & (stock_data['volume_change'] > 0)
    
    # Extrair valores atuais
    current_mfi = stock_data['mfi_smooth'].iloc[-1]
    current_mfi_change = stock_data['mfi_change'].iloc[-1]
    current_volume_change = stock_data['volume_change'].iloc[-1]
    
    current_green = stock_data['green'].iloc[-1]
    current_fade = stock_data['fade'].iloc[-1]
    current_fake = stock_data['fake'].iloc[-1]
    current_squat = stock_data['squat'].iloc[-1]
    
    # Determinar sequências de estados
    green_sequence = 0
    fade_sequence = 0
    fake_sequence = 0
    squat_sequence = 0
    
    if len(stock_data) >= 3:
        # Contar sequências dos últimos 3 períodos
        for i in range(1, 4):
            if i <= len(stock_data) and stock_data['green'].iloc[-i]:
                green_sequence += 1
            if i <= len(stock_data) and stock_data['fade'].iloc[-i]:
                fade_sequence += 1
            if i <= len(stock_data) and stock_data['fake'].iloc[-i]:
                fake_sequence += 1
            if i <= len(stock_data) and stock_data['squat'].iloc[-i]:
                squat_sequence += 1
    
    # Gerar sinais de compra e venda
    # Comprar: Sequência de "Green" (força do mercado)
    # Vender: Sequência de "Fade" (exaustão do mercado) ou "Squat" (bloqueio na tendência)
    
    buy_signal = green_sequence >= 2
    sell_signal = fade_sequence >= 2 or squat_sequence >= 2
    
    # Ajuste para 'Fake' (movimento falso) - suprimir sinais de compra
    if fake_sequence >= 2:
        buy_signal = False
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Market Facilitation Index")
        print(f" | Período: {period}")
        print(f" | MFI Atual: {current_mfi:.6f}")
        print(f" | Variação MFI: {current_mfi_change:.6f}")
        print(f" | Variação Volume: {current_volume_change:.0f}")
        print(f" | Estado Green (Força): {current_green}")
        print(f" | Estado Fade (Exaustão): {current_fade}")
        print(f" | Estado Fake (Falso): {current_fake}")
        print(f" | Estado Squat (Bloqueio): {current_squat}")
        print(f" | Sequência Green: {green_sequence}")
        print(f" | Sequência Fade: {fade_sequence}")
        print(f" | Sequência Fake: {fake_sequence}")
        print(f" | Sequência Squat: {squat_sequence}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision