import pandas as pd


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return (100 - (100 / (1 + rs))).fillna(50)


def _tema(series: pd.Series, period: int) -> pd.Series:
    ema1 = series.ewm(span=period, adjust=False).mean()
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    ema3 = ema2.ewm(span=period, adjust=False).mean()
    return 3 * ema1 - 3 * ema2 + ema3


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


def ChampionsStrategy(
    self,
    stock_data: pd.DataFrame,
    verbose: bool = True,
    take_profit_pct: float = 0.9,
    stop_loss_pct: float = 2.0,
    rsi_min: float = 40.0,
    rsi_max: float = 64.0,
    pullback_pct: float = 0.7,
    max_hold_bars: int = 24,
    min_atr_pct: float = 0.10,
    max_atr_pct: float = 7.0,
    min_volume_factor: float = 0.30,
) -> bool | None:
    """
    Estrategia dos Campeoes.

    Combina filtros dos melhores resultados historicos do projeto:
    tendencia por EMAs, confirmacao TEMA, volatilidade ATR e retomada por RSI.
    A saida principal fica alinhada ao take profit/stop do robo.
    """
    min_bars = 220
    if stock_data is None or len(stock_data) < min_bars:
        return self.actual_trade_position

    df = stock_data.copy().reset_index(drop=True)
    close = df["close_price"]

    for window in [8, 20, 50, 200]:
        df[f"ema{window}"] = close.ewm(span=window, adjust=False).mean()

    df["tema7"] = _tema(close, 7)
    df["tema21"] = _tema(close, 21)
    df["tema21_slope"] = df["tema21"].diff()
    df["rsi"] = _rsi(close, period=14)
    df["atr_pct"] = _atr_pct(df, period=14)
    df["volume_ma24"] = df["volume"].rolling(24, min_periods=24).mean()
    df["ret3_pct"] = close.pct_change(3) * 100

    last = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(last["close_price"])

    if self.actual_trade_position and self.last_buy_price:
        entry = float(self.last_buy_price)
        pnl_pct = (price - entry) / entry * 100
        bars_since_buy = 0
        if getattr(self, "last_buy_time", None) is not None and "open_time" in df.columns:
            bars_since_buy = int((df["open_time"] >= self.last_buy_time).sum())

        hit_target = pnl_pct >= take_profit_pct
        hit_stop = pnl_pct <= -stop_loss_pct
        time_exit = bars_since_buy >= max_hold_bars and pnl_pct > 0
        protect_profit = pnl_pct > 0.20 and price < float(last["ema20"]) and float(last["rsi"]) < 48
        trend_failure = pnl_pct < 0 and price < float(last["ema50"]) and float(last["rsi"]) < 43
        sell_signal = hit_target or hit_stop or time_exit or protect_profit or trend_failure

        if verbose:
            print("-------")
            print("Estrategia dos Campeoes")
            print(f" | Preco: {price:.4f}")
            print(f" | Entrada: {entry:.4f}")
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
    champion_ok = (
        float(last["tema7"]) > float(last["tema21"])
        and float(last["tema21_slope"]) > 0
    )
    pullback_ok = (
        bool((recent["low_price"] <= recent["ema20"] * (1 + pullback_pct / 100)).any())
        and price > float(last["ema8"])
    )
    momentum_ok = (
        float(last["rsi"]) > float(prev["rsi"])
        and rsi_min <= float(last["rsi"]) <= rsi_max
        and price > float(prev["close_price"])
        and float(last["ret3_pct"]) > -2.5
    )
    volatility_ok = min_atr_pct <= float(last["atr_pct"]) <= max_atr_pct
    volume_ok = float(last["volume"]) >= float(last["volume_ma24"]) * min_volume_factor

    buy_signal = trend_ok and champion_ok and pullback_ok and momentum_ok and volatility_ok and volume_ok

    if verbose:
        print("-------")
        print("Estrategia dos Campeoes")
        print(f" | Preco: {price:.4f}")
        print(f" | RSI: {float(last['rsi']):.2f}")
        print(f" | ATR%: {float(last['atr_pct']):.2f}")
        print(f" | Tendencia EMA: {'OK' if trend_ok else 'Nao'}")
        print(f" | TEMA Campeao: {'OK' if champion_ok else 'Nao'}")
        print(f" | Pullback: {'OK' if pullback_ok else 'Nao'}")
        print(f" | Momentum: {'OK' if momentum_ok else 'Nao'}")
        print(f" | Volume: {'OK' if volume_ok else 'Nao'}")
        print(f" | Decisao: {'Comprar' if buy_signal else 'Aguardar'}")
        print("-------")

    return True if buy_signal else None


# Identificador canonico (sobrevive a refactors de __name__).
ChampionsStrategy.__strategy_name__ = "Champions"  # type: ignore[attr-defined]
