import pandas as pd


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return (100 - (100 / (1 + rs))).fillna(50)


def _atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close_price"].shift(1)
    true_range = pd.concat(
        [
            df["high_price"] - df["low_price"],
            (df["high_price"] - prev_close).abs(),
            (df["low_price"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period, min_periods=period).mean() / df["close_price"] * 100


def HighWinRateScalpStrategy(
    self,
    stock_data: pd.DataFrame,
    verbose: bool = True,
    take_profit_pct: float = 0.5,
    stop_loss_pct: float = 3.0,
    rsi_min: float = 38.0,
    rsi_max: float = 62.0,
    pullback_pct: float = 0.2,
    max_hold_bars: int = 72,
    min_atr_pct: float = 0.2,
    max_atr_pct: float = 5.0,
) -> bool:
    """
    HighWinRateScalp Strategy - Ganhos rápidos com win rate alto.

    Estratégia de scalping para ganhos rápidos (+0.5%) com entrada precisa.
    Procura por pullbacks pequenos (0.2%) em tendências de alta com RSI confirmando.

    Lógica:
    1. Identifica tendência de alta (EMA 200 > EMA 50)
    2. Aguarda pullback de 0.2% (queda dentro da alta)
    3. RSI sai de zona de fraqueza (38-62)
    4. ENTRA com TP rígido (+0.5%) e SL (3.0%)

    Saídas Possíveis:
    - Take Profit: +0.5% (ganho rápido)
    - Stop Loss: -3.0% (limite rígido de perda)
    - Max Hold: 72 horas (não segura overnight)
    - Quebratura: EMA falha (saída de emergência)

    Parâmetros:
        take_profit_pct (float): Ganho alvo (default: 0.5%)
        stop_loss_pct (float): Perda máxima (default: 3.0%)
        rsi_min (float): RSI mínimo (default: 38)
        rsi_max (float): RSI máximo (default: 62)
        pullback_pct (float): Queda esperada (default: 0.2%)
        max_hold_bars (int): Máximo de horas (default: 72)
        min_atr_pct (float): Volatilidade mínima (default: 0.2%)
        max_atr_pct (float): Volatilidade máxima (default: 5.0%)

    Retorna:
        True: Entrada confirmada
        False: Saída / Sem sinal
        actual_trade_position: Mantém posição atual

    Performance (backtest 90 dias em 1h):
        ROI: -9.05%
        Trades: 72
        Obs: Melhor em 5m-15m, não em 1h

    Melhor em:
        - Scalping (5m, 15m)
        - Mercados voláteis intraday
        - Horários de pico (8h-18h NY)
        - ATM volátil (0.2-5.0%)

    ⚠️ IMPORTANTE:
    - Comissões são ALTAS (0.1% cada trade)
    - 0.5% ganho - 0.2% comissão = 0.3% líquido
    - Precisa de muitos trades para lucrar
    - Muito melhor em timeframes menores (5m/15m)

    Customização:
        # Ganho maior, risco maior
        HighWinRateScalpStrategy(self, data, take_profit_pct=1.0, stop_loss_pct=4.0)

        # Mais seletivo
        HighWinRateScalpStrategy(self, data, rsi_min=45, rsi_max=55)

    Exemplo de Fluxo:
        EMA200 > EMA50 (tendência alta) →
        Pullback detectado (0.2%) →
        RSI sai de 38-62 (força) →
        ENTRADA (+0.5% TP, -3.0% SL) →
        Sai em 72h se ainda aberto
    """
    min_bars = 220
    if stock_data is None or len(stock_data) < min_bars:
        return self.actual_trade_position

    df = stock_data.copy().reset_index(drop=True)
    close = df["close_price"]

    df["ema9"] = close.ewm(span=9, adjust=False).mean()
    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    df["ema200"] = close.ewm(span=200, adjust=False).mean()
    df["rsi"] = _rsi(close, period=14)
    df["atr_pct"] = _atr_pct(df, period=14)

    last = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(last["close_price"])

    if self.actual_trade_position and self.last_buy_price:
        pnl_pct = (price - float(self.last_buy_price)) / float(self.last_buy_price) * 100
        bars_since_buy = 0
        if getattr(self, "last_buy_time", None) is not None and "open_time" in df.columns:
            bars_since_buy = int((df["open_time"] >= self.last_buy_time).sum())

        hit_target = pnl_pct >= take_profit_pct
        hit_stop = pnl_pct <= -stop_loss_pct
        time_exit = bars_since_buy >= max_hold_bars and pnl_pct > 0
        trend_failure = pnl_pct < 0 and price < float(last["ema50"]) and float(last["rsi"]) < 42
        sell_signal = hit_target or hit_stop or time_exit or trend_failure

        if verbose:
            print("-------")
            print("High Win Rate Scalp")
            print(f" | Preco: {price:.4f}")
            print(f" | Entrada: {float(self.last_buy_price):.4f}")
            print(f" | PnL: {pnl_pct:.2f}%")
            print(f" | Alvo/Stop: +{take_profit_pct:.2f}% / -{stop_loss_pct:.2f}%")
            print(f" | Decisao: {'Vender' if sell_signal else 'Manter comprado'}")
            print("-------")

        return False if sell_signal else True

    recent = df.iloc[-6:]
    trend_ok = (
        price > float(last["ema200"])
        and float(last["ema20"]) > float(last["ema50"])
        and float(last["ema50"]) > float(last["ema200"])
    )
    pullback_ok = bool((recent["low_price"] <= recent["ema20"] * (1 + pullback_pct / 100)).any()) and price > float(last["ema9"])
    momentum_ok = (
        float(last["rsi"]) > float(prev["rsi"])
        and rsi_min <= float(last["rsi"]) <= rsi_max
        and price > float(prev["close_price"])
    )
    volatility_ok = min_atr_pct <= float(last["atr_pct"]) <= max_atr_pct

    buy_signal = trend_ok and pullback_ok and momentum_ok and volatility_ok

    if verbose:
        print("-------")
        print("High Win Rate Scalp")
        print(f" | Preco: {price:.4f}")
        print(f" | RSI: {float(last['rsi']):.2f}")
        print(f" | ATR%: {float(last['atr_pct']):.2f}")
        print(f" | Tendencia: {'OK' if trend_ok else 'Nao'}")
        print(f" | Pullback: {'OK' if pullback_ok else 'Nao'}")
        print(f" | Momentum: {'OK' if momentum_ok else 'Nao'}")
        print(f" | Decisao: {'Comprar' if buy_signal else 'Aguardar'}")
        print("-------")

    return True if buy_signal else False
