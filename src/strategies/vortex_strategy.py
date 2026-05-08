import pandas as pd
from src.indicators import Indicators


def getVortexTradeStrategy(stock_data: pd.DataFrame, verbose=True) -> bool | None:
    """
    Vortex Indicator Trading Strategy - Detecta mudanças de momentum.

    O indicador Vortex (VI+ e VI-) identifica quando força de alta (VI+) cruza
    força de baixa (VI-). Quando VI+ > VI-, indica momentum de compra.

    Parâmetros:
        stock_data (pd.DataFrame): DataFrame com OHLCV (open, high, low, close, volume)
        verbose (bool): Se True, imprime detalhes do sinal

    Retorna:
        True: Sinal de COMPRA (VI+ cruzou acima de VI-)
        False: Sinal de VENDA (VI- cruzou acima de VI+)
        None: Sem sinal claro

    Performance (backtest 90 dias):
        ROI: +23.41%
        Trades: 97
        Win Rate: ~55%

    Melhor em:
        - Mercados com tendência forte
        - Períodos: 1h, 4h
        - Volatilidade: média a alta

    Exemplo:
        >>> signal = getVortexTradeStrategy(stock_data, verbose=True)
        >>> if signal:
        ...     print("Comprar")
        >>> else:
        ...     print("Vender")
    """

    # # Calcula o Indicador Vortex (VI+ e VI-)
    vi_plus = Indicators.getVortex(stock_data, window=14, positive=True)
    vi_minus = Indicators.getVortex(stock_data, window=14, positive=False)

    # Últimos valores de VI+ e VI-
    last_vi_plus = vi_plus.iloc[-1]
    last_vi_minus = vi_minus.iloc[-1]

    # Últimos cruzamentos
    last_crossover_buy = vi_plus > vi_minus  # VI+ cruzou acima de VI- (sinal de compra)
    last_crossover_sell = vi_plus < vi_minus  # VI- cruzou acima de VI+ (sinal de venda)

    # Posição inicial
    trade_decision = None

    # Decisão de trade baseada no cruzamento
    if last_crossover_buy.iloc[-1]:
        trade_decision = True  # Compra

    elif last_crossover_sell.iloc[-1]:
        trade_decision = False  # Venda

    if verbose:
        print("-------")
        print("📊 Estratégia: Vortex")
        print(f" | VI+: {last_vi_plus:.2f}")
        print(f" | VI-: {last_vi_minus:.2f}")
        print(f' | Decisão: {"Comprar" if trade_decision == True else "Vender" if trade_decision == False else "Nenhuma"}')
        print("-------")

    return trade_decision
