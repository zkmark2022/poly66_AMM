"""Order sanitizer — validate and fix OrderIntent before execution."""
import logging
from typing import TYPE_CHECKING

from src.amm.models.enums import DefenseLevel, QuoteAction
from src.amm.strategy.models import OrderIntent
from src.amm.utils.integer_math import clamp

if TYPE_CHECKING:
    from src.amm.models.market_context import MarketContext

logger = logging.getLogger(__name__)


class OrderSanitizer:
    """Filter, clamp, and transform OrderIntents based on defense level and inventory."""

    def sanitize(
        self,
        intents: list[OrderIntent],
        defense: DefenseLevel,
        ctx: "MarketContext",
    ) -> list[OrderIntent]:
        """Apply defense-level filtering and inventory constraints."""
        result = []
        for intent in intents:
            sanitized = self._sanitize_one(intent, defense, ctx)
            if sanitized is not None:
                result.append(sanitized)
        return result

    def _sanitize_one(
        self,
        intent: OrderIntent,
        defense: DefenseLevel,
        ctx: "MarketContext",
    ) -> OrderIntent | None:
        # Price clamping
        price = clamp(intent.price_cents, 1, 99)

        # Quantity must be positive
        if intent.quantity <= 0:
            return None

        # ONE_SIDE: only allow quotes on the heavy side (reduce inventory skew)
        if defense == DefenseLevel.ONE_SIDE:
            skew = ctx.inventory.inventory_skew
            if skew > 0 and intent.side == "NO":
                # Long YES — suppress SELL NO to force YES reduction
                return None
            if skew < 0 and intent.side == "YES":
                # Long NO — suppress SELL YES
                return None

        # WIDEN: enforce minimum spread (handled by spread_min_cents in config)
        # No extra filtering needed here; A-S engine handles spread widening

        return OrderIntent(
            action=intent.action,
            side=intent.side,
            direction=intent.direction,
            price_cents=price,
            quantity=intent.quantity,
            reason=intent.reason,
            old_order_id=intent.old_order_id,
        )
