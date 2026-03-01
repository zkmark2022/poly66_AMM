"""Strategy layer data models."""
from dataclasses import dataclass
from src.amm.models.enums import QuoteAction


@dataclass
class OrderIntent:
    """Desired order state from strategy layer."""
    action: QuoteAction
    side: str       # "YES" or "NO"
    direction: str  # "SELL" (AMM only sells)
    price_cents: int
    quantity: int
    reason: str = ""
    old_order_id: str | None = None  # for REPLACE action
