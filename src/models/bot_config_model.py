from typing import Dict, List, Optional
from pydantic import BaseModel


class BotConfigModel(BaseModel):
    main_strategy: str
    main_strategy_args: Dict[str, object] = {}
    fallback_activated: bool = False
    fallback_strategy: Optional[str] = None
    fallback_strategy_args: Dict[str, object] = {}
    acceptable_loss_percentage: float = 100.0
    stop_loss_percentage: float = 3.5
    tp_at_percentage: List[float] = []
    tp_amount_percentage: List[float] = []
    candle_period: str
    tempo_entre_trades: float = 30.0
    delay_entre_ordens: float = 30.0
    thread_lock: bool = True
    stocks_traded: List[str] = []
    execute_market_orders: bool = False
    trailing_stop_active: bool = False
    trailing_stop_percentage: float = 10.0
    pause_after_take_profit: bool = False
    pause_after_stop_loss: bool = False
    pause_after_trailing_stop: bool = False
