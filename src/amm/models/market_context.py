"""AMM market runtime context."""
import asyncio
import time
import uuid
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
    session_pnl_cents: int = 0
    initial_inventory_value_cents: int = 0
    trade_count: int = 0
    shutdown_requested: bool = False
    last_known_market_active: bool = False
    market_status_checked_at: float = 0.0
    active_orders: dict = field(default_factory=dict)
    oracle_lag_threshold: float = 10.0
    oracle_deviation_threshold: float = 20.0
    started_at: float = field(default_factory=time.monotonic)
    winding_down: bool = False
    winding_down_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    inventory_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
