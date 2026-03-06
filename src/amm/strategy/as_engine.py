"""Avellaneda-Stoikov pricing model adapted for prediction markets."""
from __future__ import annotations

import logging
import math
from datetime import date as _date
from typing import TYPE_CHECKING

from src.amm.config.models import GAMMA_TIERS
from src.amm.utils.integer_math import clamp

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.amm.config.models import MarketConfig


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

    def get_gamma_for_age(self, config: MarketConfig) -> float:
        """Return gamma based on market age (days since creation)."""
        if config.market_creation_date is None:
            return config.gamma  # fallback to static config
        try:
            created = _date.fromisoformat(config.market_creation_date)
        except ValueError:
            logger.warning(
                "Invalid market_creation_date %r, falling back to static gamma",
                config.market_creation_date,
            )
            return config.gamma
        age_days = (_date.today() - created).days
        # Negative age (future creation date) is treated as EARLY
        if age_days <= 3:
            return GAMMA_TIERS["EARLY"]
        elif age_days <= 14:
            return GAMMA_TIERS["MID"]
        elif age_days <= 30:
            return GAMMA_TIERS["LATE"]
        else:
            return GAMMA_TIERS["MATURE"]

    def compute_quotes(
        self, mid_price: int, inventory_skew: float,
        gamma: float, sigma: float, tau_hours: float, kappa: float,
        spread_min_cents: int = 2, spread_max_cents: int = 20,
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

        if spread_min_cents > spread_max_cents:
            logger.warning(
                "spread_min_cents=%d > spread_max_cents=%d; ignoring constraints",
                spread_min_cents, spread_max_cents,
            )
        else:
            mid_r = round(r)

            # Enforce minimum spread
            if ask - bid < spread_min_cents:
                half = spread_min_cents // 2
                ask = clamp(mid_r + (spread_min_cents - half), 1, 99)
                bid = clamp(mid_r - half, 1, 99)

            # Enforce maximum spread
            if ask - bid > spread_max_cents:
                half = spread_max_cents // 2
                ask = clamp(mid_r + (spread_max_cents - half), 1, 99)
                bid = clamp(mid_r - half, 1, 99)

            # Re-check after boundary clamping
            if ask <= bid:
                ask = min(bid + 1, 99)

        return ask, bid
