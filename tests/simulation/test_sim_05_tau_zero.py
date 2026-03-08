from __future__ import annotations

import pytest

from src.amm.main import quote_cycle

from tests.simulation.conftest import (
    compute_effective_spread,
    make_config,
    make_context,
    make_inventory,
    make_real_services,
)


async def _run_tau(tau: float):
    ctx = make_context(
        inventory=make_inventory(yes_volume=200, no_volume=200),
        config=make_config(remaining_hours=tau, spread_max_cents=50, kappa=200.0),
    )
    services, order_mgr = make_real_services(ctx)
    await quote_cycle(ctx, **services)
    return order_mgr.all_intents


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tau", "expected_spread"),
    [
        (0.0, 2),
        (0.001, 2),
        (0.1, 2),
        (24.0, 2),
    ],
)
async def test_tau_values_produce_stable_non_negative_spreads(
    tau: float,
    expected_spread: int,
) -> None:
    intents = await _run_tau(tau)

    # The A-S engine should keep a valid positive spread even at expiry.
    assert compute_effective_spread(intents) == expected_spread
    assert all(1 <= intent.price_cents <= 99 for intent in intents)


@pytest.mark.asyncio
async def test_tau_zero_and_tau_twentyfour_preserve_correct_formula_ordering() -> None:
    spread_zero = compute_effective_spread(await _run_tau(0.0))
    spread_long = compute_effective_spread(await _run_tau(24.0))

    # Current A-S formula adds a positive tau component, so long horizon cannot be tighter.
    assert spread_long >= spread_zero


@pytest.mark.asyncio
@pytest.mark.parametrize("tau", [0.0, 0.001])
async def test_tau_near_zero_keeps_quotes_centered_on_anchor(tau: float) -> None:
    ctx = make_context(
        inventory=make_inventory(yes_volume=258, no_volume=142),
        config=make_config(remaining_hours=tau, anchor_price_cents=50, spread_max_cents=50),
    )
    services, order_mgr = make_real_services(ctx)

    await quote_cycle(ctx, **services)

    yes_prices = [i.price_cents for i in order_mgr.all_intents if i.side == "YES"]
    no_prices = [i.price_cents for i in order_mgr.all_intents if i.side == "NO"]
    quote_mid = (min(yes_prices) + (100 - min(no_prices))) / 2
    # Near expiry, inventory adjustment should vanish and keep the quote center near the anchor.
    assert quote_mid == 50.0
