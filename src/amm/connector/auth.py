"""JWT Token management for AMM ↔ matching engine authentication."""
import logging

import httpx

logger = logging.getLogger(__name__)


class TokenManager:
    """Manages access/refresh tokens for the AMM service account."""

    def __init__(
        self,
        base_url: str,
        username: str = "amm_market_maker",
        password: str = "",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._access_token = ""
        self._refresh_token = ""
        self._client = http_client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = http_client is None

    @property
    def access_token(self) -> str:
        return self._access_token

    async def login(self) -> None:
        """POST /auth/login — obtain initial tokens."""
        resp = await self._client.post(
            f"{self._base_url}/auth/login",
            json={"username": self._username, "password": self._password},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        logger.info("AMM logged in successfully")

    async def refresh(self) -> None:
        """POST /auth/refresh — renew access token using refresh token."""
        resp = await self._client.post(
            f"{self._base_url}/auth/refresh",
            json={"refresh_token": self._refresh_token},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        logger.debug("AMM token refreshed")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
