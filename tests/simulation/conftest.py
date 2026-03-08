"""Shared fixtures for Layer 3 simulation tests.

Provides:
- mock_exchange: respx-mocked httpx.AsyncClient with call recording
- fake_redis_sync / fake_redis_async: fakeredis instances
- make_market_config: factory for MarketConfig with sensible test defaults
"""
from __future__ import annotations

import json
from typing import Any, Callable
import fakeredis
import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
import respx

from src.amm.config.models import MarketConfig


# ---------------------------------------------------------------------------
# 1. mock_exchange — respx-based mock exchange with call recording
# ---------------------------------------------------------------------------

def _default_orderbook() -> dict:
    return {"data": {"best_bid": 48, "best_ask": 52, "bid_depth": 500, "ask_depth": 500}}


def _default_positions(market_id: str) -> dict:
    return {
        "data": {
            "market_id": market_id,
            "yes_shares": 1000,
            "no_shares": 1000,
            "cash_cents": 50_000,
        }
    }


def _default_market(market_id: str) -> dict:
    return {"data": {"market_id": market_id, "status": "active"}}


def _default_trades() -> dict:
    return {"data": {"trades": [], "cursor": ""}}


def _default_balance() -> dict:
    return {"data": {"balance_cents": 100_000, "frozen_cents": 0}}


@pytest.fixture()
def mock_exchange() -> Any:
    """Mock exchange yielding an httpx.AsyncClient with call recording."""
    orders_placed: list[dict] = []
    orders_cancelled: list[str] = []
    call_log: list[dict] = []

    base_url = "http://test-exchange"

    with respx.mock(base_url=base_url, assert_all_mocked=True, assert_all_called=False) as router:
        # --- order endpoints ---
        def _handle_place_order(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            orders_placed.append(body)
            call_log.append({"method": "POST", "path": "/orders", "body": body})
            return httpx.Response(200, json={"data": {"order_id": f"ord-{len(orders_placed)}"}})

        def _handle_cancel_order(request: httpx.Request) -> httpx.Response:
            path = request.url.path.rstrip("/")
            # extract order id: /orders/{id}/cancel
            parts = path.split("/")
            order_id = parts[-2] if parts[-1] == "cancel" else parts[-1]
            orders_cancelled.append(order_id)
            call_log.append({"method": "POST", "path": path, "body": None})
            return httpx.Response(200, json={"data": {"status": "cancelled"}})

        def _handle_batch_cancel(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            call_log.append({"method": "POST", "path": "/amm/orders/batch-cancel", "body": body})
            return httpx.Response(200, json={"data": {"cancelled": 0}})

        def _handle_replace(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            call_log.append({"method": "POST", "path": "/amm/orders/replace", "body": body})
            return httpx.Response(200, json={"data": {"order_id": "ord-replaced"}})

        def _handle_mint(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            call_log.append({"method": "POST", "path": "/amm/mint", "body": body})
            return httpx.Response(200, json={"data": {"status": "minted"}})

        def _handle_burn(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            call_log.append({"method": "POST", "path": "/amm/burn", "body": body})
            return httpx.Response(200, json={"data": {"status": "burned"}})

        # POST routes
        router.post("/orders").mock(side_effect=_handle_place_order)
        router.post(path__regex=r"/orders/.+/cancel").mock(side_effect=_handle_cancel_order)
        router.post("/amm/orders/batch-cancel").mock(side_effect=_handle_batch_cancel)
        router.post("/amm/orders/replace").mock(side_effect=_handle_replace)
        router.post("/amm/mint").mock(side_effect=_handle_mint)
        router.post("/amm/burn").mock(side_effect=_handle_burn)

        # GET routes — use side_effect lambdas so path params work
        def _handle_orderbook(request: httpx.Request) -> httpx.Response:
            call_log.append({"method": "GET", "path": request.url.path, "body": None})
            return httpx.Response(200, json=_default_orderbook())

        def _handle_positions(request: httpx.Request) -> httpx.Response:
            parts = request.url.path.split("/")
            mid = parts[-1]
            call_log.append({"method": "GET", "path": request.url.path, "body": None})
            return httpx.Response(200, json=_default_positions(mid))

        def _handle_market(request: httpx.Request) -> httpx.Response:
            parts = request.url.path.split("/")
            mid = parts[-1]
            call_log.append({"method": "GET", "path": request.url.path, "body": None})
            return httpx.Response(200, json=_default_market(mid))

        def _handle_trades(request: httpx.Request) -> httpx.Response:
            call_log.append({"method": "GET", "path": request.url.path, "body": None})
            return httpx.Response(200, json=_default_trades())

        def _handle_balance(request: httpx.Request) -> httpx.Response:
            call_log.append({"method": "GET", "path": request.url.path, "body": None})
            return httpx.Response(200, json=_default_balance())

        router.get(path__regex=r"/markets/.+/orderbook").mock(side_effect=_handle_orderbook)
        router.get(path__regex=r"/positions/.+").mock(side_effect=_handle_positions)
        router.get("/trades").mock(side_effect=_handle_trades)
        router.get("/account/balance").mock(side_effect=_handle_balance)
        # Must be after /markets/.../orderbook to avoid shadowing
        router.get(path__regex=r"/markets/[^/]+$").mock(side_effect=_handle_market)

        client = httpx.AsyncClient(base_url=base_url)

        yield {
            "client": client,
            "orders_placed": orders_placed,
            "orders_cancelled": orders_cancelled,
            "call_log": call_log,
        }


# ---------------------------------------------------------------------------
# 2. fake_redis_sync
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_redis_sync() -> Any:
    """Synchronous fakeredis instance, auto-flushed on teardown."""
    r = fakeredis.FakeRedis()
    yield r
    r.flushall()
    r.close()


# ---------------------------------------------------------------------------
# 3. fake_redis_async
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def fake_redis_async() -> Any:
    """Async fakeredis instance, auto-flushed on teardown."""
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.flushall()
    await r.aclose()


# ---------------------------------------------------------------------------
# 4. make_market_config — factory fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def make_market_config() -> Callable[..., MarketConfig]:
    """Factory that creates MarketConfig with test-friendly defaults.

    Parameters map to simulation concepts:
    - market_id: unique market identifier
    - yes_volume / no_volume: initial mint quantity derives from these
    - cash_cents: starting cash (used for risk budget calculations)
    - tau_hours: remaining_hours_override for A-S model time horizon
    - mid_price: float [0,1] → converted to anchor_price_cents (int cents)
    """

    def _factory(
        *,
        market_id: str = "test-market-001",
        yes_volume: int = 1000,
        no_volume: int = 1000,
        cash_cents: int = 50_000,
        tau_hours: float = 24.0,
        mid_price: float = 0.50,
    ) -> MarketConfig:
        anchor_cents = int(mid_price * 100)
        total_shares = yes_volume + no_volume
        return MarketConfig(
            market_id=market_id,
            anchor_price_cents=anchor_cents,
            initial_mint_quantity=total_shares,
            remaining_hours_override=tau_hours,
            max_daily_loss_cents=cash_cents,
            max_per_market_loss_cents=cash_cents // 2,
        )

    return _factory
