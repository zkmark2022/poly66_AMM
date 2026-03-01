"""Tests for AMMApiClient and TokenManager using respx HTTP mocking."""
import pytest
import respx
import httpx

from src.amm.connector.api_client import AMMApiClient, MAX_RETRY_SECONDS, _sanitize_id
from src.amm.connector.auth import AuthError, TokenManager

BASE = "http://localhost:8000/api/v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_token_manager(access: str = "acc-tok", refresh: str = "ref-tok") -> TokenManager:
    """Return a TokenManager with tokens pre-loaded (skip login)."""
    tm = TokenManager(BASE, "amm_user", "secret")
    tm._access_token = access
    tm._refresh_token = refresh
    return tm


async def make_client(access: str = "acc-tok") -> AMMApiClient:
    tm = make_token_manager(access=access)
    return AMMApiClient(BASE, tm)


# ---------------------------------------------------------------------------
# TokenManager
# ---------------------------------------------------------------------------

class TestTokenManager:
    @respx.mock
    async def test_login_obtains_tokens(self) -> None:
        respx.post(f"{BASE}/auth/login").mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "acc-123", "refresh_token": "ref-456"},
            )
        )
        tm = TokenManager(BASE, "user", "pass")
        await tm.login()
        assert tm.access_token == "acc-123"
        assert tm._refresh_token == "ref-456"

    @respx.mock
    async def test_login_raises_on_failure(self) -> None:
        respx.post(f"{BASE}/auth/login").mock(return_value=httpx.Response(401, json={}))
        tm = TokenManager(BASE, "user", "badpass")
        with pytest.raises(AuthError):
            await tm.login()

    @respx.mock
    async def test_refresh_updates_access_token(self) -> None:
        respx.post(f"{BASE}/auth/refresh").mock(
            return_value=httpx.Response(200, json={"access_token": "new-acc"})
        )
        tm = make_token_manager(access="old-acc", refresh="ref-tok")
        await tm.refresh()
        assert tm.access_token == "new-acc"

    @respx.mock
    async def test_refresh_falls_back_to_login_on_401(self) -> None:
        respx.post(f"{BASE}/auth/refresh").mock(return_value=httpx.Response(401, json={}))
        respx.post(f"{BASE}/auth/login").mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "fresh-acc", "refresh_token": "fresh-ref"},
            )
        )
        tm = make_token_manager()
        await tm.refresh()
        assert tm.access_token == "fresh-acc"


# ---------------------------------------------------------------------------
# AMMApiClient
# ---------------------------------------------------------------------------

class TestAMMApiClient:
    @respx.mock
    async def test_place_order(self) -> None:
        respx.post(f"{BASE}/orders").mock(
            return_value=httpx.Response(201, json={"id": "ord-1"})
        )
        client = await make_client()
        result = await client.place_order({"side": "YES_BID", "price_cents": 50, "quantity": 10})
        assert result["id"] == "ord-1"

    @respx.mock
    async def test_cancel_order(self) -> None:
        respx.post(f"{BASE}/orders/ord-1/cancel").mock(
            return_value=httpx.Response(200, json={"status": "cancelled"})
        )
        client = await make_client()
        result = await client.cancel_order("ord-1")
        assert result["status"] == "cancelled"

    @respx.mock
    async def test_replace_order(self) -> None:
        respx.post(f"{BASE}/amm/orders/replace").mock(
            return_value=httpx.Response(200, json={"new_order_id": "ord-2"})
        )
        client = await make_client()
        result = await client.replace_order("ord-1", {"price_cents": 52, "quantity": 10})
        assert result["new_order_id"] == "ord-2"

    @respx.mock
    async def test_batch_cancel(self) -> None:
        respx.post(f"{BASE}/amm/orders/batch-cancel").mock(
            return_value=httpx.Response(200, json={"cancelled": 5})
        )
        client = await make_client()
        result = await client.batch_cancel("mkt-1")
        assert result["cancelled"] == 5

    @respx.mock
    async def test_mint(self) -> None:
        respx.post(f"{BASE}/amm/mint").mock(
            return_value=httpx.Response(200, json={"minted": 100})
        )
        client = await make_client()
        result = await client.mint("mkt-1", 100, "idem-key-1")
        assert result["minted"] == 100

    @respx.mock
    async def test_burn(self) -> None:
        respx.post(f"{BASE}/amm/burn").mock(
            return_value=httpx.Response(200, json={"burned": 50})
        )
        client = await make_client()
        result = await client.burn("mkt-1", 50, "idem-key-2")
        assert result["burned"] == 50

    @respx.mock
    async def test_get_balance(self) -> None:
        respx.get(f"{BASE}/account/balance").mock(
            return_value=httpx.Response(200, json={"cash_cents": 500_000})
        )
        client = await make_client()
        result = await client.get_balance()
        assert result["cash_cents"] == 500_000

    @respx.mock
    async def test_get_positions(self) -> None:
        respx.get(f"{BASE}/positions/mkt-1").mock(
            return_value=httpx.Response(200, json={"yes_volume": 100, "no_volume": 100})
        )
        client = await make_client()
        result = await client.get_positions("mkt-1")
        assert result["yes_volume"] == 100

    @respx.mock
    async def test_get_market(self) -> None:
        respx.get(f"{BASE}/markets/mkt-1").mock(
            return_value=httpx.Response(200, json={"id": "mkt-1", "status": "OPEN"})
        )
        client = await make_client()
        result = await client.get_market("mkt-1")
        assert result["id"] == "mkt-1"

    @respx.mock
    async def test_auto_refresh_on_401(self) -> None:
        """On 401, client should refresh token and retry."""
        call_count = 0

        def balance_handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(401, json={})
            return httpx.Response(200, json={"cash_cents": 100})

        respx.post(f"{BASE}/auth/refresh").mock(
            return_value=httpx.Response(200, json={"access_token": "new-tok"})
        )
        respx.get(f"{BASE}/account/balance").mock(side_effect=balance_handler)

        client = await make_client()
        result = await client.get_balance()
        assert result["cash_cents"] == 100
        assert call_count == 2  # first 401, then success after refresh

    @respx.mock
    async def test_rate_limit_backoff(self) -> None:
        """On 429, client should back off and retry (mocked asyncio.sleep)."""
        import asyncio
        from unittest.mock import patch

        call_count = 0

        def orders_handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return httpx.Response(429, headers={"Retry-After": "0"}, json={})
            return httpx.Response(201, json={"id": "ord-99"})

        respx.post(f"{BASE}/orders").mock(side_effect=orders_handler)

        with patch("src.amm.connector.api_client.asyncio.sleep") as mock_sleep:
            mock_sleep.return_value = None
            client = await make_client()
            result = await client.place_order({"side": "YES_BID"})

        assert result["id"] == "ord-99"
        assert call_count == 2
        mock_sleep.assert_called_once_with(0)  # Retry-After=0 → backoff=0*2^0=0

    @respx.mock
    async def test_rate_limit_max_retries_raises(self) -> None:
        """After MAX_RETRY_ATTEMPTS 429s, should raise HTTPStatusError."""
        respx.post(f"{BASE}/orders").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "0"}, json={})
        )

        import asyncio
        from unittest.mock import patch

        with patch("src.amm.connector.api_client.asyncio.sleep"):
            client = await make_client()
            with pytest.raises(httpx.HTTPStatusError):
                await client.place_order({"side": "YES_BID"})


# ---------------------------------------------------------------------------
# Security: path traversal prevention
# ---------------------------------------------------------------------------

class TestSanitizeId:
    def test_valid_ids_pass(self) -> None:
        assert _sanitize_id("ord-1") == "ord-1"
        assert _sanitize_id("mkt_ABC-123") == "mkt_ABC-123"

    def test_path_traversal_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid ID format"):
            _sanitize_id("../secret")

    def test_slash_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid ID format"):
            _sanitize_id("a/b")

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid ID format"):
            _sanitize_id("")

    def test_special_chars_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid ID format"):
            _sanitize_id("id;rm -rf /")

    @respx.mock
    async def test_cancel_order_rejects_bad_id(self) -> None:
        client = await make_client()
        with pytest.raises(ValueError, match="Invalid ID format"):
            await client.cancel_order("../admin")

    @respx.mock
    async def test_get_positions_rejects_bad_id(self) -> None:
        client = await make_client()
        with pytest.raises(ValueError, match="Invalid ID format"):
            await client.get_positions("../../etc/passwd")

    @respx.mock
    async def test_get_market_rejects_bad_id(self) -> None:
        client = await make_client()
        with pytest.raises(ValueError, match="Invalid ID format"):
            await client.get_market("mkt/../../config")


# ---------------------------------------------------------------------------
# Security: Retry-After DoS prevention
# ---------------------------------------------------------------------------

class TestRetryAfterSafety:
    @respx.mock
    async def test_retry_after_capped_at_max(self) -> None:
        """Huge Retry-After header must be capped at MAX_RETRY_SECONDS."""
        from unittest.mock import patch

        call_count = 0

        def orders_handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return httpx.Response(
                    429, headers={"Retry-After": "99999"}, json={}
                )
            return httpx.Response(201, json={"id": "ord-safe"})

        respx.post(f"{BASE}/orders").mock(side_effect=orders_handler)

        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        with patch("src.amm.connector.api_client.asyncio.sleep", side_effect=fake_sleep):
            client = await make_client()
            result = await client.place_order({"side": "YES_BID"})

        assert result["id"] == "ord-safe"
        assert slept[0] <= MAX_RETRY_SECONDS, f"sleep {slept[0]}s exceeds cap {MAX_RETRY_SECONDS}s"

    @respx.mock
    async def test_retry_after_non_numeric_defaults_to_one(self) -> None:
        """Non-numeric Retry-After must not raise; should default to 1s backoff."""
        from unittest.mock import patch

        call_count = 0

        def orders_handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return httpx.Response(
                    429, headers={"Retry-After": "not-a-number"}, json={}
                )
            return httpx.Response(201, json={"id": "ord-safe2"})

        respx.post(f"{BASE}/orders").mock(side_effect=orders_handler)

        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        with patch("src.amm.connector.api_client.asyncio.sleep", side_effect=fake_sleep):
            client = await make_client()
            result = await client.place_order({"side": "YES_BID"})

        assert result["id"] == "ord-safe2"
        # default retry_after=1, backoff=min(1*2^0, 30)=1
        assert slept[0] == 1
