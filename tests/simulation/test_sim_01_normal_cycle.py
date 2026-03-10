from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.amm.lifecycle.reconciler import AMMReconciler
from src.amm.main import quote_cycle, reconcile_loop
from src.amm.strategy.phase_manager import PhaseManager

from tests.simulation.conftest import (
    make_config,
    make_context,
    make_inventory,
    make_mock_api,
    make_real_services,
    price_band,
)

# MATURE gamma (1.5) + kappa=10.0 yields ~19¢ spread, producing 3+3 gradient levels.
_TIGHT_CFG = dict(gamma_tier="MATURE", kappa=10.0)


@pytest.mark.asyncio
async def test_balanced_cycle_places_both_sides_with_tight_prices() -> None:
    config = make_config(**_TIGHT_CFG)
    ctx = make_context(inventory=make_inventory(yes_volume=200, no_volume=200), config=config)
    services, order_mgr = make_real_services(ctx)

    await quote_cycle(ctx, **services)

    yes_sells = [i for i in order_mgr.all_intents if i.side == "YES" and i.direction == "SELL"]
    no_sells = [i for i in order_mgr.all_intents if i.side == "NO" and i.direction == "SELL"]
    low, high = price_band(order_mgr.all_intents)

    # Balanced steady-state must quote both sides in the same cycle.
    assert len(yes_sells) == 3
    assert len(no_sells) == 3
    # With MATURE gamma + kappa=10, prices cluster around mid±10.
    assert 1 <= low <= 99
    assert 1 <= high <= 99
    assert high - low <= 10, f"Price band too wide: ({low}, {high})"
    # AMM invariant: strategy never sends BUY intents downstream.
    assert [i for i in order_mgr.all_intents if i.direction != "SELL"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "best_bid,best_ask",
    [
        (48, 52),
        (50, 50),
    ],
)
async def test_normal_cycle_respects_reasonable_price_band_for_orderbook_edges(
    best_bid: int,
    best_ask: int,
) -> None:
    config = make_config(**_TIGHT_CFG)
    ctx = make_context(inventory=make_inventory(yes_volume=200, no_volume=200), config=config)
    api = make_mock_api(best_bid=best_bid, best_ask=best_ask)
    services, order_mgr = make_real_services(ctx, api=api)

    await quote_cycle(ctx, **services)

    low, high = price_band(order_mgr.all_intents)
    # Quotes must remain within reasonable bounds regardless of orderbook edge values.
    assert 1 <= low <= 99
    assert 1 <= high <= 99
    assert len(order_mgr.all_intents) == 6


@pytest.mark.asyncio
async def test_phase_manager_update_called_once_per_cycle() -> None:
    ctx = make_context(inventory=make_inventory(yes_volume=200, no_volume=200))
    phase_mgr = PhaseManager(config=ctx.config)
    services, _ = make_real_services(ctx, phase_mgr=phase_mgr)

    with patch.object(phase_mgr, "update", wraps=phase_mgr.update) as spy:
        await quote_cycle(ctx, **services)

    # quote_cycle should perform exactly one phase transition check per loop.
    spy.assert_called_once()


@pytest.mark.asyncio
async def test_reconcile_loop_invokes_reconciler_and_stops_after_one_pass() -> None:
    ctx = make_context()
    reconciler = AsyncMock(spec=AMMReconciler)
    # Use the actual API response format: {"data": {"balance_cents": ..., "frozen_balance_cents": ...}}
    _balance_resp = {"data": {"balance_cents": 100_00, "frozen_balance_cents": 0}}
    reconciler.fetch_balance.return_value = _balance_resp
    reconciler.reconcile.return_value = {ctx.market_id: {"drifted": False, "fields": []}}

    async def one_pass_sleep(_: float) -> None:
        ctx.shutdown_requested = True

    with patch("src.amm.main.asyncio.sleep", side_effect=one_pass_sleep):
        await reconcile_loop(reconciler, {ctx.market_id: ctx}, interval_seconds=0.01)

    # reconcile_loop must fetch balance then reconcile each market.
    reconciler.fetch_balance.assert_awaited_once()
    reconciler.reconcile.assert_awaited_once_with(
        [ctx.market_id],
        n_markets_total=1,
        balance_resp=_balance_resp,
    )
