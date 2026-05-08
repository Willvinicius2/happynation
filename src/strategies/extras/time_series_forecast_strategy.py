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

def getTimeSeriesForecastTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    ma_period: int = 9,
    forecast_periods: int = 1,
    use_close: bool = True,
    verbose: bool = True
):
    """
    Estratégia baseada em Time Series Forecast.
    
    Parâmetros:
    - period: Período para cálculo da regressão linear
    - ma_period: Período para a média móvel do TSF
    - forecast_periods: Períodos à frente para previsão
    - use_close: Usar preço de fechamento para cálculos
    """
    stock_data = stock_data.copy()
    
    # Verificar se a coluna de preço existe
    price_col = 'close' if use_close else 'open'
    if price_col not in stock_data.columns:
        raise ValueError(f"Coluna '{price_col}' não encontrada nos dados")
    
    # Verificar se há dados suficientes
    if len(stock_data) <= period:
        return None  # Dados insuficientes para cálculo
    
    # Função para calcular a previsão de regressão linear para um ponto no futuro
    def linear_regression_forecast(series, period, forecast_periods=1):
        # Criar array de índices (x) para a regressão
        x = np.arange(period)
        # Inicializar array para os resultados
        result = np.zeros(len(series))
        result[:] = np.nan
        
        # Percorrer a série para calcular a regressão para cada janela
        for i in range(period - 1, len(series)):
            # Obter os últimos 'period' valores (y)
            y = series.iloc[i - period + 1:i + 1].values
            
            # Calcular a regressão linear (mx + b)
            m, b = np.polyfit(x, y, 1)
            
            # Calcular o valor previsto para 'forecast_periods' à frente
            forecast = m * (period - 1 + forecast_periods) + b
            result[i] = forecast
        
        return pd.Series(result, index=series.index)
    
    # Calcular o Time Series Forecast (previsão n períodos à frente)
    stock_data['tsf'] = linear_regression_forecast(stock_data[price_col], period, forecast_periods)
    
    # Calcular a inclinação da linha de regressão (tendência)
    def calculate_slope(series, period):
        slope = np.zeros(len(series))
        slope[:] = np.nan
        
        for i in range(period - 1, len(series)):
            y = series.iloc[i - period + 1:i + 1].values
            x = np.arange(period)
            m, b = np.polyfit(x, y, 1)
            slope[i] = m
        
        return pd.Series(slope, index=series.index)
    
    stock_data['tsf_slope'] = calculate_slope(stock_data[price_col], period)
    
    # Calcular a média móvel do TSF
    stock_data['tsf_ma'] = stock_data['tsf'].rolling(window=ma_period).mean()
    
    # Gerar sinais de negociação baseados na relação entre TSF e preço atual
    stock_data['signal'] = np.where(
        stock_data['tsf'] > stock_data[price_col], 1,  # TSF acima do preço: sinal de compra (expectativa de alta)
        np.where(stock_data['tsf'] < stock_data[price_col], -1, 0)  # TSF abaixo do preço: sinal de venda (expectativa de baixa)
    )
    
    # Detectar cruzamentos entre TSF e sua média móvel
    stock_data['cross_signal'] = np.where(
        (stock_data['tsf'].shift(1) <= stock_data['tsf_ma'].shift(1)) & 
        (stock_data['tsf'] > stock_data['tsf_ma']), 1,  # Cruzamento para cima: sinal de compra
        np.where(
            (stock_data['tsf'].shift(1) >= stock_data['tsf_ma'].shift(1)) & 
            (stock_data['tsf'] < stock_data['tsf_ma']), -1,  # Cruzamento para baixo: sinal de venda
            0  # Sem cruzamento: neutro
        )
    )
    
    # Verificar as condições atuais
    last_price = stock_data[price_col].iloc[-1] if not stock_data.empty else 0
    last_tsf = stock_data['tsf'].iloc[-1] if not stock_data.empty else 0
    last_tsf_ma = stock_data['tsf_ma'].iloc[-1] if not stock_data.empty else 0
    last_slope = stock_data['tsf_slope'].iloc[-1] if not stock_data.empty else 0
    last_signal = stock_data['signal'].iloc[-1] if not stock_data.empty else 0
    last_cross_signal = stock_data['cross_signal'].iloc[-1] if not stock_data.empty else 0
    
    # Decisão de negociação
    # Prioridade: cruzamento de MA > relação TSF/preço > inclinação
    if last_cross_signal == 1:
        trade_decision = True  # Comprar no cruzamento para cima da MA
    elif last_cross_signal == -1:
        trade_decision = False  # Vender no cruzamento para baixo da MA
    elif last_signal == 1 and last_slope > 0:
        trade_decision = True  # Comprar quando TSF está acima do preço e a inclinação é positiva
    elif last_signal == -1 and last_slope < 0:
        trade_decision = False  # Vender quando TSF está abaixo do preço e a inclinação é negativa
    elif last_slope > 0:
        trade_decision = True  # Tendência a comprar quando a inclinação é positiva
    elif last_slope < 0:
        trade_decision = False  # Tendência a vender quando a inclinação é negativa
    else:
        trade_decision = None  # Neutro
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Time Series Forecast")
        print(f" | Período: {period}")
        print(f" | Período MA: {ma_period}")
        print(f" | Períodos de Previsão: {forecast_periods}")
        print(f" | Último Preço: {last_price:.2f}")
        print(f" | Último TSF: {last_tsf:.2f}")
        print(f" | Último TSF MA: {last_tsf_ma:.2f}")
        print(f" | Inclinação: {last_slope:.4f}")
        print(f" | Sinal TSF/Preço: {last_signal}")
        print(f" | Sinal de Cruzamento: {last_cross_signal}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision