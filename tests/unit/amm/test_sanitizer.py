"""Tests for OrderSanitizer — defense-level filtering and inventory checks."""
from __future__ import annotations

import pytest

from src.amm.config.models import MarketConfig
from src.amm.models.enums import DefenseLevel, Phase, QuoteAction
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext
from src.amm.risk.sanitizer import OrderSanitizer
from src.amm.strategy.models import OrderIntent


def _make_intent(
    side: str = "YES",
    quantity: int = 100,
    price: int = 55,
    action: QuoteAction = QuoteAction.PLACE,
) -> OrderIntent:
    return OrderIntent(
        action=action,
        side=side,
        direction="SELL",
        price_cents=price,
        quantity=quantity,
    )


def _make_ctx(
    yes: int = 500,
    no: int = 500,
    yes_pending: int = 0,
    no_pending: int = 0,
) -> MarketContext:
    return MarketContext(
        market_id="mkt-1",
        config=MarketConfig(market_id="mkt-1"),
        inventory=Inventory(
            cash_cents=500_000,
            yes_volume=yes, no_volume=no,
            yes_cost_sum_cents=yes * 50, no_cost_sum_cents=no * 50,
            yes_pending_sell=yes_pending, no_pending_sell=no_pending,
            frozen_balance_cents=0,
        ),
        phase=Phase.EXPLORATION,
        defense_level=DefenseLevel.NORMAL,
    )


class TestOrderSanitizerInventoryChecks:
    def test_drops_intent_when_no_yes_available(self) -> None:
        ctx = _make_ctx(yes=100, yes_pending=100)  # yes_available = 0
        sanitizer = OrderSanitizer()
        result = sanitizer.sanitize([_make_intent(side="YES", quantity=10)],
                                    DefenseLevel.NORMAL, ctx)
        assert result == []

    def test_drops_intent_when_no_no_available(self) -> None:
        ctx = _make_ctx(no=50, no_pending=50)  # no_available = 0
        sanitizer = OrderSanitizer()
        result = sanitizer.sanitize([_make_intent(side="NO", quantity=10)],
                                    DefenseLevel.NORMAL, ctx)
        assert result == []

    def test_clamps_yes_quantity_to_available(self) -> None:
        ctx = _make_ctx(yes=100, yes_pending=70)  # yes_available = 30
        sanitizer = OrderSanitizer()
        result = sanitizer.sanitize([_make_intent(side="YES", quantity=50)],
                                    DefenseLevel.NORMAL, ctx)
        assert len(result) == 1
        assert result[0].quantity == 30

    def test_clamps_no_quantity_to_available(self) -> None:
        ctx = _make_ctx(no=200, no_pending=160)  # no_available = 40
        sanitizer = OrderSanitizer()
        result = sanitizer.sanitize([_make_intent(side="NO", quantity=100)],
                                    DefenseLevel.NORMAL, ctx)
        assert len(result) == 1
        assert result[0].quantity == 40

    def test_passes_intent_when_sufficient_inventory(self) -> None:
        ctx = _make_ctx(yes=500, yes_pending=0)
        sanitizer = OrderSanitizer()
        result = sanitizer.sanitize([_make_intent(side="YES", quantity=100)],
                                    DefenseLevel.NORMAL, ctx)
        assert len(result) == 1
        assert result[0].quantity == 100

    def test_drops_intent_with_zero_quantity(self) -> None:
        ctx = _make_ctx(yes=500)
        sanitizer = OrderSanitizer()
        result = sanitizer.sanitize([_make_intent(side="YES", quantity=0)],
                                    DefenseLevel.NORMAL, ctx)
        assert result == []

    def test_clamps_price_within_range(self) -> None:
        ctx = _make_ctx()
        sanitizer = OrderSanitizer()
        result = sanitizer.sanitize([_make_intent(price=0)],
                                    DefenseLevel.NORMAL, ctx)
        assert result[0].price_cents == 1

        result = sanitizer.sanitize([_make_intent(price=150)],
                                    DefenseLevel.NORMAL, ctx)
        assert result[0].price_cents == 99


class TestOrderSanitizerDefenseLevels:
    def test_one_side_suppresses_no_when_long_yes(self) -> None:
        ctx = _make_ctx(yes=700, no=300)  # skew > 0 → long YES
        sanitizer = OrderSanitizer()
        intents = [
            _make_intent(side="YES", quantity=50),
            _make_intent(side="NO", quantity=50),
        ]
        result = sanitizer.sanitize(intents, DefenseLevel.ONE_SIDE, ctx)
        sides = {i.side for i in result}
        assert "NO" not in sides
        assert "YES" in sides

    def test_one_side_suppresses_yes_when_long_no(self) -> None:
        ctx = _make_ctx(yes=300, no=700)  # skew < 0 → long NO
        sanitizer = OrderSanitizer()
        intents = [
            _make_intent(side="YES", quantity=50),
            _make_intent(side="NO", quantity=50),
        ]
        result = sanitizer.sanitize(intents, DefenseLevel.ONE_SIDE, ctx)
        sides = {i.side for i in result}
        assert "YES" not in sides
        assert "NO" in sides
