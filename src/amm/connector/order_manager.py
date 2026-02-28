"""Order Manager — smart order diffing to minimize API calls.

v1.0 Review Fix #4: pending_sell MUST be synced after every order change.
Without this, yes_available == yes_volume which causes over-selling and
Burn/Merge 5001 errors.
"""
import logging
from typing import Any
from src.amm.strategy.models import (
    ActiveOrder, OrderIntent, ReplaceAction, PlaceAction, CancelAction,
)

logger = logging.getLogger(__name__)


class OrderManager:
    """Maintains active order state and syncs with exchange minimally.

    Design invariant: after every call to execute_intents() or cancel_all(),
    _sync_pending_sell() is always called to keep Redis cache up-to-date.
    """

    def __init__(self, api: Any, cache: Any) -> None:
        self._api = api
        self._cache = cache
        # order_id → ActiveOrder; source of truth for pending_sell calculation
        self.active_orders: dict[str, ActiveOrder] = {}

    # ──────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────

    async def execute_intents(
        self, intents: list[OrderIntent], market_id: str,
    ) -> None:
        """Execute order intents by comparing with current active orders.

        Strategy:
        1. Compute minimum diff (replace / place / cancel)
        2. Execute all replaces first (atomic, no inventory gap)
        3. Execute cancels
        4. Execute places
        5. Sync pending_sell into Redis
        """
        replaces, places, cancels = self._compute_diff(self.active_orders, intents)

        # Execute replaces (atomic API call)
        for action in replaces:
            try:
                resp = await self._api.replace_order(
                    action.old_order_id,
                    {
                        "side": action.side,
                        "price_cents": action.new_price_cents,
                        "quantity": action.new_quantity,
                    },
                )
                new_id = resp["data"]["order"]["id"]
                old = self.active_orders.pop(action.old_order_id)
                self.active_orders[new_id] = ActiveOrder(
                    order_id=new_id,
                    side=old.side,
                    direction=old.direction,
                    price_cents=action.new_price_cents,
                    remaining_quantity=action.new_quantity,
                    market_id=market_id,
                )
            except Exception:
                logger.exception("Replace failed for order %s", action.old_order_id)

        # Execute cancels
        for action in cancels:
            try:
                await self._api.cancel_order(action.order_id)
                self.active_orders.pop(action.order_id, None)
            except Exception:
                logger.exception("Cancel failed for order %s", action.order_id)

        # Execute places
        for action in places:
            try:
                resp = await self._api.place_order(
                    {
                        "market_id": market_id,
                        "side": action.side,
                        "direction": action.direction,
                        "price_cents": action.price_cents,
                        "quantity": action.quantity,
                    }
                )
                new_id = resp["data"]["order"]["id"]
                self.active_orders[new_id] = ActiveOrder(
                    order_id=new_id,
                    side=action.side,
                    direction=action.direction,
                    price_cents=action.price_cents,
                    remaining_quantity=action.quantity,
                    market_id=market_id,
                )
            except Exception:
                logger.exception("Place failed for %s@%d", action.side, action.price_cents)

        await self._sync_pending_sell(market_id)

    async def cancel_all(self, market_id: str) -> None:
        """Cancel all active orders for a market via batch endpoint."""
        await self._api.batch_cancel(market_id, scope="ALL")
        self.active_orders.clear()
        await self._sync_pending_sell(market_id)

    def get_pending_sells(self) -> tuple[int, int]:
        """Return (yes_pending, no_pending) from current active orders.

        v1.0 Review Fix #4: Call before each quote cycle so yes_available /
        no_available reflect real lock-up and prevent over-selling.
        """
        yes_pending = sum(
            o.remaining_quantity
            for o in self.active_orders.values()
            if o.side == "YES"
        )
        no_pending = sum(
            o.remaining_quantity
            for o in self.active_orders.values()
            if o.side == "NO"
        )
        return yes_pending, no_pending

    # ──────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────

    def _compute_diff(
        self,
        active: dict[str, ActiveOrder],
        target: list[OrderIntent],
    ) -> tuple[list[ReplaceAction], list[PlaceAction], list[CancelAction]]:
        """Compute the minimum set of actions to move from active → target state.

        Matching strategy: match by (side) — one active per side bucket.
        If price or quantity differs → REPLACE.
        If active side no longer in targets → CANCEL.
        If target side not in active → PLACE.
        """
        # Group active orders by side (take the first per side for matching)
        active_by_side: dict[str, ActiveOrder] = {}
        unmatched_active: list[ActiveOrder] = []
        for order in active.values():
            if order.side not in active_by_side:
                active_by_side[order.side] = order
            else:
                # Extra orders for same side → always cancel
                unmatched_active.append(order)

        replaces: list[ReplaceAction] = []
        places: list[PlaceAction] = []
        cancels: list[CancelAction] = []

        matched_sides: set[str] = set()

        for intent in target:
            side = intent.side
            if side in active_by_side:
                existing = active_by_side[side]
                matched_sides.add(side)
                # Check if update needed
                if (
                    existing.price_cents != intent.price_cents
                    or existing.remaining_quantity != intent.quantity
                ):
                    replaces.append(ReplaceAction(
                        old_order_id=existing.order_id,
                        new_price_cents=intent.price_cents,
                        new_quantity=intent.quantity,
                        side=side,
                    ))
                # else: exact match — no action needed
            else:
                places.append(PlaceAction(
                    side=intent.side,
                    direction=intent.direction,
                    price_cents=intent.price_cents,
                    quantity=intent.quantity,
                    reason=intent.reason,
                ))

        # Cancel any active sides not in targets
        for side, order in active_by_side.items():
            if side not in matched_sides:
                cancels.append(CancelAction(order_id=order.order_id, side=side))

        # Cancel extra unmatched active orders
        for order in unmatched_active:
            cancels.append(CancelAction(order_id=order.order_id, side=order.side))

        return replaces, places, cancels

    async def _sync_pending_sell(self, market_id: str) -> None:
        """Write current pending_sell values to Redis inventory cache."""
        yes_pending, no_pending = self.get_pending_sells()
        await self._cache.set_pending_sell(
            market_id,
            yes_pending_sell=yes_pending,
            no_pending_sell=no_pending,
        )
