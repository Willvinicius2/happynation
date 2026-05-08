import pandas as pd


# Estratégia Simples de Médias Móveis
def getMovingAverageTradeStrategy(stock_data: pd.DataFrame, fast_window=7, slow_window=40, verbose=True) -> bool | None:
    """
    Simple Moving Average (SMA) Crossover Strategy - Segue tendências.

    Estratégia clássica de cruzamento de médias móveis. Usa duas médias:
    - MA Rápida (7 períodos): Responde rápido ao preço
    - MA Lenta (40 períodos): Define a tendência principal

    Sinais:
    - COMPRA: MA Rápida cruza ACIMA da MA Lenta (trend bullish)
    - VENDA: MA Rápida cruza ABAIXO da MA Lenta (trend bearish)

    Parâmetros:
        stock_data (pd.DataFrame): DataFrame com OHLCV
        fast_window (int): Período da média rápida (default: 7)
        slow_window (int): Período da média lenta (default: 40)
        verbose (bool): Se True, imprime detalhes

    Retorna:
        True: COMPRA (MA rápida > MA lenta)
        False: VENDA (MA rápida < MA lenta)
        None: Dados insuficientes

    Performance (backtest 90 dias):
        ROI: -16.60% (negativo neste período)
        Trades: 35
        Win Rate: ~48%
        Obs: Melhor em trends fortes (não funcionou bem em JUP/USDT lateral)

    Melhor em:
        - Trends de longo prazo
        - Períodos: 4h, 1d
        - Volatilidade: alta (precisa de movimento)
        - Ativos: BTC, ETH (líderes de trend)

    Customização:
        # Mais responsivo (mais trades, mais risco)
        getMovingAverageTradeStrategy(data, fast_window=3, slow_window=10)

        # Mais conservador (menos trades, mais confiável)
        getMovingAverageTradeStrategy(data, fast_window=12, slow_window=50)

    Gráfico:
        Preço
        |     ╱─╲  ╱─╲
        |    ╱   ╲╱   ╲
        |___╱──────────╲─ MA Rápida (7)
        |      ╱────────╲
        |     ╱          ╲ MA Lenta (40)

    Exemplo:
        >>> signal = getMovingAverageTradeStrategy(stock_data, fast_window=7, slow_window=40)
        >>> if signal == True:
        ...     print("Trend de alta detectado - COMPRAR")
        >>> elif signal == False:
        ...     print("Trend de baixa detectado - VENDER")
    """

    # Calcula indicadores em Series locais para nao mutar o DataFrame recebido.
    # Calcula as Médias Moveis Rápida e Lenta
    ma_fast = stock_data["close_price"].rolling(window=fast_window).mean()
    ma_slow = stock_data["close_price"].rolling(window=slow_window).mean()

    # 🔹 REMOVE PERÍODOS INICIAIS COM NaN
    valid_rows = pd.concat([ma_fast, ma_slow], axis=1).dropna()

    # Se não houver dados suficientes após remover os NaNs, retorna None
    if len(valid_rows) < slow_window:
        if verbose:
            print("⚠️ Dados insuficientes após remoção de NaN. Pulando período...")
        return None

    # Pega os últimos valores das médias móveis
    last_ma_fast = valid_rows.iloc[-1, 0]
    last_ma_slow = valid_rows.iloc[-1, 1]

    # Toma a decisão com base na posição da média móvel
    trade_decision = last_ma_fast > last_ma_slow  # True = Comprar, False = Vender

    if verbose:
        print("-------")
        print("📊 Estratégia: Moving Average Simples")
        print(f" | Última Média Rápida: {last_ma_fast:.3f}")
        print(f" | Última Média Lenta: {last_ma_slow:.3f}")
        print(f' | Decisão: {"Comprar" if trade_decision == True else "Vender" if trade_decision == False else "Nenhuma"}')

        print("-------")

    return trade_decision
