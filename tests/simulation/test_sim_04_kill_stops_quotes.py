from __future__ import annotations

import pytest

from src.amm.cache.inventory_cache import InventoryCache
from src.amm.main import quote_cycle
from src.amm.models.enums import DefenseLevel

from tests.simulation.conftest import (
    build_live_cycle_services,
    make_config,
    make_inventory,
    make_live_context,
)


@pytest.mark.asyncio
async def test_kill_switch_cancels_existing_orders_and_freezes_new_placements(
    mock_exchange: dict,
    fake_redis_async,
) -> None:
    normal_config = make_config(market_id="sim-kill", max_per_market_loss_cents=25_000)
    kill_config = make_config(market_id="sim-kill", inventory_skew_kill=0.80, max_per_market_loss_cents=25_000)
    cache = InventoryCache(fake_redis_async)
    client = mock_exchange["client"]

    normal_ctx = make_live_context(normal_config, make_inventory(yes_volume=200, no_volume=200, cash_cents=50_000))
    await cache.set(normal_ctx.market_id, normal_ctx.inventory)
    services = build_live_cycle_services(config=normal_config, cache=cache, client=client)
    await quote_cycle(normal_ctx, **services)

    pre_kill_orders = len(services["order_mgr"].active_orders)
    assert pre_kill_orders == 6

    kill_ctx = make_live_context(kill_config, make_inventory(yes_volume=360, no_volume=40, cash_cents=50_000))
    kill_ctx.initial_inventory_value_cents = normal_ctx.initial_inventory_value_cents
    await cache.set(kill_ctx.market_id, kill_ctx.inventory)
    services["risk"] = services["risk"].__class__(kill_config)
    services["pricing"].config = kill_config

    order_counts: list[int] = []
    for _ in range(3):
        before = len(mock_exchange["orders_placed"])
        await quote_cycle(kill_ctx, **services)
        order_counts.append(len(mock_exchange["orders_placed"]) - before)

    batch_cancel_calls = [c for c in mock_exchange["call_log"] if c["path"] == "/amm/orders/batch-cancel"]
    # KILL must clear live orders immediately and forbid any fresh placements afterwards.
    assert kill_ctx.defense_level == DefenseLevel.KILL_SWITCH
    assert order_counts == [0, 0, 0]
    assert len(services["order_mgr"].active_orders) == 0
    assert len(batch_cancel_calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("loss_cents", [5_000, 10_000])
async def test_pnl_kill_switch_places_no_new_orders(
    loss_cents: int,
    mock_exchange: dict,
    fake_redis_async,
) -> None:
    config = make_config(market_id=f"sim-pnl-kill-{loss_cents}", max_per_market_loss_cents=loss_cents)
    ctx = make_live_context(config, make_inventory(yes_volume=200, no_volume=200, cash_cents=50_000))
    ctx.initial_inventory_value_cents = ctx.inventory.total_value_cents(50) + loss_cents
    cache = InventoryCache(fake_redis_async)
    await cache.set(ctx.market_id, ctx.inventory)
    services = build_live_cycle_services(config=config, cache=cache, client=mock_exchange["client"])

    await quote_cycle(ctx, **services)

    batch_cancel_calls = [c for c in mock_exchange["call_log"] if c["path"] == "/amm/orders/batch-cancel"]
    # PnL KILL should halt the cycle before any placement and still issue market-wide cancel.
    assert ctx.defense_level == DefenseLevel.KILL_SWITCH
    assert len(mock_exchange["orders_placed"]) == 0
    assert len(batch_cancel_calls) == 1
