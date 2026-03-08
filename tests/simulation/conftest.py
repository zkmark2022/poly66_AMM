"""Shared fixtures and builders for Layer 3 simulation tests."""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any
from unittest.mock import AsyncMock

import fakeredis
import fakeredis.aioredis
import httpx
import pytest
import respx

from src.amm.cache.inventory_cache import InventoryCache
from src.amm.config.models import MarketConfig
from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.auth import TokenManager
from src.amm.connector.order_manager import OrderManager
from src.amm.connector.trade_poller import TradePoller
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext
from src.amm.risk.defense_stack import DefenseStack
from src.amm.risk.sanitizer import OrderSanitizer
from src.amm.strategy.as_engine import ASEngine
from src.amm.strategy.gradient import GradientEngine
from src.amm.strategy.models import OrderIntent
from src.amm.strategy.phase_manager import PhaseManager
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.pricing.micro import MicroPricing
from src.amm.strategy.pricing.posterior import PosteriorPricing
from src.amm.strategy.pricing.three_layer import ThreeLayerPricing


def make_inventory(
    yes_volume: int = 200,
    no_volume: int = 200,
    cash_cents: int = 500_000,
    yes_pending_sell: int = 0,
    no_pending_sell: int = 0,
    allocated_cash_cents: int = 0,
) -> Inventory:
    return Inventory(
        cash_cents=cash_cents,
        yes_volume=yes_volume,
        no_volume=no_volume,
        yes_cost_sum_cents=yes_volume * 50,
        no_cost_sum_cents=no_volume * 50,
        yes_pending_sell=yes_pending_sell,
        no_pending_sell=no_pending_sell,
        frozen_balance_cents=0,
        allocated_cash_cents=allocated_cash_cents,
    )


def make_config(
    market_id: str = "sim-mkt-test",
    remaining_hours: float | None = 24.0,
    **overrides: Any,
) -> MarketConfig:
    defaults: dict[str, Any] = {
        "remaining_hours_override": remaining_hours,
        "anchor_price_cents": 50,
        "spread_min_cents": 2,
        "spread_max_cents": 30,
        "gradient_levels": 3,
        "gradient_price_step_cents": 1,
        "gradient_quantity_decay": 0.5,
        "initial_mint_quantity": 600,
        "defense_cooldown_cycles": 3,
        "kappa": 1.5,
        "exploration_duration_hours": 1.0,
        "stabilization_volume_threshold": 5,
    }
    defaults.update(overrides)
    return MarketConfig(market_id=market_id, **defaults)


def make_context(
    market_id: str = "sim-mkt-test",
    inventory: Inventory | None = None,
    config: MarketConfig | None = None,
    market_active: bool = True,
) -> MarketContext:
    inv = inventory or make_inventory()
    cfg = config or make_config(market_id=market_id)
    return MarketContext(
        market_id=cfg.market_id,
        config=cfg,
        inventory=inv,
        initial_inventory_value_cents=inv.total_value_cents(cfg.anchor_price_cents),
        last_known_market_active=market_active,
        market_status_checked_at=0.0,
    )


def make_mock_api(
    *,
    best_bid: int = 48,
    best_ask: int = 52,
    bid_depth: int = 10,
    ask_depth: int = 10,
    market_status: str = "active",
) -> AsyncMock:
    api = AsyncMock(spec=AMMApiClient)
    api.get_orderbook.return_value = {
        "data": {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
        }
    }
    api.get_market_status.return_value = market_status
    api.place_order.return_value = {"data": {"order_id": "mock-order-id"}}
    api.cancel_order.return_value = {"data": {"status": "cancelled"}}
    api.batch_cancel.return_value = {"data": {"cancelled": 0}}
    api.get_balance.return_value = {"data": {"balance_cents": 100_000, "frozen_balance_cents": 0}}
    api.get_positions.return_value = {
        "data": {
            "yes_volume": 200,
            "no_volume": 200,
            "yes_cost_sum_cents": 10_000,
            "no_cost_sum_cents": 10_000,
        }
    }
    return api


def make_mock_poller(*, trades: list[dict] | None = None) -> AsyncMock:
    poller = AsyncMock(spec=TradePoller)
    poller.poll.return_value = trades or []
    return poller


def make_mock_inventory_cache(ctx: MarketContext | None = None) -> AsyncMock:
    cache = AsyncMock(spec=InventoryCache)
    cache.get.return_value = ctx.inventory if ctx is not None else None
    cache.mark_order_submission.return_value = True
    cache.clear_order_submission.return_value = None
    cache.set_pending_sell.return_value = None
    cache.set.return_value = None
    return cache


class CapturingOrderManager:
    """Drop-in OrderManager double that records intents and cancellations."""

    def __init__(self) -> None:
        self._mock = AsyncMock(spec=OrderManager)
        self.captured: list[list[OrderIntent]] = []
        self.cancelled_markets: list[str] = []
        self.active_orders: dict[str, Any] = {}

        async def _capture(intents: list[OrderIntent], market_id: str) -> None:
            self.captured.append(list(intents))

        async def _cancel_all(market_id: str) -> None:
            self.cancelled_markets.append(market_id)
            self.active_orders.clear()

        self._mock.execute_intents.side_effect = _capture
        self._mock.cancel_all.side_effect = _cancel_all

    def __getattr__(self, name: str) -> Any:
        return getattr(self._mock, name)

    @property
    def all_intents(self) -> list[OrderIntent]:
        return [intent for batch in self.captured for intent in batch]


def make_real_services(
    ctx: MarketContext,
    *,
    api: AsyncMock | None = None,
    poller: AsyncMock | None = None,
    inventory_cache: AsyncMock | None = None,
    order_mgr: CapturingOrderManager | None = None,
    phase_mgr: PhaseManager | None = None,
    risk: DefenseStack | None = None,
) -> tuple[dict[str, Any], CapturingOrderManager]:
    mock_api = api or make_mock_api()
    mock_poller = poller or make_mock_poller()
    mock_cache = inventory_cache or make_mock_inventory_cache(ctx)
    capture_mgr = order_mgr or CapturingOrderManager()
    phase_manager = phase_mgr or PhaseManager(config=ctx.config)
    pricing = ThreeLayerPricing(
        anchor=AnchorPricing(ctx.config.anchor_price_cents),
        micro=MicroPricing(),
        posterior=PosteriorPricing(),
        config=ctx.config,
    )
    services = {
        "api": mock_api,
        "poller": mock_poller,
        "pricing": pricing,
        "as_engine": ASEngine(),
        "gradient": GradientEngine(),
        "risk": risk or DefenseStack(ctx.config),
        "sanitizer": OrderSanitizer(),
        "order_mgr": capture_mgr,
        "inventory_cache": mock_cache,
        "phase_mgr": phase_manager,
    }
    return services, capture_mgr


def compute_effective_spread(intents: list[OrderIntent]) -> int:
    yes_prices = [i.price_cents for i in intents if i.side == "YES" and i.direction == "SELL"]
    no_prices = [i.price_cents for i in intents if i.side == "NO" and i.direction == "SELL"]
    if not yes_prices or not no_prices:
        return -1
    return min(yes_prices) - (100 - min(no_prices))


def price_band(intents: list[OrderIntent]) -> tuple[int, int]:
    prices = [intent.price_cents for intent in intents]
    return min(prices), max(prices)


def serialize_inventory(inventory: Inventory) -> dict[str, int]:
    return {k: v for k, v in asdict(inventory).items() if isinstance(v, int)}


def build_live_cycle_services(
    *,
    config: MarketConfig,
    cache: InventoryCache,
    client: httpx.AsyncClient,
    risk: DefenseStack | None = None,
    phase_mgr: PhaseManager | None = None,
) -> dict[str, Any]:
    token_mgr = TokenManager("http://test-exchange", "user", "pass", client)
    token_mgr.access_token = "fake-token"
    api = AMMApiClient("http://test-exchange", token_mgr, http_client=client)
    return {
        "api": api,
        "poller": TradePoller(api=api, cache=cache, amm_user_id="amm-user-never-match"),
        "pricing": ThreeLayerPricing(
            anchor=AnchorPricing(config.anchor_price_cents),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
            config=config,
        ),
        "as_engine": ASEngine(),
        "gradient": GradientEngine(),
        "risk": risk or DefenseStack(config),
        "sanitizer": OrderSanitizer(),
        "order_mgr": OrderManager(api=api, cache=cache),
        "inventory_cache": cache,
        "phase_mgr": phase_mgr or PhaseManager(config=config),
    }


async def clear_intent_keys(redis_client: Any) -> None:
    for key in await redis_client.keys("amm:intent:*"):
        await redis_client.delete(key)


def _default_orderbook() -> dict[str, Any]:
    return {"data": {"best_bid": 48, "best_ask": 52, "bid_depth": 500, "ask_depth": 500}}


def _default_positions(market_id: str) -> dict[str, Any]:
    return {
        "data": {
            "market_id": market_id,
            "yes_volume": 1000,
            "no_volume": 1000,
            "yes_cost_sum_cents": 50_000,
            "no_cost_sum_cents": 50_000,
        }
    }


def _default_market(market_id: str) -> dict[str, Any]:
    return {"data": {"market_id": market_id, "status": "active"}}


def _default_trades() -> dict[str, Any]:
    return {"data": {"trades": [], "cursor": ""}}


def _default_balance() -> dict[str, Any]:
    return {"data": {"balance_cents": 100_000, "frozen_balance_cents": 0}}


@pytest.fixture()
def mock_exchange() -> Any:
    orders_placed: list[dict[str, Any]] = []
    orders_cancelled: list[str] = []
    call_log: list[dict[str, Any]] = []
    base_url = "http://test-exchange"

    with respx.mock(base_url=base_url, assert_all_mocked=False, assert_all_called=False) as router:
        def _json_body(request: httpx.Request) -> dict[str, Any]:
            return json.loads(request.content) if request.content else {}

        router.post("/auth/login").mock(
            return_value=httpx.Response(
                200,
                json={"data": {"access_token": "token", "refresh_token": "refresh"}},
            )
        )
        router.post("/auth/refresh").mock(
            return_value=httpx.Response(
                200,
                json={"data": {"access_token": "token-2", "refresh_token": "refresh-2"}},
            )
        )

        def _handle_place_order(request: httpx.Request) -> httpx.Response:
            body = _json_body(request)
            orders_placed.append(body)
            call_log.append({"method": "POST", "path": "/orders", "body": body})
            return httpx.Response(200, json={"data": {"order_id": f"ord-{len(orders_placed)}"}})

        def _handle_cancel_order(request: httpx.Request) -> httpx.Response:
            parts = request.url.path.split("/")
            order_id = parts[-2] if parts[-1] == "cancel" else parts[-1]
            orders_cancelled.append(order_id)
            call_log.append({"method": "POST", "path": request.url.path, "body": None})
            return httpx.Response(200, json={"data": {"status": "cancelled"}})

        def _handle_batch_cancel(request: httpx.Request) -> httpx.Response:
            body = _json_body(request)
            call_log.append({"method": "POST", "path": "/amm/orders/batch-cancel", "body": body})
            return httpx.Response(200, json={"data": {"cancelled": body.get("market_id", "")}})

        def _handle_replace(request: httpx.Request) -> httpx.Response:
            body = _json_body(request)
            call_log.append({"method": "POST", "path": "/amm/orders/replace", "body": body})
            return httpx.Response(200, json={"data": {"order_id": "ord-replaced"}})

        router.post("/orders").mock(side_effect=_handle_place_order)
        router.post(path__regex=r"/orders/.+/cancel").mock(side_effect=_handle_cancel_order)
        router.post("/amm/orders/batch-cancel").mock(side_effect=_handle_batch_cancel)
        router.post("/amm/orders/replace").mock(side_effect=_handle_replace)
        router.post("/amm/mint").mock(return_value=httpx.Response(200, json={"data": {"status": "minted"}}))
        router.post("/amm/burn").mock(return_value=httpx.Response(200, json={"data": {"status": "burned"}}))
        router.get(path__regex=r"/markets/.+/orderbook").mock(
            side_effect=lambda request: httpx.Response(200, json=_default_orderbook())
        )
        router.get(path__regex=r"/positions/.+").mock(
            side_effect=lambda request: httpx.Response(200, json=_default_positions(request.url.path.split("/")[-1]))
        )
        router.get("/trades").mock(return_value=httpx.Response(200, json=_default_trades()))
        router.get("/account/balance").mock(return_value=httpx.Response(200, json=_default_balance()))
        router.get(path__regex=r"/markets/[^/]+$").mock(
            side_effect=lambda request: httpx.Response(200, json=_default_market(request.url.path.split("/")[-1]))
        )

        client = httpx.AsyncClient(base_url=base_url)
        yield {
            "client": client,
            "orders_placed": orders_placed,
            "orders_cancelled": orders_cancelled,
            "call_log": call_log,
        }
        import asyncio
        asyncio.run(client.aclose())


@pytest.fixture()
def fake_redis_sync() -> Any:
    redis_client = fakeredis.FakeRedis()
    yield redis_client
    redis_client.flushall()
    redis_client.close()


@pytest.fixture()
async def fake_redis_async() -> Any:
    redis_client = fakeredis.aioredis.FakeRedis()
    yield redis_client
    await redis_client.flushall()
    await redis_client.aclose()


def make_shared_async_redis() -> tuple[Any, Any]:
    server = fakeredis.FakeServer()
    return fakeredis.aioredis.FakeRedis(server=server), fakeredis.aioredis.FakeRedis(server=server)


def make_live_context(config: MarketConfig, inventory: Inventory | None = None) -> MarketContext:
    inv = inventory or make_inventory(yes_volume=1000, no_volume=1000, cash_cents=50_000)
    return MarketContext(
        market_id=config.market_id,
        config=config,
        inventory=inv,
        initial_inventory_value_cents=inv.total_value_cents(config.anchor_price_cents),
        last_known_market_active=True,
        market_status_checked_at=time.monotonic(),
    )
