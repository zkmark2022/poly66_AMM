"""Avellaneda-Stoikov pricing model adapted for prediction markets.

See AMM design v7.1 §5:
  r = s - q · γ · σ² · τ(h) × 100
  δ = (γ · σ² · τ(h) + (2/γ) · ln(1 + γ/κ)) × 100

Key adaptations for prediction markets:
- σ uses Bernoulli: σ = sqrt(p(1-p)) / 100 (binary outcome)
- τ is absolute hours remaining (not fraction of day)
- γ is lifecycle-stratified (EARLY/MID/LATE/MATURE)
- All final prices clamped to [1, 99] integer cents
"""
import math

from src.amm.config.models import GAMMA_TIERS
from src.amm.utils.integer_math import clamp


class ASEngine:
    def reservation_price(
        self,
        mid_price: float,
        inventory_skew: float,
        gamma: float,
        sigma: float,
        tau_hours: float,
    ) -> float:
        """r = s - q · γ · σ² · τ(h) × 100

        CRITICAL DIMENSION NOTE (v1.0 Review Fix #1):
        mid_price is in cents [1, 99] (= probability × 100).
        σ = sqrt(p(1-p)) / 100, so σ² ≈ 0.000025 (probability-space).
        The adjustment term q·γ·σ²·τ is in probability-space [0, 1].
        We must multiply by 100 to convert to cents-space, matching mid_price.

        Example: mid=50, skew=0.5, γ=0.3, σ=0.005, τ=24
          adjustment = 0.5 * 0.3 * 0.000025 * 24 = 0.00009 (probability)
          adjustment_cents = 0.00009 * 100 = 0.009 cents — still small,
          but with σ=0.05 (high vol): 0.5 * 0.3 * 0.0025 * 24 * 100 = 0.9 cents.
        Without ×100, the adjustment would be 0.009 → rounds to 0 → NO inventory control.
        """
        adjustment = inventory_skew * gamma * (sigma**2) * tau_hours
        return mid_price - (adjustment * 100)  # ×100: probability→cents conversion

    def optimal_spread(
        self,
        gamma: float,
        sigma: float,
        tau_hours: float,
        kappa: float,
    ) -> float:
        """δ = (γ · σ² · τ(h) + (2/γ) · ln(1 + γ/κ)) × 100

        Same dimension fix as reservation_price: both terms are in
        probability-space, multiply by 100 to get cents-space spread.
        """
        inventory_component = gamma * (sigma**2) * tau_hours
        depth_component = (2.0 / gamma) * math.log(1.0 + gamma / kappa)
        return (inventory_component + depth_component) * 100  # ×100: prob→cents

    def bernoulli_sigma(self, mid_price_cents: int) -> float:
        """σ = sqrt(p(1-p)) / 100 for binary prediction market."""
        p = mid_price_cents / 100.0
        p = max(0.01, min(0.99, p))  # avoid zero variance
        return math.sqrt(p * (1 - p)) / 100.0

    def get_gamma(self, tier: str) -> float:
        return GAMMA_TIERS.get(tier, 0.3)

    def compute_quotes(
        self,
        mid_price: int,
        inventory_skew: float,
        gamma: float,
        sigma: float,
        tau_hours: float,
        kappa: float,
    ) -> tuple[int, int]:
        """Compute ask and bid prices. Returns (ask_cents, bid_cents).

        v1.0 Review Fix #6: Use math.ceil/floor instead of round().
        Python's round() uses banker's rounding (round-half-to-even):
          round(2.5) = 2 (NOT 3!)
        For market making, ask must round UP (ceil) and bid must round DOWN
        (floor) to ensure spread is never accidentally compressed.
        """
        r = self.reservation_price(mid_price, inventory_skew, gamma, sigma, tau_hours)
        delta = self.optimal_spread(gamma, sigma, tau_hours, kappa)

        ask_raw = r + delta / 2
        bid_raw = r - delta / 2

        # CRITICAL: ceil for ask (push outward), floor for bid (push outward)
        ask = clamp(math.ceil(ask_raw), 1, 99)
        bid = clamp(math.floor(bid_raw), 1, 99)

        # Ensure positive spread
        if ask <= bid:
            ask = min(bid + 1, 99)

        return ask, bid
