"""Shared fixtures for simulation Group B tests."""
from __future__ import annotations

import httpx
import pytest
import respx
import fakeredis.aioredis

BASE_URL = "http://test-exchange"

_LOGIN_RESPONSE = {
    "data": {"access_token": "test_token", "refresh_token": "test_refresh"}
}
_REFRESH_RESPONSE = {
    "data": {"access_token": "refreshed_token", "refresh_token": "refreshed_refresh"}
}
_BALANCE_RESPONSE = {"data": {"balance_cents": 1_000_000, "frozen_balance_cents": 0}}
_POSITIONS_RESPONSE = {
    "data": {
        "yes_volume": 0,
        "no_volume": 0,
        "yes_cost_sum_cents": 0,
        "no_cost_sum_cents": 0,
    }
}
_MARKET_RESPONSE = {"data": {"id": "test-mkt", "status": "ACTIVE"}}
_MINT_RESPONSE = {"data": {"yes_shares": 1000, "no_shares": 1000}}
_BATCH_CANCEL_RESPONSE = {"data": {"cancelled_count": 5, "market_id": "test-mkt"}}
_ORDER_RESPONSE = {"data": {"order_id": "order-1"}}
_REPLACE_RESPONSE = {"data": {"order_id": "replaced-1"}}
_TRADES_RESPONSE = {"data": {"trades": [], "cursor": ""}}
_ORDERBOOK_RESPONSE = {"data": {"yes_asks": [], "no_asks": []}}


@pytest.fixture
async def fake_redis_async():  # type: ignore[return]
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.aclose()


@pytest.fixture
def mock_exchange():  # type: ignore[return]
    """Mocked exchange client.

    Yields a dict with:
    - ``client``: ``httpx.AsyncClient`` with all standard routes mocked.
    - ``orders_placed``: list of raw ``httpx.Request`` objects for POST /orders.
    - ``call_log``: list of ``{"path": str, "method": str}`` for every request.
    """
    orders_placed: list[httpx.Request] = []
    call_log: list[dict[str, str]] = []

    order_counter = [0]

    def _make_side_effect(path: str, response_json: dict):
        def _handler(request: httpx.Request) -> httpx.Response:
            call_log.append({"path": path, "method": request.method})
            if path == "/orders":
                orders_placed.append(request)
                order_counter[0] += 1
                return httpx.Response(
                    200, json={"data": {"order_id": f"order-{order_counter[0]}"}}
                )
            return httpx.Response(200, json=response_json)

        return _handler

    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.post("/auth/login").mock(
            return_value=httpx.Response(200, json=_LOGIN_RESPONSE)
        )
        router.post("/auth/refresh").mock(
            return_value=httpx.Response(200, json=_REFRESH_RESPONSE)
        )
        router.get("/account/balance").mock(
            return_value=httpx.Response(200, json=_BALANCE_RESPONSE)
        )
        router.get(url__regex=r"/positions/").mock(
            return_value=httpx.Response(200, json=_POSITIONS_RESPONSE)
        )
        router.get(url__regex=r"/markets/[^/]+$").mock(
            return_value=httpx.Response(200, json=_MARKET_RESPONSE)
        )
        router.get(url__regex=r"/markets/.+/orderbook").mock(
            return_value=httpx.Response(200, json=_ORDERBOOK_RESPONSE)
        )
        router.post("/amm/mint").mock(
            return_value=httpx.Response(200, json=_MINT_RESPONSE)
        )
        router.post("/amm/orders/batch-cancel").mock(
            side_effect=_make_side_effect("/amm/orders/batch-cancel", _BATCH_CANCEL_RESPONSE)
        )
        router.post("/orders").mock(
            side_effect=_make_side_effect("/orders", _ORDER_RESPONSE)
        )
        router.post("/amm/orders/replace").mock(
            return_value=httpx.Response(200, json=_REPLACE_RESPONSE)
        )
        router.get(url__regex=r"/trades").mock(
            return_value=httpx.Response(200, json=_TRADES_RESPONSE)
        )

        client = httpx.AsyncClient(base_url=BASE_URL)
        yield {
            "client": client,
            "orders_placed": orders_placed,
            "call_log": call_log,
        }
