"""Order manager — smart order diffing to minimize API calls."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from src.amm.connector.api_client import AMMApiClient
from src.amm.cache.inventory_cache import InventoryCache
from src.amm.strategy.models import OrderIntent
from src.amm.models.enums import QuoteAction

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
        target_keys = {(i.side, i.price_cents) for i in intents}
        active_keys = {(o.side, o.price_cents): oid
                       for oid, o in self.active_orders.items()}

        # Cancel orders not in target
        to_cancel = [oid for (s, p), oid in active_keys.items()
                     if (s, p) not in target_keys]
        for oid in to_cancel:
            try:
                await self._api.cancel_order(oid)
                self.active_orders.pop(oid, None)
            except Exception as e:
                logger.error("Failed to cancel order %s: %s", oid, e)

        # Place new orders
        for intent in intents:
            key = (intent.side, intent.price_cents)
            if key not in active_keys:
                try:
                    resp = await self._api.place_order({
                        "market_id": market_id,
                        "side": intent.side,
                        "direction": intent.direction,
                        "price_cents": intent.price_cents,
                        "quantity": intent.quantity,
                    })
                    order_id = resp.get("data", {}).get("order_id", "")
                    if order_id:
                        self.active_orders[order_id] = ActiveOrder(
                            order_id=order_id,
                            side=intent.side,
                            direction=intent.direction,
                            price_cents=intent.price_cents,
                            remaining_quantity=intent.quantity,
                        )
                except Exception as e:
                    logger.error("Failed to place order: %s", e)

        await self._sync_pending_sell(market_id)

    def get_pending_sells(self) -> tuple[int, int]:
        yes_pending = sum(
            o.remaining_quantity for o in self.active_orders.values()
            if o.side == "YES"
        )
        no_pending = sum(
            o.remaining_quantity for o in self.active_orders.values()
            if o.side == "NO"
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
