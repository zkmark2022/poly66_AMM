"""R2D fix tests — lifecycle + cache layer correctness.

Tests:
  1. test_cash_not_shared_across_markets         — 3 markets each get 1/3 of total cash
  2. test_winding_down_idempotency_key_stable    — retry uses same key (session_id based)
  3. test_reinvest_idempotency_key_stable        — same balance → same key
  4. test_order_cache_loaded_on_restart          — load_from_cache restores active_orders
  5. test_health_binds_localhost                 — default host is 127.0.0.1
  6. test_coerce_tuple_from_string               — "0.6,0.3,0.1" → (0.6, 0.3, 0.1)
  7. test_winding_down_blocks_new_orders         — winding_down=True set + shutdown_requested
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.amm.config.loader import _coerce
from src.amm.config.models import MarketConfig
from src.amm.lifecycle.initializer import AMMInitializer
from src.amm.lifecycle.reconciler import AMMReconciler
from src.amm.lifecycle.reinvest import maybe_auto_reinvest
from src.amm.lifecycle.winding_down import handle_winding_down
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext
from src.amm.models.enums import Phase, DefenseLevel


# ─── helpers ──────────────────────────────────────────────────────────────────

def _inv(cash: int = 300_000, yes: int = 1000, no: int = 1000) -> Inventory:
    return Inventory(
        cash_cents=cash,
        yes_volume=yes,
        no_volume=no,
        yes_cost_sum_cents=yes * 50,
        no_cost_sum_cents=no * 50,
        yes_pending_sell=0,
        no_pending_sell=0,
        frozen_balance_cents=0,
    )


def _ctx(market_id: str = "mkt-1", cash: int = 300_000) -> MarketContext:
    cfg = MarketConfig(
        market_id=market_id,
        auto_reinvest_enabled=True,
    )
    return MarketContext(
        market_id=market_id,
        config=cfg,
        inventory=_inv(cash),
    )


# ─── FIX 1: cash allocation ───────────────────────────────────────────────────

class TestCashAllocation:
    async def test_cash_not_shared_across_markets(self) -> None:
        """3 markets: each should get total_cash // 3 as allocated_cash_cents."""
        total_cash = 900_000  # $9000

        api = AsyncMock()
        api.get_balance.return_value = {
            "data": {"balance_cents": total_cash, "frozen_balance_cents": 0}
        }
        api.get_positions.return_value = {
            "data": {
                "yes_volume": 500, "no_volume": 500,
                "yes_cost_sum_cents": 25000, "no_cost_sum_cents": 25000,
            }
        }
        api.get_market.return_value = {"data": {"id": "mkt-x", "status": "ACTIVE"}}

        cache = AsyncMock()
        loader = AsyncMock()
        from src.amm.config.models import GlobalConfig
        loader.load_global.return_value = GlobalConfig()
        loader.load_market.side_effect = lambda mid: MarketConfig(market_id=mid)

        init = AMMInitializer(
            token_manager=AsyncMock(),
            api=api,
            config_loader=loader,
            inventory_cache=cache,
        )
        contexts = await init.initialize(["mkt-1", "mkt-2", "mkt-3"])

        for market_id, ctx in contexts.items():
            assert ctx.inventory.allocated_cash_cents == total_cash // 3, (
                f"Market {market_id}: expected {total_cash // 3}, "
                f"got {ctx.inventory.allocated_cash_cents}"
            )

    async def test_reconciler_allocates_cash_per_market(self) -> None:
        """Reconciler should divide total cash by number of markets."""
        total_cash = 600_000

        api = AsyncMock()
        api.get_balance.return_value = {
            "data": {"balance_cents": total_cash, "frozen_balance_cents": 0}
        }
        api.get_positions.return_value = {
            "data": {
                "yes_volume": 1000, "no_volume": 1000,
                "yes_cost_sum_cents": 50000, "no_cost_sum_cents": 50000,
            }
        }

        # Cache returns same values → no drift → update not called normally,
        # but we need drift to trigger update. Force drift by using different
        # cached values.
        cached_inv = _inv(cash=total_cash, yes=999, no=999)  # drift in volume
        inv_cache = AsyncMock()
        inv_cache.get.return_value = cached_inv

        reconciler = AMMReconciler(api=api, inventory_cache=inv_cache)
        await reconciler.reconcile(["mkt-a", "mkt-b"])

        # Each market should have allocated_cash_cents = total_cash // 2
        calls = inv_cache.set.call_args_list
        assert len(calls) == 2
        for call in calls:
            written_inv: Inventory = call[0][1]
            assert written_inv.allocated_cash_cents == total_cash // 2


# ─── FIX 2: winding_down deterministic key ────────────────────────────────────

class TestWindingDownIdempotencyKey:
    async def test_winding_down_idempotency_key_stable_on_retry(self) -> None:
        """Two calls with same ctx should use same idempotency key (no time.time())."""
        ctx = _ctx("mkt-1")
        ctx.inventory.yes_volume = 500
        ctx.inventory.no_volume = 500

        api = AsyncMock()
        api.burn.return_value = {"data": {}}

        # First call
        await handle_winding_down(ctx, api, "RESOLVED")

        first_key = api.burn.call_args[0][2]
        assert "mkt-1" in first_key
        # The key must NOT be time-based — reset and call again with same quantity
        # Use a fresh api to capture second call's key

        # Reset inventory (simulate retry scenario with same session_id)
        ctx.inventory.yes_volume = 500
        ctx.inventory.no_volume = 500
        ctx.shutdown_requested = False

        api2 = AsyncMock()
        api2.burn.return_value = {"data": {}}
        await handle_winding_down(ctx, api2, "RESOLVED")

        second_key = api2.burn.call_args[0][2]
        # Same session_id + quantity → same key
        assert first_key == second_key, (
            f"Keys differ across retries: {first_key!r} vs {second_key!r}"
        )

    async def test_winding_down_sets_winding_down_flag(self) -> None:
        """handle_winding_down must set ctx.winding_down = True."""
        ctx = _ctx("mkt-1")
        ctx.inventory.yes_volume = 500
        ctx.inventory.no_volume = 500

        api = AsyncMock()
        api.burn.return_value = {"data": {}}

        assert not getattr(ctx, "winding_down", False)
        await handle_winding_down(ctx, api, "RESOLVED")
        assert ctx.winding_down is True


# ─── FIX 3: reinvest deterministic key ───────────────────────────────────────

class TestReinvestIdempotencyKey:
    async def test_reinvest_idempotency_key_stable_same_balance(self) -> None:
        """Same cash_cents → same idempotency key across two calls."""
        ctx = _ctx("mkt-2", cash=200_000)
        ctx.config.auto_reinvest_enabled = True

        api1 = AsyncMock()
        api1.mint.return_value = {"data": {}}
        await maybe_auto_reinvest(ctx, api1, threshold_cents=100_000)
        first_key = api1.mint.call_args[0][2]

        # Reset inventory
        ctx2 = _ctx("mkt-2", cash=200_000)
        ctx2.config.auto_reinvest_enabled = True
        api2 = AsyncMock()
        api2.mint.return_value = {"data": {}}
        await maybe_auto_reinvest(ctx2, api2, threshold_cents=100_000)
        second_key = api2.mint.call_args[0][2]

        assert first_key == second_key, (
            f"Reinvest keys differ for same balance: {first_key!r} vs {second_key!r}"
        )

    async def test_reinvest_idempotency_key_differs_with_different_balance(self) -> None:
        """Different cash → different key (confirms key is balance-dependent)."""
        ctx1 = _ctx("mkt-2", cash=200_000)
        ctx1.config.auto_reinvest_enabled = True
        api1 = AsyncMock()
        api1.mint.return_value = {"data": {}}
        await maybe_auto_reinvest(ctx1, api1, threshold_cents=100_000)
        key1 = api1.mint.call_args[0][2]

        ctx2 = _ctx("mkt-2", cash=300_000)
        ctx2.config.auto_reinvest_enabled = True
        api2 = AsyncMock()
        api2.mint.return_value = {"data": {}}
        await maybe_auto_reinvest(ctx2, api2, threshold_cents=100_000)
        key2 = api2.mint.call_args[0][2]

        assert key1 != key2


# ─── FIX 4: OrderCache in OrderManager ───────────────────────────────────────

class TestOrderCacheIntegration:
    async def test_order_cache_loaded_on_restart(self) -> None:
        """load_from_cache() should populate active_orders from Redis."""
        from src.amm.connector.order_manager import OrderManager, ActiveOrder
        from src.amm.cache.order_cache import OrderCache

        order_cache = AsyncMock(spec=OrderCache)
        order_cache.get_all_orders.return_value = {
            "ord-abc": {
                "order_id": "ord-abc",
                "side": "YES",
                "direction": "SELL",
                "price_cents": 55,
                "remaining_quantity": 100,
            },
            "ord-def": {
                "order_id": "ord-def",
                "side": "NO",
                "direction": "SELL",
                "price_cents": 45,
                "remaining_quantity": 200,
            },
        }

        api = AsyncMock()
        inv_cache = AsyncMock()
        mgr = OrderManager(api=api, cache=inv_cache, order_cache=order_cache)

        await mgr.load_from_cache("mkt-1")

        assert "ord-abc" in mgr.active_orders
        assert "ord-def" in mgr.active_orders
        assert mgr.active_orders["ord-abc"].price_cents == 55
        assert mgr.active_orders["ord-def"].remaining_quantity == 200

    async def test_place_order_writes_to_cache(self) -> None:
        """Successful place_order must persist to order_cache."""
        from src.amm.connector.order_manager import OrderManager
        from src.amm.cache.order_cache import OrderCache
        from src.amm.strategy.models import OrderIntent, QuoteAction

        order_cache = AsyncMock(spec=OrderCache)
        api = AsyncMock()
        api.place_order.return_value = {"data": {"order_id": "ord-new"}}
        inv_cache = AsyncMock()
        inv_cache.mark_order_submission.return_value = True

        mgr = OrderManager(api=api, cache=inv_cache, order_cache=order_cache)
        intent = OrderIntent(
            side="YES",
            direction="SELL",
            price_cents=60,
            quantity=50,
            action=QuoteAction.PLACE,
        )
        await mgr._place_intent(intent, "mkt-1")

        order_cache.set_order.assert_called_once()
        call_args = order_cache.set_order.call_args[0]
        assert call_args[0] == "mkt-1"
        assert call_args[1] == "ord-new"

    async def test_cancel_all_clears_cache(self) -> None:
        """cancel_all must also clear the order_cache for that market."""
        from src.amm.connector.order_manager import OrderManager
        from src.amm.cache.order_cache import OrderCache

        order_cache = AsyncMock(spec=OrderCache)
        api = AsyncMock()
        api.batch_cancel.return_value = {"data": {}}
        inv_cache = AsyncMock()

        mgr = OrderManager(api=api, cache=inv_cache, order_cache=order_cache)
        await mgr.cancel_all("mkt-1")

        order_cache.clear.assert_called_once_with("mkt-1")


# ─── FIX 5: health server binds localhost ─────────────────────────────────────

class TestHealthServerBinding:
    async def test_health_binds_localhost_by_default(self) -> None:
        """run_health_server must bind to 127.0.0.1 (not 0.0.0.0) by default."""
        from src.amm.lifecycle.health import run_health_server, HealthState

        state = HealthState()
        captured_configs = []

        class FakeServer:
            def __init__(self, config: object) -> None:
                captured_configs.append(config)

            async def serve(self) -> None:
                pass  # immediately return

        with patch("src.amm.lifecycle.health.uvicorn.Server", FakeServer), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AMM_HEALTH_HOST", None)
            await run_health_server(state)

        assert len(captured_configs) == 1
        config = captured_configs[0]
        assert config.host == "127.0.0.1", (
            f"Expected 127.0.0.1, got {config.host!r}"
        )

    async def test_health_respects_env_override(self) -> None:
        """AMM_HEALTH_HOST env var must override the default binding."""
        from src.amm.lifecycle.health import run_health_server, HealthState

        state = HealthState()
        captured_configs = []

        class FakeServer:
            def __init__(self, config: object) -> None:
                captured_configs.append(config)

            async def serve(self) -> None:
                pass

        with patch("src.amm.lifecycle.health.uvicorn.Server", FakeServer), \
             patch.dict(os.environ, {"AMM_HEALTH_HOST": "0.0.0.0"}):
            await run_health_server(state)

        assert captured_configs[0].host == "0.0.0.0"


# ─── FIX 6: _coerce tuple/list ────────────────────────────────────────────────

class TestCoerceTuple:
    def test_coerce_tuple_from_csv_string(self) -> None:
        """_coerce must parse '0.6,0.3,0.1' into (0.6, 0.3, 0.1) for tuple[float,...]."""
        result = _coerce(tuple[float, float, float], "0.6,0.3,0.1")
        assert result == (0.6, 0.3, 0.1)
        assert isinstance(result, tuple)

    def test_coerce_list_from_csv_string(self) -> None:
        """_coerce must parse '1,2,3' into [1, 2, 3] for list[int]."""
        result = _coerce(list[int], "1,2,3")
        assert result == [1, 2, 3]
        assert isinstance(result, list)

    def test_coerce_optional_float(self) -> None:
        """_coerce must handle Optional[float] → float."""
        from typing import Optional
        result = _coerce(Optional[float], "1.5")
        assert result == 1.5

    def test_coerce_bool(self) -> None:
        result = _coerce(bool, "true")
        assert result is True

    def test_coerce_phase_weights_exploration_field(self) -> None:
        """phase_weights_exploration must be parsed correctly from a Redis string."""
        import dataclasses
        fields = {f.name: f.type for f in dataclasses.fields(MarketConfig)}
        field_type = fields["phase_weights_exploration"]
        result = _coerce(field_type, "0.6,0.3,0.1")
        assert result == (0.6, 0.3, 0.1)


# ─── FIX 7: WINDING_DOWN flag on MarketContext ────────────────────────────────

class TestWindingDownFlag:
    async def test_winding_down_blocks_new_orders_via_flag(self) -> None:
        """handle_winding_down sets ctx.winding_down = True; new orders must not be placed."""
        ctx = _ctx("mkt-1")
        ctx.inventory.yes_volume = 200
        ctx.inventory.no_volume = 200

        api = AsyncMock()
        api.burn.return_value = {"data": {}}
        order_mgr = AsyncMock()

        await handle_winding_down(ctx, api, "RESOLVED", order_mgr=order_mgr)

        assert ctx.winding_down is True
        assert ctx.shutdown_requested is True
        # cancel_all was called during winding_down
        order_mgr.cancel_all.assert_called_once_with("mkt-1")

    def test_market_context_has_winding_down_field(self) -> None:
        """MarketContext must have winding_down: bool = False by default."""
        ctx = _ctx()
        assert hasattr(ctx, "winding_down")
        assert ctx.winding_down is False

    def test_market_context_has_winding_down_session_id(self) -> None:
        """MarketContext must have winding_down_session_id field (UUID string)."""
        import uuid
        ctx = _ctx()
        assert hasattr(ctx, "winding_down_session_id")
        # Verify it's a valid UUID
        uuid.UUID(ctx.winding_down_session_id)

    def test_winding_down_session_id_stable_across_retries(self) -> None:
        """session_id does not change — it's set once at context creation."""
        ctx = _ctx()
        first_id = ctx.winding_down_session_id
        # Simulate multiple accesses — must be same value
        assert ctx.winding_down_session_id == first_id
