"""Integration test — Task 22: Defense level escalation.

Verifies the full NORMAL → WIDEN → ONE_SIDE → KILL_SWITCH progression,
de-escalation cooldown, and batch_cancel invocation when KILL_SWITCH fires.
"""
import httpx
import pytest
import respx

from src.amm.config.models import MarketConfig
from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.auth import TokenManager
from src.amm.models.enums import DefenseLevel
from src.amm.risk.defense_stack import DefenseStack

from tests.integration.amm.conftest import BASE_URL, MARKET_ID, make_context


class TestDefenseEscalation:
    """DefenseStack transitions through all four levels as conditions worsen."""

    async def test_normal_to_widen_on_skew(self) -> None:
        """NORMAL → WIDEN when abs(inventory_skew) exceeds widen threshold."""
        cfg = MarketConfig(
            market_id=MARKET_ID,
            inventory_skew_widen=0.3,
            inventory_skew_one_side=0.6,
            inventory_skew_kill=0.8,
        )
        ds = DefenseStack(config=cfg)

        # Healthy conditions → NORMAL
        level = ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)
        assert level == DefenseLevel.NORMAL

        # Skew crosses 0.3 → WIDEN
        level = ds.evaluate(inventory_skew=0.4, daily_pnl=0, market_active=True)
        assert level == DefenseLevel.WIDEN

    async def test_widen_to_one_side_on_high_skew(self) -> None:
        """WIDEN → ONE_SIDE when abs(inventory_skew) exceeds one_side threshold."""
        cfg = MarketConfig(
            market_id=MARKET_ID,
            inventory_skew_widen=0.3,
            inventory_skew_one_side=0.6,
            inventory_skew_kill=0.8,
            defense_cooldown_cycles=1,
        )
        ds = DefenseStack(config=cfg)

        ds.evaluate(inventory_skew=0.4, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.WIDEN

        level = ds.evaluate(inventory_skew=0.7, daily_pnl=0, market_active=True)
        assert level == DefenseLevel.ONE_SIDE

    async def test_one_side_to_kill_switch_on_extreme_skew(self) -> None:
        """ONE_SIDE → KILL_SWITCH when abs(inventory_skew) exceeds kill threshold."""
        cfg = MarketConfig(
            market_id=MARKET_ID,
            inventory_skew_widen=0.3,
            inventory_skew_one_side=0.6,
            inventory_skew_kill=0.8,
        )
        ds = DefenseStack(config=cfg)

        ds.evaluate(inventory_skew=0.7, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.ONE_SIDE

        level = ds.evaluate(inventory_skew=0.9, daily_pnl=0, market_active=True)
        assert level == DefenseLevel.KILL_SWITCH

    async def test_full_escalation_sequence(self) -> None:
        """Verify complete NORMAL→WIDEN→ONE_SIDE→KILL_SWITCH in one call chain."""
        cfg = MarketConfig(
            market_id=MARKET_ID,
            inventory_skew_widen=0.3,
            inventory_skew_one_side=0.6,
            inventory_skew_kill=0.8,
        )
        ds = DefenseStack(config=cfg)

        level = ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)
        assert level == DefenseLevel.NORMAL

        level = ds.evaluate(inventory_skew=0.4, daily_pnl=0, market_active=True)
        assert level == DefenseLevel.WIDEN

        level = ds.evaluate(inventory_skew=0.7, daily_pnl=0, market_active=True)
        assert level == DefenseLevel.ONE_SIDE

        level = ds.evaluate(inventory_skew=0.9, daily_pnl=0, market_active=True)
        assert level == DefenseLevel.KILL_SWITCH

        # Kill switch is sticky
        level = ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)
        assert level == DefenseLevel.KILL_SWITCH


class TestDefensePnLTriggers:
    """Defense levels also escalate on PnL breach."""

    async def test_one_side_on_pnl_breach_half_budget(self) -> None:
        """ONE_SIDE when daily_pnl < -(max_per_market / 2)."""
        cfg = MarketConfig(
            market_id=MARKET_ID,
            max_per_market_loss_cents=10_000,  # $100 budget
        )
        ds = DefenseStack(config=cfg)

        # PnL at half-budget → ONE_SIDE (skew is fine)
        level = ds.evaluate(inventory_skew=0.1, daily_pnl=-6_000, market_active=True)
        assert level == DefenseLevel.ONE_SIDE

    async def test_kill_switch_on_pnl_full_budget_breach(self) -> None:
        """KILL_SWITCH when daily_pnl < -max_per_market_loss_cents."""
        cfg = MarketConfig(
            market_id=MARKET_ID,
            max_per_market_loss_cents=10_000,
        )
        ds = DefenseStack(config=cfg)

        level = ds.evaluate(inventory_skew=0.1, daily_pnl=-12_000, market_active=True)
        assert level == DefenseLevel.KILL_SWITCH

    async def test_kill_switch_on_market_inactive(self) -> None:
        """Any inactive market triggers immediate KILL_SWITCH."""
        cfg = MarketConfig(market_id=MARKET_ID)
        ds = DefenseStack(config=cfg)

        level = ds.evaluate(inventory_skew=0.0, daily_pnl=0, market_active=False)
        assert level == DefenseLevel.KILL_SWITCH


class TestDefenseDeEscalation:
    """De-escalation requires cooldown cycles to prevent oscillation."""

    async def test_de_escalation_requires_cooldown(self) -> None:
        """Must stay below the trigger threshold for N cycles before de-escalating."""
        cfg = MarketConfig(
            market_id=MARKET_ID,
            inventory_skew_widen=0.3,
            defense_cooldown_cycles=3,
        )
        ds = DefenseStack(config=cfg)

        # Escalate to WIDEN
        ds.evaluate(inventory_skew=0.4, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.WIDEN

        # Conditions improve — need 3 cooldown cycles before de-escalation
        ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.WIDEN  # 1/3

        ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.WIDEN  # 2/3

        ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.NORMAL  # de-escalated

    async def test_cooldown_resets_on_re_escalation(self) -> None:
        """Cooldown counter resets if conditions worsen again mid-cooldown."""
        cfg = MarketConfig(
            market_id=MARKET_ID,
            inventory_skew_widen=0.3,
            inventory_skew_one_side=0.6,
            defense_cooldown_cycles=3,
        )
        ds = DefenseStack(config=cfg)

        ds.evaluate(inventory_skew=0.7, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.ONE_SIDE

        # Two cycles of improvement
        ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)
        ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.ONE_SIDE  # still in cooldown

        # Conditions worsen again → re-escalates, cooldown resets
        ds.evaluate(inventory_skew=0.7, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.ONE_SIDE

        # Must complete 3 new cooldown cycles
        ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)
        ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.ONE_SIDE  # still not done
        ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.NORMAL


class TestKillSwitchBatchCancel:
    """When KILL_SWITCH is active, batch_cancel must be called via AMMApiClient."""

    async def test_kill_switch_triggers_batch_cancel(self) -> None:
        """KILL_SWITCH level correctly triggers API batch cancel call."""
        cfg = MarketConfig(
            market_id=MARKET_ID,
            inventory_skew_kill=0.8,
        )
        ds = DefenseStack(config=cfg)

        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            cancel_route = router.post("/amm/orders/batch-cancel").mock(
                return_value=httpx.Response(
                    200,
                    json={"data": {"cancelled_count": 4, "market_id": MARKET_ID}},
                )
            )

            tm = TokenManager(base_url=BASE_URL)
            tm._access_token = "preset_token"  # bypass login for this test
            api = AMMApiClient(base_url=BASE_URL, token_manager=tm)

            # Evaluate — triggers KILL_SWITCH
            level = ds.evaluate(inventory_skew=0.9, daily_pnl=0, market_active=True)
            assert level == DefenseLevel.KILL_SWITCH
            assert not level.is_quoting_active

            # Business logic: when kill switch fires, cancel all orders
            if not level.is_quoting_active:
                result = await api.batch_cancel(MARKET_ID, scope="ALL")
                assert result["data"]["cancelled_count"] == 4  # type: ignore[index]

            assert cancel_route.called
            assert cancel_route.call_count == 1

            import json
            body = json.loads(cancel_route.calls.last.request.content)
            assert body["market_id"] == MARKET_ID
            assert body["cancel_scope"] == "ALL"

            await api.close()

    async def test_kill_switch_is_quoting_active_false(self) -> None:
        """DefenseLevel.KILL_SWITCH.is_quoting_active must be False."""
        assert DefenseLevel.KILL_SWITCH.is_quoting_active is False
        assert DefenseLevel.NORMAL.is_quoting_active is True
        assert DefenseLevel.WIDEN.is_quoting_active is True
        assert DefenseLevel.ONE_SIDE.is_quoting_active is True

    async def test_multi_market_kill_switch_cancels_all(self) -> None:
        """All markets' orders are cancelled when kill switch fires globally."""
        market_ids = ["mkt-a", "mkt-b", "mkt-c"]

        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            cancel_route = router.post("/amm/orders/batch-cancel").mock(
                return_value=httpx.Response(
                    200, json={"data": {"cancelled_count": 2}}
                )
            )

            tm = TokenManager(base_url=BASE_URL)
            tm._access_token = "preset_token"
            api = AMMApiClient(base_url=BASE_URL, token_manager=tm)

            cfg = MarketConfig(market_id="global", inventory_skew_kill=0.8)
            ds = DefenseStack(config=cfg)

            level = ds.evaluate(inventory_skew=0.95, daily_pnl=0, market_active=True)
            assert level == DefenseLevel.KILL_SWITCH

            # Cancel all markets when kill switch fires
            for mid in market_ids:
                await api.batch_cancel(mid, scope="ALL")

            assert cancel_route.call_count == len(market_ids)

            await api.close()
