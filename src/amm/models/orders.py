"""AMM order models."""
from dataclasses import dataclass


@dataclass
class ActiveOrder:
    """Represents a live order placed by the AMM."""

    order_id: str
    side: str           # YES / NO
    direction: str      # SELL (AMM never BUYs)
    price_cents: int    # [1, 99]
    quantity: int
    filled_qty: int = 0
    level: int = 0      # gradient level index (0 = closest to mid)

    @property
    def remaining_qty(self) -> int:
        return self.quantity - self.filled_qty
