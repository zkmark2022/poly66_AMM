"""Redis-backed inventory cache. Key: amm:inventory:{market_id} (Hash)."""
from __future__ import annotations

import redis.asyncio as aioredis

from src.amm.models.inventory import Inventory


class InventoryCache:
    """CRUD for AMM inventory stored as a Redis Hash."""

    def __init__(self, redis: aioredis.Redis) -> None:  # type: ignore[type-arg]
        self._redis = redis

    def _key(self, market_id: str) -> str:
        return f"amm:inventory:{market_id}"

    async def set(self, market_id: str, inventory: Inventory) -> None:
        await self._redis.hset(
            self._key(market_id),
            mapping={
                "cash_cents": str(inventory.cash_cents),
                "yes_volume": str(inventory.yes_volume),
                "no_volume": str(inventory.no_volume),
                "yes_cost_sum_cents": str(inventory.yes_cost_sum_cents),
                "no_cost_sum_cents": str(inventory.no_cost_sum_cents),
                "yes_pending_sell": str(inventory.yes_pending_sell),
                "no_pending_sell": str(inventory.no_pending_sell),
                "frozen_balance_cents": str(inventory.frozen_balance_cents),
            },
        )

    async def get(self, market_id: str) -> Inventory | None:
        data = await self._redis.hgetall(self._key(market_id))
        if not data:
            return None
        return Inventory(
            cash_cents=int(data.get(b"cash_cents", 0)),
            yes_volume=int(data.get(b"yes_volume", 0)),
            no_volume=int(data.get(b"no_volume", 0)),
            yes_cost_sum_cents=int(data.get(b"yes_cost_sum_cents", 0)),
            no_cost_sum_cents=int(data.get(b"no_cost_sum_cents", 0)),
            yes_pending_sell=int(data.get(b"yes_pending_sell", 0)),
            no_pending_sell=int(data.get(b"no_pending_sell", 0)),
            frozen_balance_cents=int(data.get(b"frozen_balance_cents", 0)),
        )

    async def set_pending_sell(
        self,
        market_id: str,
        yes_pending_sell: int,
        no_pending_sell: int,
    ) -> None:
        await self._redis.hset(
            self._key(market_id),
            mapping={
                "yes_pending_sell": str(yes_pending_sell),
                "no_pending_sell": str(no_pending_sell),
            },
        )

    async def delete(self, market_id: str) -> None:
        await self._redis.delete(self._key(market_id))
