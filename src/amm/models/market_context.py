"""AMM market runtime context."""
from dataclasses import dataclass, field
from src.amm.models.enums import DefenseLevel, Phase
from src.amm.models.inventory import Inventory
from src.amm.config.models import MarketConfig


@dataclass
class MarketContext:
    """Runtime state for a single market."""
    market_id: str
    config: MarketConfig
    inventory: Inventory
    phase: Phase = Phase.EXPLORATION
    defense_level: DefenseLevel = DefenseLevel.NORMAL
    daily_pnl_cents: int = 0
    trade_count: int = 0
    shutdown_requested: bool = False
    active_orders: dict = field(default_factory=dict)
