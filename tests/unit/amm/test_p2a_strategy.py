"""Tests for p2a strategy improvements.

FIX 1 (ISSUE-O-007): Bayesian posterior pricing (Beta-Binomial conjugate model)
FIX 2 (ISSUE-O-008): Micro pricing anti-spoofing / thin book detection
FIX 3 (ISSUE-O-013): Dynamic gamma tier by market age
"""
from __future__ import annotations

import pytest
from datetime import date, timedelta

from src.amm.config.models import MarketConfig
from src.amm.strategy.as_engine import ASEngine
from src.amm.strategy.pricing.posterior import PosteriorPricing
from src.amm.strategy.pricing.micro import MicroPricing
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.pricing.three_layer import ThreeLayerPricing


# ---------------------------------------------------------------------------
# FIX 1: Bayesian Posterior Pricing (Beta-Binomial)
# ---------------------------------------------------------------------------

class TestPosteriorPricingBayesian:
    def test_initial_prior_is_uniform(self) -> None:
        """Default α=β=1 → posterior mean = 0.5 → 50 cents."""
        p = PosteriorPricing()
        price = p.compute()
        assert price == pytest.approx(50.0)

    def test_yes_trade_increases_alpha(self) -> None:
        """A YES trade increases α, raising posterior mean above 50."""
        p = PosteriorPricing(alpha=1.0, beta=1.0)
        p.update([{"scenario": "TRANSFER_YES", "quantity": 100}])
        # α now > β → posterior mean > 0.5
        assert p._alpha > p._beta

    def test_no_trade_increases_beta(self) -> None:
        """A NO trade increases β, lowering posterior mean below 50."""
        p = PosteriorPricing(alpha=1.0, beta=1.0)
        p.update([{"scenario": "TRANSFER_NO", "quantity": 100}])
        assert p._beta > p._alpha

    def test_mint_treated_as_yes_trade(self) -> None:
        """MINT scenario raises α like a YES trade."""
        p = PosteriorPricing(alpha=1.0, beta=1.0)
        p.update([{"scenario": "MINT", "quantity": 100}])
        assert p._alpha > p._beta

    def test_yes_heavy_trades_push_price_above_50(self) -> None:
        p = PosteriorPricing(alpha=1.0, beta=1.0)
        trades = [{"scenario": "TRANSFER_YES", "quantity": 500}]
        price = p.compute(trades)
        assert price > 50.0

    def test_no_heavy_trades_push_price_below_50(self) -> None:
        p = PosteriorPricing(alpha=1.0, beta=1.0)
        trades = [{"scenario": "TRANSFER_NO", "quantity": 500}]
        price = p.compute(trades)
        assert price < 50.0

    def test_decay_moves_alpha_beta_toward_one(self) -> None:
        """After update with decay, α and β shrink toward 1.0."""
        p = PosteriorPricing(alpha=10.0, beta=10.0, decay=0.9)
        p.update([])
        # decay: 10 * 0.9 = 9.0, clamped to max(1, 9.0) = 9.0
        assert p._alpha == pytest.approx(9.0)
        assert p._beta == pytest.approx(9.0)

    def test_decay_does_not_go_below_one(self) -> None:
        """α and β never decay below 1.0."""
        p = PosteriorPricing(alpha=1.0, beta=1.0, decay=0.1)
        p.update([])
        assert p._alpha >= 1.0
        assert p._beta >= 1.0

    def test_compute_clamps_to_1_99(self) -> None:
        """Posterior mean is clamped to [1, 99]."""
        # Extreme β → mean near 0
        p = PosteriorPricing(alpha=1.0, beta=1e9)
        price = p.compute()
        assert price >= 1.0

        # Extreme α → mean near 1
        p2 = PosteriorPricing(alpha=1e9, beta=1.0)
        price2 = p2.compute()
        assert price2 <= 99.0

    def test_compute_with_trades_updates_and_returns(self) -> None:
        """compute(trades) both updates and returns price in one call."""
        p = PosteriorPricing()
        price = p.compute([{"scenario": "TRANSFER_YES", "quantity": 200}])
        assert isinstance(price, float)
        assert 50.0 < price <= 99.0

    def test_compute_with_none_returns_prior_mean(self) -> None:
        """compute(None) returns prior mean without modifying state."""
        p = PosteriorPricing(alpha=3.0, beta=1.0)
        price = p.compute(None)
        # prior mean = 3/(3+1) * 100 = 75
        assert price == pytest.approx(75.0)


# ---------------------------------------------------------------------------
# FIX 2: Micro Pricing Anti-Spoofing / Thin Book Detection
# ---------------------------------------------------------------------------

class TestMicroPricingAntiSpoof:
    def test_thin_book_returns_none(self) -> None:
        """When total depth < threshold, return None."""
        m = MicroPricing(min_depth_threshold=10)
        result = m.compute(best_bid=48, best_ask=52, bid_depth=3, ask_depth=4)
        assert result is None

    def test_sufficient_depth_returns_float(self) -> None:
        """When total depth >= threshold, return a float."""
        m = MicroPricing(min_depth_threshold=10)
        result = m.compute(best_bid=48, best_ask=52, bid_depth=10, ask_depth=10)
        assert isinstance(result, float)

    def test_symmetric_depth_gives_simple_mid(self) -> None:
        """Equal depth → volume-weighted mid equals arithmetic mid."""
        m = MicroPricing(min_depth_threshold=1)
        # bid=40, ask=60, depth=10 each
        # vwmid = (40*10 + 60*10) / 20 = 50
        result = m.compute(best_bid=40, best_ask=60, bid_depth=10, ask_depth=10)
        assert result == pytest.approx(50.0)

    def test_asymmetric_depth_skews_toward_thicker_side(self) -> None:
        """Heavy ask depth pulls mid price toward ask."""
        m = MicroPricing(min_depth_threshold=1)
        # bid=40, ask=60; bid_depth=5, ask_depth=20
        # vwmid = (40*20 + 60*5) / 25 = (800+300)/25 = 44
        result = m.compute(best_bid=40, best_ask=60, bid_depth=5, ask_depth=20)
        assert result == pytest.approx(44.0)

    def test_heavy_bid_depth_pulls_mid_toward_bid(self) -> None:
        """Heavy bid depth pulls mid price toward bid."""
        m = MicroPricing(min_depth_threshold=1)
        # bid=40, ask=60; bid_depth=20, ask_depth=5
        # vwmid = (40*5 + 60*20) / 25 = (200+1200)/25 = 56
        result = m.compute(best_bid=40, best_ask=60, bid_depth=20, ask_depth=5)
        assert result == pytest.approx(56.0)

    def test_result_clamped_to_1_99(self) -> None:
        """Result is clamped to [1, 99]."""
        m = MicroPricing(min_depth_threshold=1)
        result = m.compute(best_bid=1, best_ask=2, bid_depth=100, ask_depth=100)
        assert result is not None
        assert 1.0 <= result <= 99.0

    def test_zero_depth_at_threshold_boundary(self) -> None:
        """Default threshold is applied correctly."""
        m = MicroPricing(min_depth_threshold=10)
        # exactly at threshold
        result = m.compute(best_bid=48, best_ask=52, bid_depth=5, ask_depth=5)
        assert result is not None

    def test_backward_compat_no_depth_args(self) -> None:
        """Old callers passing only bid/ask still work (depth defaults to 0 → thin → None)."""
        m = MicroPricing(min_depth_threshold=10)
        result = m.compute(best_bid=48, best_ask=52)
        # 0+0=0 < 10 → thin → None
        assert result is None


class TestThreeLayerFallsBackOnThinBook:
    def test_micro_none_redistributes_weight_to_anchor(self) -> None:
        """When micro returns None (thin book), its weight goes to anchor."""
        anchor = AnchorPricing(60)  # anchor price 60
        micro = MicroPricing(min_depth_threshold=1000)  # impossible to satisfy
        posterior = PosteriorPricing(alpha=1.0, beta=1.0)  # prior mean = 50

        # EXPLORATION weights: (0.6, 0.3, 0.1)
        # micro returns None → w_a becomes 0.6+0.3=0.9, w_p=0.1
        # expected = 0.9*60 + 0.1*50 = 54 + 5 = 59
        pricing = ThreeLayerPricing(anchor=anchor, micro=micro, posterior=posterior)
        result = pricing.compute(
            phase="EXPLORATION",
            anchor_price=60,
            best_bid=1,
            best_ask=2,
            recent_trades=[],
            bid_depth=0,
            ask_depth=0,
        )
        assert result == 59


# ---------------------------------------------------------------------------
# FIX 3: Dynamic Gamma Tier by Market Age
# ---------------------------------------------------------------------------

class TestDynamicGammaByAge:
    def _config_with_age(self, days: int) -> MarketConfig:
        creation = date.today() - timedelta(days=days)
        return MarketConfig(
            market_id="test",
            market_creation_date=creation.isoformat(),
        )

    def test_early_market_gets_gamma_01(self) -> None:
        engine = ASEngine()
        cfg = self._config_with_age(days=1)
        assert engine.get_gamma_for_age(cfg) == pytest.approx(0.1)

    def test_boundary_3days_is_early(self) -> None:
        engine = ASEngine()
        cfg = self._config_with_age(days=3)
        assert engine.get_gamma_for_age(cfg) == pytest.approx(0.1)

    def test_mid_market_4_days_gets_gamma_03(self) -> None:
        engine = ASEngine()
        cfg = self._config_with_age(days=4)
        assert engine.get_gamma_for_age(cfg) == pytest.approx(0.3)

    def test_mid_market_14_days_gets_gamma_03(self) -> None:
        engine = ASEngine()
        cfg = self._config_with_age(days=14)
        assert engine.get_gamma_for_age(cfg) == pytest.approx(0.3)

    def test_late_market_15_days_gets_gamma_08(self) -> None:
        engine = ASEngine()
        cfg = self._config_with_age(days=15)
        assert engine.get_gamma_for_age(cfg) == pytest.approx(0.8)

    def test_late_market_30_days_gets_gamma_08(self) -> None:
        engine = ASEngine()
        cfg = self._config_with_age(days=30)
        assert engine.get_gamma_for_age(cfg) == pytest.approx(0.8)

    def test_mature_market_31_days_gets_gamma_15(self) -> None:
        engine = ASEngine()
        cfg = self._config_with_age(days=31)
        assert engine.get_gamma_for_age(cfg) == pytest.approx(1.5)

    def test_none_creation_date_falls_back_to_static_config(self) -> None:
        """No market_creation_date → use gamma_tier from config."""
        engine = ASEngine()
        cfg = MarketConfig(market_id="test", gamma_tier="EARLY")
        assert cfg.market_creation_date is None
        # fallback: EARLY tier = 0.1
        assert engine.get_gamma_for_age(cfg) == pytest.approx(0.1)

    def test_none_creation_date_mid_tier_fallback(self) -> None:
        engine = ASEngine()
        cfg = MarketConfig(market_id="test", gamma_tier="MID")
        assert engine.get_gamma_for_age(cfg) == pytest.approx(0.3)

    def test_market_config_has_market_creation_date_field(self) -> None:
        """MarketConfig must have market_creation_date field defaulting to None."""
        cfg = MarketConfig(market_id="test")
        assert hasattr(cfg, "market_creation_date")
        assert cfg.market_creation_date is None


class TestASEngineSpreadRegression:
    def test_mid_50_gamma_03_produces_sane_quotes_without_boundary_clamp(self) -> None:
        """Regression: the spread should stay in a realistic cents range near mid=50."""
        engine = ASEngine()

        ask, bid = engine.compute_quotes(
            mid_price=50,
            inventory_skew=0.0,
            gamma=0.3,
            sigma=engine.bernoulli_sigma(50),
            tau_hours=24.0,
            kappa=1.5,
        )

        assert (ask, bid) != (99, 1)
        assert ask > 50
        assert bid < 50
        assert 3 <= (ask - bid) <= 10
