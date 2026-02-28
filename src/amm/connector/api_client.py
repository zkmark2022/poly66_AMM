"""REST API client for AMM ↔ matching engine communication."""
import asyncio
import logging
from typing import Any

import httpx

from src.amm.connector.auth import TokenManager

logger = logging.getLogger(__name__)

MAX_RETRY_ATTEMPTS = 3


class AMMApiClient:
    def __init__(self, base_url: str, token_manager: TokenManager) -> None:
        self._base_url = base_url
        self._token_manager = token_manager
        self._client = httpx.AsyncClient(base_url=base_url, timeout=10.0)

    async def _request(
        self, method: str, path: str, _retry_count: int = 0, **kwargs: Any,
    ) -> dict:
        """Make authenticated request with auto-retry on 401 and 429."""
        headers = {"Authorization": f"Bearer {self._token_manager.access_token}"}
        resp = await self._client.request(method, path, headers=headers, **kwargs)

        if resp.status_code == 401:
            await self._token_manager.refresh()
            headers["Authorization"] = f"Bearer {self._token_manager.access_token}"
            resp = await self._client.request(method, path, headers=headers, **kwargs)

        if resp.status_code == 429 and _retry_count < MAX_RETRY_ATTEMPTS:
            retry_after = int(resp.headers.get("Retry-After",
                              resp.headers.get("X-RateLimit-Reset", "1")))
            backoff = min(retry_after * (2 ** _retry_count), 30)
            logger.warning("Rate limited on %s %s (attempt %d/%d), sleeping %ds",
                           method, path, _retry_count + 1, MAX_RETRY_ATTEMPTS, backoff)
            await asyncio.sleep(backoff)
            return await self._request(method, path, _retry_count=_retry_count + 1, **kwargs)

        resp.raise_for_status()
        return resp.json()

    async def place_order(self, params: dict) -> dict:
        return await self._request("POST", "/orders", json=params)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._request("POST", f"/orders/{order_id}/cancel")

    async def replace_order(self, old_order_id: str, new_order: dict) -> dict:
        return await self._request("POST", "/amm/orders/replace",
                                   json={"old_order_id": old_order_id, "new_order": new_order})

    async def batch_cancel(self, market_id: str, scope: str = "ALL") -> dict:
        return await self._request("POST", "/amm/orders/batch-cancel",
                                   json={"market_id": market_id, "cancel_scope": scope})

    async def mint(self, market_id: str, quantity: int, key: str) -> dict:
        return await self._request("POST", "/amm/mint",
                                   json={"market_id": market_id, "quantity": quantity,
                                         "idempotency_key": key})

    async def burn(self, market_id: str, quantity: int, key: str) -> dict:
        return await self._request("POST", "/amm/burn",
                                   json={"market_id": market_id, "quantity": quantity,
                                         "idempotency_key": key})

    async def get_balance(self) -> dict:
        return await self._request("GET", "/account/balance")

    async def get_positions(self, market_id: str) -> dict:
        return await self._request("GET", f"/positions/{market_id}")

    async def get_trades(self, cursor: str = "", limit: int = 50) -> dict:
        params: dict = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/trades", params=params)

    async def get_market(self, market_id: str) -> dict:
        return await self._request("GET", f"/markets/{market_id}")

    async def get_orderbook(self, market_id: str) -> dict:
        return await self._request("GET", f"/markets/{market_id}/orderbook")

    async def close(self) -> None:
        await self._client.aclose()
