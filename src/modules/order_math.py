"""Helpers puros para sizing/arredondamento de ordens conforme filtros da exchange."""

import math


def adjust_to_step(value, step, as_string=False):
    """Ajusta um valor para baixo no múltiplo mais próximo do step informado.

    Mantém o comportamento histórico do BinanceTraderBot.adjust_to_step:
    - rejeita step <= 0;
    - usa floor para não exceder o valor informado;
    - retorna float por padrão;
    - retorna string com precisão fixa quando as_string=True.
    """
    if step <= 0:
        raise ValueError("O valor de 'step' deve ser maior que zero.")

    decimal_places = (
        max(
            0,
            abs(int(math.floor(math.log10(step)))),
        )
        if step < 1
        else 0
    )

    adjusted_value = math.floor(value / step) * step
    adjusted_value = round(adjusted_value, decimal_places)

    if as_string:
        return f"{adjusted_value:.{decimal_places}f}"
    return adjusted_value


def validate_position_size(quantity, price, max_position_usd, min_position_usd):
    """Limita quantity para não exceder o notional máximo permitido.

    Retorna a tupla historicamente usada pelo BinanceTraderBot:
    (ok, adjusted_quantity, reason).
    """
    notional = float(price) * float(quantity)
    if notional <= max_position_usd:
        return True, quantity, ""

    new_qty = max_position_usd / float(price)
    new_notional = new_qty * float(price)
    if new_notional < min_position_usd:
        return False, 0.0, f"notional ${new_notional:.2f} < min ${min_position_usd:.2f}"

    return (
        True,
        new_qty,
        f"quantity reduzida de {quantity:.6f} para {new_qty:.6f} (cap ${max_position_usd:.2f})",
    )
