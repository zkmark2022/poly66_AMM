"""Graceful shutdown handler for AMM bot.

SIGTERM → cancel all markets (via OrderManager or API) → close API client → exit.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.amm.connector.api_client import AMMApiClient
from src.amm.models.market_context import MarketContext

if TYPE_CHECKING:
    from src.amm.connector.order_manager import OrderManager

logger = logging.getLogger(__name__)


class GracefulShutdown:
    def __init__(self, api: AMMApiClient) -> None:
        self._api = api

    async def execute(
        self,
        contexts: dict[str, MarketContext],
        order_managers: dict[str, OrderManager] | None = None,
    ) -> None:
        """Cancel all orders across all markets and shutdown cleanly.

        When order_managers is provided, delegates to order_mgr.cancel_all() so
        active_orders and OrderCache are properly cleared. Falls back to direct
        api.batch_cancel() for markets without a matching order_manager entry.
        """
        logger.info("AMM shutdown initiated — cancelling orders for %d markets",
                    len(contexts))

        for market_id, ctx in contexts.items():
            ctx.shutdown_requested = True
            try:
                if order_managers and market_id in order_managers:
                    await order_managers[market_id].cancel_all(market_id)
                else:
                    await self._api.batch_cancel(market_id, scope="ALL")
                logger.info("Cancelled all orders for market %s", market_id)
            except Exception as e:
                logger.error("Failed to cancel orders for %s: %s", market_id, e)

        await self._api.close()
        logger.info("AMM shutdown complete")
