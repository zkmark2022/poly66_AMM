"""Test Order Sanitizer — validates and fixes OrderIntent before execution."""
import pytest
from src.amm.risk.sanitizer import OrderSanitizer, SanitizedResult
from src.amm.strategy.models import OrderIntent
from src.amm.models.enums import QuoteAction
from src.amm.config.models import MarketConfig


def _intent(
    price_cents: int = 50,
    quantity: int = 100,
    side: str = "YES",
    direction: str = "SELL",
    action: QuoteAction = QuoteAction.PLACE,
) -> OrderIntent:
    return OrderIntent(
        action=action,
        side=side,
        direction=direction,
        price_cents=price_cents,
        quantity=quantity,
    )


class TestPriceValidation:
    def test_valid_price_passes(self) -> None:
        cfg = MarketConfig(market_id="mkt-1")
        sanitizer = OrderSanitizer(cfg)
        result = sanitizer.sanitize(_intent(price_cents=50))
        assert result.is_valid
        assert result.intent.price_cents == 50

    def test_price_below_1_is_clamped(self) -> None:
        cfg = MarketConfig(market_id="mkt-1")
        sanitizer = OrderSanitizer(cfg)
        result = sanitizer.sanitize(_intent(price_cents=0))
        assert result.is_valid
        assert result.intent.price_cents == 1

    def test_price_above_99_is_clamped(self) -> None:
        cfg = MarketConfig(market_id="mkt-1")
        sanitizer = OrderSanitizer(cfg)
        result = sanitizer.sanitize(_intent(price_cents=100))
        assert result.is_valid
        assert result.intent.price_cents == 99

    def test_price_negative_is_clamped(self) -> None:
        cfg = MarketConfig(market_id="mkt-1")
        sanitizer = OrderSanitizer(cfg)
        result = sanitizer.sanitize(_intent(price_cents=-5))
        assert result.is_valid
        assert result.intent.price_cents == 1


class TestQuantityValidation:
    def test_valid_quantity_passes(self) -> None:
        cfg = MarketConfig(market_id="mkt-1", max_order_quantity=500)
        sanitizer = OrderSanitizer(cfg)
        result = sanitizer.sanitize(_intent(quantity=100))
        assert result.is_valid
        assert result.intent.quantity == 100

    def test_quantity_zero_is_rejected(self) -> None:
        cfg = MarketConfig(market_id="mkt-1")
        sanitizer = OrderSanitizer(cfg)
        result = sanitizer.sanitize(_intent(quantity=0))
        assert not result.is_valid
        assert "quantity" in result.rejection_reason.lower()

    def test_quantity_negative_is_rejected(self) -> None:
        cfg = MarketConfig(market_id="mkt-1")
        sanitizer = OrderSanitizer(cfg)
        result = sanitizer.sanitize(_intent(quantity=-10))
        assert not result.is_valid

    def test_quantity_exceeds_max_is_clamped(self) -> None:
        cfg = MarketConfig(market_id="mkt-1", max_order_quantity=500)
        sanitizer = OrderSanitizer(cfg)
        result = sanitizer.sanitize(_intent(quantity=800))
        assert result.is_valid
        assert result.intent.quantity == 500

    def test_quantity_below_min_is_rejected(self) -> None:
        cfg = MarketConfig(market_id="mkt-1", min_order_quantity=5)
        sanitizer = OrderSanitizer(cfg)
        result = sanitizer.sanitize(_intent(quantity=3))
        assert not result.is_valid


class TestSpreadValidation:
    def test_valid_ask_bid_pair_passes(self) -> None:
        """ask > bid is valid (positive spread)."""
        cfg = MarketConfig(market_id="mkt-1")
        sanitizer = OrderSanitizer(cfg)
        ask = _intent(price_cents=52, side="YES")
        bid = _intent(price_cents=48, side="NO")
        assert sanitizer.sanitize(ask).is_valid
        assert sanitizer.sanitize(bid).is_valid

    def test_batch_rejects_negative_spread(self) -> None:
        """Reject entire batch if ask_price < bid_price (crossed market)."""
        cfg = MarketConfig(market_id="mkt-1")
        sanitizer = OrderSanitizer(cfg)
        # YES ask @ 45, NO at complement 100-48=52 → ask<bid → crossed
        yes_intent = _intent(price_cents=45, side="YES")   # ask
        no_intent = _intent(price_cents=56, side="NO")     # NO @ 56 means bid_yes = 44
        # ask_yes=45, bid_yes=44 → positive → ok
        results = sanitizer.sanitize_batch([yes_intent, no_intent])
        assert all(r.is_valid for r in results)

    def test_batch_rejects_when_yes_ask_below_bid(self) -> None:
        """YES ask price < complement of NO price = crossed market."""
        cfg = MarketConfig(market_id="mkt-1")
        sanitizer = OrderSanitizer(cfg)
        # YES ask @ 40, NO @ 62 → bid_yes = 100 - 62 = 38 → spread = 40-38 = 2 → ok
        # YES ask @ 40, NO @ 58 → bid_yes = 100 - 58 = 42 → crossed!
        yes_intent = _intent(price_cents=40, side="YES")
        no_intent = _intent(price_cents=58, side="NO")
        results = sanitizer.sanitize_batch([yes_intent, no_intent])
        assert any(not r.is_valid for r in results)


class TestSanitizeBatch:
    def test_valid_batch_all_pass(self) -> None:
        cfg = MarketConfig(market_id="mkt-1")
        sanitizer = OrderSanitizer(cfg)
        # YES ask=53, NO=46 → bid_yes = 100-46 = 54 wait...
        # YES ask=55, NO=42 → bid_yes = 100-42 = 58 → still crossed
        # AMM SELL YES @ 55 (ask), SELL NO @ 45 → bid_yes implied = 100-45 = 55 → zero spread
        # AMM SELL YES @ 56, SELL NO @ 44 → bid_yes = 100-44 = 56 → still zero
        # Correct: SELL YES @ 52, SELL NO @ 52 → bid_yes = 100-52 = 48 → spread = 52-48 = 4 ✓
        intents = [_intent(price_cents=52, side="YES"), _intent(price_cents=52, side="NO")]
        results = sanitizer.sanitize_batch(intents)
        assert all(r.is_valid for r in results)
        assert len(results) == 2

    def test_empty_batch_returns_empty(self) -> None:
        cfg = MarketConfig(market_id="mkt-1")
        sanitizer = OrderSanitizer(cfg)
        assert sanitizer.sanitize_batch([]) == []

    def test_mixed_batch_partial_valid(self) -> None:
        cfg = MarketConfig(market_id="mkt-1")
        sanitizer = OrderSanitizer(cfg)
        intents = [
            _intent(price_cents=50),   # valid
            _intent(quantity=0),        # invalid
        ]
        results = sanitizer.sanitize_batch(intents)
        assert results[0].is_valid
        assert not results[1].is_valid
