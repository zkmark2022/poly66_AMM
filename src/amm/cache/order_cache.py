"""Redis CRUD for AMM open orders.

Key pattern: amm:orders:{market_id}
Storage: Redis Hash mapping order_id → JSON-encoded order dict.
"""
import json

import redis.asyncio as aioredis


def _key(market_id: str) -> str:
    return f"amm:orders:{market_id}"


class OrderCache:
    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    async def get_all(self, market_id: str) -> dict[str, dict]:
        """Return all orders for a market as {order_id: order_dict}."""
        raw = await self._redis.hgetall(_key(market_id))
        return {order_id: json.loads(payload) for order_id, payload in raw.items()}

    async def get_order(self, market_id: str, order_id: str) -> dict | None:
        """Return a single order, or None if not present."""
        raw = await self._redis.hget(_key(market_id), order_id)
        if raw is None:
            return None
        return json.loads(raw)  # type: ignore[return-value]

    async def set_order(self, market_id: str, order_id: str, order: dict) -> None:
        """Upsert a single order."""
        await self._redis.hset(_key(market_id), order_id, json.dumps(order))

    async def delete_order(self, market_id: str, order_id: str) -> None:
        """Remove a single order from the cache."""
        await self._redis.hdel(_key(market_id), order_id)

    async def delete_all(self, market_id: str) -> None:
        """Remove all orders for a market (e.g. after batch-cancel)."""
        await self._redis.delete(_key(market_id))
