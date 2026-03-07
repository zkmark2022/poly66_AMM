"""Typing protocols for async Redis interactions used by the AMM."""
from __future__ import annotations

from typing import Any, Awaitable, Protocol


class AsyncRedisLike(Protocol):
    def hset(
        self,
        name: str,
        key: str | None = None,
        value: Any = None,
        mapping: dict[str, Any] | None = None,
        items: list[Any] | None = None,
    ) -> Awaitable[int]: ...

    def hget(self, name: str, key: str) -> Awaitable[Any]: ...

    def hgetall(self, name: str) -> Awaitable[dict[Any, Any]]: ...

    def hdel(self, name: str, *keys: str) -> Awaitable[int]: ...

    def delete(self, *names: str) -> Awaitable[int]: ...

    def set(
        self,
        name: str,
        value: Any,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
        xx: bool = False,
    ) -> Awaitable[Any]: ...

    def pipeline(self, transaction: bool = True) -> Any: ...
