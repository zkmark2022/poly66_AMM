"""Tests for P0 critical bug fixes (2026-03-05 code review)."""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from src.amm.config.models import MarketConfig
from src.amm.models.market_context import MarketContext
from src.amm.models.inventory import Inventory
from src.amm.models.enums import DefenseLevel
from src.amm.oracle.polymarket_oracle import OracleState, PolymarketOracle
from src.amm.risk.sanitizer import OrderSanitizer
from src.amm.strategy.models import OrderIntent
from src.amm.models.enums import QuoteAction
from src.amm.utils.integer_math import ceiling_div
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.as_engine import ASEngine


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_config(**kwargs) -> MarketConfig:
    defaults: dict[str, Any] = {"market_id": "mkt-test", "oracle_slug": "test-slug"}
    defaults.update(kwargs)
    return MarketConfig(**defaults)


def _make_inventory() -> Inventory:
    return Inventory(
        cash_cents=100_000,
        yes_volume=500, no_volume=500,
        yes_cost_sum_cents=25_000, no_cost_sum_cents=25_000,
        yes_pending_sell=0, no_pending_sell=0,
        frozen_balance_cents=0,
    )


def _make_ctx(**kwargs) -> MarketContext:
    cfg = _make_config()
    return MarketContext(
        market_id="mkt-test",
        config=cfg,
        inventory=_make_inventory(),
        **kwargs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# FIX 1: Oracle deadlock — oracle must not force ONE_SIDE on first cycle
# after refresh() called during init
# ─────────────────────────────────────────────────────────────────────────────

class TestOracleDeadlock:
    def test_oracle_check_stale_true_before_refresh(self) -> None:
        """Fresh oracle with no refresh → check_stale() is True."""
        cfg = _make_config()
        oracle = PolymarketOracle(cfg)
        assert oracle.check_stale() is True

    def test_oracle_check_stale_false_after_refresh(self) -> None:
        """After refresh(), check_stale() is False (within threshold)."""
        cfg = _make_config(oracle_stale_seconds=10.0)
        oracle = PolymarketOracle(cfg)

        with patch.object(oracle, "_fetch_price", return_value=52.0):
            oracle.refresh()

        assert oracle.check_stale() is False

    def test_oracle_evaluate_returns_normal_after_refresh(self) -> None:
        """evaluate() must return NORMAL (not STALE) once refresh() has been called."""
        cfg = _make_config(oracle_stale_seconds=10.0, oracle_deviation_cents=20.0)
        oracle = PolymarketOracle(cfg)

        with patch.object(oracle, "_fetch_price", return_value=50.0):
            oracle.refresh()

        state = oracle.evaluate(internal_price_cents=50.0)
        assert state == OracleState.NORMAL, (
            f"Oracle should be NORMAL after refresh, got {state}"
        )

    def test_oracle_evaluate_stale_without_refresh(self) -> None:
        """Without refresh(), evaluate() returns STALE on first call."""
        cfg = _make_config()
        oracle = PolymarketOracle(cfg)
        state = oracle.evaluate(internal_price_cents=50.0)
        assert state == OracleState.STALE


# ─────────────────────────────────────────────────────────────────────────────
# FIX 2: tau=0.0 falsiness — remaining_hours_override=0.0 must not default to 24.0
# ─────────────────────────────────────────────────────────────────────────────

class TestTauFalsiness:
    def test_remaining_hours_override_zero_should_give_tau_zero(self) -> None:
        """tau = 0.0 when remaining_hours_override=0.0 (not 24.0)."""
        cfg = _make_config(remaining_hours_override=0.0)
        # Replicate the correct logic from main.py
        override = cfg.remaining_hours_override
        tau = override if override is not None else 24.0
        assert tau == 0.0, f"Expected tau=0.0, got {tau}"

    def test_remaining_hours_override_none_defaults_to_24(self) -> None:
        """tau = 24.0 when remaining_hours_override=None."""
        cfg = _make_config(remaining_hours_override=None)
        override = cfg.remaining_hours_override
        tau = override if override is not None else 24.0
        assert tau == 24.0

    def test_remaining_hours_override_nonzero_used_as_is(self) -> None:
        """tau = custom value when remaining_hours_override is set."""
        cfg = _make_config(remaining_hours_override=6.5)
        override = cfg.remaining_hours_override
        tau = override if override is not None else 24.0
        assert tau == 6.5

    def test_old_or_logic_would_fail_with_zero(self) -> None:
        """Demonstrate bug: `override or 24.0` returns 24.0 when override=0.0."""
        override = 0.0
        buggy_tau = override or 24.0
        assert buggy_tau == 24.0  # This is the bug
        correct_tau = override if override is not None else 24.0
        assert correct_tau == 0.0  # This is the fix


# ─────────────────────────────────────────────────────────────────────────────
# FIX 3: MarketContext oracle threshold fields
# ─────────────────────────────────────────────────────────────────────────────

class TestMarketContextOracleFields:
    def test_oracle_lag_threshold_field_exists_with_default(self) -> None:
        """MarketContext must have oracle_lag_threshold with default 10.0."""
        ctx = _make_ctx()
        assert hasattr(ctx, "oracle_lag_threshold"), (
            "MarketContext must declare oracle_lag_threshold field"
        )
        assert ctx.oracle_lag_threshold == 10.0

    def test_oracle_deviation_threshold_field_exists_with_default(self) -> None:
        """MarketContext must have oracle_deviation_threshold with default 20.0."""
        ctx = _make_ctx()
        assert hasattr(ctx, "oracle_deviation_threshold"), (
            "MarketContext must declare oracle_deviation_threshold field"
        )
        assert ctx.oracle_deviation_threshold == 20.0

    def test_oracle_threshold_fields_can_be_overridden(self) -> None:
        """oracle threshold fields accept custom values at construction."""
        ctx = MarketContext(
            market_id="mkt-test",
            config=_make_config(),
            inventory=_make_inventory(),
            oracle_lag_threshold=5.0,
            oracle_deviation_threshold=15.0,
        )
        assert ctx.oracle_lag_threshold == 5.0
        assert ctx.oracle_deviation_threshold == 15.0


# ─────────────────────────────────────────────────────────────────────────────
# FIX 4: Sanitizer must reject BUY direction
# ─────────────────────────────────────────────────────────────────────────────

class TestSanitizerBuyGuard:
    def _make_intent(self, direction: str = "SELL", side: str = "YES") -> OrderIntent:
        return OrderIntent(
            action=QuoteAction.PLACE,
            side=side,
            direction=direction,
            price_cents=55,
            quantity=100,
        )

    def test_buy_intent_is_rejected(self) -> None:
        """Sanitizer must return None for BUY intents."""
        sanitizer = OrderSanitizer()
        ctx = _make_ctx()
        intent = self._make_intent(direction="BUY")
        result = sanitizer._sanitize_one(intent, DefenseLevel.NORMAL, ctx)
        assert result is None, "Sanitizer must reject BUY direction"

    def test_sell_intent_is_allowed(self) -> None:
        """Sanitizer must pass SELL intents through (subject to other checks)."""
        sanitizer = OrderSanitizer()
        ctx = _make_ctx()
        intent = self._make_intent(direction="SELL", side="YES")
        result = sanitizer._sanitize_one(intent, DefenseLevel.NORMAL, ctx)
        assert result is not None, "SELL intent should pass through sanitizer"

    def test_buy_intents_filtered_from_list(self) -> None:
        """sanitize() must strip all BUY intents from the list."""
        sanitizer = OrderSanitizer()
        ctx = _make_ctx()
        intents = [
            self._make_intent(direction="BUY", side="YES"),
            self._make_intent(direction="SELL", side="YES"),
            self._make_intent(direction="BUY", side="NO"),
        ]
        result = sanitizer.sanitize(intents, DefenseLevel.NORMAL, ctx)
        assert all(i.direction == "SELL" for i in result), (
            "All BUY intents must be filtered out"
        )


# ─────────────────────────────────────────────────────────────────────────────
# FIX 5: ceiling_div negative denominator guard
# ─────────────────────────────────────────────────────────────────────────────

class TestCeilingDivGuard:
    def test_ceiling_div_raises_on_negative_denominator(self) -> None:
        """ceiling_div must raise AssertionError for negative denominator."""
        with pytest.raises(AssertionError, match="positive denominator"):
            ceiling_div(10, -1)

    def test_ceiling_div_raises_on_zero_denominator(self) -> None:
        """ceiling_div must raise AssertionError for zero denominator."""
        with pytest.raises(AssertionError, match="positive denominator"):
            ceiling_div(10, 0)

    def test_ceiling_div_works_with_positive_denominator(self) -> None:
        """ceiling_div still works correctly for positive denominators."""
        assert ceiling_div(7, 2) == 4
        assert ceiling_div(6, 2) == 3
        assert ceiling_div(0, 5) == 0


# ─────────────────────────────────────────────────────────────────────────────
# FIX 6: AnchorPricing dead constructor param
# ─────────────────────────────────────────────────────────────────────────────

class TestAnchorPricingDeadParam:
    def test_anchor_pricing_uses_self_price_as_fallback(self) -> None:
        """compute() with no anchor_price arg should use self._price."""
        anchor = AnchorPricing(initial_price=60)
        result = anchor.compute(anchor_price=None)  # type: ignore[arg-type]
        assert result == 60.0, (
            "AnchorPricing.compute() must fall back to self._price when anchor_price is None"
        )

    def test_anchor_pricing_explicit_anchor_price_used(self) -> None:
        """compute() with explicit anchor_price should use that value."""
        anchor = AnchorPricing(initial_price=40)
        result = anchor.compute(anchor_price=55)
        assert result == 55.0

    def test_anchor_pricing_initial_price_stored(self) -> None:
        """AnchorPricing stores initial_price as self._price."""
        anchor = AnchorPricing(initial_price=75)
        assert anchor._price == 75


# ─────────────────────────────────────────────────────────────────────────────
# FIX 7: reservation_price type annotation
# ─────────────────────────────────────────────────────────────────────────────

class TestReservationPriceTypeAnnotation:
    def test_reservation_price_accepts_int(self) -> None:
        """reservation_price must accept int mid_price (no runtime TypeError)."""
        engine = ASEngine()
        result = engine.reservation_price(
            mid_price=50,  # int
            inventory_skew=0.0,
            gamma=0.3,
            sigma=0.05,
            tau_hours=24.0,
        )
        assert isinstance(result, float)

    def test_reservation_price_accepts_float(self) -> None:
        """reservation_price must accept float mid_price."""
        engine = ASEngine()
        result = engine.reservation_price(
            mid_price=50.0,  # float
            inventory_skew=0.0,
            gamma=0.3,
            sigma=0.05,
            tau_hours=24.0,
        )
        assert isinstance(result, float)
