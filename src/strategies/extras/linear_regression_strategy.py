import pandas as pd
import numpy as np
import os
import sys
from datetime import datetime
from scipy import stats # Importar stats do SciPy para regressão linear  | pip install scipy

# Configuração de caminhos para importações
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Adicionar diretórios ao sys.path
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, SRC_DIR)

def getLinearRegressionTradeStrategy(
    stock_data: pd.DataFrame,
    period: int = 14,
    deviation_mult: float = 2.0,
    forecast_periods: int = 3,
    verbose: bool = True
):
    """
    Estratégia baseada em Linear Regression.
    
    Esta estratégia usa regressão linear para determinar a tendência e canais
    de desvio padrão para gerar sinais de negociação.
    
    Parâmetros:
    - period: Período para cálculo da linha de regressão
    - deviation_mult: Multiplicador para determinar a largura do canal de desvio
    - forecast_periods: Número de períodos para prever no futuro
    """
    stock_data = stock_data.copy()
    
    # Verificar se temos a coluna 'close'
    if 'close' not in stock_data.columns:
        # Tentar converter para minúsculas
        stock_data.columns = [col.lower() for col in stock_data.columns]
        if 'close' not in stock_data.columns:
            raise ValueError("Coluna 'close' não encontrada nos dados.")
    
    # Adicionar uma coluna de tempo (índice sequencial)
    stock_data['time_index'] = range(len(stock_data))
    
    # Função para calcular a regressão linear e os canais
    def calculate_regression(data, period, x_col, y_col, dev_mult):
        result = pd.DataFrame(index=data.index)
        result['regression'] = np.nan
        result['upper_channel'] = np.nan
        result['lower_channel'] = np.nan
        result['slope'] = np.nan
        result['r_squared'] = np.nan
        result['forecast'] = np.nan
        
        for i in range(period-1, len(data)):
            # Dados para regressão
            x = data[x_col].iloc[i-period+1:i+1].values
            y = data[y_col].iloc[i-period+1:i+1].values
            
            # Calcular regressão linear
            slope, intercept, r_value, p_value, std_err = stats.linregress(x, y)
            
            # Valores previstos
            predicted = intercept + slope * x
            
            # Desvio padrão dos resíduos
            residuals = y - predicted
            std_dev = np.std(residuals)
            
            # Valor da regressão no ponto atual
            current_x = data[x_col].iloc[i]
            regression_value = intercept + slope * current_x
            
            # Canais de desvio padrão
            upper_channel = regression_value + dev_mult * std_dev
            lower_channel = regression_value - dev_mult * std_dev
            
            # Valor previsto para períodos futuros
            forecast_x = current_x + forecast_periods
            forecast_value = intercept + slope * forecast_x
            
            # Armazenar resultados
            result.loc[data.index[i], 'regression'] = regression_value
            result.loc[data.index[i], 'upper_channel'] = upper_channel
            result.loc[data.index[i], 'lower_channel'] = lower_channel
            result.loc[data.index[i], 'slope'] = slope
            result.loc[data.index[i], 'r_squared'] = r_value ** 2
            result.loc[data.index[i], 'forecast'] = forecast_value
        
        return result
    
    # Calcular regressão linear e canais
    regression_data = calculate_regression(
        stock_data, period, 'time_index', 'close', deviation_mult
    )
    
    # Mesclar os resultados com os dados originais
    stock_data = pd.concat([stock_data, regression_data], axis=1)
    
    # Extrair valores atuais
    current_close = stock_data['close'].iloc[-1]
    current_regression = stock_data['regression'].iloc[-1]
    current_upper = stock_data['upper_channel'].iloc[-1]
    current_lower = stock_data['lower_channel'].iloc[-1]
    current_slope = stock_data['slope'].iloc[-1]
    current_r_squared = stock_data['r_squared'].iloc[-1]
    current_forecast = stock_data['forecast'].iloc[-1]
    
    # Determinar posição do preço em relação aos canais
    is_above_upper = current_close > current_upper
    is_below_lower = current_close < current_lower
    is_inside_channel = not is_above_upper and not is_below_lower
    
    # Verificar valores anteriores para detectar cruzamentos
    prev_close = stock_data['close'].iloc[-2] if len(stock_data) > 1 else current_close
    prev_regression = stock_data['regression'].iloc[-2] if len(stock_data) > 1 else current_regression
    prev_upper = stock_data['upper_channel'].iloc[-2] if len(stock_data) > 1 else current_upper
    prev_lower = stock_data['lower_channel'].iloc[-2] if len(stock_data) > 1 else current_lower
    
    # Detectar cruzamentos com os canais e a linha de regressão
    upper_cross_up = (current_close > current_upper) and (prev_close <= prev_upper)
    upper_cross_down = (current_close < current_upper) and (prev_close >= prev_upper)
    lower_cross_up = (current_close > current_lower) and (prev_close <= prev_lower)
    lower_cross_down = (current_close < current_lower) and (prev_close >= prev_lower)
    
    regression_cross_up = (current_close > current_regression) and (prev_close <= prev_regression)
    regression_cross_down = (current_close < current_regression) and (prev_close >= prev_regression)
    
    # Determinar a tendência pela inclinação
    is_uptrend = current_slope > 0
    is_strong_uptrend = current_slope > 0 and current_r_squared > 0.7
    is_strong_downtrend = current_slope < 0 and current_r_squared > 0.7
    
    # Comparar previsão com preço atual
    is_forecast_higher = current_forecast > current_close
    
    # Gerar sinais de compra e venda
    # Comprar: 
    # 1. Preço cruza acima da linha de regressão em tendência de alta
    # 2. Preço cruza acima da banda inferior em tendência de alta forte
    # 3. Preço dentro do canal, tendência de alta forte e previsão mais alta
    
    # Vender:
    # 1. Preço cruza abaixo da linha de regressão em tendência de baixa
    # 2. Preço cruza abaixo da banda superior em tendência de baixa forte
    # 3. Preço dentro do canal, tendência de baixa forte e previsão mais baixa
    
    buy_signal = (regression_cross_up and is_uptrend) or \
                (lower_cross_up and is_strong_uptrend) or \
                (is_inside_channel and is_strong_uptrend and is_forecast_higher)
    
    sell_signal = (regression_cross_down and not is_uptrend) or \
                 (upper_cross_down and is_strong_downtrend) or \
                 (is_inside_channel and is_strong_downtrend and not is_forecast_higher)
    
    # Decisão final
    trade_decision = None
    
    if buy_signal:
        trade_decision = True  # Comprar
    elif sell_signal:
        trade_decision = False  # Vender
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: Linear Regression")
        print(f" | Período: {period}")
        print(f" | Multiplicador de Desvio: {deviation_mult}")
        print(f" | Períodos de Previsão: {forecast_periods}")
        print(f" | Preço Atual: {current_close:.2f}")
        print(f" | Valor da Regressão: {current_regression:.2f}")
        print(f" | Canal Superior: {current_upper:.2f}")
        print(f" | Canal Inferior: {current_lower:.2f}")
        print(f" | Inclinação: {current_slope:.6f}")
        print(f" | R²: {current_r_squared:.4f}")
        print(f" | Previsão para {forecast_periods} período(s): {current_forecast:.2f}")
        print(f" | Preço Acima do Canal Superior: {is_above_upper}")
        print(f" | Preço Abaixo do Canal Inferior: {is_below_lower}")
        print(f" | Preço Dentro do Canal: {is_inside_channel}")
        print(f" | Tendência de Alta: {is_uptrend}")
        print(f" | Tendência de Alta Forte: {is_strong_uptrend}")
        print(f" | Tendência de Baixa Forte: {is_strong_downtrend}")
        print(f" | Previsão Mais Alta: {is_forecast_higher}")
        print(f" | Decisão: {'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}")
        print("-------")
    
    return trade_decision