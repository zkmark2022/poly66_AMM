"""Strategy layer data models — pure data, no I/O."""
from dataclasses import dataclass, field

from src.amm.models.enums import QuoteAction


@dataclass
class OrderIntent:
    """Strategy layer output — pure intent, no I/O.

    v1.0 Review Fix #3 — CRITICAL MAPPING RULE:
    AMM NEVER issues BUY orders. All intents must be direction="SELL".
      - Ask ladder: side="YES", direction="SELL", price=ask_price
      - Bid ladder: side="NO",  direction="SELL", price=100-bid_price
    Issuing BUY YES would freeze cash instead of utilizing existing NO shares
    from Mint, violating the single-orderbook dual-inventory design.
    """

    action: QuoteAction  # PLACE / REPLACE / CANCEL / HOLD
    side: str  # YES / NO (bid maps to NO)
    direction: str  # SELL only (AMM never BUYs)
    price_cents: int  # [1, 99]
    quantity: int  # > 0
    replace_order_id: str | None = None  # for REPLACE action
    reason: str = ""  # audit trail
