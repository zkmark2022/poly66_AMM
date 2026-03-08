"""T-SIM-03: WIDEN defense effectiveness — steady-state scenario.

Scenario: inventory is heavily tilted toward YES (yes_volume >> no_volume),
crossing the inventory_skew_widen threshold. The defense layer must respond
by widening the effective spread by at least the configured widen_factor.

Test steps:
  1. Skewed inventory (skew ≥ inventory_skew_widen) → run quote_cycle
     → record spread_widen.
  2. Normal inventory (skew ≈ 0) → run quote_cycle → record spread_normal.
  3. Assert spread_widen >= spread_normal * widen_factor (numeric).
  4. Assert ctx.defense_level == DefenseLevel.WIDEN for skewed case.

Key mock strategy:
  - Real DefenseStack + OrderSanitizer so defense logic runs authentically.
  - Mock api/poller/order_mgr; CapturingOrderManager records every intent.
  - Separate DefenseStack instances per scenario to avoid state leakage.
"""
from __future__ import annotations

import pytest

from src.amm.main import quote_cycle
from src.amm.models.enums import DefenseLevel

from tests.simulation.conftest import (
    CapturingOrderManager,
    compute_effective_spread,
    make_config,
    make_context,
    make_inventory,
    make_real_services,
)


@pytest.mark.asyncio
class TestWidenDefense:
    """T-SIM-03: WIDEN spread widening is numerically verified."""

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _skewed_ctx(self):
        """Context with inventory skew = 0.43 (> inventory_skew_widen=0.30)."""
        # skew = (600 - 200) / (600 + 200) = 400/800 = 0.50
        return make_context(
            inventory=make_inventory(yes_volume=600, no_volume=200),
            config=make_config(
                inventory_skew_widen=0.30,
                inventory_skew_one_side=0.70,   # keep WIDEN, not ONE_SIDE
                inventory_skew_kill=0.90,
                widen_factor=1.5,
                spread_max_cents=50,  # high cap so widening is not truncated
            ),
        )

    def _normal_ctx(self):
        """Context with balanced inventory (skew = 0.0)."""
        return make_context(
            inventory=make_inventory(yes_volume=200, no_volume=200),
            config=make_config(
                inventory_skew_widen=0.30,
                inventory_skew_one_side=0.70,
                inventory_skew_kill=0.90,
                widen_factor=1.5,
                spread_max_cents=50,
            ),
        )

    async def _run_and_capture(self, ctx) -> list:
        """Run one quote cycle and return captured intents."""
        services, order_mgr = make_real_services(ctx)
        await quote_cycle(ctx, **services)
        return order_mgr.all_intents

    # -----------------------------------------------------------------------
    # 1. DefenseLevel is WIDEN for skewed inventory
    # -----------------------------------------------------------------------

    async def test_defense_level_is_widen_for_skewed_inventory(self) -> None:
        """DefenseStack must escalate to WIDEN when inventory is skewed."""
        ctx = self._skewed_ctx()
        inventory_skew = ctx.inventory.inventory_skew
        assert inventory_skew >= 0.30, f"Test setup error: skew={inventory_skew} not >= 0.30"

        await self._run_and_capture(ctx)

        assert ctx.defense_level == DefenseLevel.WIDEN, (
            f"Expected WIDEN, got {ctx.defense_level} (skew={inventory_skew:.2f})"
        )

    # -----------------------------------------------------------------------
    # 2. Defense stays NORMAL for balanced inventory
    # -----------------------------------------------------------------------

    async def test_defense_level_normal_for_balanced_inventory(self) -> None:
        """DefenseStack must stay NORMAL when inventory is balanced."""
        ctx = self._normal_ctx()
        await self._run_and_capture(ctx)
        assert ctx.defense_level == DefenseLevel.NORMAL, (
            f"Expected NORMAL, got {ctx.defense_level}"
        )

    # -----------------------------------------------------------------------
    # 3. WIDEN spread >= normal spread * widen_factor (numeric assertion)
    # -----------------------------------------------------------------------

    async def test_widen_spread_is_larger_than_normal_spread(self) -> None:
        """Effective spread after WIDEN must be >= normal spread * widen_factor."""
        widen_factor = 1.5

        ctx_widen = self._skewed_ctx()
        intents_widen = await self._run_and_capture(ctx_widen)
        assert ctx_widen.defense_level == DefenseLevel.WIDEN, (
            "Pre-condition: defense must be WIDEN for skewed inventory"
        )

        ctx_normal = self._normal_ctx()
        intents_normal = await self._run_and_capture(ctx_normal)

        spread_widen = compute_effective_spread(intents_widen)
        spread_normal = compute_effective_spread(intents_normal)

        assert spread_normal > 0, (
            f"Test setup error: normal spread = {spread_normal} (expected > 0). "
            f"Intents: {intents_normal}"
        )
        assert spread_widen > 0, (
            f"WIDEN spread = {spread_widen} (expected > 0). "
            f"Intents: {intents_widen}"
        )
        assert spread_widen >= spread_normal * widen_factor, (
            f"WIDEN spread ({spread_widen}c) must be >= "
            f"normal spread ({spread_normal}c) * {widen_factor} = "
            f"{spread_normal * widen_factor:.1f}c"
        )

    # -----------------------------------------------------------------------
    # 4. Both scenarios still produce valid orders (prices in range)
    # -----------------------------------------------------------------------

    async def test_widen_prices_remain_valid(self) -> None:
        """Even with WIDEN, all submitted prices must be in [1, 99]."""
        ctx = self._skewed_ctx()
        intents = await self._run_and_capture(ctx)
        out_of_range = [i for i in intents if i.price_cents < 1 or i.price_cents > 99]
        assert not out_of_range, (
            f"WIDEN produced out-of-range prices: "
            f"{[(i.side, i.price_cents) for i in out_of_range]}"
        )

    # -----------------------------------------------------------------------
    # 5. WIDEN does NOT trigger KILL_SWITCH (quoting remains active)
    # -----------------------------------------------------------------------

    async def test_widen_defense_does_not_halt_quoting(self) -> None:
        """WIDEN defense must not block order submission (not a KILL_SWITCH)."""
        ctx = self._skewed_ctx()
        intents = await self._run_and_capture(ctx)
        assert intents, (
            "No orders submitted under WIDEN — quoting should remain active"
        )
