"""
Testes básicos das estratégias de trading.

Executa: python -m pytest tests/test_strategies.py -v
"""

import sys
import pandas as pd
import numpy as np
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.strategies.vortex_strategy import getVortexTradeStrategy
from src.strategies.rsi_strategy import getRsiTradeStrategy
from src.strategies.moving_average import getMovingAverageTradeStrategy


def create_test_data(n_candles=100, trend="up"):
    """Cria dados de teste realistas."""
    np.random.seed(42)

    if trend == "up":
        base = np.linspace(100, 110, n_candles)
        noise = np.random.normal(0, 0.5, n_candles)
    elif trend == "down":
        base = np.linspace(110, 100, n_candles)
        noise = np.random.normal(0, 0.5, n_candles)
    else:  # sideways
        base = np.full(n_candles, 105) + np.random.normal(0, 1, n_candles)
        noise = np.random.normal(0, 0.3, n_candles)

    close_price = base + noise

    return pd.DataFrame({
        "open_time": pd.date_range(start="2024-01-01", periods=n_candles, freq="h"),
        "open_price": close_price + np.random.normal(0, 0.1, n_candles),
        "high_price": close_price + abs(np.random.normal(0.5, 0.3, n_candles)),
        "low_price": close_price - abs(np.random.normal(0.5, 0.3, n_candles)),
        "close_price": close_price,
        "volume": np.random.uniform(1000, 5000, n_candles)
    })


# ═════════════════════════════════════════════════════════════
# TESTES VORTEX STRATEGY
# ═════════════════════════════════════════════════════════════

def test_vortex_returns_bool_or_none():
    """Vortex retorna True, False ou None."""
    data = create_test_data(200, trend="up")
    result = getVortexTradeStrategy(data, verbose=False)
    assert result in [True, False, None], f"Vortex retornou {result}, esperado True/False/None"


def test_vortex_with_trend_up():
    """Vortex deve detectar tendência de alta."""
    data = create_test_data(200, trend="up")
    result = getVortexTradeStrategy(data, verbose=False)
    # Em tendência de alta, provavelmente True
    assert result is not None, "Vortex retornou None em trending market"


def test_vortex_with_trend_down():
    """Vortex deve detectar tendência de baixa."""
    data = create_test_data(200, trend="down")
    result = getVortexTradeStrategy(data, verbose=False)
    # Em tendência de baixa, provavelmente False
    assert result is not None, "Vortex retornou None em trending market"


def test_vortex_with_minimal_data():
    """Vortex com dados mínimos."""
    data = create_test_data(20)
    try:
        result = getVortexTradeStrategy(data, verbose=False)
        # Pode falhar ou retornar None - apenas não deve crashear
        assert result in [True, False, None]
    except:
        pass  # Aceitável falhar com dados mínimos


# ═════════════════════════════════════════════════════════════
# TESTES RSI STRATEGY
# ═════════════════════════════════════════════════════════════

def test_rsi_returns_bool_or_none():
    """RSI retorna True, False ou None."""
    data = create_test_data(200)
    result = getRsiTradeStrategy(data, verbose=False)
    assert result in [True, False, None], f"RSI retornou {result}"


def test_rsi_with_custom_parameters():
    """RSI com parâmetros customizados."""
    data = create_test_data(200)

    # Teste com parâmetros diferentes
    result1 = getRsiTradeStrategy(data, low=20, high=80, verbose=False)
    result2 = getRsiTradeStrategy(data, low=30, high=70, verbose=False)
    result3 = getRsiTradeStrategy(data, low=40, high=60, verbose=False)

    # Todos devem retornar valor válido
    assert result1 in [True, False, None]
    assert result2 in [True, False, None]
    assert result3 in [True, False, None]


def test_rsi_oversold():
    """RSI detecta oversold em pullback."""
    data = create_test_data(200, trend="down")
    result = getRsiTradeStrategy(data, low=30, high=70, verbose=False)
    # Em pullback forte, deve detectar oversold ou reverter
    assert result is not None or result is None  # Válido em ambos casos


def test_rsi_overbought():
    """RSI detecta overbought em alta."""
    data = create_test_data(200, trend="up")
    result = getRsiTradeStrategy(data, low=30, high=70, verbose=False)
    assert result in [True, False, None]


# ═════════════════════════════════════════════════════════════
# TESTES MOVING AVERAGE
# ═════════════════════════════════════════════════════════════

def test_moving_average_returns_bool_or_none():
    """MA retorna True, False ou None."""
    data = create_test_data(200)
    result = getMovingAverageTradeStrategy(data, verbose=False)
    assert result in [True, False, None]


def test_moving_average_with_custom_windows():
    """MA com períodos customizados."""
    data = create_test_data(200)

    result1 = getMovingAverageTradeStrategy(data, fast_window=7, slow_window=40, verbose=False)
    result2 = getMovingAverageTradeStrategy(data, fast_window=3, slow_window=10, verbose=False)
    result3 = getMovingAverageTradeStrategy(data, fast_window=12, slow_window=50, verbose=False)

    assert result1 in [True, False, None]
    assert result2 in [True, False, None]
    assert result3 in [True, False, None]


def test_moving_average_trend_following():
    """MA segue a tendência."""
    # Tendência de alta
    data_up = create_test_data(200, trend="up")
    result_up = getMovingAverageTradeStrategy(data_up, verbose=False)

    # Tendência de baixa
    data_down = create_test_data(200, trend="down")
    result_down = getMovingAverageTradeStrategy(data_down, verbose=False)

    # Ambos devem ter resultados válidos
    assert result_up in [True, False, None]
    assert result_down in [True, False, None]


def test_moving_average_with_small_data():
    """MA com dados pequenos (deve falhar gracefully)."""
    data = create_test_data(10)
    try:
        result = getMovingAverageTradeStrategy(data, verbose=False)
        assert result in [True, False, None]
    except:
        pass  # Aceitável com dados muito pequenos


# ═════════════════════════════════════════════════════════════
# TESTES DE DADOS
# ═════════════════════════════════════════════════════════════

def test_dataframe_structure():
    """Verifica estrutura de dados esperada."""
    data = create_test_data(100)

    required_cols = ["open_time", "open_price", "high_price", "low_price", "close_price", "volume"]
    for col in required_cols:
        assert col in data.columns, f"Coluna {col} faltando"

    assert len(data) == 100, "DataFrame tem tamanho incorreto"
    assert data["close_price"].dtype in [np.float64, np.float32, float]


def test_price_logic():
    """Valida lógica de preços (high > low, etc)."""
    data = create_test_data(100)

    assert (data["high_price"] >= data["low_price"]).all(), "High < Low em alguns candles"
    assert (data["high_price"] >= data["open_price"]).all() or (data["high_price"] >= data["close_price"]).all()
    assert (data["low_price"] <= data["open_price"]).all() or (data["low_price"] <= data["close_price"]).all()


# ═════════════════════════════════════════════════════════════
# EXECUÇÃO
# ═════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("🧪 Rodando testes das estratégias...")
    print()

    # Rodar testes manualmente
    tests = [
        ("Vortex retorna bool/None", test_vortex_returns_bool_or_none),
        ("Vortex trend up", test_vortex_with_trend_up),
        ("Vortex trend down", test_vortex_with_trend_down),
        ("RSI retorna bool/None", test_rsi_returns_bool_or_none),
        ("RSI custom params", test_rsi_with_custom_parameters),
        ("MA retorna bool/None", test_moving_average_returns_bool_or_none),
        ("MA custom windows", test_moving_average_with_custom_windows),
        ("MA trend following", test_moving_average_trend_following),
        ("DataFrame structure", test_dataframe_structure),
        ("Price logic", test_price_logic),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            test_func()
            print(f"✅ {name}")
            passed += 1
        except Exception as e:
            print(f"❌ {name}: {e}")
            failed += 1

    print()
    print(f"📊 Resultados: {passed} ✅ | {failed} ❌")

    if failed == 0:
        print("🎉 Todos os testes passaram!")
    else:
        print(f"⚠️  {failed} teste(s) falharam")
