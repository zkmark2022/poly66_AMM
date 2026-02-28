"""MarketContext: single-market runtime state for the AMM."""
from dataclasses import dataclass, field

from src.amm.config.models import MarketConfig
from src.amm.models.enums import DefenseLevel, Phase
from src.amm.models.inventory import Inventory


@dataclass
class MarketContext:
    """Everything the AMM needs to make decisions for one market."""

    market_id: str
    config: MarketConfig

    # Inventory (from Redis cache, updated by trade_poller)
    inventory: Inventory

    # Strategy state
    phase: Phase
    mid_price: int  # current mid-price in cents [1, 99]
    reservation_price: float
    optimal_spread: float

    # Active orders (local cache, synced with Redis)
    active_orders: dict[str, object] = field(default_factory=dict)

    # Risk state
    defense_level: DefenseLevel = DefenseLevel.NORMAL
    daily_pnl_cents: int = 0
    session_start_inventory: Inventory | None = None

    # Timing
    last_quote_at: float = 0.0
    last_reconcile_at: float = 0.0

    # Lifecycle flag
    shutdown_requested: bool = False
