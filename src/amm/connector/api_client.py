"""REST API client for AMM ↔ matching engine communication."""
import asyncio
import logging

import httpx

from src.amm.connector.auth import TokenManager

logger = logging.getLogger(__name__)

MAX_RETRY_ATTEMPTS = 3


class AMMApiClient:
    """Thin async wrapper around httpx with auto-retry on 401/429."""

    def __init__(
        self,
        base_url: str,
        token_manager: TokenManager,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_manager = token_manager
        self._client = http_client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = http_client is None

    async def _request(
        self,
        method: str,
        path: str,
        _retry_count: int = 0,
        **kwargs: object,
    ) -> dict[str, object]:
        """Authenticated request with 401 token refresh and 429 backoff.

        v1.0 Review Fix #5: 429 exponential backoff prevents rapid retries
        on rate-limit and crashing the quote cycle.
        """
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {self._token_manager.access_token}"}
        resp = await self._client.request(method, url, headers=headers, **kwargs)

        if resp.status_code == 401:
            await self._token_manager.refresh()
            headers["Authorization"] = f"Bearer {self._token_manager.access_token}"
            resp = await self._client.request(method, url, headers=headers, **kwargs)

        if resp.status_code == 429 and _retry_count < MAX_RETRY_ATTEMPTS:
            retry_after = int(resp.headers.get("Retry-After", "1"))
            backoff = min(retry_after * (2**_retry_count), 30)
            logger.warning(
                "Rate limited on %s %s (attempt %d/%d), backoff %ds",
                method,
                path,
                _retry_count + 1,
                MAX_RETRY_ATTEMPTS,
                backoff,
            )
            await asyncio.sleep(backoff)
            return await self._request(
                method, path, _retry_count=_retry_count + 1, **kwargs
            )

        resp.raise_for_status()
        return resp.json()  # type: ignore[return-value]

    async def place_order(self, params: dict[str, object]) -> dict[str, object]:
        return await self._request("POST", "/orders", json=params)

    async def cancel_order(self, order_id: str) -> dict[str, object]:
        return await self._request("POST", f"/orders/{order_id}/cancel")

    async def replace_order(
        self, old_order_id: str, new_order: dict[str, object]
    ) -> dict[str, object]:
        return await self._request(
            "POST",
            "/amm/orders/replace",
            json={"old_order_id": old_order_id, "new_order": new_order},
        )

    async def batch_cancel(
        self, market_id: str, scope: str = "ALL"
    ) -> dict[str, object]:
        return await self._request(
            "POST",
            "/amm/orders/batch-cancel",
            json={"market_id": market_id, "cancel_scope": scope},
        )

    async def mint(
        self, market_id: str, quantity: int, key: str
    ) -> dict[str, object]:
        return await self._request(
            "POST",
            "/amm/mint",
            json={"market_id": market_id, "quantity": quantity, "idempotency_key": key},
        )

    async def burn(
        self, market_id: str, quantity: int, key: str
    ) -> dict[str, object]:
        return await self._request(
            "POST",
            "/amm/burn",
            json={"market_id": market_id, "quantity": quantity, "idempotency_key": key},
        )

    async def get_balance(self) -> dict[str, object]:
        return await self._request("GET", "/account/balance")

    async def get_positions(self, market_id: str) -> dict[str, object]:
        return await self._request("GET", f"/positions/{market_id}")

    async def get_trades(self, cursor: str = "", limit: int = 50) -> dict[str, object]:
        params: dict[str, object] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/trades", params=params)

    async def get_market(self, market_id: str) -> dict[str, object]:
        return await self._request("GET", f"/markets/{market_id}")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
