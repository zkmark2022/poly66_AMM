"""MarketContext — single-market runtime state for the AMM."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from src.amm.models.enums import DefenseLevel, Phase
from src.amm.models.inventory import Inventory
from src.amm.models.orders import ActiveOrder


@dataclass
class MarketContext:
    """Everything the AMM needs to make decisions for one market."""

    market_id: str

    # Inventory (from Redis cache, updated by trade_poller)
    inventory: Inventory

    # Strategy state
    phase: Phase = Phase.EXPLORATION
    mid_price: int = 50              # current mid-price in cents [1, 99]
    reservation_price: float = 50.0  # A-S reservation price
    optimal_spread: float = 2.0      # A-S optimal spread

    # Active orders (local cache, synced with Redis)
    active_orders: dict[str, ActiveOrder] = field(default_factory=dict)

    # Risk state
    defense_level: DefenseLevel = DefenseLevel.NORMAL
    daily_pnl_cents: int = 0
    session_start_inventory: Inventory | None = None  # snapshot at AMM start

    # Timing
    last_quote_at: float = 0.0    # monotonic time of last quote cycle
    last_reconcile_at: float = 0.0  # monotonic time of last full reconciliation
