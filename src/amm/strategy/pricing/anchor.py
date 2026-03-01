"""Anchor price layer — admin-set or initial probability."""


class AnchorPricing:
    def __init__(self, initial_price: int = 50) -> None:
        self._price = initial_price

    def compute(self, anchor_price: int) -> float:
        return float(anchor_price)
