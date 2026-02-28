"""Redis CRUD for AMM inventory state.

Key pattern: amm:inventory:{market_id}
Storage: Redis Hash with integer string values.
"""
import redis.asyncio as aioredis

from src.amm.models.inventory import Inventory

_FIELDS = (
    "cash_cents",
    "yes_volume",
    "no_volume",
    "yes_cost_sum_cents",
    "no_cost_sum_cents",
    "yes_pending_sell",
    "no_pending_sell",
    "frozen_balance_cents",
)


def _key(market_id: str) -> str:
    return f"amm:inventory:{market_id}"


class InventoryCache:
    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    async def get(self, market_id: str) -> Inventory | None:
        """Return Inventory from Redis, or None if key does not exist."""
        data = await self._redis.hgetall(_key(market_id))
        if not data:
            return None
        return Inventory(
            cash_cents=int(data["cash_cents"]),
            yes_volume=int(data["yes_volume"]),
            no_volume=int(data["no_volume"]),
            yes_cost_sum_cents=int(data["yes_cost_sum_cents"]),
            no_cost_sum_cents=int(data["no_cost_sum_cents"]),
            yes_pending_sell=int(data["yes_pending_sell"]),
            no_pending_sell=int(data["no_pending_sell"]),
            frozen_balance_cents=int(data["frozen_balance_cents"]),
        )

    async def set(self, market_id: str, inventory: Inventory) -> None:
        """Write full inventory snapshot to Redis."""
        mapping = {field: str(getattr(inventory, field)) for field in _FIELDS}
        await self._redis.hset(_key(market_id), mapping=mapping)

    async def adjust(
        self,
        market_id: str,
        yes_delta: int = 0,
        no_delta: int = 0,
        cash_delta: int = 0,
        yes_cost_delta: int = 0,
        no_cost_delta: int = 0,
        yes_pending_delta: int = 0,
        no_pending_delta: int = 0,
    ) -> None:
        """Atomically increment inventory fields via HINCRBY.

        Uses a pipeline for efficiency. Does NOT create the key if absent —
        caller must ensure set() is called on cold-start.
        """
        key = _key(market_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            if cash_delta:
                pipe.hincrby(key, "cash_cents", cash_delta)
            if yes_delta:
                pipe.hincrby(key, "yes_volume", yes_delta)
            if no_delta:
                pipe.hincrby(key, "no_volume", no_delta)
            if yes_cost_delta:
                pipe.hincrby(key, "yes_cost_sum_cents", yes_cost_delta)
            if no_cost_delta:
                pipe.hincrby(key, "no_cost_sum_cents", no_cost_delta)
            if yes_pending_delta:
                pipe.hincrby(key, "yes_pending_sell", yes_pending_delta)
            if no_pending_delta:
                pipe.hincrby(key, "no_pending_sell", no_pending_delta)
            await pipe.execute()

    async def delete(self, market_id: str) -> None:
        """Remove inventory key for a market."""
        await self._redis.delete(_key(market_id))
