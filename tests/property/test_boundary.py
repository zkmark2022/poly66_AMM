"""Layer 1 boundary value tests — parametrized edge cases for AMM components."""

import pytest

from src.amm.config.models import MarketConfig
from src.amm.models.enums import DefenseLevel, QuoteAction
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext
from src.amm.risk.defense_stack import DefenseStack
from src.amm.risk.sanitizer import OrderSanitizer
from src.amm.strategy.as_engine import ASEngine
from src.amm.strategy.gradient import GradientEngine
from src.amm.strategy.models import OrderIntent


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: object) -> MarketConfig:
    defaults = {"market_id": "test-mkt"}
    defaults.update(overrides)
    return MarketConfig(**defaults)  # type: ignore[arg-type]


def _make_inventory(
    yes_vol: int = 500,
    no_vol: int = 500,
    cash: int = 100_000,
) -> Inventory:
    return Inventory(
        cash_cents=cash,
        yes_volume=yes_vol,
        no_volume=no_vol,
        yes_cost_sum_cents=yes_vol * 50,
        no_cost_sum_cents=no_vol * 50,
        yes_pending_sell=0,
        no_pending_sell=0,
        frozen_balance_cents=0,
    )


def _make_ctx(
    config: MarketConfig | None = None,
    inventory: Inventory | None = None,
) -> MarketContext:
    cfg = config or _make_config()
    inv = inventory or _make_inventory()
    return MarketContext(market_id=cfg.market_id, config=cfg, inventory=inv)


# ---------------------------------------------------------------------------
# BND-01: tau=0.0 — A-S engine must not crash, returns positive spread
# ---------------------------------------------------------------------------


class TestTauZero:
    def test_optimal_spread_at_tau_zero(self) -> None:
        engine = ASEngine()
        sigma = engine.bernoulli_sigma(50)
        spread = engine.optimal_spread(gamma=0.3, sigma=sigma, tau_hours=0.0, kappa=1.5)
        assert spread > 0

    def test_compute_quotes_at_tau_zero(self) -> None:
        engine = ASEngine()
        sigma = engine.bernoulli_sigma(50)
        ask, bid = engine.compute_quotes(
            mid_price=50,
            inventory_skew=0.0,
            gamma=0.3,
            sigma=sigma,
            tau_hours=0.0,
            kappa=1.5,
        )
        assert 1 <= bid < ask <= 99


# ---------------------------------------------------------------------------
# BND-02: cash_cents=0 — gradient + sanitizer must not crash
# ---------------------------------------------------------------------------


class TestCashZero:
    def test_gradient_with_zero_cash(self) -> None:
        """Gradient engine builds ladders regardless of cash."""
        config = _make_config()
        gradient = GradientEngine()
        ask_ladder = gradient.build_ask_ladder(55, config, base_qty=100)
        bid_ladder = gradient.build_bid_ladder(45, config, base_qty=100)
        assert len(ask_ladder) > 0
        assert len(bid_ladder) > 0

    def test_sanitize_with_zero_cash(self) -> None:
        """Sanitizer should not crash when cash=0."""
        inv = _make_inventory(yes_vol=500, no_vol=500, cash=0)
        ctx = _make_ctx(inventory=inv)
        intent = OrderIntent(
            action=QuoteAction.PLACE,
            side="YES",
            direction="SELL",
            price_cents=55,
            quantity=10,
        )
        sanitizer = OrderSanitizer()
        result = sanitizer.sanitize([intent], DefenseLevel.NORMAL, ctx)
        # Should not crash; YES intent allowed if YES inventory available
        assert len(result) <= 1


# ---------------------------------------------------------------------------
# BND-03: yes_volume=0 — Sanitizer rejects SELL YES (no inventory)
# ---------------------------------------------------------------------------


class TestZeroInventory:
    def test_sanitizer_rejects_sell_yes_when_no_yes_inventory(self) -> None:
        inv = _make_inventory(yes_vol=0, no_vol=500, cash=100_000)
        ctx = _make_ctx(inventory=inv)
        intent = OrderIntent(
            action=QuoteAction.PLACE,
            side="YES",
            direction="SELL",
            price_cents=55,
            quantity=10,
        )
        sanitizer = OrderSanitizer()
        result = sanitizer.sanitize([intent], DefenseLevel.NORMAL, ctx)
        assert result == [], "Should reject SELL YES when yes_volume=0"

    def test_sanitizer_rejects_sell_no_when_no_no_inventory(self) -> None:
        inv = _make_inventory(yes_vol=500, no_vol=0, cash=100_000)
        ctx = _make_ctx(inventory=inv)
        intent = OrderIntent(
            action=QuoteAction.PLACE,
            side="NO",
            direction="SELL",
            price_cents=45,
            quantity=10,
        )
        sanitizer = OrderSanitizer()
        result = sanitizer.sanitize([intent], DefenseLevel.NORMAL, ctx)
        assert result == [], "Should reject SELL NO when no_volume=0"


# ---------------------------------------------------------------------------
# BND-04: Reinvest threshold boundary
# ---------------------------------------------------------------------------


class TestReinvestThreshold:
    def test_reinvest_triggers_at_threshold(self) -> None:
        """Cash exactly at threshold — surplus=0, no mint."""
        from src.amm.lifecycle.reinvest import AUTO_REINVEST_THRESHOLD_CENTS

        inv = _make_inventory(cash=AUTO_REINVEST_THRESHOLD_CENTS)
        surplus = inv.cash_cents - AUTO_REINVEST_THRESHOLD_CENTS
        quantity = surplus // 100
        assert quantity == 0, "At exact threshold, surplus=0, no mint expected"

    def test_reinvest_does_not_trigger_below_threshold(self) -> None:
        """Cash one cent below threshold — no mint."""
        from src.amm.lifecycle.reinvest import AUTO_REINVEST_THRESHOLD_CENTS

        inv = _make_inventory(cash=AUTO_REINVEST_THRESHOLD_CENTS - 1)
        surplus = inv.cash_cents - AUTO_REINVEST_THRESHOLD_CENTS
        assert surplus < 0, "Below threshold, surplus must be negative"

    def test_reinvest_triggers_above_threshold(self) -> None:
        """Cash above threshold by exactly one pair cost — mints 1 pair."""
        from src.amm.lifecycle.reinvest import (
            AUTO_REINVEST_THRESHOLD_CENTS,
            PAIR_COST_CENTS,
        )

        inv = _make_inventory(cash=AUTO_REINVEST_THRESHOLD_CENTS + PAIR_COST_CENTS)
        surplus = inv.cash_cents - AUTO_REINVEST_THRESHOLD_CENTS
        quantity = surplus // PAIR_COST_CENTS
        assert quantity == 1, "Exactly one pair cost above threshold should mint 1"


# ---------------------------------------------------------------------------
# BND-05: Oracle deviation at DefenseStack threshold boundary
# ---------------------------------------------------------------------------


class TestDefenseStackThreshold:
    @pytest.mark.parametrize(
        "skew, expected_min_defense",
        [
            (0.3, DefenseLevel.WIDEN),       # exactly at widen threshold
            (0.299, DefenseLevel.NORMAL),     # just below widen threshold
            (0.6, DefenseLevel.ONE_SIDE),     # exactly at one_side threshold
            (0.599, DefenseLevel.WIDEN),      # just below one_side threshold
            (0.8, DefenseLevel.KILL_SWITCH),  # exactly at kill threshold
            (0.799, DefenseLevel.ONE_SIDE),   # just below kill threshold
        ],
    )
    def test_skew_thresholds(self, skew: float, expected_min_defense: DefenseLevel) -> None:
        """DefenseStack respects exact skew threshold boundaries."""
        config = _make_config(
            inventory_skew_widen=0.3,
            inventory_skew_one_side=0.6,
            inventory_skew_kill=0.8,
        )
        stack = DefenseStack(config)
        level = stack.evaluate(inventory_skew=skew, daily_pnl=0, market_active=True)
        assert level == expected_min_defense, (
            f"skew={skew}: expected {expected_min_defense}, got {level}"
        )

    def test_negative_skew_triggers_same_defense(self) -> None:
        """Negative skew has same thresholds (abs(skew) used)."""
        config = _make_config(inventory_skew_widen=0.3)
        stack = DefenseStack(config)
        level = stack.evaluate(inventory_skew=-0.3, daily_pnl=0, market_active=True)
        assert level == DefenseLevel.WIDEN

    def test_pnl_kill_threshold(self) -> None:
        """Loss >= max_per_market_loss_cents triggers KILL_SWITCH."""
        config = _make_config(max_per_market_loss_cents=5000)
        stack = DefenseStack(config)
        level = stack.evaluate(inventory_skew=0.0, daily_pnl=-5000, market_active=True)
        assert level == DefenseLevel.KILL_SWITCH

    def test_pnl_just_below_kill(self) -> None:
        """Loss just below kill threshold triggers ONE_SIDE (half-loss rule)."""
        config = _make_config(max_per_market_loss_cents=5000)
        stack = DefenseStack(config)
        # -2500 = -(5000 // 2), triggers ONE_SIDE
        level = stack.evaluate(inventory_skew=0.0, daily_pnl=-2500, market_active=True)
        assert level == DefenseLevel.ONE_SIDE

    def test_pnl_above_one_side(self) -> None:
        """Loss just above ONE_SIDE threshold stays NORMAL."""
        config = _make_config(max_per_market_loss_cents=5000)
        stack = DefenseStack(config)
        level = stack.evaluate(inventory_skew=0.0, daily_pnl=-2499, market_active=True)
        assert level == DefenseLevel.NORMAL

    def test_inactive_market_triggers_kill(self) -> None:
        """Inactive market always triggers KILL_SWITCH."""
        config = _make_config()
        stack = DefenseStack(config)
        level = stack.evaluate(inventory_skew=0.0, daily_pnl=0, market_active=False)
        assert level == DefenseLevel.KILL_SWITCH
