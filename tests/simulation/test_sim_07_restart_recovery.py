from __future__ import annotations

import pytest

from src.amm.cache.inventory_cache import InventoryCache
from src.amm.main import quote_cycle

from tests.simulation.conftest import (
    build_live_cycle_services,
    clear_intent_keys,
    make_config,
    make_inventory,
    make_live_context,
    make_shared_async_redis,
    serialize_inventory,
)


@pytest.mark.asyncio
async def test_restart_recovery_preserves_cached_inventory_and_requotes(
    mock_exchange: dict,
) -> None:
    # Use MATURE gamma + high kappa for reasonable spread (3+3 gradient levels).
    config = make_config(market_id="sim-restart", gamma_tier="MATURE", kappa=10.0)
    redis_a, redis_b = make_shared_async_redis()
    cache_a = InventoryCache(redis_a)
    initial_inventory = make_inventory(yes_volume=200, no_volume=200, cash_cents=50_000)
    ctx_a = make_live_context(config, initial_inventory)
    await cache_a.set(ctx_a.market_id, ctx_a.inventory)
    services_a = build_live_cycle_services(config=config, cache=cache_a, client=mock_exchange["client"])

    await quote_cycle(ctx_a, **services_a)

    recovered_after_a = await cache_a.get(ctx_a.market_id)
    assert recovered_after_a is not None
    # Instance A must have written meaningful runtime state, not just no-op persisted the initial snapshot.
    assert recovered_after_a.yes_pending_sell > 0
    assert recovered_after_a.no_pending_sell > 0

    cache_b = InventoryCache(redis_b)
    recovered = await cache_b.get(ctx_a.market_id)
    assert recovered is not None
    assert serialize_inventory(recovered) == serialize_inventory(recovered_after_a)

    ctx_b = make_live_context(config, recovered)
    await clear_intent_keys(redis_b)
    services_b = build_live_cycle_services(config=config, cache=cache_b, client=mock_exchange["client"])
    await quote_cycle(ctx_b, **services_b)

    prices = sorted(order.price_cents for order in services_b["order_mgr"].active_orders.values())
    # Restarted instance should resume normal quoting with 3 ask + 3 bid gradient levels.
    assert len(prices) == 6
    # All prices must be in reasonable range around mid (not pinned to extremes).
    assert all(30 <= p <= 70 for p in prices), f"Prices out of range: {prices}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cash_cents", "yes_volume", "no_volume", "allocated_cash_cents"),
    [
        (12_345, 777, 333, 6_172),
        (54_321, 111, 999, 27_160),
    ],
)
async def test_restart_recovery_round_trips_integer_fields_exactly(
    cash_cents: int,
    yes_volume: int,
    no_volume: int,
    allocated_cash_cents: int,
) -> None:
    redis_a, _ = make_shared_async_redis()
    cache = InventoryCache(redis_a)
    inventory = make_inventory(
        yes_volume=yes_volume,
        no_volume=no_volume,
        cash_cents=cash_cents,
        yes_pending_sell=42,
        no_pending_sell=17,
        allocated_cash_cents=allocated_cash_cents,
    )
    inventory.frozen_balance_cents = 999
    await cache.set("sim-precision", inventory)

    recovered = await cache.get("sim-precision")

    # Redis serialization must preserve integer cents exactly across restart boundaries.
    assert recovered is not None
    assert serialize_inventory(recovered) == serialize_inventory(inventory)
