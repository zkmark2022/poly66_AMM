"""T-SIM-01: Normal quote cycle — steady-state scenario.

Scenario: balanced inventory, Oracle disabled, no defense triggers.

Assertions (all must be numeric/structural, not just "no exception"):
  1. At least 1 YES SELL order + at least 1 NO SELL order submitted.
  2. All submitted prices are in [10, 90] cents.
  3. No BUY-direction order ever reaches execute_intents (AMM invariant).
  4. PhaseManager.update is called during the cycle.
  5. reconcile_loop calls AMMReconciler.reconcile at least once.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.amm.main import quote_cycle, reconcile_loop
from src.amm.lifecycle.reconciler import AMMReconciler
from src.amm.strategy.phase_manager import PhaseManager

from tests.simulation.helpers import (
    make_context,
    make_inventory,
    make_real_services,
)


@pytest.mark.asyncio
class TestNormalQuoteCycle:
    """T-SIM-01: normal operating conditions."""

    async def _run_cycle(self) -> tuple:
        """Helper: set up and run one quote_cycle. Returns (ctx, order_mgr)."""
        ctx = make_context(
            inventory=make_inventory(yes_volume=200, no_volume=200),
        )
        phase_mgr = PhaseManager(config=ctx.config)
        services, order_mgr = make_real_services(ctx, phase_mgr=phase_mgr)
        await quote_cycle(ctx, **services)
        return ctx, order_mgr, phase_mgr

    # ------------------------------------------------------------------
    # 1. YES + NO sell orders submitted
    # ------------------------------------------------------------------

    async def test_yes_sell_orders_submitted(self) -> None:
        """At least one SELL YES order must be submitted each cycle."""
        _, order_mgr, _ = await self._run_cycle()
        yes_sells = [i for i in order_mgr.all_intents if i.side == "YES" and i.direction == "SELL"]
        assert len(yes_sells) >= 1, (
            f"Expected >=1 YES SELL order, got {len(yes_sells)}. "
            f"All intents: {order_mgr.all_intents}"
        )

    async def test_no_sell_orders_submitted(self) -> None:
        """At least one SELL NO order must be submitted each cycle."""
        _, order_mgr, _ = await self._run_cycle()
        no_sells = [i for i in order_mgr.all_intents if i.side == "NO" and i.direction == "SELL"]
        assert len(no_sells) >= 1, (
            f"Expected >=1 NO SELL order, got {len(no_sells)}. "
            f"All intents: {order_mgr.all_intents}"
        )

    # ------------------------------------------------------------------
    # 2. Prices in [10, 90] range
    # ------------------------------------------------------------------

    async def test_all_prices_in_valid_range(self) -> None:
        """All submitted prices must be in [10, 90] cents."""
        _, order_mgr, _ = await self._run_cycle()
        intents = order_mgr.all_intents
        assert intents, "No intents were submitted"
        out_of_range = [
            i for i in intents
            if i.price_cents < 10 or i.price_cents > 90
        ]
        assert not out_of_range, (
            f"Prices outside [10,90]: {[(i.side, i.price_cents) for i in out_of_range]}"
        )

    # ------------------------------------------------------------------
    # 3. No BUY orders — AMM invariant
    # ------------------------------------------------------------------

    async def test_no_buy_direction_orders(self) -> None:
        """AMM must NEVER submit BUY-direction orders."""
        _, order_mgr, _ = await self._run_cycle()
        buy_orders = [i for i in order_mgr.all_intents if i.direction == "BUY"]
        assert buy_orders == [], (
            f"AMM invariant violated — BUY orders found: {buy_orders}"
        )

    # ------------------------------------------------------------------
    # 4. PhaseManager.update is invoked during the cycle
    # ------------------------------------------------------------------

    async def test_phase_manager_update_called(self) -> None:
        """PhaseManager.update must be called during quote_cycle."""
        ctx = make_context(
            inventory=make_inventory(yes_volume=200, no_volume=200),
        )
        phase_mgr = PhaseManager(config=ctx.config)

        with patch.object(phase_mgr, "update", wraps=phase_mgr.update) as spy:
            services, _ = make_real_services(ctx, phase_mgr=phase_mgr)
            await quote_cycle(ctx, **services)
            spy.assert_called_once()

    # ------------------------------------------------------------------
    # 5. reconcile_loop calls reconciler.reconcile at least once
    # ------------------------------------------------------------------

    async def test_reconciler_called_at_least_once(self) -> None:
        """reconcile_loop must invoke reconciler.reconcile >= 1 time."""
        ctx = make_context()
        # Make reconciler.reconcile return instantly; sleep is patched so
        # the loop terminates immediately after first iteration.
        reconciler = AsyncMock(spec=AMMReconciler)
        reconciler.reconcile.return_value = None

        contexts = {ctx.market_id: ctx}

        async def one_shot_sleep(_: float) -> None:
            # Cancel loop after first reconcile call
            ctx.shutdown_requested = True

        with patch("src.amm.main.asyncio.sleep", side_effect=one_shot_sleep):
            await reconcile_loop(reconciler, contexts, interval_seconds=0.001)

        assert reconciler.reconcile.call_count >= 1, (
            f"reconciler.reconcile not called (call_count={reconciler.reconcile.call_count})"
        )

    # ------------------------------------------------------------------
    # Edge: both YES and NO orders have correct direction field
    # ------------------------------------------------------------------

    async def test_all_submitted_orders_are_sell_direction(self) -> None:
        """Every intent that reaches execute_intents must have direction='SELL'."""
        _, order_mgr, _ = await self._run_cycle()
        non_sell = [i for i in order_mgr.all_intents if i.direction != "SELL"]
        assert non_sell == [], (
            f"Non-SELL intents found: {non_sell}"
        )
