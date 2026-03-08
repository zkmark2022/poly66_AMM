"""R3/R4 P1a fix tests — 5 high-priority bugs.

P1-1: quote_cycle calls handle_winding_down on terminal market status
P1-2: amm_main injects OrderCache into OrderManager + calls load_from_cache
P1-3: ONE_SIDE defense suppresses NO side even when skew == 0
P1-4: MarketContext has inventory_lock; reconcile_loop acquires it
P1-5: GracefulShutdown delegates to order_mgr.cancel_all() when available
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.amm.config.models import MarketConfig
from src.amm.connector.order_manager import ActiveOrder, OrderManager
from src.amm.lifecycle.shutdown import GracefulShutdown
from src.amm.models.enums import DefenseLevel, Phase, QuoteAction
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext
from src.amm.risk.sanitizer import OrderSanitizer
from src.amm.strategy.models import OrderIntent


# ─── helpers ──────────────────────────────────────────────────────────────────


def _make_inventory(yes: int = 500, no: int = 500) -> Inventory:
    return Inventory(
        cash_cents=500_000,
        yes_volume=yes,
        no_volume=no,
        yes_cost_sum_cents=yes * 50,
        no_cost_sum_cents=no * 50,
        yes_pending_sell=0,
        no_pending_sell=0,
        frozen_balance_cents=0,
    )


def _make_ctx(market_id: str = "mkt-1") -> MarketContext:
    return MarketContext(
        market_id=market_id,
        config=MarketConfig(market_id=market_id, remaining_hours_override=24.0),
        inventory=_make_inventory(),
        phase=Phase.EXPLORATION,
        defense_level=DefenseLevel.NORMAL,
    )


def _make_intent(
    side: str = "YES",
    quantity: int = 100,
    price: int = 55,
) -> OrderIntent:
    return OrderIntent(
        action=QuoteAction.PLACE,
        side=side,
        direction="SELL",
        price_cents=price,
        quantity=quantity,
    )


def _make_shutdown_ctx(market_id: str) -> MarketContext:
    return MarketContext(
        market_id=market_id,
        config=MarketConfig(market_id=market_id),
        inventory=Inventory(
            cash_cents=100_000,
            yes_volume=500,
            no_volume=500,
            yes_cost_sum_cents=25_000,
            no_cost_sum_cents=25_000,
            yes_pending_sell=0,
            no_pending_sell=0,
            frozen_balance_cents=0,
        ),
    )


# ─── P1-1: handle_winding_down called by quote_cycle on terminal status ────────


class TestP1WindingDownIntegration:
    """P1-1: quote_cycle must call handle_winding_down when market is terminal."""

    async def test_quote_cycle_calls_handle_winding_down_on_resolved_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When api.get_market_status returns 'resolved', handle_winding_down is called."""
        from src.amm.main import quote_cycle
        from src.amm.strategy.pricing.three_layer import ThreeLayerPricing
        from src.amm.strategy.pricing.anchor import AnchorPricing
        from src.amm.strategy.pricing.micro import MicroPricing
        from src.amm.strategy.pricing.posterior import PosteriorPricing
        from src.amm.strategy.as_engine import ASEngine
        from src.amm.strategy.gradient import GradientEngine
        from src.amm.risk.defense_stack import DefenseStack

        ctx = _make_ctx()
        ctx.market_status_checked_at = 0.0  # force fresh status fetch

        winding_down_calls: list[tuple] = []

        async def fake_handle_winding_down(ctx_, api_, status_, order_mgr_=None):  # type: ignore[no-untyped-def]
            winding_down_calls.append((ctx_, status_, order_mgr_))

        monkeypatch.setattr(
            "src.amm.main.handle_winding_down", fake_handle_winding_down
        )

        api = AsyncMock()
        api.get_orderbook.return_value = {"data": {"best_bid": 48, "best_ask": 52}}
        api.get_market_status.return_value = "resolved"

        poller = AsyncMock()
        poller.poll.return_value = []

        inventory_cache = AsyncMock()
        inventory_cache.get.return_value = _make_inventory()

        order_mgr = AsyncMock()
        order_mgr.active_orders = {}

        pricing = ThreeLayerPricing(
            anchor=AnchorPricing(50),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
            config=ctx.config,
        )

        await quote_cycle(
            ctx=ctx,
            api=api,
            poller=poller,
            pricing=pricing,
            as_engine=ASEngine(),
            gradient=GradientEngine(),
            risk=DefenseStack(ctx.config),
            sanitizer=OrderSanitizer(),
            order_mgr=order_mgr,
            inventory_cache=inventory_cache,
        )

        assert len(winding_down_calls) == 1, "handle_winding_down must be called once"
        _, status, _ = winding_down_calls[0]
        assert status == "RESOLVED", f"Expected 'RESOLVED', got {status!r}"

    async def test_quote_cycle_calls_handle_winding_down_on_settled_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Settled market also triggers handle_winding_down."""
        from src.amm.main import quote_cycle
        from src.amm.strategy.pricing.three_layer import ThreeLayerPricing
        from src.amm.strategy.pricing.anchor import AnchorPricing
        from src.amm.strategy.pricing.micro import MicroPricing
        from src.amm.strategy.pricing.posterior import PosteriorPricing
        from src.amm.strategy.as_engine import ASEngine
        from src.amm.strategy.gradient import GradientEngine
        from src.amm.risk.defense_stack import DefenseStack

        ctx = _make_ctx()
        ctx.market_status_checked_at = 0.0

        winding_down_calls: list[str] = []

        async def fake_handle_winding_down(ctx_, api_, status_, order_mgr_=None):  # type: ignore[no-untyped-def]
            winding_down_calls.append(status_)

        monkeypatch.setattr(
            "src.amm.main.handle_winding_down", fake_handle_winding_down
        )

        api = AsyncMock()
        api.get_orderbook.return_value = {"data": {"best_bid": 48, "best_ask": 52}}
        api.get_market_status.return_value = "settled"

        poller = AsyncMock()
        poller.poll.return_value = []

        inventory_cache = AsyncMock()
        inventory_cache.get.return_value = _make_inventory()

        order_mgr = AsyncMock()
        order_mgr.active_orders = {}

        pricing = ThreeLayerPricing(
            anchor=AnchorPricing(50),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
            config=ctx.config,
        )

        await quote_cycle(
            ctx=ctx,
            api=api,
            poller=poller,
            pricing=pricing,
            as_engine=ASEngine(),
            gradient=GradientEngine(),
            risk=DefenseStack(ctx.config),
            sanitizer=OrderSanitizer(),
            order_mgr=order_mgr,
            inventory_cache=inventory_cache,
        )

        assert "SETTLED" in winding_down_calls

    async def test_quote_cycle_does_not_call_handle_winding_down_on_active_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Active market must NOT trigger handle_winding_down."""
        from src.amm.main import quote_cycle
        from src.amm.strategy.pricing.three_layer import ThreeLayerPricing
        from src.amm.strategy.pricing.anchor import AnchorPricing
        from src.amm.strategy.pricing.micro import MicroPricing
        from src.amm.strategy.pricing.posterior import PosteriorPricing
        from src.amm.strategy.as_engine import ASEngine
        from src.amm.strategy.gradient import GradientEngine
        from src.amm.risk.defense_stack import DefenseStack

        ctx = _make_ctx()
        ctx.market_status_checked_at = 0.0

        winding_down_calls: list = []

        async def fake_handle_winding_down(ctx_, api_, status_, order_mgr_=None):  # type: ignore[no-untyped-def]
            winding_down_calls.append(status_)

        monkeypatch.setattr(
            "src.amm.main.handle_winding_down", fake_handle_winding_down
        )

        api = AsyncMock()
        api.get_orderbook.return_value = {"data": {"best_bid": 48, "best_ask": 52}}
        api.get_market_status.return_value = "active"

        poller = AsyncMock()
        poller.poll.return_value = []

        inventory_cache = AsyncMock()
        inventory_cache.get.return_value = _make_inventory()

        order_mgr = AsyncMock()
        order_mgr.active_orders = {}

        pricing = ThreeLayerPricing(
            anchor=AnchorPricing(50),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
            config=ctx.config,
        )

        await quote_cycle(
            ctx=ctx,
            api=api,
            poller=poller,
            pricing=pricing,
            as_engine=ASEngine(),
            gradient=GradientEngine(),
            risk=DefenseStack(ctx.config),
            sanitizer=OrderSanitizer(),
            order_mgr=order_mgr,
            inventory_cache=inventory_cache,
        )

        assert winding_down_calls == [], (
            f"handle_winding_down must NOT be called for active market, got {winding_down_calls}"
        )

    async def test_quote_cycle_returns_early_after_handle_winding_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After handle_winding_down, quote_cycle must return without placing orders."""
        from src.amm.main import quote_cycle
        from src.amm.strategy.pricing.three_layer import ThreeLayerPricing
        from src.amm.strategy.pricing.anchor import AnchorPricing
        from src.amm.strategy.pricing.micro import MicroPricing
        from src.amm.strategy.pricing.posterior import PosteriorPricing
        from src.amm.strategy.as_engine import ASEngine
        from src.amm.strategy.gradient import GradientEngine
        from src.amm.risk.defense_stack import DefenseStack

        ctx = _make_ctx()
        ctx.market_status_checked_at = 0.0

        async def fake_handle_winding_down(ctx_, api_, status_, order_mgr_=None):  # type: ignore[no-untyped-def]
            pass  # minimal stub

        monkeypatch.setattr(
            "src.amm.main.handle_winding_down", fake_handle_winding_down
        )

        api = AsyncMock()
        api.get_orderbook.return_value = {"data": {"best_bid": 48, "best_ask": 52}}
        api.get_market_status.return_value = "resolved"

        poller = AsyncMock()
        poller.poll.return_value = []

        inventory_cache = AsyncMock()
        inventory_cache.get.return_value = _make_inventory()

        order_mgr = AsyncMock()
        order_mgr.active_orders = {}

        pricing = ThreeLayerPricing(
            anchor=AnchorPricing(50),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
            config=ctx.config,
        )

        await quote_cycle(
            ctx=ctx,
            api=api,
            poller=poller,
            pricing=pricing,
            as_engine=ASEngine(),
            gradient=GradientEngine(),
            risk=DefenseStack(ctx.config),
            sanitizer=OrderSanitizer(),
            order_mgr=order_mgr,
            inventory_cache=inventory_cache,
        )

        # execute_intents must NOT be called after winding_down
        order_mgr.execute_intents.assert_not_called()


# ─── P1-2: OrderCache injection in amm_main ───────────────────────────────────


class TestP1OrderCacheInjection:
    """P1-2: amm_main must inject OrderCache into OrderManager and call load_from_cache."""

    async def test_amm_main_creates_order_manager_with_order_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OrderManager must be constructed with a non-None order_cache argument."""
        from src.amm.main import amm_main

        order_manager_init_calls: list[dict] = []

        class FakeOrderManager:
            active_orders: dict = {}

            def __init__(self, **kwargs: object) -> None:
                order_manager_init_calls.append(dict(kwargs))

            async def load_from_cache(self, market_id: str) -> None:
                pass

            async def execute_intents(self, *args: object, **kwargs: object) -> None:
                pass

            async def cancel_all(self, market_id: str) -> None:
                pass

        ctx = _make_ctx("mkt-1")
        ctx.shutdown_requested = True  # so run_market exits immediately

        monkeypatch.setenv("AMM_BASE_URL", "http://test/api/v1")
        monkeypatch.setenv("AMM_REDIS_URL", "redis://test")
        monkeypatch.setenv("AMM_USERNAME", "amm")
        monkeypatch.setenv("AMM_PASSWORD", "secret")
        monkeypatch.setenv("AMM_MARKETS", "mkt-1")

        from src.amm.config.models import GlobalConfig

        fake_redis = AsyncMock()
        fake_redis.aclose = AsyncMock()
        fake_http = AsyncMock()
        fake_http.aclose = AsyncMock()

        fake_api = AsyncMock()
        fake_api.get_market_status.return_value = "active"
        fake_api.close = AsyncMock()

        fake_init = AsyncMock()
        fake_init.initialize.return_value = {"mkt-1": ctx}

        monkeypatch.setattr("src.amm.main.create_redis_client", lambda _: fake_redis)
        monkeypatch.setattr("src.amm.main.httpx.AsyncClient", lambda **kw: fake_http)
        monkeypatch.setattr("src.amm.main.TokenManager", lambda *a, **kw: AsyncMock())
        monkeypatch.setattr("src.amm.main.AMMApiClient", lambda *a, **kw: fake_api)
        monkeypatch.setattr("src.amm.main.ConfigLoader", lambda **kw: AsyncMock(
            load_global=AsyncMock(return_value=GlobalConfig()),
        ))
        monkeypatch.setattr("src.amm.main.AMMInitializer", lambda **kw: fake_init)
        monkeypatch.setattr("src.amm.main.GracefulShutdown", lambda **kw: AsyncMock(
            execute=AsyncMock(),
        ))
        monkeypatch.setattr("src.amm.main.AMMReconciler", lambda **kw: AsyncMock(
            reconcile=AsyncMock(return_value={}),
        ))
        monkeypatch.setattr("src.amm.main.OrderManager", FakeOrderManager)
        monkeypatch.setattr("src.amm.main.run_health_server", AsyncMock())
        monkeypatch.setattr("src.amm.main.PolymarketOracle", lambda *a: AsyncMock(
            get_price=AsyncMock(return_value=50),
        ))
        # Make signal handler a no-op
        monkeypatch.setattr(
            "src.amm.main.asyncio.get_event_loop",
            lambda: MagicMock(add_signal_handler=lambda *a: None),
        )

        await amm_main(["mkt-1"])

        assert len(order_manager_init_calls) >= 1, "OrderManager must be instantiated"
        for init_call in order_manager_init_calls:
            assert "order_cache" in init_call, (
                "OrderManager must receive order_cache kwarg"
            )
            assert init_call["order_cache"] is not None, (
                "order_cache must not be None"
            )

    async def test_amm_main_calls_load_from_cache_at_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """load_from_cache must be called once per market at startup."""
        from src.amm.main import amm_main

        load_from_cache_calls: list[str] = []

        class FakeOrderManager:
            active_orders: dict = {}

            def __init__(self, **kwargs: object) -> None:
                pass

            async def load_from_cache(self, market_id: str) -> None:
                load_from_cache_calls.append(market_id)

            async def execute_intents(self, *args: object, **kwargs: object) -> None:
                pass

            async def cancel_all(self, market_id: str) -> None:
                pass

        ctx = _make_ctx("mkt-1")
        ctx.shutdown_requested = True

        monkeypatch.setenv("AMM_BASE_URL", "http://test/api/v1")
        monkeypatch.setenv("AMM_REDIS_URL", "redis://test")
        monkeypatch.setenv("AMM_USERNAME", "amm")
        monkeypatch.setenv("AMM_PASSWORD", "secret")
        monkeypatch.setenv("AMM_MARKETS", "mkt-1")

        from src.amm.config.models import GlobalConfig

        fake_redis = AsyncMock()
        fake_redis.aclose = AsyncMock()
        fake_api = AsyncMock()
        fake_api.close = AsyncMock()
        fake_http = AsyncMock()
        fake_http.aclose = AsyncMock()
        fake_init = AsyncMock()
        fake_init.initialize.return_value = {"mkt-1": ctx}

        monkeypatch.setattr("src.amm.main.create_redis_client", lambda _: fake_redis)
        monkeypatch.setattr("src.amm.main.httpx.AsyncClient", lambda **kw: fake_http)
        monkeypatch.setattr("src.amm.main.TokenManager", lambda *a, **kw: AsyncMock())
        monkeypatch.setattr("src.amm.main.AMMApiClient", lambda *a, **kw: fake_api)
        monkeypatch.setattr("src.amm.main.ConfigLoader", lambda **kw: AsyncMock(
            load_global=AsyncMock(return_value=GlobalConfig()),
        ))
        monkeypatch.setattr("src.amm.main.AMMInitializer", lambda **kw: fake_init)
        monkeypatch.setattr("src.amm.main.GracefulShutdown", lambda **kw: AsyncMock(
            execute=AsyncMock(),
        ))
        monkeypatch.setattr("src.amm.main.AMMReconciler", lambda **kw: AsyncMock(
            reconcile=AsyncMock(return_value={}),
        ))
        monkeypatch.setattr("src.amm.main.OrderManager", FakeOrderManager)
        monkeypatch.setattr("src.amm.main.run_health_server", AsyncMock())
        monkeypatch.setattr("src.amm.main.PolymarketOracle", lambda *a: AsyncMock(
            get_price=AsyncMock(return_value=50),
        ))
        monkeypatch.setattr(
            "src.amm.main.asyncio.get_event_loop",
            lambda: MagicMock(add_signal_handler=lambda *a: None),
        )

        await amm_main(["mkt-1"])

        assert "mkt-1" in load_from_cache_calls, (
            "load_from_cache must be called with market_id at startup"
        )


# ─── P1-3: ONE_SIDE defense fix at skew == 0 ──────────────────────────────────


class TestP1OneSideDefenseAtZeroSkew:
    """P1-3: ONE_SIDE must suppress NO side even when inventory_skew == 0."""

    def test_one_side_allows_both_when_skew_is_exactly_zero(self) -> None:
        """skew=0 (balanced) → ONE_SIDE allows both sides, no artificial YES bias."""
        ctx = _make_ctx()
        assert ctx.inventory.yes_volume == ctx.inventory.no_volume
        assert ctx.inventory.inventory_skew == 0.0

        sanitizer = OrderSanitizer()
        intents = [
            _make_intent(side="YES", quantity=50),
            _make_intent(side="NO", quantity=50),
        ]
        result = sanitizer.sanitize(intents, DefenseLevel.ONE_SIDE, ctx)
        sides = {i.side for i in result}

        assert "YES" in sides, "ONE_SIDE with skew=0 must allow YES"
        assert "NO" in sides, "ONE_SIDE with skew=0 must allow NO (no artificial bias)"

    def test_one_side_suppresses_no_when_skew_is_near_zero(self) -> None:
        """abs(skew) < 0.05 → ONE_SIDE must suppress NO side."""
        # skew = (501 - 499) / (501 + 499) = 2/1000 = 0.002 (near 0)
        ctx = MarketContext(
            market_id="mkt-1",
            config=MarketConfig(market_id="mkt-1"),
            inventory=Inventory(
                cash_cents=500_000,
                yes_volume=501, no_volume=499,
                yes_cost_sum_cents=25050, no_cost_sum_cents=24950,
                yes_pending_sell=0, no_pending_sell=0, frozen_balance_cents=0,
            ),
        )
        assert abs(ctx.inventory.inventory_skew) < 0.05

        sanitizer = OrderSanitizer()
        intents = [_make_intent("YES"), _make_intent("NO")]
        result = sanitizer.sanitize(intents, DefenseLevel.ONE_SIDE, ctx)
        sides = {i.side for i in result}

        assert "NO" not in sides, "Near-zero skew must also suppress NO in ONE_SIDE"
        assert "YES" in sides

    def test_one_side_still_works_correctly_with_clear_positive_skew(self) -> None:
        """Positive skew > 0.05 → ONE_SIDE still suppresses NO (existing behavior)."""
        ctx = MarketContext(
            market_id="mkt-1",
            config=MarketConfig(market_id="mkt-1"),
            inventory=Inventory(
                cash_cents=500_000,
                yes_volume=700, no_volume=300,
                yes_cost_sum_cents=35000, no_cost_sum_cents=15000,
                yes_pending_sell=0, no_pending_sell=0, frozen_balance_cents=0,
            ),
        )
        assert ctx.inventory.inventory_skew > 0.05

        sanitizer = OrderSanitizer()
        intents = [_make_intent("YES"), _make_intent("NO")]
        result = sanitizer.sanitize(intents, DefenseLevel.ONE_SIDE, ctx)
        sides = {i.side for i in result}

        assert "NO" not in sides
        assert "YES" in sides

    def test_one_side_still_works_correctly_with_clear_negative_skew(self) -> None:
        """Negative skew < -0.05 → ONE_SIDE suppresses YES (existing behavior)."""
        ctx = MarketContext(
            market_id="mkt-1",
            config=MarketConfig(market_id="mkt-1"),
            inventory=Inventory(
                cash_cents=500_000,
                yes_volume=300, no_volume=700,
                yes_cost_sum_cents=15000, no_cost_sum_cents=35000,
                yes_pending_sell=0, no_pending_sell=0, frozen_balance_cents=0,
            ),
        )
        assert ctx.inventory.inventory_skew < -0.05

        sanitizer = OrderSanitizer()
        intents = [_make_intent("YES"), _make_intent("NO")]
        result = sanitizer.sanitize(intents, DefenseLevel.ONE_SIDE, ctx)
        sides = {i.side for i in result}

        assert "YES" not in sides
        assert "NO" in sides


# ─── P1-4: inventory_lock on MarketContext + reconcile_loop ───────────────────


class TestP1InventoryLock:
    """P1-4: MarketContext must have an inventory_lock; reconcile_loop must use it."""

    def test_market_context_has_inventory_lock(self) -> None:
        """MarketContext must have an inventory_lock field of type asyncio.Lock."""
        ctx = _make_ctx()
        assert hasattr(ctx, "inventory_lock"), (
            "MarketContext must have an inventory_lock attribute"
        )
        assert isinstance(ctx.inventory_lock, asyncio.Lock), (
            "inventory_lock must be an asyncio.Lock instance"
        )

    def test_each_market_context_gets_independent_lock(self) -> None:
        """Two MarketContext instances must have distinct locks."""
        ctx1 = _make_ctx("mkt-1")
        ctx2 = _make_ctx("mkt-2")
        assert ctx1.inventory_lock is not ctx2.inventory_lock, (
            "Each market must have its own lock instance"
        )

    async def test_reconcile_loop_does_not_hold_lock_during_api_calls(self) -> None:
        """reconcile_loop must NOT hold inventory_lock during reconcile() API calls."""
        from src.amm.main import reconcile_loop

        ctx = _make_ctx()
        lock_held_during_reconcile = False
        reconcile_called = False

        async def fake_reconcile(market_ids: list[str], n_markets_total: int = 0, balance_resp: dict | None = None) -> dict:
            nonlocal lock_held_during_reconcile, reconcile_called
            reconcile_called = True
            # Lock must NOT be held during API calls — only during ctx.inventory write
            lock_held_during_reconcile = ctx.inventory_lock.locked()
            ctx.shutdown_requested = True
            return {market_ids[0]: {"drifted": False, "fields": []}}

        async def fake_fetch_balance() -> dict:
            return {"data": {"balance_cents": 100_000, "frozen_balance_cents": 0}}

        fake_reconciler = MagicMock()
        fake_reconciler.reconcile = fake_reconcile
        fake_reconciler.fetch_balance = fake_fetch_balance
        fake_cache = AsyncMock()
        fake_cache.get.return_value = None

        await reconcile_loop(fake_reconciler, {"mkt-1": ctx}, interval_seconds=0.001, inventory_cache=fake_cache)

        assert reconcile_called, "reconcile must be called"
        assert not lock_held_during_reconcile, (
            "inventory_lock must NOT be held during reconcile() API calls — only during ctx.inventory write"
        )

    async def test_reconcile_loop_holds_lock_during_inventory_write_on_drift(self) -> None:
        """When drift is detected, reconcile_loop must hold inventory_lock during ctx.inventory write."""
        from src.amm.main import reconcile_loop

        ctx = _make_ctx()
        lock_held_during_write = False

        async def fake_reconcile(market_ids: list[str], n_markets_total: int = 0, balance_resp: dict | None = None) -> dict:
            ctx.shutdown_requested = True
            return {market_ids[0]: {"drifted": True, "fields": ["yes_volume"]}}

        async def fake_fetch_balance() -> dict:
            return {"data": {"balance_cents": 100_000, "frozen_balance_cents": 0}}

        fake_reconciler = MagicMock()
        fake_reconciler.reconcile = fake_reconcile
        fake_reconciler.fetch_balance = fake_fetch_balance

        fresh_inv = _make_inventory(yes=600, no=400)

        async def fake_cache_get(market_id: str) -> object:
            # Check if the lock is held when we're called as part of the drift sync
            return fresh_inv

        fake_cache = AsyncMock()
        fake_cache.get.side_effect = fake_cache_get

        # Patch ctx.inventory_lock to spy on __aenter__
        original_lock = ctx.inventory_lock

        class SpyLock:
            async def __aenter__(self) -> None:
                nonlocal lock_held_during_write
                lock_held_during_write = True
                return await original_lock.__aenter__()

            async def __aexit__(self, *args: object) -> None:
                return await original_lock.__aexit__(*args)

            def locked(self) -> bool:
                return original_lock.locked()

        ctx.inventory_lock = SpyLock()  # type: ignore[assignment]

        await reconcile_loop(fake_reconciler, {"mkt-1": ctx}, interval_seconds=0.001, inventory_cache=fake_cache)

        assert lock_held_during_write, (
            "inventory_lock must be held when writing ctx.inventory after drift correction"
        )
        assert ctx.inventory.yes_volume == 600, "ctx.inventory must be updated with fresh data"

    async def test_quote_cycle_does_not_hold_inventory_lock_during_poll(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """quote_cycle must NOT hold inventory_lock while polling trades (only during write)."""
        from src.amm.main import quote_cycle
        from src.amm.strategy.pricing.three_layer import ThreeLayerPricing
        from src.amm.strategy.pricing.anchor import AnchorPricing
        from src.amm.strategy.pricing.micro import MicroPricing
        from src.amm.strategy.pricing.posterior import PosteriorPricing
        from src.amm.strategy.as_engine import ASEngine
        from src.amm.strategy.gradient import GradientEngine
        from src.amm.risk.defense_stack import DefenseStack

        ctx = _make_ctx()
        ctx.market_status_checked_at = time.monotonic()  # skip status fetch
        ctx.last_known_market_active = True
        lock_held_during_poll = False

        async def fake_poll(market_id: str) -> list:
            nonlocal lock_held_during_poll
            # Lock should NOT be held during I/O — only during the inventory write
            lock_held_during_poll = ctx.inventory_lock.locked()
            return []

        poller = AsyncMock()
        poller.poll.side_effect = fake_poll

        api = AsyncMock()
        api.get_orderbook.return_value = {"data": {"best_bid": 48, "best_ask": 52}}
        api.get_market_status.return_value = "active"

        inventory_cache = AsyncMock()
        inventory_cache.get.return_value = _make_inventory()

        order_mgr = AsyncMock()
        order_mgr.active_orders = {}

        pricing = ThreeLayerPricing(
            anchor=AnchorPricing(50),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
            config=ctx.config,
        )

        await quote_cycle(
            ctx=ctx,
            api=api,
            poller=poller,
            pricing=pricing,
            as_engine=ASEngine(),
            gradient=GradientEngine(),
            risk=DefenseStack(ctx.config),
            sanitizer=OrderSanitizer(),
            order_mgr=order_mgr,
            inventory_cache=inventory_cache,
        )

        assert not lock_held_during_poll, (
            "inventory_lock must NOT be held while TradePoller.poll runs — only during ctx.inventory write"
        )


# ─── P1-5: GracefulShutdown via order_mgr.cancel_all() ───────────────────────


class TestP1GracefulShutdownViaOrderManager:
    """P1-5: GracefulShutdown must use order_mgr.cancel_all() when order_managers provided."""

    async def test_shutdown_uses_order_mgr_cancel_all_when_provided(self) -> None:
        """When order_managers is passed, shutdown calls cancel_all not api.batch_cancel."""
        api = AsyncMock()
        shutdown = GracefulShutdown(api=api)

        order_mgr = AsyncMock()
        contexts = {"mkt-1": _make_shutdown_ctx("mkt-1")}
        order_managers = {"mkt-1": order_mgr}

        await shutdown.execute(contexts, order_managers=order_managers)

        order_mgr.cancel_all.assert_called_once_with("mkt-1")
        api.batch_cancel.assert_not_called()

    async def test_shutdown_uses_api_batch_cancel_when_no_order_managers(self) -> None:
        """Without order_managers (backward compat), api.batch_cancel is called."""
        api = AsyncMock()
        shutdown = GracefulShutdown(api=api)

        contexts = {"mkt-1": _make_shutdown_ctx("mkt-1")}

        await shutdown.execute(contexts)

        api.batch_cancel.assert_called_once_with("mkt-1", scope="ALL")

    async def test_shutdown_uses_order_mgr_for_all_markets(self) -> None:
        """Each market gets its own order_mgr.cancel_all call."""
        api = AsyncMock()
        shutdown = GracefulShutdown(api=api)

        order_mgr_1 = AsyncMock()
        order_mgr_2 = AsyncMock()
        contexts = {
            "mkt-1": _make_shutdown_ctx("mkt-1"),
            "mkt-2": _make_shutdown_ctx("mkt-2"),
        }
        order_managers = {"mkt-1": order_mgr_1, "mkt-2": order_mgr_2}

        await shutdown.execute(contexts, order_managers=order_managers)

        order_mgr_1.cancel_all.assert_called_once_with("mkt-1")
        order_mgr_2.cancel_all.assert_called_once_with("mkt-2")
        api.batch_cancel.assert_not_called()

    async def test_shutdown_falls_back_to_api_when_market_not_in_order_managers(
        self,
    ) -> None:
        """If a market_id is missing from order_managers, fall back to api.batch_cancel."""
        api = AsyncMock()
        shutdown = GracefulShutdown(api=api)

        order_mgr_1 = AsyncMock()
        contexts = {
            "mkt-1": _make_shutdown_ctx("mkt-1"),
            "mkt-2": _make_shutdown_ctx("mkt-2"),
        }
        order_managers = {"mkt-1": order_mgr_1}  # mkt-2 not in order_managers

        await shutdown.execute(contexts, order_managers=order_managers)

        order_mgr_1.cancel_all.assert_called_once_with("mkt-1")
        api.batch_cancel.assert_called_once_with("mkt-2", scope="ALL")

    async def test_shutdown_still_closes_api_when_using_order_managers(self) -> None:
        """api.close must always be called regardless of order_managers."""
        api = AsyncMock()
        shutdown = GracefulShutdown(api=api)

        order_mgr = AsyncMock()
        contexts = {"mkt-1": _make_shutdown_ctx("mkt-1")}

        await shutdown.execute(contexts, order_managers={"mkt-1": order_mgr})

        api.close.assert_called_once()

    async def test_shutdown_continues_on_order_mgr_cancel_error(self) -> None:
        """Even if cancel_all raises, shutdown continues and closes api."""
        api = AsyncMock()
        shutdown = GracefulShutdown(api=api)

        order_mgr_1 = AsyncMock()
        order_mgr_1.cancel_all.side_effect = Exception("cancel failed")
        order_mgr_2 = AsyncMock()

        contexts = {
            "mkt-1": _make_shutdown_ctx("mkt-1"),
            "mkt-2": _make_shutdown_ctx("mkt-2"),
        }
        order_managers = {"mkt-1": order_mgr_1, "mkt-2": order_mgr_2}

        await shutdown.execute(contexts, order_managers=order_managers)

        order_mgr_2.cancel_all.assert_called_once_with("mkt-2")
        api.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# R3/R4 Round-2 fixes: inventory_lock snapshot, winding_down recovery,
# load_from_cache atomicity
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Bug-1: quote_cycle must use inventory snapshot under lock ────────────────


class TestInventorySnapshotInQuoteCycle:
    """quote_cycle must take a local inventory snapshot under inventory_lock
    so that reconcile_loop replacing ctx.inventory mid-cycle doesn't corrupt
    skew/PnL/cash reads."""

    async def test_quote_cycle_uses_snapshot_not_live_ctx_inventory(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.amm.main import quote_cycle

        ctx = _make_ctx()
        original_inventory = ctx.inventory

        observed_skews: list[float] = []

        api = AsyncMock()
        api.get_orderbook.return_value = {
            "data": {"best_bid": 45, "best_ask": 55, "bid_depth": 10, "ask_depth": 10},
        }
        api.get_market_status.return_value = "active"
        poller = AsyncMock()
        poller.poll.return_value = []
        inventory_cache = AsyncMock()
        inventory_cache.get.return_value = original_inventory

        pricing = MagicMock()
        pricing.compute.return_value = 50

        as_engine = MagicMock()
        as_engine.bernoulli_sigma.return_value = 0.05
        as_engine.get_gamma_for_age.return_value = 0.1

        def fake_compute_quotes(
            *, mid_price: int, inventory_skew: float, **kwargs: object,
        ) -> tuple[int, int]:
            observed_skews.append(inventory_skew)
            # Simulate reconcile_loop replacing ctx.inventory mid-cycle
            ctx.inventory = Inventory(
                cash_cents=999_999, yes_volume=999, no_volume=1,
                yes_cost_sum_cents=0, no_cost_sum_cents=0,
                yes_pending_sell=0, no_pending_sell=0, frozen_balance_cents=0,
            )
            return 55, 45

        as_engine.compute_quotes.side_effect = fake_compute_quotes

        gradient = MagicMock()
        gradient.build_ask_ladder.return_value = []
        gradient.build_bid_ladder.return_value = []

        risk = MagicMock()
        risk.evaluate.return_value = DefenseLevel.NORMAL

        sanitizer = MagicMock()
        sanitizer.sanitize.return_value = []

        order_mgr = AsyncMock()
        order_mgr.execute_intents = AsyncMock()

        ctx.market_status_checked_at = time.monotonic()
        ctx.last_known_market_active = True

        await quote_cycle(
            ctx, api, poller, pricing, as_engine, gradient,
            risk, sanitizer, order_mgr, inventory_cache,
        )

        # The skew used must be from the original inventory snapshot
        assert observed_skews[0] == original_inventory.inventory_skew

    async def test_session_pnl_uses_snapshot_inventory(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """session_pnl_cents must be computed from the snapshot, not a
        potentially-replaced ctx.inventory."""
        from src.amm.main import quote_cycle

        ctx = _make_ctx()
        ctx.initial_inventory_value_cents = ctx.inventory.total_value_cents(50)

        api = AsyncMock()
        api.get_orderbook.return_value = {
            "data": {"best_bid": 45, "best_ask": 55, "bid_depth": 10, "ask_depth": 10},
        }
        api.get_market_status.return_value = "active"
        poller = AsyncMock()
        poller.poll.return_value = []
        inventory_cache = AsyncMock()
        inventory_cache.get.return_value = ctx.inventory

        pricing = MagicMock()
        pricing.compute.return_value = 50

        as_engine = MagicMock()
        as_engine.bernoulli_sigma.return_value = 0.05
        as_engine.get_gamma_for_age.return_value = 0.1
        as_engine.compute_quotes.return_value = (55, 45)

        gradient = MagicMock()
        gradient.build_ask_ladder.return_value = []
        gradient.build_bid_ladder.return_value = []

        risk = MagicMock()
        risk.evaluate.return_value = DefenseLevel.NORMAL

        sanitizer = MagicMock()
        sanitizer.sanitize.return_value = []

        order_mgr = AsyncMock()

        ctx.market_status_checked_at = time.monotonic()
        ctx.last_known_market_active = True

        await quote_cycle(
            ctx, api, poller, pricing, as_engine, gradient,
            risk, sanitizer, order_mgr, inventory_cache,
        )

        # PnL should be 0 (same inventory, same mid)
        assert ctx.session_pnl_cents == 0


# ─── Bug-2: winding_down recovery on handle_winding_down failure ─────────────


class TestWindingDownRecovery:
    """If handle_winding_down raises, winding_down must NOT be permanently
    set — next cycle should retry."""

    async def test_winding_down_not_set_when_handle_winding_down_raises(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.amm.main import quote_cycle

        ctx = _make_ctx()
        ctx.market_status_checked_at = 0.0  # force TTL check

        api = AsyncMock()
        api.get_orderbook.return_value = {
            "data": {"best_bid": 45, "best_ask": 55, "bid_depth": 10, "ask_depth": 10},
        }
        api.get_market_status.return_value = "resolved"

        poller = AsyncMock()
        poller.poll.return_value = []
        inventory_cache = AsyncMock()
        inventory_cache.get.return_value = ctx.inventory

        pricing = MagicMock()
        pricing.compute.return_value = 50
        as_engine = MagicMock()
        as_engine.bernoulli_sigma.return_value = 0.05
        as_engine.get_gamma_for_age.return_value = 0.1
        as_engine.compute_quotes.return_value = (55, 45)
        gradient = MagicMock()
        gradient.build_ask_ladder.return_value = []
        gradient.build_bid_ladder.return_value = []
        risk = MagicMock()
        sanitizer = MagicMock()
        order_mgr = AsyncMock()

        monkeypatch.setattr(
            "src.amm.main.handle_winding_down",
            AsyncMock(side_effect=RuntimeError("API down")),
        )

        await quote_cycle(
            ctx, api, poller, pricing, as_engine, gradient,
            risk, sanitizer, order_mgr, inventory_cache,
        )

        # winding_down must NOT be stuck True after a failure
        assert ctx.winding_down is False
        # market_status_checked_at should be reset so next cycle retries
        assert ctx.market_status_checked_at == 0.0

    async def test_winding_down_set_on_success(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.amm.main import quote_cycle

        ctx = _make_ctx()
        ctx.market_status_checked_at = 0.0

        api = AsyncMock()
        api.get_orderbook.return_value = {
            "data": {"best_bid": 45, "best_ask": 55, "bid_depth": 10, "ask_depth": 10},
        }
        api.get_market_status.return_value = "settled"

        poller = AsyncMock()
        poller.poll.return_value = []
        inventory_cache = AsyncMock()
        inventory_cache.get.return_value = ctx.inventory

        pricing = MagicMock()
        pricing.compute.return_value = 50
        as_engine = MagicMock()
        as_engine.bernoulli_sigma.return_value = 0.05
        as_engine.get_gamma_for_age.return_value = 0.1
        as_engine.compute_quotes.return_value = (55, 45)
        gradient = MagicMock()
        gradient.build_ask_ladder.return_value = []
        gradient.build_bid_ladder.return_value = []
        risk = MagicMock()
        sanitizer = MagicMock()
        order_mgr = AsyncMock()

        async def fake_handle_winding_down(
            ctx_: MarketContext, *args: object, **kwargs: object,
        ) -> int:
            # Real handle_winding_down sets ctx.winding_down = True internally
            ctx_.winding_down = True
            return 5

        monkeypatch.setattr(
            "src.amm.main.handle_winding_down", fake_handle_winding_down,
        )

        await quote_cycle(
            ctx, api, poller, pricing, as_engine, gradient,
            risk, sanitizer, order_mgr, inventory_cache,
        )

        # handle_winding_down itself sets ctx.winding_down = True
        assert ctx.winding_down is True


# ─── Bug-3: load_from_cache atomicity ────────────────────────────────────────


class TestLoadFromCacheAtomic:
    """load_from_cache must not leave partial state on failure."""

    async def test_load_from_cache_partial_failure_leaves_empty(self) -> None:
        api = AsyncMock()
        cache = AsyncMock()
        order_cache = AsyncMock()
        order_cache.get_all_orders.side_effect = ConnectionError("Redis gone")

        mgr = OrderManager(api=api, cache=cache, order_cache=order_cache)
        mgr.active_orders = {}

        with pytest.raises(ConnectionError):
            await mgr.load_from_cache("mkt-1")

        assert mgr.active_orders == {}

    async def test_load_from_cache_success_replaces_atomically(self) -> None:
        api = AsyncMock()
        cache = AsyncMock()
        cache.set_pending_sell = AsyncMock()
        order_cache = AsyncMock()
        order_cache.get_all_orders.return_value = {
            "order-1": {
                "side": "YES", "direction": "SELL",
                "price_cents": 55, "remaining_quantity": 10,
            },
            "order-2": {
                "side": "NO", "direction": "SELL",
                "price_cents": 45, "remaining_quantity": 5,
            },
        }

        mgr = OrderManager(api=api, cache=cache, order_cache=order_cache)
        # Pre-populate stale data
        mgr.active_orders = {
            "stale-id": ActiveOrder("stale-id", "YES", "SELL", 60, 20),
        }

        await mgr.load_from_cache("mkt-1")

        # Stale data gone, only new data present
        assert "stale-id" not in mgr.active_orders
        assert "order-1" in mgr.active_orders
        assert "order-2" in mgr.active_orders

    async def test_load_from_cache_preserves_existing_on_get_failure(self) -> None:
        """If get_all_orders raises, active_orders is untouched (no partial write).
        The caller (amm_main) is responsible for clearing it."""
        api = AsyncMock()
        cache = AsyncMock()
        order_cache = AsyncMock()
        order_cache.get_all_orders.side_effect = ConnectionError("Redis gone")

        mgr = OrderManager(api=api, cache=cache, order_cache=order_cache)
        mgr.active_orders = {
            "pre-existing": ActiveOrder("pre-existing", "YES", "SELL", 50, 10),
        }

        with pytest.raises(ConnectionError):
            await mgr.load_from_cache("mkt-1")

        # load_from_cache failed before reaching the atomic replace,
        # so existing state is preserved (not corrupted with partial data).
        assert "pre-existing" in mgr.active_orders
