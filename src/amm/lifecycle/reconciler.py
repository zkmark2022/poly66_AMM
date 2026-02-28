"""Periodic Redis vs DB reconciliation for AMM inventory.

Runs every 5 minutes (default) to detect and correct drift between
the Redis cache (fast path) and the DB truth (via REST API).
"""
from __future__ import annotations

import logging

from src.amm.connector.api_client import AMMApiClient
from src.amm.cache.inventory_cache import InventoryCache
from src.amm.models.inventory import Inventory

logger = logging.getLogger(__name__)

# Drift threshold: flag if any field differs by more than this fraction
_DRIFT_THRESHOLD = 0


class AMMReconciler:
    def __init__(self, api: AMMApiClient, inventory_cache: InventoryCache) -> None:
        self._api = api
        self._cache = inventory_cache

    async def reconcile(self, market_ids: list[str]) -> dict[str, dict]:
        """Full-state reconciliation for all markets.

        Fetches DB truth via API and compares with Redis cache.
        Updates Redis if drift is detected.

        Returns dict[market_id → {"drifted": bool, "fields": list[str]}].
        """
        balance_resp = await self._api.get_balance()
        bal = balance_resp.get("data", {})
        cash_cents = int(bal.get("balance_cents", 0))
        frozen_cents = int(bal.get("frozen_balance_cents", 0))

        results: dict[str, dict] = {}

        for market_id in market_ids:
            pos_resp = await self._api.get_positions(market_id)
            pos = pos_resp.get("data", {})

            db_truth = Inventory(
                cash_cents=cash_cents,
                yes_volume=int(pos.get("yes_volume", 0)),
                no_volume=int(pos.get("no_volume", 0)),
                yes_cost_sum_cents=int(pos.get("yes_cost_sum_cents", 0)),
                no_cost_sum_cents=int(pos.get("no_cost_sum_cents", 0)),
                yes_pending_sell=0,
                no_pending_sell=0,
                frozen_balance_cents=frozen_cents,
            )

            cached = await self._cache.get(market_id)

            drifted_fields = self._detect_drift(cached, db_truth)

            if drifted_fields:
                logger.warning(
                    "Drift detected for market %s — fields: %s. Correcting...",
                    market_id, drifted_fields,
                )
                # Preserve pending_sell from cache (not tracked in DB)
                if cached is not None:
                    db_truth.yes_pending_sell = cached.yes_pending_sell
                    db_truth.no_pending_sell = cached.no_pending_sell
                await self._cache.set(market_id, db_truth)
                results[market_id] = {"drifted": True, "fields": drifted_fields}
            else:
                logger.debug("Market %s in sync", market_id)
                results[market_id] = {"drifted": False, "fields": []}

        return results

    def _detect_drift(
        self, cached: Inventory | None, db: Inventory,
    ) -> list[str]:
        """Return list of field names that have drifted."""
        if cached is None:
            return ["all"]  # Redis key missing entirely

        drifted = []
        fields = [
            ("cash_cents", cached.cash_cents, db.cash_cents),
            ("yes_volume", cached.yes_volume, db.yes_volume),
            ("no_volume", cached.no_volume, db.no_volume),
            ("yes_cost_sum_cents", cached.yes_cost_sum_cents, db.yes_cost_sum_cents),
            ("no_cost_sum_cents", cached.no_cost_sum_cents, db.no_cost_sum_cents),
        ]
        for name, cached_val, db_val in fields:
            if cached_val != db_val:
                drifted.append(name)

        return drifted
