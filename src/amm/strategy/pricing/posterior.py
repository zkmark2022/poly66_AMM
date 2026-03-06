"""Posterior price layer — Bayesian Beta-Binomial conjugate model."""


class PosteriorPricing:
    def __init__(self, alpha: float = 1.0, beta: float = 1.0, decay: float = 0.95) -> None:
        self._alpha = alpha
        self._beta = beta
        self._decay = decay

    def update(self, trades: list[dict]) -> None:
        """Update Beta distribution from recent trades."""
        # Decay toward uniform prior
        self._alpha = max(1.0, self._alpha * self._decay)
        self._beta = max(1.0, self._beta * self._decay)
        for trade in trades:
            # YES trade = buyer of YES = probability goes up
            if trade.get("scenario") in ("TRANSFER_YES", "MINT"):
                qty = trade.get("quantity", 0)
                if not isinstance(qty, (int, float)) or qty < 0:
                    continue
                self._alpha += qty / 100.0
            elif trade.get("scenario") in ("TRANSFER_NO",):
                qty = trade.get("quantity", 0)
                if not isinstance(qty, (int, float)) or qty < 0:
                    continue
                self._beta += qty / 100.0

    def compute(self, trades: list[dict] | None = None) -> float:
        """Return posterior mean price in cents [1, 99]."""
        if trades:
            self.update(trades)
        denom = self._alpha + self._beta
        mean = self._alpha / denom if denom > 0 else 0.5
        return max(1.0, min(99.0, mean * 100.0))
