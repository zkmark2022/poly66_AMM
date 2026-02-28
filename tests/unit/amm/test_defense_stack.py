"""Test three-line defense escalation. See AMM design v7.1 §10."""
import pytest
from src.amm.risk.defense_stack import DefenseStack
from src.amm.models.enums import DefenseLevel
from src.amm.config.models import MarketConfig


class TestDefenseStack:
    def test_normal_state(self) -> None:
        ds = DefenseStack(MarketConfig(market_id="mkt-1"))
        level = ds.evaluate(inventory_skew=0.1, daily_pnl=-100, market_active=True)
        assert level == DefenseLevel.NORMAL

    def test_widen_on_skew(self) -> None:
        ds = DefenseStack(MarketConfig(market_id="mkt-1", inventory_skew_widen=0.3))
        level = ds.evaluate(inventory_skew=0.4, daily_pnl=-100, market_active=True)
        assert level == DefenseLevel.WIDEN

    def test_one_side_on_high_skew(self) -> None:
        ds = DefenseStack(MarketConfig(market_id="mkt-1", inventory_skew_one_side=0.6))
        level = ds.evaluate(inventory_skew=0.7, daily_pnl=-100, market_active=True)
        assert level == DefenseLevel.ONE_SIDE

    def test_kill_switch_on_extreme_skew(self) -> None:
        ds = DefenseStack(MarketConfig(market_id="mkt-1", inventory_skew_kill=0.8))
        level = ds.evaluate(inventory_skew=0.9, daily_pnl=-100, market_active=True)
        assert level == DefenseLevel.KILL_SWITCH

    def test_kill_switch_on_budget_breach(self) -> None:
        ds = DefenseStack(MarketConfig(market_id="mkt-1", max_per_market_loss_cents=10000))
        level = ds.evaluate(inventory_skew=0.1, daily_pnl=-15000, market_active=True)
        assert level == DefenseLevel.KILL_SWITCH

    def test_kill_switch_on_market_inactive(self) -> None:
        ds = DefenseStack(MarketConfig(market_id="mkt-1"))
        level = ds.evaluate(inventory_skew=0.0, daily_pnl=0, market_active=False)
        assert level == DefenseLevel.KILL_SWITCH

    def test_one_side_on_half_budget_breach(self) -> None:
        """ONE_SIDE triggers when pnl <= -(budget / 2)."""
        ds = DefenseStack(MarketConfig(market_id="mkt-1", max_per_market_loss_cents=10000))
        level = ds.evaluate(inventory_skew=0.1, daily_pnl=-5000, market_active=True)
        assert level == DefenseLevel.ONE_SIDE

    def test_de_escalation_requires_cooldown(self) -> None:
        """Must stay at lower level for N cycles before de-escalating."""
        ds = DefenseStack(MarketConfig(market_id="mkt-1", defense_cooldown_cycles=3))
        # Escalate to WIDEN
        ds.evaluate(inventory_skew=0.4, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.WIDEN

        # Conditions improve, but need 3 cycles of cooldown
        ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.WIDEN  # still WIDEN (1/3)
        ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.WIDEN  # (2/3)
        ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.NORMAL  # de-escalated after 3 cycles

    def test_escalation_is_immediate(self) -> None:
        """Escalation bypasses cooldown — instant."""
        ds = DefenseStack(MarketConfig(market_id="mkt-1", defense_cooldown_cycles=10))
        ds.evaluate(inventory_skew=0.4, daily_pnl=0, market_active=True)
        assert ds.current_level == DefenseLevel.WIDEN
        # Immediately escalate further
        level = ds.evaluate(inventory_skew=0.7, daily_pnl=0, market_active=True)
        assert level == DefenseLevel.ONE_SIDE

    def test_cooldown_resets_on_re_escalation(self) -> None:
        """Cooldown counter resets if conditions worsen again."""
        ds = DefenseStack(MarketConfig(market_id="mkt-1", defense_cooldown_cycles=3))
        ds.evaluate(inventory_skew=0.4, daily_pnl=0, market_active=True)  # WIDEN
        ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)  # cooldown 1
        ds.evaluate(inventory_skew=0.4, daily_pnl=0, market_active=True)  # re-escalate
        # Counter should have reset
        ds.evaluate(inventory_skew=0.1, daily_pnl=0, market_active=True)  # cooldown 1 again
        assert ds.current_level == DefenseLevel.WIDEN  # NOT NORMAL yet

    def test_negative_skew_also_triggers(self) -> None:
        """Negative skew (NO-heavy) also escalates."""
        ds = DefenseStack(MarketConfig(market_id="mkt-1", inventory_skew_widen=0.3))
        level = ds.evaluate(inventory_skew=-0.4, daily_pnl=0, market_active=True)
        assert level == DefenseLevel.WIDEN
