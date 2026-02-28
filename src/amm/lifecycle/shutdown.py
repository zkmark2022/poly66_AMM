"""Graceful shutdown handler for AMM bot.

SIGTERM → batch_cancel all markets → close API client → exit.
"""
import logging

from src.amm.connector.api_client import AMMApiClient
from src.amm.models.market_context import MarketContext

logger = logging.getLogger(__name__)


class GracefulShutdown:
    def __init__(self, api: AMMApiClient) -> None:
        self._api = api

    async def execute(self, contexts: dict[str, MarketContext]) -> None:
        """Cancel all orders across all markets and shutdown cleanly."""
        logger.info("AMM shutdown initiated — cancelling orders for %d markets",
                    len(contexts))

        for market_id, ctx in contexts.items():
            ctx.shutdown_requested = True
            try:
                await self._api.batch_cancel(market_id, scope="ALL")
                logger.info("Cancelled all orders for market %s", market_id)
            except Exception as e:
                logger.error("Failed to cancel orders for %s: %s", market_id, e)

        await self._api.close()
        logger.info("AMM shutdown complete")
