"""AMM strategy data models — OrderIntent, ActiveOrder, diff actions."""
from dataclasses import dataclass, field
from src.amm.models.enums import QuoteAction


@dataclass
class OrderIntent:
    """A desired order state from the strategy layer."""
    action: QuoteAction
    side: str           # "YES" or "NO"
    direction: str      # "SELL" (AMM only issues sells)
    price_cents: int
    quantity: int
    reason: str = ""
    # Optional: link to existing order for REPLACE action
    existing_order_id: str | None = None


@dataclass
class ActiveOrder:
    """A live order currently tracked by the order manager."""
    order_id: str
    side: str           # "YES" or "NO"
    direction: str      # "SELL"
    price_cents: int
    remaining_quantity: int
    market_id: str


@dataclass
class ReplaceAction:
    """Replace an existing order with new params."""
    old_order_id: str
    new_price_cents: int
    new_quantity: int
    side: str


@dataclass
class PlaceAction:
    """Place a brand-new order."""
    side: str
    direction: str
    price_cents: int
    quantity: int
    reason: str = ""


@dataclass
class CancelAction:
    """Cancel a stale order."""
    order_id: str
    side: str
