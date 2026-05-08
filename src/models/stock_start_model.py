from dataclasses import dataclass, field
from typing import Callable, Dict, Optional


@dataclass
class StockStartModel:
    # fmt: off
    baseStock: str # Código base da stock negociada (ex: 'BTC')
    quoteStock: str # Código da moeda cotada (ex: 'USDT')
    tradedQuantity: float
    candlePeriod: str
    tradedPercentage: float  = 100 # Ainda não implementado

    # Ajustes técnicos    
    fallBackActivated: bool = True          # Define se a estratégia de Fallback será usada (ela pode entrar comprada em mercados subindo)
    
    mainStrategy: Callable = None                                             # Estratégia principal
    mainStrategyArgs: Dict[str, any] = field(default_factory=dict)      # (opcional) Argumentos da estratégia principal
    fallbackStrategy: Optional[Callable] = None                         # (opcional) Estratégia de Fallback
    fallbackStrategyArgs: Dict[str, any] = field(default_factory=dict)  # (opcional) Argumentos da estratégia de fallback

    # Ajustes de Loss Protection
    acceptableLossPercentage: float = 0.5     # (Usar em base 100%) O quando o bot aceita perder de % (se for negativo, o bot só aceita lucro)
    stopLossPercentage: float = 3.5           # (Usar em base 100%) % Máxima de loss que ele aceita, em caso de não vender na ordem limitada

    # Ajustes de Take Profit
    takeProfitAtPercentage: list[float] = field(default_factory=list)        # (Usar em base 100%) Quanto de valorização para pegar lucro. (Array exemplo: [2, 5, 10])
    takeProfitAmountPercentage: list[float] = field(default_factory=list)    # (Usar em base 100%) Quanto da quantidade tira de lucro. (Array exemplo: [25, 25, 40])

    # Ajuste de tempos    
    tempoEntreTrades: int = 30 * 60         # Tempo que o bot espera para verificar o mercado (em segundos)
    delayEntreOrdens: int = 60 * 60         # Tempo que o bot espera depois de realizar uma ordem de compra ou venda (ajuda a diminuir trades de borda)

    # 1.5
    executeMarketOrders: bool = False       # Define se o bot irá executar ordens de mercado (se False, ele executa ordens limitadas)

    trailingStopPercent: float = 100        # (Usar em base 100%) Define o trailing stop, que é um stop que acompanha a valorização do ativo.
    trailingStopEnabled: bool = False        # Define se o trailing stop está ativado

    pauseAfterTakeProfit: bool = False      # Define se o bot deve pausar após executar o take profit
    pauseAfterStopLoss: bool = False        # Define se o bot deve pausar após executar o stop loss
    pauseAfterTrailingStop: bool = False    # Define se o bot deve pausar após executar o trailing stop

    # Protecao USD-based (sobrepoe % quando > 0)
    stopLossUsd: float = 0.0                # SL fixo em USD (ex: 10.0 = sai em -$10)
    trailActivationUsd: float = 0.0         # Lucro USD que ativa o trailing
    trailFloorUsd: float = 0.0              # Lucro minimo USD que o trailing trava
    trailGivebackPct: float = 25.0          # % do peak de lucro que o trail pode devolver


# fmt: on
