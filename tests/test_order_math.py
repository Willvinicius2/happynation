import pytest

from src.modules.order_math import adjust_to_step, validate_position_size
from src.modules.BinanceTraderBot import BinanceTraderBot


def test_adjust_to_step_floors_value_to_exchange_step():
    assert adjust_to_step(1.239, 0.01) == 1.23
    assert adjust_to_step(0.001239, 0.0001) == 0.0012


def test_adjust_to_step_can_return_fixed_precision_string():
    assert adjust_to_step(1.239, 0.01, as_string=True) == "1.23"
    assert adjust_to_step(0.001239, 0.0001, as_string=True) == "0.0012"


def test_adjust_to_step_rejects_invalid_step():
    with pytest.raises(ValueError, match="step"):
        adjust_to_step(1.0, 0.0)


def test_validate_position_size_allows_notional_under_cap():
    assert validate_position_size(
        quantity=2.0,
        price=10.0,
        max_position_usd=25.0,
        min_position_usd=5.0,
    ) == (True, 2.0, "")


def test_validate_position_size_reduces_quantity_to_cap():
    ok, new_qty, reason = validate_position_size(
        quantity=3.0,
        price=10.0,
        max_position_usd=25.0,
        min_position_usd=5.0,
    )

    assert ok is True
    assert new_qty == 2.5
    assert reason == "quantity reduzida de 3.000000 para 2.500000 (cap $25.00)"


def test_validate_position_size_blocks_when_cap_is_below_minimum_notional():
    ok, new_qty, reason = validate_position_size(
        quantity=3.0,
        price=10.0,
        max_position_usd=4.0,
        min_position_usd=5.0,
    )

    assert ok is False
    assert new_qty == 0.0
    assert reason == "notional $4.00 < min $5.00"


def test_trader_validate_real_position_size_keeps_existing_method_contract(monkeypatch):
    import src.modules.PaperBalanceLogger as pbl

    monkeypatch.setattr(pbl, "MAX_POSITION_USD", 25.0)
    monkeypatch.setattr(pbl, "MIN_POSITION_USD", 5.0)
    trader = BinanceTraderBot.__new__(BinanceTraderBot)

    assert trader._validate_real_position_size(3.0, 10.0) == (
        True,
        2.5,
        "quantity reduzida de 3.000000 para 2.500000 (cap $25.00)",
    )


def test_trader_adjust_to_step_keeps_existing_method_contract():
    trader = BinanceTraderBot.__new__(BinanceTraderBot)

    assert trader.adjust_to_step(10.987, 0.1) == 10.9
    assert trader.adjust_to_step(10.987, 0.1, as_string=True) == "10.9"
