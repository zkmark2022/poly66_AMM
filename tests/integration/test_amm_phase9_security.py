"""Phase 9 security tests (T9.1)."""
from __future__ import annotations

import httpx
import pytest
import respx

from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.auth import TokenManager
from tests.integration.amm.conftest import BASE_URL


class TestPhase9Security:
    async def test_t91_unauthorized_access_rejected_for_normal_user_jwt(self) -> None:
        """T9.1: non-AMM JWT hitting AMM endpoint must be rejected (401/403)."""
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            router.post("/amm/orders/replace").mock(
                return_value=httpx.Response(403, json={"code": 403, "message": "forbidden"})
            )

            async with httpx.AsyncClient(base_url=BASE_URL) as client:
                tm = TokenManager(base_url=BASE_URL, username="normal", password="x", client=client)
                tm.access_token = "normal-user-jwt"
                api = AMMApiClient(base_url=BASE_URL, token_manager=tm, http_client=client)

                with pytest.raises(httpx.HTTPStatusError) as exc:
                    await api.replace_order(
                        old_order_id="ord-1",
                        new_order={
                            "market_id": "mkt-test-1",
                            "side": "YES",
                            "direction": "SELL",
                            "price_cents": 55,
                            "quantity": 100,
                        },
                    )

        assert exc.value.response.status_code in (401, 403)
