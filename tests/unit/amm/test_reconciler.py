"""Tests for AMMReconciler — periodic Redis vs DB reconciliation."""
from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest

from src.amm.lifecycle.reconciler import AMMReconciler
from src.amm.models.inventory import Inventory


def _make_inventory(
    cash: int = 500_000,
    yes: int = 1000,
    no: int = 1000,
) -> Inventory:
    return Inventory(
        cash_cents=cash, yes_volume=yes, no_volume=no,
        yes_cost_sum_cents=yes * 50, no_cost_sum_cents=no * 50,
        yes_pending_sell=0, no_pending_sell=0, frozen_balance_cents=0,
    )


def _make_api(
    balance: dict | None = None,
    positions: dict | None = None,
) -> AsyncMock:
    api = AsyncMock()
    api.get_balance.return_value = balance or {
        "data": {"balance_cents": 500_000, "frozen_balance_cents": 0}
    }
    api.get_positions.return_value = positions or {
        "data": {"yes_volume": 1000, "no_volume": 1000,
                 "yes_cost_sum_cents": 50000, "no_cost_sum_cents": 50000}
    }
    return api


def _make_cache(inventory: Inventory | None = None) -> AsyncMock:
    cache = AsyncMock()
    cache.get.return_value = inventory or _make_inventory()
    return cache


class TestAMMReconciler:
    async def test_reconcile_fetches_db_state(self) -> None:
        api = _make_api()
        reconciler = AMMReconciler(api=api, inventory_cache=_make_cache())
        await reconciler.reconcile(["mkt-1"])
        api.get_balance.assert_called_once()
        api.get_positions.assert_called_once_with("mkt-1")

    async def test_reconcile_updates_cache_when_drift_detected(self) -> None:
        # Redis has stale yes_volume=800, DB says 1000
        stale = _make_inventory(yes=800)
        cache = _make_cache(inventory=stale)
        api = _make_api(positions={
            "data": {"yes_volume": 1000, "no_volume": 1000,
                     "yes_cost_sum_cents": 50000, "no_cost_sum_cents": 50000}
        })
        reconciler = AMMReconciler(api=api, inventory_cache=cache)
        await reconciler.reconcile(["mkt-1"])
        cache.set.assert_called_once()

    async def test_reconcile_no_update_when_in_sync(self) -> None:
        cache = _make_cache(inventory=_make_inventory())
        api = _make_api()
        reconciler = AMMReconciler(api=api, inventory_cache=cache)
        await reconciler.reconcile(["mkt-1"])
        cache.set.assert_not_called()

    async def test_reconcile_multiple_markets(self) -> None:
        api = _make_api()
        cache = _make_cache()
        reconciler = AMMReconciler(api=api, inventory_cache=cache)
        await reconciler.reconcile(["mkt-1", "mkt-2"])
        assert api.get_positions.call_count == 2

    async def test_reconcile_handles_missing_redis_key(self) -> None:
        cache = _make_cache()
        cache.get.return_value = None  # Redis key missing
        api = _make_api()
        reconciler = AMMReconciler(api=api, inventory_cache=cache)
        # Should write DB truth to Redis without raising
        await reconciler.reconcile(["mkt-1"])
        cache.set.assert_called_once()

    async def test_reconcile_returns_drift_summary(self) -> None:
        stale = _make_inventory(yes=800)
        cache = _make_cache(inventory=stale)
        api = _make_api(positions={
            "data": {"yes_volume": 1000, "no_volume": 1000,
                     "yes_cost_sum_cents": 50000, "no_cost_sum_cents": 50000}
        })
        reconciler = AMMReconciler(api=api, inventory_cache=cache)
        result = await reconciler.reconcile(["mkt-1"])
        assert "mkt-1" in result
        assert result["mkt-1"]["drifted"] is True

    async def test_reconcile_cash_drift_triggers_update(self) -> None:
        stale = _make_inventory(cash=400_000)  # Redis says 400k
        cache = _make_cache(inventory=stale)
        api = _make_api(balance={
            "data": {"balance_cents": 500_000, "frozen_balance_cents": 0}
        })
        reconciler = AMMReconciler(api=api, inventory_cache=cache)
        await reconciler.reconcile(["mkt-1"])
        cache.set.assert_called_once()

    async def test_reconcile_frozen_balance_drift_triggers_update(self) -> None:
        # frozen_balance_cents must be included in drift detection
        stale = _make_inventory()  # frozen_balance_cents=0
        cache = _make_cache(inventory=stale)
        api = _make_api(balance={
            "data": {"balance_cents": 500_000, "frozen_balance_cents": 10_000}
        })
        reconciler = AMMReconciler(api=api, inventory_cache=cache)
        result = await reconciler.reconcile(["mkt-1"])
        assert result["mkt-1"]["drifted"] is True
        assert "frozen_balance_cents" in result["mkt-1"]["fields"]
        cache.set.assert_called_once()
