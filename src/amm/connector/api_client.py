"""REST API client for AMM ↔ matching engine communication."""
import asyncio
import logging
import random
import re
from typing import Any

import httpx

from src.amm.connector.auth import TokenManager

logger = logging.getLogger(__name__)

MAX_RETRY_ATTEMPTS = 3
MAX_RETRY_SECONDS = 60
RETRYABLE_STATUS = {502, 503, 504}
RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.TransportError,
    httpx.ConnectError,
)

_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _sanitize_id(id_value: str) -> str:
    """Reject IDs containing path separators or other unsafe characters."""
    if not _ID_PATTERN.match(str(id_value)):
        raise ValueError(f"Invalid ID format: {id_value!r}")
    return str(id_value)


class AMMApiClient:
    def __init__(self, base_url: str, token_manager: TokenManager,
                 http_client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url
        self._token_manager = token_manager
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(base_url=base_url, timeout=10.0)

    async def _request(
        self, method: str, path: str, **kwargs: Any,
    ) -> dict:
        """Make authenticated request with retries for safe/idempotent operations only."""
        method = method.upper()
        can_retry = method in {"GET", "DELETE"}

        for attempt in range(MAX_RETRY_ATTEMPTS):
            headers = {"Authorization": f"Bearer {self._token_manager.access_token}"}
            try:
                resp = await self._client.request(method, path, headers=headers, **kwargs)
            except RETRYABLE_EXCEPTIONS:
                if not can_retry or attempt == MAX_RETRY_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(min(2 ** attempt, MAX_RETRY_SECONDS))
                continue

            if resp.status_code == 401:
                await self._token_manager.refresh()
                continue

            if resp.status_code == 429:
                if not can_retry or attempt == MAX_RETRY_ATTEMPTS - 1:
                    resp.raise_for_status()
                try:
                    retry_after = float(resp.headers.get("Retry-After", "2"))
                except ValueError:
                    retry_after = 2.0
                await asyncio.sleep(min(retry_after, MAX_RETRY_SECONDS))
                continue

            if resp.status_code in RETRYABLE_STATUS:
                if not can_retry or attempt == MAX_RETRY_ATTEMPTS - 1:
                    resp.raise_for_status()
                wait = (2 ** attempt) + random.uniform(0, 1)
                await asyncio.sleep(min(wait, MAX_RETRY_SECONDS))
                continue

            resp.raise_for_status()
            return resp.json()

        raise RuntimeError(f"Max retries exceeded for {method} {path}")

    async def place_order(self, params: dict) -> dict:
        return await self._request("POST", "/orders", json=params)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._request("POST", f"/orders/{_sanitize_id(order_id)}/cancel")

    async def replace_order(self, old_order_id: str, new_order: dict) -> dict:
        return await self._request("POST", "/amm/orders/replace",
                                   json={"old_order_id": old_order_id, "new_order": new_order})

    async def batch_cancel(self, market_id: str, scope: str = "ALL") -> dict:
        return await self._request("POST", "/amm/orders/batch-cancel",
                                   json={"market_id": _sanitize_id(market_id), "cancel_scope": scope})

    async def mint(self, market_id: str, quantity: int, key: str) -> dict:
        return await self._request("POST", "/amm/mint",
                                   json={"market_id": _sanitize_id(market_id), "quantity": quantity,
                                         "idempotency_key": key})

    async def burn(self, market_id: str, quantity: int, key: str) -> dict:
        return await self._request("POST", "/amm/burn",
                                   json={"market_id": _sanitize_id(market_id), "quantity": quantity,
                                         "idempotency_key": key})

    async def get_balance(self) -> dict:
        return await self._request("GET", "/account/balance")

    async def get_positions(self, market_id: str) -> dict:
        return await self._request("GET", f"/positions/{_sanitize_id(market_id)}")

    async def get_trades(self, market_id: str, cursor: str = "", limit: int = 50) -> dict:
        params: dict = {"market_id": _sanitize_id(market_id), "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/trades", params=params)

    async def get_market(self, market_id: str) -> dict:
        return await self._request("GET", f"/markets/{_sanitize_id(market_id)}")

    async def get_orderbook(self, market_id: str) -> dict:
        return await self._request("GET", f"/markets/{_sanitize_id(market_id)}/orderbook")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
