from pydantic import BaseModel, Field
from typing import Dict, Optional, List


class StockStartModelSchema(BaseModel):
    symbol: str
    baseStock: str
    quoteStock: str
    tradedQuantity: float
    candlePeriod: str
    tradedPercentage: float = 100
    fallBackActivated: bool = True
    mainStrategy: Optional[str] = None
    mainStrategyArgs: Dict[str, object] = Field(default_factory=dict)
    fallbackStrategy: Optional[str] = None
    fallbackStrategyArgs: Dict[str, object] = Field(default_factory=dict)
    acceptableLossPercentage: float = 0.5
    stopLossPercentage: float = 3.5
    takeProfitAtPercentage: List[float] = Field(default_factory=list)
    takeProfitAmountPercentage: List[float] = Field(default_factory=list)
    tempoEntreTrades: int = 30 * 60
    delayEntreOrdens: int = 60 * 60
    executeMarketOrders: bool = False
    trailingStopPercent: float = 100
    trailingStopActive: bool = False
    pauseAfterTakeProfit: bool = False
    pauseAfterStopLoss: bool = False
    pauseAfterTrailingStop: bool = False
