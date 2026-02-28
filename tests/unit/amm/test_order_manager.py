"""Test OrderManager — smart order diffing and pending_sell tracking."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.amm.connector.order_manager import OrderManager
from src.amm.strategy.models import (
    OrderIntent, ActiveOrder, PlaceAction, ReplaceAction, CancelAction,
)
from src.amm.models.enums import QuoteAction


# ────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────

def _active(
    order_id: str,
    side: str = "YES",
    price_cents: int = 52,
    qty: int = 100,
    market_id: str = "mkt-1",
) -> ActiveOrder:
    return ActiveOrder(
        order_id=order_id,
        side=side,
        direction="SELL",
        price_cents=price_cents,
        remaining_quantity=qty,
        market_id=market_id,
    )


def _intent(
    side: str = "YES",
    price_cents: int = 52,
    qty: int = 100,
    action: QuoteAction = QuoteAction.PLACE,
    existing_order_id: str | None = None,
) -> OrderIntent:
    return OrderIntent(
        action=action,
        side=side,
        direction="SELL",
        price_cents=price_cents,
        quantity=qty,
        existing_order_id=existing_order_id,
    )


def _make_manager() -> tuple[OrderManager, AsyncMock, AsyncMock]:
    """Return (manager, mock_api, mock_cache)."""
    api = AsyncMock()
    cache = AsyncMock()
    # place_order returns a dict with new order id
    api.place_order.return_value = {"data": {"order": {"id": "new-1"}}}
    api.replace_order.return_value = {"data": {"order": {"id": "replaced-1"}}}
    api.cancel_order.return_value = {}
    api.batch_cancel.return_value = {}
    mgr = OrderManager(api=api, cache=cache)
    return mgr, api, cache


# ────────────────────────────────────────────────────────────────
# _compute_diff tests (pure logic, synchronous)
# ────────────────────────────────────────────────────────────────

class TestComputeDiff:
    def test_empty_active_all_places(self) -> None:
        mgr, _, _ = _make_manager()
        targets = [_intent(price_cents=52), _intent(side="NO", price_cents=48)]
        replaces, places, cancels = mgr._compute_diff({}, targets)
        assert len(places) == 2
        assert len(replaces) == 0
        assert len(cancels) == 0

    def test_exact_match_no_action(self) -> None:
        mgr, _, _ = _make_manager()
        active = {"ord-1": _active("ord-1", side="YES", price_cents=52, qty=100)}
        targets = [_intent(side="YES", price_cents=52, qty=100)]
        replaces, places, cancels = mgr._compute_diff(active, targets)
        assert len(replaces) == 0
        assert len(places) == 0
        assert len(cancels) == 0

    def test_price_change_triggers_replace(self) -> None:
        mgr, _, _ = _make_manager()
        active = {"ord-1": _active("ord-1", side="YES", price_cents=52, qty=100)}
        targets = [_intent(side="YES", price_cents=54, qty=100)]
        replaces, places, cancels = mgr._compute_diff(active, targets)
        assert len(replaces) == 1
        assert replaces[0].old_order_id == "ord-1"
        assert replaces[0].new_price_cents == 54

    def test_quantity_change_triggers_replace(self) -> None:
        mgr, _, _ = _make_manager()
        active = {"ord-1": _active("ord-1", side="YES", price_cents=52, qty=100)}
        targets = [_intent(side="YES", price_cents=52, qty=200)]
        replaces, places, cancels = mgr._compute_diff(active, targets)
        assert len(replaces) == 1
        assert replaces[0].new_quantity == 200

    def test_stale_active_gets_cancelled(self) -> None:
        mgr, _, _ = _make_manager()
        active = {
            "ord-1": _active("ord-1", side="YES", price_cents=52),
            "ord-2": _active("ord-2", side="NO", price_cents=48),
        }
        targets = [_intent(side="YES", price_cents=52)]  # NO order no longer wanted
        replaces, places, cancels = mgr._compute_diff(active, targets)
        assert len(cancels) == 1
        assert cancels[0].order_id == "ord-2"

    def test_empty_targets_cancels_all(self) -> None:
        mgr, _, _ = _make_manager()
        active = {
            "ord-1": _active("ord-1"),
            "ord-2": _active("ord-2", side="NO"),
        }
        replaces, places, cancels = mgr._compute_diff(active, [])
        assert len(cancels) == 2
        assert len(places) == 0
        assert len(replaces) == 0


# ────────────────────────────────────────────────────────────────
# get_pending_sells tests
# ────────────────────────────────────────────────────────────────

class TestGetPendingSells:
    def test_empty_active_orders(self) -> None:
        mgr, _, _ = _make_manager()
        yes_p, no_p = mgr.get_pending_sells()
        assert yes_p == 0
        assert no_p == 0

    def test_yes_pending(self) -> None:
        mgr, _, _ = _make_manager()
        mgr.active_orders["ord-1"] = _active("ord-1", side="YES", qty=150)
        yes_p, no_p = mgr.get_pending_sells()
        assert yes_p == 150
        assert no_p == 0

    def test_no_pending(self) -> None:
        mgr, _, _ = _make_manager()
        mgr.active_orders["ord-1"] = _active("ord-1", side="NO", qty=80)
        yes_p, no_p = mgr.get_pending_sells()
        assert yes_p == 0
        assert no_p == 80

    def test_mixed_pending(self) -> None:
        mgr, _, _ = _make_manager()
        mgr.active_orders["ord-1"] = _active("ord-1", side="YES", qty=100)
        mgr.active_orders["ord-2"] = _active("ord-2", side="YES", qty=50)
        mgr.active_orders["ord-3"] = _active("ord-3", side="NO", qty=200)
        yes_p, no_p = mgr.get_pending_sells()
        assert yes_p == 150
        assert no_p == 200


# ────────────────────────────────────────────────────────────────
# execute_intents + _sync_pending_sell tests (async)
# ────────────────────────────────────────────────────────────────

class TestExecuteIntents:
    async def test_place_new_order(self) -> None:
        mgr, api, cache = _make_manager()
        api.place_order.return_value = {"data": {"order": {"id": "new-ord-1"}}}
        intents = [_intent(side="YES", price_cents=52, qty=100)]
        await mgr.execute_intents(intents, market_id="mkt-1")
        api.place_order.assert_called_once()
        # pending_sell must be synced after execution
        cache.set_pending_sell.assert_called_once()

    async def test_cancel_stale_order(self) -> None:
        mgr, api, cache = _make_manager()
        mgr.active_orders["ord-1"] = _active("ord-1", side="YES", qty=100)
        # No intents — all active orders should be cancelled
        await mgr.execute_intents([], market_id="mkt-1")
        api.cancel_order.assert_called_once_with("ord-1")
        assert "ord-1" not in mgr.active_orders
        cache.set_pending_sell.assert_called()

    async def test_replace_existing_order(self) -> None:
        mgr, api, cache = _make_manager()
        mgr.active_orders["ord-1"] = _active("ord-1", side="YES", price_cents=52, qty=100)
        api.replace_order.return_value = {"data": {"order": {"id": "ord-2"}}}
        intents = [_intent(side="YES", price_cents=54, qty=100)]
        await mgr.execute_intents(intents, market_id="mkt-1")
        api.replace_order.assert_called_once()
        cache.set_pending_sell.assert_called()

    async def test_sync_pending_sell_called_after_each_change(self) -> None:
        """v1.0 Review Fix #4: pending_sell must be synced after EVERY order change."""
        mgr, api, cache = _make_manager()
        mgr.active_orders["ord-1"] = _active("ord-1", side="YES", qty=100)
        api.place_order.return_value = {"data": {"order": {"id": "new-1"}}}

        await mgr.execute_intents(
            [_intent(side="NO", price_cents=48, qty=80)],
            market_id="mkt-1",
        )
        # set_pending_sell must have been called
        assert cache.set_pending_sell.call_count >= 1

    async def test_cancel_all(self) -> None:
        mgr, api, cache = _make_manager()
        mgr.active_orders["ord-1"] = _active("ord-1", side="YES")
        mgr.active_orders["ord-2"] = _active("ord-2", side="NO")
        await mgr.cancel_all("mkt-1")
        api.batch_cancel.assert_called_once_with("mkt-1", scope="ALL")
        assert mgr.active_orders == {}
        cache.set_pending_sell.assert_called()

    async def test_active_orders_updated_after_place(self) -> None:
        mgr, api, cache = _make_manager()
        api.place_order.return_value = {"data": {"order": {"id": "ord-placed"}}}
        intents = [_intent(side="YES", price_cents=52, qty=100)]
        await mgr.execute_intents(intents, market_id="mkt-1")
        assert "ord-placed" in mgr.active_orders

    async def test_active_orders_removed_after_cancel(self) -> None:
        mgr, api, cache = _make_manager()
        mgr.active_orders["ord-1"] = _active("ord-1", side="YES")
        await mgr.execute_intents([], market_id="mkt-1")
        assert "ord-1" not in mgr.active_orders
