"""Swing trade engines.

Esses engines sao adicoes ao catalogo de estrategias e nao alteram o
comportamento atual do bot. Cada um declara timeframe alvo de 4h, entao
o MultiStrategy os ignora automaticamente quando o bot roda em 1h.

Para ativar um engine de swing, monte um VoteSpec ou MAIN_STRATEGY
apontando para a funcao desejada e ajuste CANDLE_PERIOD/bot_timeframe
para 4h. Recomendado rodar em instancia paper separada.
"""
from src.strategies.swing.bollinger_meanrev import BollingerMeanRevSwing
from src.strategies.swing.donchian_breakout import DonchianBreakoutSwing
from src.strategies.swing.ema_trend import EmaTrendSwing

__all__ = [
    "BollingerMeanRevSwing",
    "DonchianBreakoutSwing",
    "EmaTrendSwing",
]
