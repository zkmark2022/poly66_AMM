"""Test gradient ladder generation. See AMM design v7.1 §6."""
import pytest
from src.amm.config.models import MarketConfig
from src.amm.models.enums import QuoteAction
from src.amm.strategy.gradient import GradientEngine
from src.amm.strategy.models import OrderIntent


class TestGradientEngine:
    def test_ask_ladder_is_sell_yes(self) -> None:
        """Ask ladder = SELL YES orders (direct)."""
        engine = GradientEngine()
        cfg = MarketConfig(market_id="mkt-1", gradient_levels=3, gradient_price_step_cents=1)
        intents = engine.build_ask_ladder(base_ask=52, config=cfg, base_qty=100)
        for intent in intents:
            assert intent.side == "YES"
            assert intent.direction == "SELL"
        # Prices ascend from base_ask
        prices = [i.price_cents for i in intents]
        assert prices == [52, 53, 54]

    def test_bid_ladder_is_sell_no(self) -> None:
        """v1.0 Review Fix #3: Bid ladder must map to SELL NO (not BUY YES).

        In single-orderbook architecture, AMM NEVER buys. It only sells from
        its dual inventory. 'Bid YES @ 48' → 'Sell NO @ 52' (100 - 48 = 52).
        This avoids freezing cash unnecessarily and aligns with the privilege
        design where AMM holds both YES and NO shares from Mint.
        """
        engine = GradientEngine()
        cfg = MarketConfig(market_id="mkt-1", gradient_levels=3, gradient_price_step_cents=1)
        intents = engine.build_bid_ladder(base_bid=48, config=cfg, base_qty=100)
        for intent in intents:
            assert intent.side == "NO"  # NOT "YES"
            assert intent.direction == "SELL"  # NOT "BUY"
        # Bid 48 → Sell NO @ 52, Bid 47 → Sell NO @ 53, Bid 46 → Sell NO @ 54
        prices = [i.price_cents for i in intents]
        assert prices == [52, 53, 54]

    def test_quantity_decay(self) -> None:
        """Each level has decay × previous level quantity."""
        engine = GradientEngine()
        cfg = MarketConfig(
            market_id="mkt-1",
            gradient_levels=3,
            gradient_quantity_decay=0.5,
            gradient_price_step_cents=1,
        )
        intents = engine.build_ask_ladder(base_ask=52, config=cfg, base_qty=100)
        quantities = [i.quantity for i in intents]
        assert quantities == [100, 50, 25]

    def test_prices_clamped_to_valid_range(self) -> None:
        """Ladder levels beyond [1, 99] are dropped."""
        engine = GradientEngine()
        cfg = MarketConfig(market_id="mkt-1", gradient_levels=5, gradient_price_step_cents=1)
        intents = engine.build_ask_ladder(base_ask=97, config=cfg, base_qty=100)
        # 97, 98, 99 (100 and 101 dropped)
        assert len(intents) <= 3
        for i in intents:
            assert 1 <= i.price_cents <= 99

    def test_ask_ladder_action_is_place(self) -> None:
        """Default action is PLACE."""
        engine = GradientEngine()
        cfg = MarketConfig(market_id="mkt-1", gradient_levels=1, gradient_price_step_cents=1)
        intents = engine.build_ask_ladder(base_ask=50, config=cfg, base_qty=100)
        assert intents[0].action == QuoteAction.PLACE

    def test_bid_ladder_action_is_place(self) -> None:
        engine = GradientEngine()
        cfg = MarketConfig(market_id="mkt-1", gradient_levels=1, gradient_price_step_cents=1)
        intents = engine.build_bid_ladder(base_bid=50, config=cfg, base_qty=100)
        assert intents[0].action == QuoteAction.PLACE

    def test_ask_ladder_correct_level_count(self) -> None:
        """Number of levels equals gradient_levels when range allows."""
        engine = GradientEngine()
        cfg = MarketConfig(market_id="mkt-1", gradient_levels=5, gradient_price_step_cents=2)
        intents = engine.build_ask_ladder(base_ask=50, config=cfg, base_qty=100)
        assert len(intents) == 5

    def test_bid_ladder_complement_mapping(self) -> None:
        """Verify complement price mapping: NO price = 100 - bid_yes_price."""
        engine = GradientEngine()
        cfg = MarketConfig(
            market_id="mkt-1", gradient_levels=3, gradient_price_step_cents=2
        )
        intents = engine.build_bid_ladder(base_bid=40, config=cfg, base_qty=100)
        # bid_yes: 40, 38, 36 → no_price: 60, 62, 64
        prices = [i.price_cents for i in intents]
        assert prices == [60, 62, 64]

    def test_bid_ladder_drops_invalid_no_prices(self) -> None:
        """NO prices beyond [1, 99] are dropped."""
        engine = GradientEngine()
        cfg = MarketConfig(market_id="mkt-1", gradient_levels=5, gradient_price_step_cents=1)
        # base_bid=2: no_prices = 98, 99, 100, 101, 102 → last 3 dropped
        intents = engine.build_bid_ladder(base_bid=2, config=cfg, base_qty=100)
        assert len(intents) == 2
        for i in intents:
            assert 1 <= i.price_cents <= 99

    def test_minimum_quantity_is_one(self) -> None:
        """Quantity never falls below 1 due to truncation."""
        engine = GradientEngine()
        cfg = MarketConfig(
            market_id="mkt-1",
            gradient_levels=5,
            gradient_quantity_decay=0.1,
            gradient_price_step_cents=1,
        )
        intents = engine.build_ask_ladder(base_ask=50, config=cfg, base_qty=1)
        for i in intents:
            assert i.quantity >= 1
