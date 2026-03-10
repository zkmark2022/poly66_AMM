from __future__ import annotations

import pytest

from src.amm.main import quote_cycle
from src.amm.models.enums import DefenseLevel

from tests.simulation.conftest import (
    compute_effective_spread,
    make_config,
    make_context,
    make_inventory,
    make_real_services,
)

# Use MATURE gamma (1.5) + high kappa (10.0) to produce ~19¢ spread at mid=50.
# Default MID gamma (0.3) + kappa (1.5) produces ~121¢ spread, pinning to 1/99.
_WIDEN_CFG = dict(
    gamma_tier="MATURE",
    kappa=10.0,
    inventory_skew_widen=0.30,
    inventory_skew_one_side=0.70,
    inventory_skew_kill=0.90,
    widen_factor=1.5,
    spread_max_cents=50,
)


async def _run_cycle(yes_volume: int, no_volume: int):
    config = make_config(**_WIDEN_CFG)
    ctx = make_context(
        inventory=make_inventory(yes_volume=yes_volume, no_volume=no_volume),
        config=config,
    )
    services, order_mgr = make_real_services(ctx)
    await quote_cycle(ctx, **services)
    return ctx, order_mgr.all_intents


@pytest.mark.asyncio
async def test_widen_increases_effective_spread_by_configured_factor() -> None:
    normal_ctx, normal_intents = await _run_cycle(200, 200)
    widen_ctx, widen_intents = await _run_cycle(260, 140)

    normal_spread = compute_effective_spread(normal_intents)
    widen_spread = compute_effective_spread(widen_intents)

    assert normal_ctx.defense_level == DefenseLevel.NORMAL
    assert widen_ctx.defense_level == DefenseLevel.WIDEN
    assert normal_spread > 0, "Normal spread must be positive"
    # WIDEN must numerically expand the top-of-book spread.
    assert widen_spread > normal_spread


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("yes_volume", "no_volume", "expected_level"),
    [
        (258, 142, DefenseLevel.NORMAL),
        (260, 140, DefenseLevel.WIDEN),
        (262, 138, DefenseLevel.WIDEN),
    ],
)
async def test_widen_threshold_boundary(
    yes_volume: int,
    no_volume: int,
    expected_level: DefenseLevel,
) -> None:
    ctx, intents = await _run_cycle(yes_volume, no_volume)

    # Boundary coverage ensures the defense uses the intended >= threshold semantics.
    assert ctx.defense_level == expected_level
    assert all(1 <= intent.price_cents <= 99 for intent in intents)


@pytest.mark.asyncio
async def test_widen_recovers_to_normal_after_rebalance() -> None:
    config = make_config(
        **{**_WIDEN_CFG, "defense_cooldown_cycles": 2},
    )
    ctx = make_context(inventory=make_inventory(yes_volume=260, no_volume=140), config=config)
    services, order_mgr = make_real_services(ctx)

    await quote_cycle(ctx, **services)
    widened_spread = compute_effective_spread(order_mgr.all_intents)
    assert ctx.defense_level == DefenseLevel.WIDEN
    assert widened_spread > 0

    ctx.inventory = make_inventory(yes_volume=200, no_volume=200)
    services["inventory_cache"].get.return_value = ctx.inventory
    order_mgr.captured.clear()
    for _ in range(config.defense_cooldown_cycles - 1):
        await quote_cycle(ctx, **services)
        assert ctx.defense_level == DefenseLevel.WIDEN

    order_mgr.captured.clear()
    await quote_cycle(ctx, **services)
    recovered_spread = compute_effective_spread(order_mgr.all_intents)
    # After rebalancing, cooldown completion must return both state and spread near normal.
    assert ctx.defense_level == DefenseLevel.NORMAL
    assert recovered_spread < widened_spread
