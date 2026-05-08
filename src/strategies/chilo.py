import pandas as pd
import numpy as np


def getChiloStrategy(
    stock_data: pd.DataFrame,
    length: int = 34,
    offset: int = 0,
    hilo_type: str = "HiLo",  # options: "HiLo", "HiLo Activator"
    ma_type: str = "SMA",  # options: "SMA", "EMA"
    verbose: bool = True,
) -> bool:
    """
    Calcula o indicador Custom HiLo e retorna uma decisão de trade:
    True = Comprar, False = Vender.

    Parâmetros:
    - stock_data: DataFrame com colunas ['high', 'low', 'close']
    - length: número de períodos para média/hi-lo (default=34)
    - offset: deslocamento para comparação (default=0)
    - hilo_type: "HiLo" (simplificado) ou "HiLo Activator" (usa high/low extremes)
    - ma_type: "SMA" ou "EMA"
    - verbose: se True, imprime detalhes no formato padrão

    Retorna:
    - chilo_trade_decision: bool (sempre True ou False)
    """
    # Evita SettingWithCopyWarning
    df = stock_data.copy()

    # Verifica dados suficientes
    min_required = length + offset + 1
    if len(df) < min_required:
        if verbose:
            print("⚠️ Dados insuficientes para cálculo do HiLo. Retornando False...")
        return False

    # Médias de high/low
    if ma_type.upper() == "EMA":
        hima = df["high_price"].ewm(span=length, adjust=False).mean()
        loma = df["low_price"].ewm(span=length, adjust=False).mean()
    else:
        hima = df["high_price"].rolling(window=length).mean()
        loma = df["low_price"].rolling(window=length).mean()

    simple_hilo = hilo_type == "HiLo"

    # HiLo Activator extras
    if not simple_hilo:
        hihi = df["high_price"].rolling(window=length).max()
        lolo = df["low_price"].rolling(window=length).min()
    else:
        hihi = lolo = pd.Series(np.nan, index=df.index)

    # Inicializa série de hilo
    hilo = pd.Series(index=df.index, dtype=float)

    # Calcula iterativamente
    for i in range(len(df)):
        if i < length + offset:
            hilo.iloc[i] = np.nan
            continue

        close_i = df["close_price"].iat[i]
        hima_off = hima.iat[i - offset]
        loma_off = loma.iat[i - offset]

        if simple_hilo:
            if close_i < loma_off:
                hilo_val = hima.iat[i]
            elif close_i > hima_off:
                hilo_val = loma.iat[i]
            else:
                hilo_val = hilo.iat[i - 1]
        else:
            if close_i < loma_off:
                hilo_val = hihi.iat[i]
            elif close_i > hima_off:
                hilo_val = lolo.iat[i]
            else:
                hilo_val = hilo.iat[i - 1]

        hilo.iloc[i] = hilo_val

    # Sinais de cruzamento
    buy_arith = np.sign(df["close_price"] - hima.shift(offset))
    sell_arith = np.sign(df["close_price"] - loma.shift(offset))

    buy = (buy_arith.shift(1) <= 0.5) & (buy_arith > 0.5)
    sell = (sell_arith.shift(1) >= -0.5) & (sell_arith < -0.5)

    # Estado de compra/venda
    buy_state = pd.Series(False, index=df.index)
    for i in range(1, len(df)):
        if buy.iat[i]:
            buy_state.iat[i] = True
        elif sell.iat[i]:
            buy_state.iat[i] = False
        else:
            buy_state.iat[i] = buy_state.iat[i - 1]

    chilo_trade_decision = bool(buy_state.iat[-1])

    # Verbose prints
    if verbose:
        last_hima = hima.iat[-1]
        last_loma = loma.iat[-1]
        last_hilo = hilo.iat[-1]

        print("-------")
        print("📊 Estratégia: Custom HiLo")
        print(f" | Última HiMA: {last_hima:.3f}")
        print(f" | Última LoMA: {last_loma:.3f}")
        print(f" | Último HiLo: {last_hilo:.3f}")
        print(f" | Decisão: {'Comprar' if chilo_trade_decision else 'Vender'}")
        print("-------")

    return chilo_trade_decision
