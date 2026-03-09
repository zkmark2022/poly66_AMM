"""Order manager — smart order diffing to minimize API calls."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.amm.connector.api_client import AMMApiClient
from src.amm.cache.inventory_cache import InventoryCache
from src.amm.strategy.models import OrderIntent

if TYPE_CHECKING:
    from src.amm.cache.order_cache import OrderCache

logger = logging.getLogger(__name__)


@dataclass
class ActiveOrder:
    order_id: str
    side: str
    direction: str
    price_cents: int
    remaining_quantity: int


class OrderManager:
    def __init__(
        self,
        api: AMMApiClient,
        cache: InventoryCache,
        order_cache: "OrderCache | None" = None,
    ) -> None:
        self._api = api
        self._cache = cache
        self._order_cache = order_cache
        self.active_orders: dict[str, ActiveOrder] = {}

    async def execute_intents(self, intents: list[OrderIntent], market_id: str) -> None:
        """Execute order intents by comparing with current active orders."""
        # Simple strategy: cancel all stale, place/replace new
        target_keys = {(i.side, i.direction, i.price_cents) for i in intents}
        active_keys = {(o.side, o.direction, o.price_cents): oid
                       for oid, o in self.active_orders.items()}
        replace_targets = {
            index: self._find_replace_target(intent)
            for index, intent in enumerate(intents)
            if intent.action.value == "REPLACE"
        }
        replace_order_ids = set()
        for replace_target in replace_targets.values():
            if replace_target is not None:
                replace_order_ids.add(replace_target[0])

        # Cancel orders not in target
        to_cancel = [oid for (s, d, p), oid in active_keys.items()
                     if (s, d, p) not in target_keys and oid not in replace_order_ids]
        for oid in to_cancel:
            try:
                await self._api.cancel_order(oid)
                self.active_orders.pop(oid, None)
                if self._order_cache is not None:
                    await self._order_cache.delete_order(market_id, oid)
            except Exception as e:
                logger.error("Failed to cancel order %s: %s", oid, e)

        # Place new orders
        for index, intent in enumerate(intents):
            key = (intent.side, intent.direction, intent.price_cents)
            if key in active_keys:
                continue

            replace_target = replace_targets.get(index)
            if replace_target is not None and intent.action.value == "REPLACE":
                _, old_order = replace_target
                await self._atomic_replace(old_order, intent, market_id)
                continue

            await self._place_intent(intent, market_id)

        await self._sync_pending_sell(market_id)

    async def _atomic_replace(
        self,
        old_order: ActiveOrder,
        new_intent: OrderIntent,
        market_id: str,
    ) -> None:
        """Atomically replace an existing order via the API replace endpoint."""
        fingerprint = self._intent_fingerprint(new_intent)
        dedupe_marked = await self._cache.mark_order_submission(market_id, fingerprint)
        if not dedupe_marked:
            logger.warning(
                "Skip duplicate replace intent after recovery: %s %s@%s qty=%s",
                new_intent.side,
                new_intent.direction,
                new_intent.price_cents,
                new_intent.quantity,
            )
            return

        try:
            placed = await self._api.replace_order(
                old_order.order_id,
                {
                    "market_id": market_id,
                    "side": new_intent.side,
                    "direction": new_intent.direction,
                    "price_cents": new_intent.price_cents,
                    "quantity": new_intent.quantity,
                },
            )
        except Exception as e:
            await self._cache.clear_order_submission(market_id, fingerprint)
            logger.error("Replace failed for %s: %s", old_order.order_id, e)
            return

        order_data = placed.get("data", {})
        order_id = order_data.get("order_id", "")
        if not order_id:
            await self._cache.clear_order_submission(market_id, fingerprint)
            return

        self.active_orders.pop(old_order.order_id, None)
        new_active = ActiveOrder(
            order_id=order_id,
            side=new_intent.side,
            direction=new_intent.direction,
            price_cents=new_intent.price_cents,
            remaining_quantity=new_intent.quantity,
        )
        self.active_orders[order_id] = new_active
        if self._order_cache is not None:
            await self._order_cache.delete_order(market_id, old_order.order_id)
            await self._order_cache.set_order(market_id, order_id, {
                "order_id": order_id,
                "side": new_intent.side,
                "direction": new_intent.direction,
                "price_cents": new_intent.price_cents,
                "remaining_quantity": new_intent.quantity,
            })

    async def _place_intent(self, intent: OrderIntent, market_id: str) -> None:
        fingerprint = self._intent_fingerprint(intent)
        dedupe_marked = await self._cache.mark_order_submission(
            market_id, fingerprint
        )
        if not dedupe_marked:
            logger.warning(
                "Skip duplicate place intent after recovery: %s %s@%s qty=%s",
                intent.side, intent.direction, intent.price_cents, intent.quantity,
            )
            return
        try:
            resp = await self._api.place_order({
                "market_id": market_id,
                "side": intent.side,
                "direction": intent.direction,
                "price_cents": intent.price_cents,
                "quantity": intent.quantity,
            })
            order_data = resp.get("data", {})
            order_id = order_data.get("order_id", "")
            if order_id:
                self.active_orders[order_id] = ActiveOrder(
                    order_id=order_id,
                    side=intent.side,
                    direction=intent.direction,
                    price_cents=intent.price_cents,
                    remaining_quantity=intent.quantity,
                )
                if self._order_cache is not None:
                    await self._order_cache.set_order(market_id, order_id, {
                        "order_id": order_id,
                        "side": intent.side,
                        "direction": intent.direction,
                        "price_cents": intent.price_cents,
                        "remaining_quantity": intent.quantity,
                    })
            else:
                await self._cache.clear_order_submission(market_id, fingerprint)
        except Exception as e:
            await self._cache.clear_order_submission(market_id, fingerprint)
            detail = ""
            if hasattr(e, "response"):
                try:
                    detail = f" body={e.response.text}"  # type: ignore[union-attr]
                except Exception:
                    pass
            logger.error("Failed to place order %s %s@%s qty=%s: %s%s",
                         intent.side, intent.direction, intent.price_cents,
                         intent.quantity, e, detail)

    def _find_replace_target(self, intent: OrderIntent) -> tuple[str, ActiveOrder] | None:
        if intent.old_order_id is not None and intent.old_order_id in self.active_orders:
            return intent.old_order_id, self.active_orders[intent.old_order_id]

        matches = [
            (order_id, order)
            for order_id, order in self.active_orders.items()
            if order.side == intent.side and order.direction == intent.direction
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def get_pending_sells(self) -> tuple[int, int]:
        yes_pending = sum(
            o.remaining_quantity for o in self.active_orders.values()
            if o.side == "YES" and o.direction == "SELL"
        )
        no_pending = sum(
            o.remaining_quantity for o in self.active_orders.values()
            if o.side == "NO" and o.direction == "SELL"
        )
        return yes_pending, no_pending

    async def _sync_pending_sell(self, market_id: str) -> None:
        yes_pending, no_pending = self.get_pending_sells()
        await self._cache.set_pending_sell(
            market_id,
            yes_pending_sell=yes_pending,
            no_pending_sell=no_pending,
        )

    async def cancel_all(self, market_id: str) -> None:
        """Cancel all active orders for a market."""
        await self._api.batch_cancel(market_id, scope="ALL")
        self.active_orders.clear()
        await self._sync_pending_sell(market_id)
        if self._order_cache is not None:
            await self._order_cache.clear(market_id)

    async def load_from_cache(self, market_id: str) -> None:
        """Restore active_orders from Redis after restart.

        Builds into a temporary dict first so that on any exception
        active_orders is left unchanged rather than in a half-populated state.
        """
        if self._order_cache is None:
            return
        orders = await self._order_cache.get_all_orders(market_id)
        tmp: dict[str, ActiveOrder] = {}
        for order_id, order_data in orders.items():
            tmp[order_id] = ActiveOrder(
                order_id=order_id,
                side=order_data["side"],
                direction=order_data["direction"],
                price_cents=order_data["price_cents"],
                remaining_quantity=order_data["remaining_quantity"],
            )
        # Atomic replace — no partial state on success or failure above.
        self.active_orders = tmp
        # Always sync pending-sell counters after a successful cache read.
        # If OrderCache returned {} (empty), there are no active orders so
        # pending-sell should be 0 — stale Redis InventoryCache values must
        # be cleared to avoid artificially low yes_available/no_available.
        await self._sync_pending_sell(market_id)

    @staticmethod
    def _intent_fingerprint(intent: OrderIntent) -> str:
        return (
            f"{intent.action.value}:"
            f"{intent.side}:{intent.direction}:{intent.price_cents}:{intent.quantity}"
        )
