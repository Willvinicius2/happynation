# create_strategies.py
import os
import sys

# Lista de estratégias para implementar
strategies = [
    "OBV",
    "ATR",
    "Aroon",
    "MFI",
    "CMF",
    "Chaikin Oscillator",
    "Elder Ray",
    "Force Index",
    "Keltner Channel",
    "Donchian Channel",
    "Pivot Points",
    "PPO",
    "ROC",
    "Ultimate Oscillator",
    "Volume-Weighted Average Price (VWAP)",
    "Williams Alligator",
    "Fractals",
    "Gator Oscillator",
    "Accelerator Oscillator",
    "Awesome Oscillator",
    "Detrended Price Oscillator",
    "Market Facilitation Index",
    "Schaff Trend Cycle",
    "Ehler Fisher Transform",
    "Fisher Transform",
    "Hilbert Transform",
    "Zero-Lag Moving Average",
    "Arnaud Legoux Moving Average",
    "Triangular Moving Average",
    "KAMA",
    "VIDYA",
    "T3 Moving Average",
    "TEMA",
    "WMA",
    "Hull Moving Average",
    "ALMA",
    "Linear Regression",
    "Time Series Forecast",
    "Moving Average Envelope",
    "Price Channels",
    "PSAR",
    "Donchian Channels",
    "Keltner Channels",
    "Ichimoku Cloud",
    "Aroon Oscillator",
    "Chande Momentum Oscillator",
    "True Strength Index",
    "Elder Force Index"
]

# Função para formatar o nome do arquivo a partir do nome da estratégia
def format_file_name(name):
    return name.lower().replace(" ", "_").replace("-", "_").replace("%", "percent").replace("(", "").replace(")", "")

# Função para formatar o nome da função a partir do nome da estratégia
def format_function_name(name):
    # Para nomes com caracteres especiais, manter o formato correto
    special_names = {
        "OBV": "OBV",
        "ATR": "ATR",
        "VWAP": "VWAP",
        "PPO": "PPO",
        "ROC": "ROC",
        "ALMA": "ALMA",
        "KAMA": "KAMA",
        "VIDYA": "VIDYA",
        "TEMA": "TEMA",
        "WMA": "WMA",
        "PSAR": "PSAR"
    }
    
    # Se o nome está no dicionário de nomes especiais, use-o
    for key, value in special_names.items():
        if key in name:
            name = name.replace(key, value)
    
    # Dividir em palavras
    words = name.replace("-", " ").split()
    
    # Capitalizar cada palavra
    for i in range(len(words)):
        if words[i] not in special_names.values():
            words[i] = words[i].capitalize()
    
    # Juntar as palavras
    camel_case = "".join(words)
    
    # Adicionar prefixo "get" e sufixo "TradeStrategy"
    return f"get{camel_case}TradeStrategy"

# Template para o conteúdo do arquivo
template = """import pandas as pd
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

def {function_name}(
    stock_data: pd.DataFrame,
    period: int = 14,
    # TODO Adicione outros parâmetros específicos aqui
    verbose: bool = True
):
    \"\"\"
    Estratégia baseada em {display_name}.
    
    Parâmetros:
    - period: Período para cálculo do indicador
    \"\"\"
    stock_data = stock_data.copy()
    
    # TODO: Implementar a lógica da estratégia {display_name}
    
    # TODO Placeholder para o resultado
    trade_decision = None
    
    if verbose:
        print("-------")
        print(f"📊 Estratégia: {display_name}")
        # TODO Adicione detalhes específicos do indicador aqui
        print(f" | Decisão: {{'Comprar' if trade_decision == True else 'Vender' if trade_decision == False else 'Nenhuma'}}")
        print("-------")
    
    return trade_decision
"""

# Criar arquivo para cada estratégia
for strategy in strategies:
    file_name = format_file_name(f"{strategy}_strategy")
    function_name = format_function_name(strategy)
    file_path = f"{file_name}.py"
    
    # Criar o conteúdo do arquivo
    content = template.format(
        function_name=function_name,
        display_name=strategy
    )
    
    # Escrever o arquivo com codificação UTF-8
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    
    print(f"Arquivo criado: {file_path}")

print("\nTodos os arquivos de estratégia foram criados com sucesso!\n")
print("Para adicionar ao strategies_config.py, use o seguinte código:\n")

# Gerando código para adicionar ao strategies_config.py
print("# Adicionar ao arquivo strategies_config.py:")
for strategy in strategies:
    file_name = format_file_name(f"{strategy}_strategy")
    function_name = format_function_name(strategy)
    
    print(f'"{strategy}": {{')
    print(f'    "module": "strategies.{file_name}",')
    print(f'    "function": "{function_name}",')
    print('    "params": {')
    print('        "period": {"default": 14, "min": 5, "max": 30, "step": 1}')
    print('    }')
    print('},')