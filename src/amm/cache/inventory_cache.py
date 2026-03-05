"""Redis inventory cache for AMM. CRUD for amm:inventory:{market_id} Hash."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.amm.models.inventory import Inventory

if TYPE_CHECKING:
    import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_KEY_PREFIX = "amm:inventory"
_INTENT_KEY_PREFIX = "amm:intent"


def _key(market_id: str) -> str:
    return f"{_KEY_PREFIX}:{market_id}"


def _intent_key(market_id: str, fingerprint: str) -> str:
    return f"{_INTENT_KEY_PREFIX}:{market_id}:{fingerprint}"


class InventoryCache:
    def __init__(self, redis: "aioredis.Redis") -> None:
        self._redis = redis

    async def set(self, market_id: str, inventory: Inventory) -> None:
        await self._redis.hset(_key(market_id), mapping={
            "cash_cents": inventory.cash_cents,
            "yes_volume": inventory.yes_volume,
            "no_volume": inventory.no_volume,
            "yes_cost_sum_cents": inventory.yes_cost_sum_cents,
            "no_cost_sum_cents": inventory.no_cost_sum_cents,
            "yes_pending_sell": inventory.yes_pending_sell,
            "no_pending_sell": inventory.no_pending_sell,
            "frozen_balance_cents": inventory.frozen_balance_cents,
        })

    async def get(self, market_id: str) -> Inventory | None:
        raw = await self._redis.hgetall(_key(market_id))
        if not raw:
            return None

        def _int(k: str) -> int:
            # hgetall returns bytes keys in production Redis but str keys in fakeredis;
            # try bytes key first, fall back to str key, default to b"0".
            v = raw.get(k.encode(), raw.get(k, b"0"))
            return int(v)

        return Inventory(
            cash_cents=_int("cash_cents"),
            yes_volume=_int("yes_volume"),
            no_volume=_int("no_volume"),
            yes_cost_sum_cents=_int("yes_cost_sum_cents"),
            no_cost_sum_cents=_int("no_cost_sum_cents"),
            yes_pending_sell=_int("yes_pending_sell"),
            no_pending_sell=_int("no_pending_sell"),
            frozen_balance_cents=_int("frozen_balance_cents"),
        )

    async def adjust(
        self,
        market_id: str,
        yes_delta: int = 0,
        no_delta: int = 0,
        cash_delta: int = 0,
        yes_cost_delta: int = 0,
        no_cost_delta: int = 0,
    ) -> None:
        key = _key(market_id)
        pipe = self._redis.pipeline()
        if yes_delta:
            pipe.hincrby(key, "yes_volume", yes_delta)
        if no_delta:
            pipe.hincrby(key, "no_volume", no_delta)
        if cash_delta:
            pipe.hincrby(key, "cash_cents", cash_delta)
        if yes_cost_delta:
            pipe.hincrby(key, "yes_cost_sum_cents", yes_cost_delta)
        if no_cost_delta:
            pipe.hincrby(key, "no_cost_sum_cents", no_cost_delta)
        await pipe.execute()

    async def set_pending_sell(
        self,
        market_id: str,
        yes_pending_sell: int,
        no_pending_sell: int,
    ) -> None:
        await self._redis.hset(_key(market_id), mapping={
            "yes_pending_sell": yes_pending_sell,
            "no_pending_sell": no_pending_sell,
        })

    async def delete(self, market_id: str) -> None:
        await self._redis.delete(_key(market_id))

    async def mark_order_submission(
        self,
        market_id: str,
        fingerprint: str,
        ttl_seconds: int = 300,
    ) -> bool:
        """Mark an order intent as submitted to prevent duplicate replay after restart."""
        created = await self._redis.set(
            _intent_key(market_id, fingerprint),
            "1",
            ex=ttl_seconds,
            nx=True,
        )
        return bool(created)

    async def clear_order_submission(self, market_id: str, fingerprint: str) -> None:
        await self._redis.delete(_intent_key(market_id, fingerprint))
