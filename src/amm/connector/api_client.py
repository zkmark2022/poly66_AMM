"""REST API client stub — full implementation in Task 6."""
from typing import Protocol, runtime_checkable


@runtime_checkable
class AMMApiClient(Protocol):
    """Protocol for the AMM REST API client."""

    async def place_order(self, params: dict) -> dict: ...

    async def cancel_order(self, order_id: str) -> dict: ...

    async def replace_order(self, old_order_id: str, new_order: dict) -> dict: ...

    async def batch_cancel(self, market_id: str, scope: str = "ALL") -> dict: ...
