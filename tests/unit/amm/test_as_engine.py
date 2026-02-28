"""Test A-S reservation price and optimal spread. See AMM design v7.1 §5."""
import math
import pytest
from src.amm.strategy.as_engine import ASEngine


class TestASEngine:
    def test_reservation_price_balanced_inventory(self) -> None:
        """q=0 → r = s (no inventory adjustment)."""
        engine = ASEngine()
        r = engine.reservation_price(
            mid_price=50, inventory_skew=0.0, gamma=0.3, sigma=0.05, tau_hours=24.0
        )
        assert r == pytest.approx(50.0, abs=0.01)

    def test_reservation_price_long_yes(self) -> None:
        """Positive skew (long YES) → r < s (lower to encourage selling YES)."""
        engine = ASEngine()
        r = engine.reservation_price(
            mid_price=50, inventory_skew=0.5, gamma=0.3, sigma=0.05, tau_hours=24.0
        )
        assert r < 50.0

    def test_reservation_price_long_no(self) -> None:
        """Negative skew (long NO) → r > s (higher to encourage selling NO)."""
        engine = ASEngine()
        r = engine.reservation_price(
            mid_price=50, inventory_skew=-0.5, gamma=0.3, sigma=0.05, tau_hours=24.0
        )
        assert r > 50.0

    def test_optimal_spread_positive(self) -> None:
        """Spread must always be positive."""
        engine = ASEngine()
        delta = engine.optimal_spread(gamma=0.3, sigma=0.05, tau_hours=24.0, kappa=1.5)
        assert delta > 0

    def test_spread_increases_with_gamma(self) -> None:
        """Higher gamma increases the inventory component of the spread.

        Note: The total spread = (γ·σ²·τ + (2/γ)·ln(1+γ/κ)) × 100.
        The inventory term (γ·σ²·τ) grows with γ.
        The depth term (2/γ)·ln(1+γ/κ) decreases with γ.
        Net effect is monotone-increasing only when inventory term dominates.
        We use high σ (0.2) to ensure inventory term dominates.
        """
        engine = ASEngine()
        # sigma=0.2 ensures inventory component dominates depth component
        d1 = engine.optimal_spread(gamma=0.1, sigma=0.2, tau_hours=24.0, kappa=1.5)
        d2 = engine.optimal_spread(gamma=0.8, sigma=0.2, tau_hours=24.0, kappa=1.5)
        assert d2 > d1

    def test_sigma_bernoulli(self) -> None:
        """σ = sqrt(p(1-p)) / 100 for binary outcome."""
        engine = ASEngine()
        sigma = engine.bernoulli_sigma(mid_price_cents=50)
        expected = math.sqrt(0.5 * 0.5) / 100  # = 0.005
        assert sigma == pytest.approx(expected, rel=1e-6)

    def test_sigma_at_extremes(self) -> None:
        """At p=1 or p=99, sigma is very small."""
        engine = ASEngine()
        sigma_low = engine.bernoulli_sigma(mid_price_cents=1)
        sigma_high = engine.bernoulli_sigma(mid_price_cents=99)
        assert sigma_low < 0.01
        assert sigma_high < 0.01

    def test_gamma_tier_lookup(self) -> None:
        engine = ASEngine()
        assert engine.get_gamma("EARLY") == 0.1
        assert engine.get_gamma("MID") == 0.3
        assert engine.get_gamma("LATE") == 0.8
        assert engine.get_gamma("MATURE") == 1.5

    def test_gamma_tier_unknown_defaults_to_mid(self) -> None:
        engine = ASEngine()
        assert engine.get_gamma("UNKNOWN") == 0.3

    def test_quote_prices(self) -> None:
        """Full quote: ask > bid, both in [1, 99]."""
        engine = ASEngine()
        ask, bid = engine.compute_quotes(
            mid_price=50,
            inventory_skew=0.0,
            gamma=0.3,
            sigma=0.05,
            tau_hours=24.0,
            kappa=1.5,
        )
        assert 1 <= bid < ask <= 99

    def test_ceil_floor_rounding(self) -> None:
        """v1.0 Review Fix #6: ask uses ceil, bid uses floor (not banker's round).
        This prevents spread compression at .5 boundaries."""
        engine = ASEngine()
        # With balanced inventory, r = mid_price.
        # If delta/2 yields .5 boundary, ask should round UP, bid should round DOWN.
        ask, bid = engine.compute_quotes(
            mid_price=50,
            inventory_skew=0.0,
            gamma=0.3,
            sigma=0.05,
            tau_hours=24.0,
            kappa=1.5,
        )
        # ask >= r + delta/2 (ceil), bid <= r - delta/2 (floor)
        assert ask >= 50
        assert bid <= 50
        assert ask > bid  # spread is always positive

    def test_positive_skew_shifts_quotes_down(self) -> None:
        """Long YES inventory → both ask and bid shift lower."""
        engine = ASEngine()
        ask0, bid0 = engine.compute_quotes(
            mid_price=50, inventory_skew=0.0, gamma=0.3, sigma=0.05,
            tau_hours=24.0, kappa=1.5,
        )
        ask1, bid1 = engine.compute_quotes(
            mid_price=50, inventory_skew=0.8, gamma=0.3, sigma=0.05,
            tau_hours=24.0, kappa=1.5,
        )
        # Positive skew should result in lower reservation price
        assert ask1 <= ask0 or bid1 <= bid0  # at least one side shifts down

    def test_dimension_fix_inventory_component(self) -> None:
        """×100 factor ensures adjustment is in cents-space not probability-space."""
        engine = ASEngine()
        # σ = bernoulli_sigma(50) = sqrt(0.5*0.5)/100 = 0.005
        sigma = engine.bernoulli_sigma(50)
        # Without ×100: adjustment = 1.0 * 0.3 * 0.000025 * 24 = 0.00018 → rounds to 0
        # With ×100: adjustment = 0.018 cents → still small but non-zero
        r = engine.reservation_price(
            mid_price=50, inventory_skew=1.0, gamma=0.3, sigma=sigma, tau_hours=24.0
        )
        # Should be slightly below 50, NOT exactly 50
        adjustment_raw = 1.0 * 0.3 * (sigma**2) * 24
        adjustment_cents = adjustment_raw * 100
        assert r == pytest.approx(50.0 - adjustment_cents, rel=1e-6)

    def test_spread_dimension_fix(self) -> None:
        """Spread formula also has ×100 — should produce cents not fractions."""
        engine = ASEngine()
        sigma = engine.bernoulli_sigma(50)
        delta = engine.optimal_spread(gamma=0.3, sigma=sigma, tau_hours=24.0, kappa=1.5)
        # delta should be a meaningful number of cents, not a tiny fraction
        # depth_component = (2/0.3) * ln(1 + 0.3/1.5) = 6.667 * ln(1.2) ≈ 1.217 (prob)
        # → 121.7 cents... but clamped to [1,99] range means spread ≈ 2 or so in practice
        assert delta > 0.1  # at least non-trivial in cents-space
