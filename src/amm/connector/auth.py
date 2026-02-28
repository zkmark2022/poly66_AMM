"""Token management for AMM ↔ matching engine authentication.

Handles login and JWT auto-refresh. Tokens are stored in memory only.
"""
import logging

import httpx

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when login or refresh fails."""


class TokenManager:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base_url = base_url
        self._username = username
        self._password = password
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._client = httpx.AsyncClient(base_url=base_url, timeout=10.0)

    @property
    def access_token(self) -> str:
        return self._access_token

    async def login(self) -> None:
        """Obtain initial access + refresh tokens via /auth/login."""
        resp = await self._client.post(
            "/auth/login",
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200:
            raise AuthError(f"Login failed: {resp.status_code} {resp.text}")
        data = resp.json()
        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        logger.info("TokenManager: login succeeded")

    async def refresh(self) -> None:
        """Refresh the access token using the stored refresh token."""
        resp = await self._client.post(
            "/auth/refresh",
            json={"refresh_token": self._refresh_token},
        )
        if resp.status_code != 200:
            # Refresh token expired — fall back to full re-login
            logger.warning("TokenManager: refresh failed (%d), re-logging in", resp.status_code)
            await self.login()
            return
        data = resp.json()
        self._access_token = data["access_token"]
        if "refresh_token" in data:
            self._refresh_token = data["refresh_token"]
        logger.debug("TokenManager: token refreshed")

    async def close(self) -> None:
        await self._client.aclose()
