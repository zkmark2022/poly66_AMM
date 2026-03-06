"""Micro-structure price layer — volume-weighted mid with thin-book detection."""


class MicroPricing:
    def __init__(self, min_depth_threshold: int = 10) -> None:
        self._min_depth = min_depth_threshold

    def compute(
        self,
        best_bid: int,
        best_ask: int,
        bid_depth: int = 0,
        ask_depth: int = 0,
    ) -> float | None:
        total_depth = bid_depth + ask_depth
        if total_depth < self._min_depth:
            # Thin book: don't trust it, caller falls back to anchor
            return None
        # Volume-weighted mid
        vwmid = (best_bid * ask_depth + best_ask * bid_depth) / total_depth
        return max(1.0, min(99.0, vwmid))
