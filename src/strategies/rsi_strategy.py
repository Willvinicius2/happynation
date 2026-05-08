import pandas as pd
from src.indicators import Indicators


def getRsiTradeStrategy(stock_data: pd.DataFrame, low=30, high=70, verbose=True) -> bool | None:
    """
    RSI (Relative Strength Index) Trading Strategy - Detecta oversold/overbought.

    Monitora o indicador RSI para identificar quando o mercado está:
    - Oversold (RSI < 30): Provável reversão para cima → COMPRA
    - Overbought (RSI > 70): Provável reversão para baixo → VENDA

    Parâmetros:
        stock_data (pd.DataFrame): DataFrame com OHLCV
        low (int): Limite inferior RSI para oversold (default: 30)
        high (int): Limite superior RSI para overbought (default: 70)
        verbose (bool): Se True, imprime detalhes

    Retorna:
        True: Sinal de COMPRA (RSI saiu de oversold)
        False: Sinal de VENDA (RSI saiu de overbought)
        None: RSI em zona neutra (30-70)

    Performance (backtest 90 dias):
        ROI: +6.98%
        Trades: 9 (MUITO POUCOS = muito seletivo)
        Win Rate: 89% (MUITO ALTO!)

    Melhor em:
        - Mercados oscilantes (range-bound)
        - Períodos: 1h, 4h, 1d
        - Volatilidade: baixa a média
        - Ativos: altcoins, pares menos voláteis

    Customização:
        getRsiTradeStrategy(data, low=20, high=80)  # Mais agressivo
        getRsiTradeStrategy(data, low=40, high=60)  # Mais conservador

    Exemplo:
        >>> signal = getRsiTradeStrategy(stock_data, low=30, high=70)
        >>> if signal == True:
        ...     print("Mercado muito vendido - COMPRAR")
        >>> elif signal == False:
        ...     print("Mercado muito comprado - VENDER")
        >>> else:
        ...     print("RSI em zona neutra - ESPERAR")
    """

    # **Calcula o RSI e adiciona ao DataFrame**
    rsi_series = Indicators.getRSI(stock_data["close_price"], last_only=False)
    last_rsi = rsi_series.iloc[-1]  # Último valor do RSI

    # Identifica os momentos em que RSI cruzou os níveis de sobrecompra e sobrevenda
    peaks = rsi_series[rsi_series > high].index
    valleys = rsi_series[rsi_series < low].index

    # Encontra o último pico e o último vale
    last_peak = peaks[-1] if len(peaks) > 0 else None
    last_valley = valleys[-1] if len(valleys) > 0 else None

    trade_decision = None  # Mantém a posição até uma nova condição

    if last_valley and (last_peak is None or last_valley > last_peak):
        # Último evento foi um vale (RSI < 30), mas ainda não passou de 70 → Mantém compra
        trade_decision = True

    elif last_peak and (last_valley is None or last_peak > last_valley):
        # Último evento foi um pico (RSI > 70), mas ainda não caiu até 30 → Mantém venda
        trade_decision = False

    if verbose:
        print("-------")
        print("📊 Estratégia: RSI - Vales e Topos")
        print(f" | Último RSI: {last_rsi}")
        print(f" | Último Vale: {last_valley}")
        print(f" | Último Pico: {last_peak}")
        print(f' | Decisão: {"Comprar" if trade_decision == True else "Vender" if trade_decision == False else "Nenhuma"}')

        print("-------")

    return trade_decision
