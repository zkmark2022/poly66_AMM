"""Micro-structure price layer — Layer 2 of three-layer pricing.

Computes mid-price and VWAP from order book and recent trade data.
Anti-spoofing: uses mid of best bid/ask rather than best-bid-only or
best-ask-only to resist quote stuffing.
"""


class MicroPricing:
    def compute(self, best_bid: int, best_ask: int) -> float:
        """Compute mid-price from best bid/ask.

        Falls back to average if one side is missing (0 = no quote).
        """
        if best_bid <= 0 and best_ask <= 0:
            return 50.0
        if best_bid <= 0:
            return float(best_ask)
        if best_ask <= 0:
            return float(best_bid)
        return (best_bid + best_ask) / 2.0

    def vwap(self, trades: list[dict]) -> float | None:
        """Volume-weighted average price from recent trades.

        Returns None if no trades available.
        """
        if not trades:
            return None
        total_value = sum(t["price_cents"] * t["quantity"] for t in trades)
        total_qty = sum(t["quantity"] for t in trades)
        if total_qty == 0:
            return None
        return total_value / total_qty
