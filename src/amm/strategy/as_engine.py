"""Avellaneda-Stoikov pricing model adapted for prediction markets."""
import math

from src.amm.config.models import GAMMA_TIERS
from src.amm.utils.integer_math import clamp


class ASEngine:
    def reservation_price(
        self, mid_price: int | float, inventory_skew: float,
        gamma: float, sigma: float, tau_hours: float,
    ) -> float:
        """r = s - q · γ · σ² · τ(h) × 100"""
        adjustment = inventory_skew * gamma * (sigma ** 2) * tau_hours
        return mid_price - (adjustment * 100)

    def optimal_spread(
        self, gamma: float, sigma: float, tau_hours: float, kappa: float,
    ) -> float:
        """δ = (γ · σ² · τ(h) + (2/γ) · ln(1 + γ/κ)) × 100"""
        inventory_component = gamma * (sigma ** 2) * tau_hours
        depth_component = (2.0 / gamma) * math.log(1.0 + gamma / kappa)
        return (inventory_component + depth_component) * 100

    def bernoulli_sigma(self, mid_price_cents: int) -> float:
        """σ = sqrt(p(1-p)) / 100 for binary prediction market."""
        p = mid_price_cents / 100.0
        p = max(0.01, min(0.99, p))
        return math.sqrt(p * (1 - p)) / 100.0

    def get_gamma(self, tier: str) -> float:
        return GAMMA_TIERS.get(tier, 0.3)

    def compute_quotes(
        self, mid_price: int, inventory_skew: float,
        gamma: float, sigma: float, tau_hours: float, kappa: float,
    ) -> tuple[int, int]:
        """Compute ask and bid prices. Returns (ask_cents, bid_cents)."""
        r = self.reservation_price(mid_price, inventory_skew, gamma, sigma, tau_hours)
        delta = self.optimal_spread(gamma, sigma, tau_hours, kappa)

        ask_raw = r + delta / 2
        bid_raw = r - delta / 2

        ask = clamp(math.ceil(ask_raw), 1, 99)
        bid = clamp(math.floor(bid_raw), 1, 99)

        if ask <= bid:
            ask = min(bid + 1, 99)

        return ask, bid
