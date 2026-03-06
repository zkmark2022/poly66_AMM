"""Anchor price layer — admin-set or initial probability."""


class AnchorPricing:
    def __init__(self, initial_price: int = 50) -> None:
        self._price = initial_price

    def compute(self, anchor_price: int | None = None) -> float:
        return float(anchor_price if anchor_price is not None else self._price)
