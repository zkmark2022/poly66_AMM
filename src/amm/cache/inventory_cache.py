"""Redis inventory cache — CRUD for amm:inventory:{market_id}."""
from typing import Protocol, runtime_checkable


@runtime_checkable
class InventoryCache(Protocol):
    """Protocol for inventory cache implementations."""

    async def set_pending_sell(
        self,
        market_id: str,
        yes_pending_sell: int,
        no_pending_sell: int,
    ) -> None:
        """Write pending_sell values into Redis inventory hash."""
        ...
