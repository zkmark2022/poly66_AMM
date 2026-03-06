"""JWT token management for AMM API authentication."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)


class TokenManager:
    """Manage JWT access/refresh tokens for AMM API."""

    def __init__(self, base_url: str, username: str, password: str,
                 client: "httpx.AsyncClient") -> None:
        self._base_url = base_url
        self._username = username
        self._password = password
        self._client = client
        self.access_token: str = ""
        self._refresh_token: str = ""

    async def login(self) -> None:
        """Obtain initial JWT tokens via username/password."""
        resp = await self._client.post(
            f"{self._base_url}/auth/login",
            json={"username": self._username, "password": self._password},
        )
        resp.raise_for_status()
        data = resp.json()
        self.access_token = data["data"]["access_token"]
        self._refresh_token = data["data"]["refresh_token"]
        logger.info("AMM login successful")

    async def refresh(self) -> None:
        """Refresh access token using refresh token."""
        resp = await self._client.post(
            f"{self._base_url}/auth/refresh",
            json={"refresh_token": self._refresh_token},
        )
        if resp.status_code == 401:
            logger.warning("Refresh token expired, re-logging in")
            await self.login()
            return
        resp.raise_for_status()
        data = resp.json()
        token_data = data["data"]
        self.access_token = token_data["access_token"]
        if "refresh_token" in token_data:
            self._refresh_token = token_data["refresh_token"]
        logger.debug("AMM token refreshed")
