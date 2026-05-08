"""Adapter pra estratégias da pasta extras/: elas esperam colunas
'close', 'high', 'low', 'open' (lowercase) mas nossa stock_data usa
'close_price', 'high_price', etc. Esta função wrappa e mapeia."""
import pandas as pd


def _adapt_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    pairs = [("close_price", "close"), ("high_price", "high"),
             ("low_price", "low"), ("open_price", "open")]
    for src, dst in pairs:
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]
    return df


def wrap_extras_strategy(fn, canonical_name: str | None = None):
    """Wraps a extras strategy (que espera 'close'/'high'/etc)
    pra aceitar stock_data com 'close_price'/'high_price'/etc.

    `canonical_name` fixa o identificador usado em paper_balance/state.json,
    blindando estatisticas contra mudancas de __name__ nas funcoes originais.
    """
    def wrapped(stock_data, verbose=True, **kwargs):
        adapted = _adapt_columns(stock_data)
        return fn(adapted, verbose=verbose, **kwargs)
    wrapped.__name__ = f"adapted_{fn.__name__}"
    if canonical_name:
        wrapped.__strategy_name__ = canonical_name
    return wrapped


# Carrega as 5 estratégias usadas no portfolio best-of-each
from src.strategies.extras.atr_strategy import getATRTradeStrategy
from src.strategies.extras.tema_strategy import getTEMATradeStrategy
from src.strategies.extras.detrended_price_oscillator_strategy import getDetrendedPriceOscillatorTradeStrategy
from src.strategies.extras.accelerator_oscillator_strategy import getAcceleratorOscillatorTradeStrategy
from src.strategies.extras.hull_moving_average_strategy import getHullMovingAverageTradeStrategy


ATR_STRATEGY = wrap_extras_strategy(getATRTradeStrategy, canonical_name="ATR")
TEMA_STRATEGY = wrap_extras_strategy(getTEMATradeStrategy, canonical_name="TEMA")
DPO_STRATEGY = wrap_extras_strategy(getDetrendedPriceOscillatorTradeStrategy, canonical_name="DPO")
ACCELERATOR_STRATEGY = wrap_extras_strategy(getAcceleratorOscillatorTradeStrategy, canonical_name="Accelerator")
HULL_MA_STRATEGY = wrap_extras_strategy(getHullMovingAverageTradeStrategy, canonical_name="HullMA")
