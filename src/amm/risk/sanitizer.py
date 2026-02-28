"""Order Sanitizer — validates and fixes OrderIntent before execution."""
import logging
from dataclasses import dataclass, replace
from src.amm.config.models import MarketConfig
from src.amm.strategy.models import OrderIntent

logger = logging.getLogger(__name__)

_PRICE_MIN = 1
_PRICE_MAX = 99


@dataclass
class SanitizedResult:
    """Result of a single OrderIntent sanitization."""
    intent: OrderIntent
    is_valid: bool
    rejection_reason: str = ""


class OrderSanitizer:
    """Validate and fix OrderIntent before sending to the order manager.

    Rules:
    - Price clamped to [1, 99] (never hard-reject — clamp silently)
    - Quantity must be >= min_order_quantity and > 0; clamped from above by max
    - Crossed market (ask_price < implicit bid_price) rejects the batch
    """

    def __init__(self, config: MarketConfig) -> None:
        self._config = config

    def sanitize(self, intent: OrderIntent) -> SanitizedResult:
        """Sanitize a single OrderIntent."""
        # --- Price: clamp to [1, 99] ---
        clamped_price = max(_PRICE_MIN, min(intent.price_cents, _PRICE_MAX))

        # --- Quantity: range check ---
        qty = intent.quantity
        if qty <= 0:
            return SanitizedResult(
                intent=intent,
                is_valid=False,
                rejection_reason=f"quantity {qty} must be > 0",
            )
        if qty < self._config.min_order_quantity:
            return SanitizedResult(
                intent=intent,
                is_valid=False,
                rejection_reason=(
                    f"quantity {qty} below min_order_quantity "
                    f"{self._config.min_order_quantity}"
                ),
            )
        clamped_qty = min(qty, self._config.max_order_quantity)

        fixed = replace(intent, price_cents=clamped_price, quantity=clamped_qty)
        return SanitizedResult(intent=fixed, is_valid=True)

    def sanitize_batch(self, intents: list[OrderIntent]) -> list[SanitizedResult]:
        """Sanitize a batch of intents and enforce no-crossed-market rule.

        The crossed-market check applies only when both YES and NO intents
        are present. YES ask_price and implied bid_yes (= 100 - NO_price)
        must satisfy: ask_price > bid_yes (positive spread).
        """
        if not intents:
            return []

        results = [self.sanitize(i) for i in intents]

        # Crossed-market guard: find YES ask and NO ask (NO price = 100 - bid_yes)
        yes_ask: int | None = None
        no_price: int | None = None
        for r in results:
            if not r.is_valid:
                continue
            if r.intent.side == "YES":
                yes_ask = r.intent.price_cents
            elif r.intent.side == "NO":
                no_price = r.intent.price_cents

        if yes_ask is not None and no_price is not None:
            # bid_yes = 100 - no_price (complement)
            bid_yes = 100 - no_price
            if yes_ask <= bid_yes:
                reason = (
                    f"crossed market: yes_ask={yes_ask} <= bid_yes={bid_yes} "
                    f"(no_price={no_price})"
                )
                logger.warning("Rejecting batch: %s", reason)
                # Mark all valid results as invalid
                results = [
                    SanitizedResult(
                        intent=r.intent,
                        is_valid=False,
                        rejection_reason=reason,
                    )
                    if r.is_valid
                    else r
                    for r in results
                ]

        return results
