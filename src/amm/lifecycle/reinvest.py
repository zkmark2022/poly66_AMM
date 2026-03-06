"""Auto-reinvest lifecycle helpers for AMM funds management."""
from __future__ import annotations

from typing import TYPE_CHECKING

from src.amm.connector.api_client import AMMApiClient
from src.amm.models.market_context import MarketContext
from src.amm.strategy.models import OrderIntent

if TYPE_CHECKING:
    from src.amm.cache.inventory_cache import InventoryCache

AUTO_REINVEST_THRESHOLD_CENTS = 50_000  # $500
PAIR_COST_CENTS = 100


async def maybe_auto_reinvest(
    ctx: MarketContext,
    api: AMMApiClient,
    inventory_cache: "InventoryCache | None" = None,
    threshold_cents: int = AUTO_REINVEST_THRESHOLD_CENTS,
) -> int:
    """Mint extra YES/NO pairs when cash exceeds threshold.

    Returns minted pair count.
    """
    if not ctx.config.auto_reinvest_enabled:
        return 0

    surplus = ctx.inventory.cash_cents - threshold_cents
    quantity = surplus // PAIR_COST_CENTS
    if quantity <= 0:
        return 0

    idempotency_key = f"reinvest_{ctx.market_id}_{quantity}_{ctx.inventory.cash_cents}"
    await api.mint(ctx.market_id, quantity, idempotency_key)

    ctx.inventory.cash_cents -= quantity * PAIR_COST_CENTS
    ctx.inventory.yes_volume += quantity
    ctx.inventory.no_volume += quantity
    # Keep reserve consistency: YES+NO cost sums increase by quantity * 100.
    ctx.inventory.yes_cost_sum_cents += quantity * 50
    ctx.inventory.no_cost_sum_cents += quantity * 50

    if inventory_cache is not None:
        await inventory_cache.set(ctx.market_id, ctx.inventory)

    return quantity


def drop_buy_side_intents_when_cash_depleted(
    intents: list[OrderIntent],
    cash_cents: int,
) -> list[OrderIntent]:
    """Remove synthetic BUY-side intents when no cash is available.

    In current quoting model, BUY-side intent is represented as SELL NO.
    """
    if cash_cents > 0:
        return intents
    return [
        intent
        for intent in intents
        if not (intent.side == "NO" and intent.direction == "SELL")
    ]
