"""Posterior price layer — Bayesian update from recent trade flow."""


class PosteriorPricing:
    def compute(self, recent_trades: list[dict], fallback: float = 50.0) -> float:
        if not recent_trades:
            return fallback
        total_qty = sum(t.get("quantity", 1) for t in recent_trades)
        if total_qty == 0:
            return fallback
        vwap = sum(t["price_cents"] * t.get("quantity", 1) for t in recent_trades) / total_qty
        return vwap
