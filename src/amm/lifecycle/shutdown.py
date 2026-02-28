"""Graceful shutdown: batch_cancel all orders, then exit cleanly."""
import logging

from src.amm.connector.api_client import AMMApiClient
from src.amm.models.market_context import MarketContext

logger = logging.getLogger(__name__)


class GracefulShutdown:
    """Handles SIGTERM: cancel all open orders across all markets."""

    def __init__(self, api: AMMApiClient) -> None:
        self._api = api

    async def execute(self, contexts: dict[str, MarketContext]) -> None:
        """Cancel all orders across all markets and shutdown cleanly.

        Continues even if cancellation fails for individual markets —
        best-effort shutdown is safer than aborting midway.
        """
        logger.info("AMM shutdown initiated — cancelling orders for %d market(s)...", len(contexts))

        cancel_errors: list[str] = []
        for market_id in contexts:
            try:
                await self._api.batch_cancel(market_id, scope="ALL")
                logger.info("Cancelled all orders for market %s", market_id)
            except Exception as exc:
                logger.error(
                    "Failed to cancel orders for market %s: %s", market_id, exc
                )
                cancel_errors.append(market_id)

        if cancel_errors:
            logger.warning(
                "Shutdown completed with %d failed cancellations: %s",
                len(cancel_errors),
                cancel_errors,
            )
        else:
            logger.info("AMM shutdown complete — all orders cancelled")

        await self._api.close()
