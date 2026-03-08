"""Order sanitizer — validate and fix OrderIntent before execution."""
import logging
from typing import TYPE_CHECKING

from src.amm.models.enums import DefenseLevel
from src.amm.models.inventory import Inventory
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
        inventory: Inventory | None = None,
    ) -> list[OrderIntent]:
        """Apply defense-level filtering and inventory constraints.

        Pass ``inventory`` to use a pre-snapshotted value instead of
        reading ``ctx.inventory`` live (avoids incoherence if reconcile_loop
        replaces ctx.inventory mid-cycle).
        """
        result = []
        for intent in intents:
            sanitized = self._sanitize_one(intent, defense, ctx, inventory)
            if sanitized is not None:
                result.append(sanitized)
        return result

    def _sanitize_one(
        self,
        intent: OrderIntent,
        defense: DefenseLevel,
        ctx: "MarketContext",
        inventory: Inventory | None = None,
    ) -> OrderIntent | None:
        # AMM NEVER issues BUY orders — reject immediately
        if intent.direction != "SELL":
            logger.critical("Sanitizer blocked BUY intent: %s", intent)
            return None

        # Price clamping
        price = clamp(intent.price_cents, 1, 99)

        # Quantity must be positive
        if intent.quantity <= 0:
            return None

        inv = inventory if inventory is not None else ctx.inventory

        # ONE_SIDE: only allow quotes on the heavy side (reduce inventory skew)
        if defense == DefenseLevel.ONE_SIDE:
            skew = inv.inventory_skew
            if skew > 0 and intent.side == "NO":
                # Long YES — suppress SELL NO to force YES reduction
                return None
            if skew < 0 and intent.side == "YES":
                # Long NO — suppress SELL YES
                return None

        # Inventory availability: clamp quantity to what is actually available
        if intent.side == "YES":
            available = inv.yes_available
        else:
            available = inv.no_available

        if available <= 0:
            logger.debug("No %s inventory available — dropping intent", intent.side)
            return None

        qty = min(intent.quantity, available)

        # WIDEN: enforce minimum spread (handled by spread_min_cents in config)
        # No extra filtering needed here; A-S engine handles spread widening

        return OrderIntent(
            action=intent.action,
            side=intent.side,
            direction=intent.direction,
            price_cents=price,
            quantity=qty,
            reason=intent.reason,
            old_order_id=intent.old_order_id,
        )
