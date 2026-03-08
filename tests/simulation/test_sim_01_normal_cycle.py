from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.amm.lifecycle.reconciler import AMMReconciler
from src.amm.main import quote_cycle, reconcile_loop
from src.amm.strategy.phase_manager import PhaseManager

from tests.simulation.conftest import (
    make_context,
    make_inventory,
    make_mock_api,
    make_real_services,
    price_band,
)


@pytest.mark.asyncio
async def test_balanced_cycle_places_both_sides_with_tight_prices() -> None:
    ctx = make_context(inventory=make_inventory(yes_volume=200, no_volume=200))
    services, order_mgr = make_real_services(ctx)

    await quote_cycle(ctx, **services)

    yes_sells = [i for i in order_mgr.all_intents if i.side == "YES" and i.direction == "SELL"]
    no_sells = [i for i in order_mgr.all_intents if i.side == "NO" and i.direction == "SELL"]
    low, high = price_band(order_mgr.all_intents)

    # Balanced steady-state must quote both sides in the same cycle.
    assert len(yes_sells) == 3
    assert len(no_sells) == 3
    # The current three-layer + binary complement mapping yields a stable 65-67 quoting band.
    assert (low, high) == (65, 67)
    # AMM invariant: strategy never sends BUY intents downstream.
    assert [i for i in order_mgr.all_intents if i.direction != "SELL"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("best_bid", "best_ask", "expected_band"),
    [
        (48, 52, (65, 67)),
        (50, 50, (65, 67)),
    ],
)
async def test_normal_cycle_respects_reasonable_price_band_for_orderbook_edges(
    best_bid: int,
    best_ask: int,
    expected_band: tuple[int, int],
) -> None:
    ctx = make_context(inventory=make_inventory(yes_volume=200, no_volume=200))
    api = make_mock_api(best_bid=best_bid, best_ask=best_ask)
    services, order_mgr = make_real_services(ctx, api=api)

    await quote_cycle(ctx, **services)

    # Edge case: even a degenerate or symmetric book must keep quotes in a bounded band.
    assert price_band(order_mgr.all_intents) == expected_band


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
    reconciler.fetch_balance.return_value = {"data": {"balance_cents": 100_000, "frozen_balance_cents": 0}}
    reconciler.reconcile.return_value = {ctx.market_id: {"drifted": False, "fields": []}}

    async def one_pass_sleep(_: float) -> None:
        ctx.shutdown_requested = True

    with patch("src.amm.main.asyncio.sleep", side_effect=one_pass_sleep):
        await reconcile_loop(reconciler, {ctx.market_id: ctx}, interval_seconds=0.01)

    # Startup supervision depends on reconcile_loop calling both fetch and per-market reconcile.
    reconciler.fetch_balance.assert_awaited_once()
    reconciler.reconcile.assert_awaited_once_with(
        [ctx.market_id],
        n_markets_total=1,
        balance_resp={"data": {"balance_cents": 100_000, "frozen_balance_cents": 0}},
    )
