"""Test three-layer pricing engine. See AMM design v7.1 §3."""
import pytest
from src.amm.strategy.pricing.three_layer import ThreeLayerPricing
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.pricing.micro import MicroPricing
from src.amm.strategy.pricing.posterior import PosteriorPricing


class TestThreeLayerPricing:
    def test_exploration_phase_anchor_dominant(self) -> None:
        """In EXPLORATION, anchor weight is highest (0.6)."""
        engine = ThreeLayerPricing(
            anchor=AnchorPricing(initial_price=50),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
        )
        mid = engine.compute(
            phase="EXPLORATION",
            anchor_price=50,
            best_bid=48,
            best_ask=52,
            recent_trades=[],
        )
        assert mid == 50  # anchor dominates, micro mid=50, posterior=50 → all same

    def test_stabilization_phase_micro_weight_increases(self) -> None:
        """In STABILIZATION, micro-price gets more weight."""
        engine = ThreeLayerPricing(
            anchor=AnchorPricing(initial_price=50),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
        )
        mid = engine.compute(
            phase="STABILIZATION",
            anchor_price=50,
            best_bid=55,
            best_ask=57,
            recent_trades=[{"price_cents": 56, "quantity": 10}],
        )
        # anchor=50 (w=0.2), micro=56 (w=0.5), posterior=56 (w=0.3)
        # weighted = 0.2*50 + 0.5*56 + 0.3*56 = 10 + 28 + 16.8 = 54.8 → 55
        assert 50 < mid < 57

    def test_output_clamped_to_valid_range(self) -> None:
        """Output must be in [1, 99]."""
        engine = ThreeLayerPricing(
            anchor=AnchorPricing(initial_price=99),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
        )
        mid = engine.compute(
            phase="EXPLORATION",
            anchor_price=99,
            best_bid=98,
            best_ask=100,  # invalid, but test robustness
            recent_trades=[],
        )
        assert 1 <= mid <= 99

    def test_exploration_weights_are_60_30_10(self) -> None:
        """Verify exact weights: anchor=0.6, micro=0.3, posterior=0.1."""
        engine = ThreeLayerPricing(
            anchor=AnchorPricing(initial_price=60),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
        )
        # anchor=60, micro=(0+0)/2 fallback=50, posterior fallback=60
        # micro with no valid bid/ask: best_bid=0, best_ask=0 → fallback 50
        mid = engine.compute(
            phase="EXPLORATION",
            anchor_price=60,
            best_bid=0,
            best_ask=0,
            recent_trades=[],
        )
        # 0.6*60 + 0.3*50 + 0.1*60 = 36 + 15 + 6 = 57
        assert mid == 57

    def test_stabilization_weights_are_20_50_30(self) -> None:
        """Verify exact weights: anchor=0.2, micro=0.5, posterior=0.3."""
        engine = ThreeLayerPricing(
            anchor=AnchorPricing(initial_price=40),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
        )
        mid = engine.compute(
            phase="STABILIZATION",
            anchor_price=40,
            best_bid=60,
            best_ask=60,  # micro = 60
            recent_trades=[{"price_cents": 70, "quantity": 1}],
        )
        # 0.2*40 + 0.5*60 + 0.3*70 = 8 + 30 + 21 = 59
        assert mid == 59

    def test_unknown_phase_defaults_to_exploration(self) -> None:
        """Unknown phase should use EXPLORATION weights."""
        engine = ThreeLayerPricing(
            anchor=AnchorPricing(initial_price=50),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
        )
        mid = engine.compute(
            phase="UNKNOWN_PHASE",
            anchor_price=50,
            best_bid=50,
            best_ask=50,
            recent_trades=[],
        )
        assert 1 <= mid <= 99


class TestAnchorPricing:
    def test_returns_anchor_as_float(self) -> None:
        anchor = AnchorPricing(initial_price=50)
        assert anchor.compute(50) == pytest.approx(50.0)

    def test_uses_provided_price(self) -> None:
        anchor = AnchorPricing(initial_price=50)
        assert anchor.compute(75) == pytest.approx(75.0)


class TestMicroPricing:
    def test_mid_price_symmetric(self) -> None:
        micro = MicroPricing()
        assert micro.compute(48, 52) == pytest.approx(50.0)

    def test_mid_price_asymmetric(self) -> None:
        micro = MicroPricing()
        assert micro.compute(55, 57) == pytest.approx(56.0)

    def test_no_bid_falls_back_to_ask(self) -> None:
        micro = MicroPricing()
        assert micro.compute(0, 55) == pytest.approx(55.0)

    def test_no_ask_falls_back_to_bid(self) -> None:
        micro = MicroPricing()
        assert micro.compute(45, 0) == pytest.approx(45.0)

    def test_both_zero_falls_back_to_50(self) -> None:
        micro = MicroPricing()
        assert micro.compute(0, 0) == pytest.approx(50.0)

    def test_vwap_single_trade(self) -> None:
        micro = MicroPricing()
        vwap = micro.vwap([{"price_cents": 55, "quantity": 10}])
        assert vwap == pytest.approx(55.0)

    def test_vwap_multiple_trades(self) -> None:
        micro = MicroPricing()
        trades = [
            {"price_cents": 50, "quantity": 10},
            {"price_cents": 60, "quantity": 10},
        ]
        vwap = micro.vwap(trades)
        assert vwap == pytest.approx(55.0)

    def test_vwap_empty_returns_none(self) -> None:
        micro = MicroPricing()
        assert micro.vwap([]) is None


class TestPosteriorPricing:
    def test_no_trades_returns_fallback(self) -> None:
        posterior = PosteriorPricing()
        assert posterior.compute([], fallback=50.0) == pytest.approx(50.0)

    def test_single_trade_returns_its_price(self) -> None:
        posterior = PosteriorPricing()
        result = posterior.compute(
            [{"price_cents": 60, "quantity": 5}], fallback=50.0
        )
        assert result == pytest.approx(60.0)

    def test_weighted_by_quantity(self) -> None:
        posterior = PosteriorPricing()
        trades = [
            {"price_cents": 40, "quantity": 10},
            {"price_cents": 60, "quantity": 30},
        ]
        result = posterior.compute(trades, fallback=50.0)
        # (40*10 + 60*30) / 40 = (400 + 1800) / 40 = 55
        assert result == pytest.approx(55.0)
