"""Redis order cache for AMM. CRUD for amm:orders:{market_id} Hash."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.amm.cache.protocols import AsyncRedisLike

logger = logging.getLogger(__name__)

_KEY_PREFIX = "amm:orders"


def _key(market_id: str) -> str:
    return f"{_KEY_PREFIX}:{market_id}"


class OrderCache:
    def __init__(self, redis: "AsyncRedisLike") -> None:
        self._redis = redis

    async def set_order(self, market_id: str, order_id: str, order_data: dict) -> None:
        await self._redis.hset(_key(market_id), order_id, json.dumps(order_data))

    async def get_order(self, market_id: str, order_id: str) -> dict | None:
        raw = await self._redis.hget(_key(market_id), order_id)
        if raw is None:
            return None
        return json.loads(raw)

    async def get_all_orders(self, market_id: str) -> dict[str, dict]:
        raw = await self._redis.hgetall(_key(market_id))
        result = {}
        for k, v in raw.items():
            key = k.decode() if isinstance(k, bytes) else k
            result[key] = json.loads(v)
        return result

    async def delete_order(self, market_id: str, order_id: str) -> None:
        await self._redis.hdel(_key(market_id), order_id)

    async def clear(self, market_id: str) -> None:
        await self._redis.delete(_key(market_id))
