"""Winding-down lifecycle helpers for market end handling."""
from __future__ import annotations

from typing import Any

from src.amm.connector.api_client import AMMApiClient
from src.amm.models.market_context import MarketContext

PAIR_COST_CENTS = 100
_FINAL_MARKET_STATES = {"RESOLVED", "SETTLED", "VOIDED"}


async def handle_winding_down(
    ctx: MarketContext,
    api: AMMApiClient,
    market_status: str,
    order_mgr: Any | None = None,
) -> int:
    """Stop quoting and burn all burnable YES/NO pairs when market ends.

    Returns burned pair count.
    """
    if market_status not in _FINAL_MARKET_STATES:
        return 0

    ctx.winding_down = True

    if order_mgr is not None:
        await order_mgr.cancel_all(ctx.market_id)

    quantity = min(ctx.inventory.yes_available, ctx.inventory.no_available)
    if quantity <= 0:
        ctx.shutdown_requested = True
        return 0

    idempotency_key = (
        f"winding_down_{ctx.market_id}_{quantity}_{ctx.winding_down_session_id}"
    )
    await api.burn(ctx.market_id, quantity, idempotency_key)

    ctx.inventory.yes_volume -= quantity
    ctx.inventory.no_volume -= quantity
    ctx.inventory.cash_cents += quantity * PAIR_COST_CENTS
    ctx.inventory.yes_cost_sum_cents = max(0, ctx.inventory.yes_cost_sum_cents - quantity * 50)
    ctx.inventory.no_cost_sum_cents = max(0, ctx.inventory.no_cost_sum_cents - quantity * 50)
    ctx.inventory.yes_pending_sell = 0
    ctx.inventory.no_pending_sell = 0
    ctx.shutdown_requested = True
    return quantity
