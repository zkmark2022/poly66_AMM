"""Order manager — smart order diffing to minimize API calls."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from src.amm.connector.api_client import AMMApiClient
from src.amm.cache.inventory_cache import InventoryCache
from src.amm.strategy.models import OrderIntent

logger = logging.getLogger(__name__)


@dataclass
class ActiveOrder:
    order_id: str
    side: str
    direction: str
    price_cents: int
    remaining_quantity: int


class OrderManager:
    def __init__(self, api: AMMApiClient, cache: InventoryCache) -> None:
        self._api = api
        self._cache = cache
        self.active_orders: dict[str, ActiveOrder] = {}

    async def execute_intents(self, intents: list[OrderIntent], market_id: str) -> None:
        """Execute order intents by comparing with current active orders."""
        # Simple strategy: cancel all stale, place/replace new
        target_keys = {(i.side, i.direction, i.price_cents) for i in intents}
        active_keys = {(o.side, o.direction, o.price_cents): oid
                       for oid, o in self.active_orders.items()}
        replace_order_ids = set()
        for intent in intents:
            if intent.action.value != "REPLACE":
                continue
            replace_target = self._find_replace_target(intent)
            if replace_target is not None:
                replace_order_ids.add(replace_target[0])

        # Cancel orders not in target
        to_cancel = [oid for (s, d, p), oid in active_keys.items()
                     if (s, d, p) not in target_keys and oid not in replace_order_ids]
        for oid in to_cancel:
            try:
                await self._api.cancel_order(oid)
                self.active_orders.pop(oid, None)
            except Exception as e:
                logger.error("Failed to cancel order %s: %s", oid, e)

        # Place new orders
        for intent in intents:
            key = (intent.side, intent.direction, intent.price_cents)
            if key in active_keys:
                continue

            replace_target = self._find_replace_target(intent)
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
        """Cancel old, place new. If place fails, do not recreate the old order."""
        try:
            await self._api.cancel_order(old_order.order_id)
        except Exception as e:
            logger.warning("Cancel failed for %s during replace: %s", old_order.order_id, e)
            return

        self.active_orders.pop(old_order.order_id, None)

        try:
            placed = await self._api.place_order({
                "market_id": market_id,
                "side": new_intent.side,
                "direction": new_intent.direction,
                "price_cents": new_intent.price_cents,
                "quantity": new_intent.quantity,
            })
            order_data = placed.get("data", placed)
            order_id = order_data.get("order_id", "")
            if order_id:
                self.active_orders[order_id] = ActiveOrder(
                    order_id=order_id,
                    side=new_intent.side,
                    direction=new_intent.direction,
                    price_cents=new_intent.price_cents,
                    remaining_quantity=new_intent.quantity,
                )
        except Exception as e:
            logger.error("Place failed after cancel for %s: %s", old_order.order_id, e)

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
            order_data = resp.get("data", resp)
            order_id = order_data.get("order_id", "")
            if order_id:
                self.active_orders[order_id] = ActiveOrder(
                    order_id=order_id,
                    side=intent.side,
                    direction=intent.direction,
                    price_cents=intent.price_cents,
                    remaining_quantity=intent.quantity,
                )
            else:
                await self._cache.clear_order_submission(market_id, fingerprint)
        except Exception as e:
            await self._cache.clear_order_submission(market_id, fingerprint)
            logger.error("Failed to place order: %s", e)

    def _find_replace_target(self, intent: OrderIntent) -> tuple[str, ActiveOrder] | None:
        if intent.old_order_id is not None and intent.old_order_id in self.active_orders:
            return intent.old_order_id, self.active_orders[intent.old_order_id]

        for order_id, order in self.active_orders.items():
            if order.side == intent.side and order.direction == intent.direction:
                return order_id, order
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

    @staticmethod
    def _intent_fingerprint(intent: OrderIntent) -> str:
        return (
            f"{intent.action.value}:"
            f"{intent.side}:{intent.direction}:{intent.price_cents}:{intent.quantity}"
        )
