"""Micro-structure price layer — mid-price from best bid/ask."""


class MicroPricing:
    def compute(self, best_bid: int, best_ask: int) -> float:
        if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
            return 50.0
        return (best_bid + best_ask) / 2.0
