"""Posterior pricing layer — Layer 3 of three-layer pricing.

Bayesian update from trade flow: prior = anchor/micro, likelihood = recent trades.
In STABILIZATION phase this gets higher weight (0.3 vs 0.1) to capture
market-implied probability movements.
"""


class PosteriorPricing:
    def compute(self, recent_trades: list[dict], fallback: float) -> float:
        """Compute posterior price estimate from recent trades.

        Uses VWAP of recent trades as a simple Bayesian update.
        Falls back to the anchor/micro price if no trades available.
        """
        if not recent_trades:
            return fallback

        total_value = sum(t["price_cents"] * t["quantity"] for t in recent_trades)
        total_qty = sum(t["quantity"] for t in recent_trades)
        if total_qty == 0:
            return fallback

        return total_value / total_qty
