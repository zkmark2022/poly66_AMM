"""Anchor price layer — Layer 1 of three-layer pricing.

The anchor price is the admin-set or initial probability estimate.
It provides a stable reference during EXPLORATION when microstructure
data is sparse and unreliable.
"""


class AnchorPricing:
    def __init__(self, initial_price: int = 50):
        self._initial_price = initial_price

    def compute(self, anchor_price: int) -> float:
        """Return anchor price as float for weighted combination."""
        return float(anchor_price)
