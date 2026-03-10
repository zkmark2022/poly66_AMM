"""Layer 4 Round 2 — BTC market risk-control validation.

Scenarios:
  C01  WIDEN         — inventory skew triggers spread widening
  C01b ONE_SIDE      — extreme skew triggers single-side quoting
  C02  KILL_SWITCH   — extreme skew/loss halts all quoting
  D01  Auto Reinvest — cash surplus triggers automatic mint
  E01  Restart       — state preserved across quote cycle restart
"""
from __future__ import annotations

import pytest

from src.amm.main import quote_cycle
from src.amm.models.enums import DefenseLevel, Phase
from src.amm.lifecycle.reinvest import maybe_auto_reinvest

from tests.simulation.conftest import (
    compute_effective_spread,
    make_config,
    make_context,
    make_inventory,
    make_real_services,
)

# Use config that produces reasonable spreads (not pinned to 1/99 ceiling).
# gamma_tier MATURE (1.5) + kappa=10.0 → ~19¢ spread at mid=50.
_TIGHT_CFG = dict(
    gamma_tier="MATURE",
    kappa=10.0,
    remaining_hours=24.0,
    anchor_price_cents=50,
    spread_min_cents=2,
    spread_max_cents=50,
    gradient_levels=3,
    gradient_price_step_cents=1,
    gradient_quantity_decay=0.5,
    initial_mint_quantity=600,
    defense_cooldown_cycles=3,
)


def _tight_config(**overrides):
    merged = {**_TIGHT_CFG, **overrides}
    return make_config(**merged)


# ---------------------------------------------------------------------------
# C01 — WIDEN
# ---------------------------------------------------------------------------


class TestC01Widen:
    """Inventory skew >= 0.3 triggers WIDEN; spread must visibly widen."""

    @pytest.mark.asyncio
    async def test_widen_defense_level_activates_at_threshold(self) -> None:
        """Skew exactly at threshold triggers WIDEN."""
        # skew = (260 - 140) / 400 = 0.30
        config = _tight_config(inventory_skew_widen=0.3)
        ctx = make_context(
            inventory=make_inventory(yes_volume=260, no_volume=140),
            config=config,
        )
        services, _ = make_real_services(ctx)
        await quote_cycle(ctx, **services)

        assert ctx.defense_level == DefenseLevel.WIDEN

    @pytest.mark.asyncio
    async def test_widen_spread_exceeds_normal(self) -> None:
        """WIDEN state produces wider spread than NORMAL with same mid-price."""
        config = _tight_config(inventory_skew_widen=0.3)

        # Normal: balanced inventory
        normal_ctx = make_context(
            inventory=make_inventory(yes_volume=200, no_volume=200),
            config=config,
        )
        normal_services, normal_mgr = make_real_services(normal_ctx)
        await quote_cycle(normal_ctx, **normal_services)
        normal_spread = compute_effective_spread(normal_mgr.all_intents)

        # WIDEN: skew = (260-140)/400 = 0.30
        widen_ctx = make_context(
            inventory=make_inventory(yes_volume=260, no_volume=140),
            config=config,
        )
        widen_services, widen_mgr = make_real_services(widen_ctx)
        await quote_cycle(widen_ctx, **widen_services)
        widen_spread = compute_effective_spread(widen_mgr.all_intents)

        assert normal_ctx.defense_level == DefenseLevel.NORMAL
        assert widen_ctx.defense_level == DefenseLevel.WIDEN
        assert normal_spread > 0, "Normal spread must be positive"
        assert widen_spread > normal_spread, (
            f"WIDEN spread ({widen_spread}) must exceed NORMAL ({normal_spread})"
        )

    @pytest.mark.asyncio
    async def test_widen_still_quotes_both_sides(self) -> None:
        """WIDEN widens spread but still quotes on both YES and NO sides."""
        config = _tight_config(inventory_skew_widen=0.3)
        ctx = make_context(
            inventory=make_inventory(yes_volume=260, no_volume=140),
            config=config,
        )
        services, mgr = make_real_services(ctx)
        await quote_cycle(ctx, **services)

        yes_intents = [i for i in mgr.all_intents if i.side == "YES"]
        no_intents = [i for i in mgr.all_intents if i.side == "NO"]

        assert ctx.defense_level == DefenseLevel.WIDEN
        assert len(yes_intents) > 0, "WIDEN must still quote YES side"
        assert len(no_intents) > 0, "WIDEN must still quote NO side"

    @pytest.mark.asyncio
    async def test_widen_state_reported_in_context(self) -> None:
        """Context defense_level is observable after WIDEN activation."""
        config = _tight_config(inventory_skew_widen=0.3)
        ctx = make_context(
            inventory=make_inventory(yes_volume=280, no_volume=120),
            config=config,
        )
        services, _ = make_real_services(ctx)
        await quote_cycle(ctx, **services)

        assert ctx.defense_level == DefenseLevel.WIDEN
        assert ctx.defense_level.is_quoting_active is True


# ---------------------------------------------------------------------------
# C01b — ONE_SIDE
# ---------------------------------------------------------------------------


class TestC01bOneSide:
    """Inventory skew >= 0.6 triggers ONE_SIDE; only heavy-side quotes survive."""

    @pytest.mark.asyncio
    async def test_one_side_activates_at_threshold(self) -> None:
        """Skew >= 0.6 triggers ONE_SIDE defense level."""
        # skew = (320 - 80) / 400 = 0.60
        config = _tight_config(inventory_skew_one_side=0.6)
        ctx = make_context(
            inventory=make_inventory(yes_volume=320, no_volume=80),
            config=config,
        )
        services, _ = make_real_services(ctx)
        await quote_cycle(ctx, **services)

        assert ctx.defense_level == DefenseLevel.ONE_SIDE

    @pytest.mark.asyncio
    async def test_one_side_suppresses_no_when_long_yes(self) -> None:
        """When long YES (skew > 0), SELL NO suppressed; only SELL YES remains."""
        config = _tight_config(inventory_skew_one_side=0.6)
        # skew = (320 - 80) / 400 = 0.60 → long YES
        ctx = make_context(
            inventory=make_inventory(yes_volume=320, no_volume=80),
            config=config,
        )
        services, mgr = make_real_services(ctx)
        await quote_cycle(ctx, **services)

        yes_intents = [i for i in mgr.all_intents if i.side == "YES"]
        no_intents = [i for i in mgr.all_intents if i.side == "NO"]

        assert ctx.defense_level == DefenseLevel.ONE_SIDE
        assert len(yes_intents) > 0, "Must still quote SELL YES to reduce YES inventory"
        assert len(no_intents) == 0, "SELL NO must be suppressed when long YES"

    @pytest.mark.asyncio
    async def test_one_side_suppresses_yes_when_long_no(self) -> None:
        """When long NO (skew < 0), SELL YES suppressed; only SELL NO remains."""
        config = _tight_config(inventory_skew_one_side=0.6)
        # skew = (80 - 320) / 400 = -0.60 → long NO
        ctx = make_context(
            inventory=make_inventory(yes_volume=80, no_volume=320),
            config=config,
        )
        services, mgr = make_real_services(ctx)
        await quote_cycle(ctx, **services)

        yes_intents = [i for i in mgr.all_intents if i.side == "YES"]
        no_intents = [i for i in mgr.all_intents if i.side == "NO"]

        assert ctx.defense_level == DefenseLevel.ONE_SIDE
        assert len(no_intents) > 0, "Must still quote SELL NO to reduce NO inventory"
        assert len(yes_intents) == 0, "SELL YES must be suppressed when long NO"

    @pytest.mark.asyncio
    async def test_one_side_is_quoting_active(self) -> None:
        """ONE_SIDE still allows quoting (unlike KILL_SWITCH)."""
        config = _tight_config(inventory_skew_one_side=0.6)
        ctx = make_context(
            inventory=make_inventory(yes_volume=320, no_volume=80),
            config=config,
        )
        services, _ = make_real_services(ctx)
        await quote_cycle(ctx, **services)

        assert ctx.defense_level == DefenseLevel.ONE_SIDE
        assert ctx.defense_level.is_quoting_active is True


# ---------------------------------------------------------------------------
# C02 — KILL_SWITCH
# ---------------------------------------------------------------------------


class TestC02KillSwitch:
    """Extreme skew or PnL loss triggers KILL_SWITCH; all quoting halted."""

    @pytest.mark.asyncio
    async def test_kill_switch_on_extreme_skew(self) -> None:
        """Skew >= 0.8 triggers KILL_SWITCH."""
        config = _tight_config(inventory_skew_kill=0.8)
        # skew = (360 - 40) / 400 = 0.80
        ctx = make_context(
            inventory=make_inventory(yes_volume=360, no_volume=40),
            config=config,
        )
        services, mgr = make_real_services(ctx)
        await quote_cycle(ctx, **services)

        assert ctx.defense_level == DefenseLevel.KILL_SWITCH
        assert ctx.defense_level.is_quoting_active is False
        assert len(mgr.all_intents) == 0, "No intents should be placed under KILL"
        assert len(mgr.cancelled_markets) == 1, "cancel_all must be called"

    @pytest.mark.asyncio
    async def test_kill_switch_on_pnl_loss(self) -> None:
        """PnL loss exceeding max_per_market_loss triggers KILL_SWITCH."""
        config = _tight_config(max_per_market_loss_cents=5000)
        ctx = make_context(
            inventory=make_inventory(yes_volume=200, no_volume=200),
            config=config,
        )
        # Set initial_inventory_value so session PnL = current - initial < -5000
        ctx.initial_inventory_value_cents = (
            ctx.inventory.total_value_cents(50) + 5001
        )
        services, mgr = make_real_services(ctx)
        await quote_cycle(ctx, **services)

        assert ctx.defense_level == DefenseLevel.KILL_SWITCH
        assert len(mgr.all_intents) == 0
        assert len(mgr.cancelled_markets) == 1

    @pytest.mark.asyncio
    async def test_kill_switch_prevents_subsequent_quotes(self) -> None:
        """After KILL, subsequent cycles still produce no intents."""
        config = _tight_config(inventory_skew_kill=0.8)
        ctx = make_context(
            inventory=make_inventory(yes_volume=360, no_volume=40),
            config=config,
        )
        services, mgr = make_real_services(ctx)

        for _ in range(3):
            await quote_cycle(ctx, **services)

        assert ctx.defense_level == DefenseLevel.KILL_SWITCH
        assert len(mgr.all_intents) == 0
        assert len(mgr.cancelled_markets) == 3, "cancel_all called each cycle"

    @pytest.mark.asyncio
    async def test_kill_on_inactive_market(self) -> None:
        """Inactive market triggers KILL_SWITCH regardless of inventory."""
        import time as _time

        config = _tight_config()
        ctx = make_context(
            inventory=make_inventory(yes_volume=200, no_volume=200),
            config=config,
            market_active=False,
        )
        # Set recent check time so cached market_active=False is used (skip API fetch)
        ctx.market_status_checked_at = _time.monotonic()
        services, mgr = make_real_services(ctx)
        await quote_cycle(ctx, **services)

        assert ctx.defense_level == DefenseLevel.KILL_SWITCH
        assert len(mgr.all_intents) == 0


# ---------------------------------------------------------------------------
# D01 — Auto Reinvest / Mint
# ---------------------------------------------------------------------------


class TestD01AutoReinvest:
    """Cash surplus triggers automatic mint of YES/NO pairs."""

    @pytest.mark.asyncio
    async def test_auto_reinvest_mints_when_surplus(self) -> None:
        """Cash exceeding threshold triggers mint; inventory increases."""
        from unittest.mock import AsyncMock
        from src.amm.connector.api_client import AMMApiClient

        config = _tight_config()
        config_obj = config
        # cash = 100_000 > threshold 50_000 → surplus = 50_000 → quantity = 500
        ctx = make_context(
            inventory=make_inventory(
                yes_volume=200, no_volume=200, cash_cents=100_000,
            ),
            config=config_obj,
        )
        ctx.phase = Phase.STABILIZATION  # reinvest only in STABILIZATION

        api = AsyncMock(spec=AMMApiClient)
        api.mint.return_value = {"data": {"status": "minted"}}

        before_yes = ctx.inventory.yes_volume
        before_no = ctx.inventory.no_volume
        before_cash = ctx.inventory.cash_cents

        minted = await maybe_auto_reinvest(ctx, api)

        assert minted == 500
        assert ctx.inventory.yes_volume == before_yes + 500
        assert ctx.inventory.no_volume == before_no + 500
        assert ctx.inventory.cash_cents == before_cash - 500 * 100
        api.mint.assert_called_once()

    @pytest.mark.asyncio
    async def test_auto_reinvest_skipped_when_below_threshold(self) -> None:
        """No mint when cash is below threshold."""
        from unittest.mock import AsyncMock
        from src.amm.connector.api_client import AMMApiClient

        config = _tight_config()
        ctx = make_context(
            inventory=make_inventory(
                yes_volume=200, no_volume=200, cash_cents=40_000,
            ),
            config=config,
        )
        ctx.phase = Phase.STABILIZATION

        api = AsyncMock(spec=AMMApiClient)
        minted = await maybe_auto_reinvest(ctx, api)

        assert minted == 0
        api.mint.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_reinvest_skipped_when_disabled(self) -> None:
        """Disabled auto_reinvest config prevents mint."""
        from unittest.mock import AsyncMock
        from src.amm.connector.api_client import AMMApiClient

        config = _tight_config(auto_reinvest_enabled=False)
        ctx = make_context(
            inventory=make_inventory(
                yes_volume=200, no_volume=200, cash_cents=100_000,
            ),
            config=config,
        )
        ctx.phase = Phase.STABILIZATION

        api = AsyncMock(spec=AMMApiClient)
        minted = await maybe_auto_reinvest(ctx, api)

        assert minted == 0
        api.mint.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_reinvest_during_quote_cycle(self) -> None:
        """In STABILIZATION phase, quote_cycle invokes reinvest when cash surplus exists."""
        from unittest.mock import AsyncMock as _AsyncMock

        config = _tight_config()
        ctx = make_context(
            inventory=make_inventory(
                yes_volume=200, no_volume=200, cash_cents=100_000,
            ),
            config=config,
        )
        ctx.phase = Phase.STABILIZATION

        services, mgr = make_real_services(ctx)
        # Mock the API mint call
        services["api"].mint = _AsyncMock(return_value={"data": {"status": "minted"}})

        await quote_cycle(ctx, **services)

        services["api"].mint.assert_called_once()
        # After reinvest, inventory should have grown
        assert ctx.inventory.yes_volume > 200
        assert ctx.inventory.no_volume > 200

    @pytest.mark.asyncio
    async def test_cash_depleted_drops_buy_side(self) -> None:
        """When cash is 0, SELL NO (synthetic buy) intents are dropped."""
        config = _tight_config()
        ctx = make_context(
            inventory=make_inventory(
                yes_volume=200, no_volume=200, cash_cents=0,
            ),
            config=config,
        )
        services, mgr = make_real_services(ctx)
        await quote_cycle(ctx, **services)

        no_intents = [i for i in mgr.all_intents if i.side == "NO"]
        yes_intents = [i for i in mgr.all_intents if i.side == "YES"]

        assert len(no_intents) == 0, "SELL NO (buy-side) dropped when cash depleted"
        assert len(yes_intents) > 0, "SELL YES (ask-side) should still exist"


# ---------------------------------------------------------------------------
# E01 — Restart / Recovery
# ---------------------------------------------------------------------------


class TestE01RestartRecovery:
    """AMM state preserved across simulated restart (re-running quote cycle)."""

    @pytest.mark.asyncio
    async def test_inventory_preserved_across_restart(self) -> None:
        """After trades change inventory, a fresh quote_cycle with same context
        reflects the modified inventory, not the original."""
        config = _tight_config()
        # Start with balanced
        inv = make_inventory(yes_volume=200, no_volume=200)
        ctx = make_context(inventory=inv, config=config)
        services, mgr1 = make_real_services(ctx)

        await quote_cycle(ctx, **services)
        intents_before = list(mgr1.all_intents)
        assert len(intents_before) > 0

        # Simulate trades changing inventory (external trade consumed YES)
        ctx.inventory.yes_volume = 150
        ctx.inventory.no_volume = 200
        services["inventory_cache"].get.return_value = ctx.inventory

        # "Restart" = fresh services but same ctx
        services2, mgr2 = make_real_services(ctx)
        services2["inventory_cache"].get.return_value = ctx.inventory
        await quote_cycle(ctx, **services2)

        intents_after = list(mgr2.all_intents)
        assert len(intents_after) > 0

        # Prices should differ because inventory skew changed
        prices_before = sorted(i.price_cents for i in intents_before)
        prices_after = sorted(i.price_cents for i in intents_after)
        assert prices_before != prices_after, (
            "Restart with changed inventory must produce different quotes"
        )

    @pytest.mark.asyncio
    async def test_defense_level_preserved_across_restart(self) -> None:
        """Defense level state persists in the context across cycles."""
        config = _tight_config(
            inventory_skew_widen=0.3,
            defense_cooldown_cycles=5,
        )
        # Start with WIDEN-triggering skew
        ctx = make_context(
            inventory=make_inventory(yes_volume=260, no_volume=140),
            config=config,
        )
        services, _ = make_real_services(ctx)
        await quote_cycle(ctx, **services)
        assert ctx.defense_level == DefenseLevel.WIDEN

        # Now rebalance — but cooldown hasn't elapsed
        ctx.inventory = make_inventory(yes_volume=200, no_volume=200)
        services["inventory_cache"].get.return_value = ctx.inventory

        await quote_cycle(ctx, **services)
        # Still WIDEN due to cooldown
        assert ctx.defense_level == DefenseLevel.WIDEN

    @pytest.mark.asyncio
    async def test_trade_count_persists(self) -> None:
        """Trade count accumulates across quote cycles (not reset)."""
        config = _tight_config()
        ctx = make_context(config=config)
        services, _ = make_real_services(ctx)

        # Simulate 3 trades in first poll
        services["poller"].poll.return_value = [
            {"trade_id": "t1"}, {"trade_id": "t2"}, {"trade_id": "t3"},
        ]
        await quote_cycle(ctx, **services)
        assert ctx.trade_count == 3

        # 2 more trades
        services["poller"].poll.return_value = [
            {"trade_id": "t4"}, {"trade_id": "t5"},
        ]
        await quote_cycle(ctx, **services)
        assert ctx.trade_count == 5

    @pytest.mark.asyncio
    async def test_last_requote_updated_after_cycle(self) -> None:
        """last_requote_at is updated after successful quote cycle.

        NOTE: Requires feat/layer4-e2e merge (last_requote_at attribute).
        """
        config = _tight_config()
        ctx = make_context(config=config)
        if not hasattr(ctx, "last_requote_at"):
            pytest.skip("last_requote_at not available on this branch (needs feat/layer4-e2e merge)")

        initial_requote = ctx.last_requote_at
        services, _ = make_real_services(ctx)
        await quote_cycle(ctx, **services)

        assert ctx.last_requote_at > initial_requote


# ---------------------------------------------------------------------------
# Observability — AMM State endpoint fields
# ---------------------------------------------------------------------------


class TestObservability:
    """Verify the /state endpoint fields are correctly populated (BUG-007 fix).

    NOTE: These tests require the feat/layer4-e2e branch merge which adds
    _build_market_state to health.py. They will skip gracefully if not available.
    """

    @pytest.mark.asyncio
    async def test_state_reflects_defense_level(self) -> None:
        """Context defense_level matches after each quote cycle."""
        try:
            from src.amm.lifecycle.health import _build_market_state
        except ImportError:
            pytest.skip("_build_market_state not available (needs feat/layer4-e2e merge)")
        import time

        config = _tight_config(inventory_skew_widen=0.3)
        ctx = make_context(
            inventory=make_inventory(yes_volume=260, no_volume=140),
            config=config,
        )
        services, _ = make_real_services(ctx)
        await quote_cycle(ctx, **services)

        state = _build_market_state(ctx, time.monotonic())

        assert state["defense_level"] == "WIDEN"
        assert state["kill_switch"] is False
        assert -1.0 <= state["inventory_skew"] <= 1.0
        assert state["phase"] in ("EXPLORATION", "STABILIZATION")
        assert state["session_pnl_cents"] is not None
        assert isinstance(state["trade_count"], int)

    @pytest.mark.asyncio
    async def test_state_shows_kill_switch_true(self) -> None:
        """When KILL_SWITCH active, state reports kill_switch=True."""
        try:
            from src.amm.lifecycle.health import _build_market_state
        except ImportError:
            pytest.skip("_build_market_state not available (needs feat/layer4-e2e merge)")
        import time

        config = _tight_config(inventory_skew_kill=0.8)
        ctx = make_context(
            inventory=make_inventory(yes_volume=360, no_volume=40),
            config=config,
        )
        services, _ = make_real_services(ctx)
        await quote_cycle(ctx, **services)

        state = _build_market_state(ctx, time.monotonic())

        assert state["defense_level"] == "KILL_SWITCH"
        assert state["kill_switch"] is True
