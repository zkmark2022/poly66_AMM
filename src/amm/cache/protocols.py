"""Typing protocols for async Redis interactions used by the AMM."""
from __future__ import annotations

from typing import Any, Protocol


class AsyncRedisLike(Protocol):
    async def hset(
        self,
        name: str,
        key: str | None = None,
        value: Any = None,
        mapping: dict[str, Any] | None = None,
    ) -> int: ...

    async def hget(self, name: str, key: str) -> Any: ...

    async def hgetall(self, name: str) -> dict[Any, Any]: ...

    async def hdel(self, name: str, *keys: str) -> int: ...

    async def delete(self, *names: str) -> int: ...

    def pipeline(self, transaction: bool = True) -> Any: ...
