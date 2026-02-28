"""Three-layer pricing engine. See AMM design v7.1 §3.

Layer 1: Anchor price (admin-set or initial probability)
Layer 2: Micro-structure price (mid-price, VWAP, anti-spoofing)
Layer 3: Posterior learning (Bayesian update from trade flow)

Weights shift from anchor-dominant (EXPLORATION) to micro-dominant (STABILIZATION).
"""
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.pricing.micro import MicroPricing
from src.amm.strategy.pricing.posterior import PosteriorPricing
from src.amm.utils.integer_math import clamp

# Phase-dependent weights: (anchor, micro, posterior)
PHASE_WEIGHTS: dict[str, tuple[float, float, float]] = {
    "EXPLORATION": (0.6, 0.3, 0.1),
    "STABILIZATION": (0.2, 0.5, 0.3),
}


class ThreeLayerPricing:
    def __init__(
        self,
        anchor: AnchorPricing,
        micro: MicroPricing,
        posterior: PosteriorPricing,
    ):
        self._anchor = anchor
        self._micro = micro
        self._posterior = posterior

    def compute(
        self,
        phase: str,
        anchor_price: int,
        best_bid: int,
        best_ask: int,
        recent_trades: list[dict],
    ) -> int:
        """Compute mid-price as weighted combination of three layers.

        Returns integer cents in [1, 99].
        """
        w_a, w_m, w_p = PHASE_WEIGHTS.get(phase, PHASE_WEIGHTS["EXPLORATION"])

        p_anchor = self._anchor.compute(anchor_price)
        p_micro = self._micro.compute(best_bid, best_ask)
        p_posterior = self._posterior.compute(recent_trades, fallback=p_anchor)

        raw = w_a * p_anchor + w_m * p_micro + w_p * p_posterior
        return clamp(round(raw), 1, 99)
