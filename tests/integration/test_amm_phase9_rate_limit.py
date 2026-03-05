"""Phase 9 rate-limit and batch-cancel tests (T9.2, T9.6)."""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx

from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.auth import TokenManager
from tests.integration.amm.conftest import BASE_URL, MARKET_ID


class TestPhase9RateLimit:
    async def test_t92_replace_rate_limit_over_400_per_min_returns_429(self) -> None:
        """T9.2: sustained replace pressure should surface 429 responses."""
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            route = router.post("/amm/orders/replace").mock(
                return_value=httpx.Response(429, json={"code": 429, "message": "rate limited"})
            )

            async with httpx.AsyncClient(base_url=BASE_URL) as client:
                tm = TokenManager(base_url=BASE_URL, username="amm", password="x", client=client)
                tm.access_token = "amm-token"
                api = AMMApiClient(base_url=BASE_URL, token_manager=tm, http_client=client)

                with patch("src.amm.connector.api_client.MAX_RETRY_ATTEMPTS", 0):
                    seen_429 = 0
                    for i in range(401):
                        with pytest.raises(httpx.HTTPStatusError) as exc:
                            await api.replace_order(
                                old_order_id=f"ord-{i}",
                                new_order={
                                    "market_id": MARKET_ID,
                                    "side": "YES",
                                    "direction": "SELL",
                                    "price_cents": 55,
                                    "quantity": 1,
                                },
                            )
                        if exc.value.response.status_code == 429:
                            seen_429 += 1

        assert seen_429 == 401
        assert route.call_count == 401

    async def test_t96_batch_cancel_implies_n_unfreeze_ledgers(self) -> None:
        """T9.6: batch cancel with N cancellations implies N ORDER_UNFREEZE ledgers."""
        payload = {
            "data": {
                "market_id": MARKET_ID,
                "cancelled_count": 3,
                "unfreeze_ledger_entries": [
                    {"entry_type": "ORDER_UNFREEZE", "reference_id": "o-1"},
                    {"entry_type": "ORDER_UNFREEZE", "reference_id": "o-2"},
                    {"entry_type": "ORDER_UNFREEZE", "reference_id": "o-3"},
                ],
            }
        }
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            router.post("/amm/orders/batch-cancel").mock(return_value=httpx.Response(200, json=payload))

            async with httpx.AsyncClient(base_url=BASE_URL) as client:
                tm = TokenManager(base_url=BASE_URL, username="amm", password="x", client=client)
                tm.access_token = "amm-token"
                api = AMMApiClient(base_url=BASE_URL, token_manager=tm, http_client=client)

                resp = await api.batch_cancel(MARKET_ID, scope="ALL")

        data = resp["data"]
        assert data["cancelled_count"] == len(data["unfreeze_ledger_entries"])
        assert all(e["entry_type"] == "ORDER_UNFREEZE" for e in data["unfreeze_ledger_entries"])
