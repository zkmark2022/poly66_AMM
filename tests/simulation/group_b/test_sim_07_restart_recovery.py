"""T-SIM-07: Process restart state recovery.

Scenario: Simulate process restart.
Steps:
1. Run N quote_cycles with Instance A, accumulating inventory state in fakeredis
2. Create new bot Instance B connected to the SAME fakeredis
3. Run quote_cycle with Instance B
Assertions:
- Inventory data recovered from Redis matches Instance A's final state (no precision loss)
- Instance B's quoting behavior is continuous (prices in normal range, not reset to initial)

Key: Two INDEPENDENT instances sharing one fakeredis, not the same instance continuing.
"""
from __future__ import annotations

import time

import pytest
import fakeredis.aioredis

from src.amm.cache.inventory_cache import InventoryCache
from src.amm.config.models import MarketConfig
from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.auth import TokenManager
from src.amm.connector.order_manager import OrderManager
from src.amm.connector.trade_poller import TradePoller
from src.amm.main import quote_cycle
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext
from src.amm.risk.defense_stack import DefenseStack
from src.amm.risk.sanitizer import OrderSanitizer
from src.amm.strategy.as_engine import ASEngine
from src.amm.strategy.gradient import GradientEngine
from src.amm.strategy.phase_manager import PhaseManager
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.pricing.micro import MicroPricing
from src.amm.strategy.pricing.posterior import PosteriorPricing
from src.amm.strategy.pricing.three_layer import ThreeLayerPricing


MARKET_ID = "test-mkt-07"


def _make_config() -> MarketConfig:
    return MarketConfig(
        market_id=MARKET_ID,
        anchor_price_cents=50,
        initial_mint_quantity=2000,
        remaining_hours_override=24.0,
        max_daily_loss_cents=50_000,
        max_per_market_loss_cents=25_000,
    )


def _make_inventory() -> Inventory:
    return Inventory(
        cash_cents=50_000,
        yes_volume=1000,
        no_volume=1000,
        yes_cost_sum_cents=50_000,
        no_cost_sum_cents=50_000,
        yes_pending_sell=0,
        no_pending_sell=0,
        frozen_balance_cents=0,
    )


def _build_services(
    config: MarketConfig,
    client,
    cache: InventoryCache,
) -> dict:
    """Build all services for a bot instance."""
    token_mgr = TokenManager("http://test-exchange", "user", "pass", client)
    token_mgr._access_token = "fake-token"
    api = AMMApiClient("http://test-exchange", token_mgr, http_client=client)
    poller = TradePoller(api=api, cache=cache, amm_user_id="amm-user-never-match")
    pricing = ThreeLayerPricing(
        anchor=AnchorPricing(config.anchor_price_cents),
        micro=MicroPricing(),
        posterior=PosteriorPricing(),
        config=config,
    )
    return {
        "api": api,
        "poller": poller,
        "pricing": pricing,
        "as_engine": ASEngine(),
        "gradient": GradientEngine(),
        "risk": DefenseStack(config),
        "sanitizer": OrderSanitizer(),
        "order_mgr": OrderManager(api=api, cache=cache),
        "inventory_cache": cache,
        "phase_mgr": PhaseManager(config=config),
    }


@pytest.mark.asyncio
async def test_restart_recovery_inventory_preserved(
    mock_exchange: dict,
) -> None:
    """Two independent bot instances sharing one fakeredis: inventory state persists
    across 'restart' (new instance creation)."""
    config = _make_config()
    client = mock_exchange["client"]

    # Shared fakeredis — both instances connect to the same in-memory store
    # Use a shared server so both FakeRedis instances see the same data
    shared_server = fakeredis.FakeServer()
    shared_redis = fakeredis.aioredis.FakeRedis(server=shared_server)

    # ---- Instance A: initial run ----
    cache_a = InventoryCache(shared_redis)
    inv_a = _make_inventory()
    ctx_a = MarketContext(
        market_id=MARKET_ID,
        config=config,
        inventory=inv_a,
        initial_inventory_value_cents=inv_a.total_value_cents(50),
        last_known_market_active=True,
        market_status_checked_at=time.monotonic(),
    )
    await cache_a.set(MARKET_ID, inv_a)

    services_a = _build_services(config, client, cache_a)

    # Run several cycles to accumulate state changes
    for _ in range(5):
        await quote_cycle(ctx_a, **services_a)

    # Capture Instance A's final inventory from Redis
    final_inv_a = await cache_a.get(MARKET_ID)
    assert final_inv_a is not None, "Instance A should have saved inventory to Redis"

    # ---- Instance B: simulated restart — new objects, same redis ----
    redis_b = fakeredis.aioredis.FakeRedis(server=shared_server)
    cache_b = InventoryCache(redis_b)

    # Load inventory from Redis (as initializer would)
    recovered_inv = await cache_b.get(MARKET_ID)
    assert recovered_inv is not None, "Instance B should recover inventory from Redis"

    # Verify exact field match — no precision loss
    assert recovered_inv.cash_cents == final_inv_a.cash_cents
    assert recovered_inv.yes_volume == final_inv_a.yes_volume
    assert recovered_inv.no_volume == final_inv_a.no_volume
    assert recovered_inv.yes_cost_sum_cents == final_inv_a.yes_cost_sum_cents
    assert recovered_inv.no_cost_sum_cents == final_inv_a.no_cost_sum_cents
    assert recovered_inv.frozen_balance_cents == final_inv_a.frozen_balance_cents

    # ---- Instance B: run quote_cycle with recovered state ----
    ctx_b = MarketContext(
        market_id=MARKET_ID,
        config=config,
        inventory=recovered_inv,
        initial_inventory_value_cents=recovered_inv.total_value_cents(50),
        last_known_market_active=True,
        market_status_checked_at=time.monotonic(),
    )

    # Clear intent dedup keys from Instance A (simulates TTL expiry on restart)
    # In production, intent keys have TTL=300s and would expire between restarts
    keys = await redis_b.keys("amm:intent:*")
    for k in keys:
        await redis_b.delete(k)

    services_b = _build_services(config, client, cache_b)

    await quote_cycle(ctx_b, **services_b)

    # Instance B should produce orders (continuity, not reset)
    order_mgr_b = services_b["order_mgr"]
    assert len(order_mgr_b.active_orders) > 0, (
        "Instance B should have active orders after recovering state from Redis"
    )

    # Prices should be in normal range (near mid=50, anchor=50), not extremes
    for order in order_mgr_b.active_orders.values():
        assert 35 <= order.price_cents <= 65, f"Order price {order.price_cents} out of expected range after recovery"

    # Cleanup
    await shared_redis.aclose()
    await redis_b.aclose()


@pytest.mark.asyncio
async def test_restart_recovery_no_precision_loss(
    mock_exchange: dict,
) -> None:
    """Verify integer cents are preserved exactly through Redis serialization round-trip."""
    _config = _make_config()

    shared_server = fakeredis.FakeServer()
    redis = fakeredis.aioredis.FakeRedis(server=shared_server)
    cache = InventoryCache(redis)

    # Use non-round values to catch float-precision bugs
    inv = Inventory(
        cash_cents=12_345,
        yes_volume=777,
        no_volume=333,
        yes_cost_sum_cents=38_850,
        no_cost_sum_cents=16_650,
        yes_pending_sell=42,
        no_pending_sell=17,
        frozen_balance_cents=999,
        allocated_cash_cents=6_172,
    )

    await cache.set(MARKET_ID, inv)
    recovered = await cache.get(MARKET_ID)
    assert recovered is not None

    assert recovered.cash_cents == 12_345
    assert recovered.yes_volume == 777
    assert recovered.no_volume == 333
    assert recovered.yes_cost_sum_cents == 38_850
    assert recovered.no_cost_sum_cents == 16_650
    assert recovered.yes_pending_sell == 42
    assert recovered.no_pending_sell == 17
    assert recovered.frozen_balance_cents == 999
    assert recovered.allocated_cash_cents == 6_172

    await redis.aclose()
