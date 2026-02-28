"""Gradient ladder engine. Generates multi-level ask/bid order intents.

v1.0 Review Fix #3 — CRITICAL MAPPING RULE:
In the single-orderbook architecture, AMM ONLY issues SELL orders:
  - Ask ladder → SELL YES (direct: ask price = sell price)
  - Bid ladder → SELL NO  (mapped: bid_yes @ P  →  sell_no @ 100-P)

This is because AMM holds dual inventory (YES + NO) from Mint.
Issuing BUY YES would freeze cash instead of utilizing existing NO shares.
See interface contract v1.4 §3.1 and AMM design v7.1 §6.
"""
from src.amm.config.models import MarketConfig
from src.amm.models.enums import QuoteAction
from src.amm.strategy.models import OrderIntent
from src.amm.utils.integer_math import clamp


class GradientEngine:
    def build_ask_ladder(
        self,
        base_ask: int,
        config: MarketConfig,
        base_qty: int,
    ) -> list[OrderIntent]:
        """Build ask (SELL YES) ladder ascending from base_ask."""
        intents = []
        qty = float(base_qty)
        for level in range(config.gradient_levels):
            price = base_ask + level * config.gradient_price_step_cents
            if price > 99:
                break
            intents.append(
                OrderIntent(
                    action=QuoteAction.PLACE,
                    side="YES",
                    direction="SELL",
                    price_cents=clamp(price, 1, 99),
                    quantity=max(1, int(qty)),
                    reason=f"ask_L{level}",
                )
            )
            qty *= config.gradient_quantity_decay
        return intents

    def build_bid_ladder(
        self,
        base_bid: int,
        config: MarketConfig,
        base_qty: int,
    ) -> list[OrderIntent]:
        """Build bid ladder as SELL NO orders (mapped from bid YES prices).

        Mapping: Bid YES @ P  →  Sell NO @ (100 - P)
        Prices descend from base_bid, so NO prices ascend from (100 - base_bid).
        """
        intents = []
        qty = float(base_qty)
        for level in range(config.gradient_levels):
            bid_price = base_bid - level * config.gradient_price_step_cents
            no_price = 100 - bid_price  # complement mapping
            if no_price > 99 or no_price < 1:
                break
            intents.append(
                OrderIntent(
                    action=QuoteAction.PLACE,
                    side="NO",  # SELL NO, not BUY YES
                    direction="SELL",  # always SELL
                    price_cents=clamp(no_price, 1, 99),
                    quantity=max(1, int(qty)),
                    reason=f"bid_L{level}(mapped_sell_no)",
                )
            )
            qty *= config.gradient_quantity_decay
        return intents
