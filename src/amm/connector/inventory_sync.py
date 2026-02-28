"""Full inventory reconciliation from matching engine API → Redis.

Used on cold-start and periodic reconcile cycles (default: every 5 min).
Unlike TradePoller (incremental deltas), InventorySync writes the ground-truth
snapshot from the API, resetting any accumulated drift.
"""
import logging

from src.amm.cache.inventory_cache import InventoryCache
from src.amm.connector.api_client import AMMApiClient
from src.amm.models.inventory import Inventory

logger = logging.getLogger(__name__)


class InventorySync:
    def __init__(self, api: AMMApiClient, cache: InventoryCache) -> None:
        self._api = api
        self._cache = cache

    async def reconcile(self, market_id: str) -> Inventory:
        """Fetch positions + balance from API and write a fresh snapshot to Redis.

        Returns the newly written Inventory.

        Position fields populated from API:
        - yes_volume, no_volume  (from /positions/{market_id})
        - cash_cents              (from /account/balance)
        - cost fields set to 0 on reconcile (tracked incrementally by TradePoller)
        - pending fields reset to 0 (order manager repopulates from open orders)
        """
        balance_data = await self._api.get_balance()
        positions_data = await self._api.get_positions(market_id)

        cash_cents = int(balance_data.get("cash_cents", 0))
        yes_volume = int(positions_data.get("yes_volume", 0))
        no_volume = int(positions_data.get("no_volume", 0))

        # Try to preserve existing cost sums if already cached (avoid wiping PnL tracking)
        existing = await self._cache.get(market_id)
        yes_cost = existing.yes_cost_sum_cents if existing else 0
        no_cost = existing.no_cost_sum_cents if existing else 0
        frozen = existing.frozen_balance_cents if existing else 0

        inv = Inventory(
            cash_cents=cash_cents,
            yes_volume=yes_volume,
            no_volume=no_volume,
            yes_cost_sum_cents=yes_cost,
            no_cost_sum_cents=no_cost,
            yes_pending_sell=0,
            no_pending_sell=0,
            frozen_balance_cents=frozen,
        )
        await self._cache.set(market_id, inv)
        logger.info(
            "InventorySync.reconcile(%s): cash=%d yes=%d no=%d",
            market_id,
            cash_cents,
            yes_volume,
            no_volume,
        )
        return inv
