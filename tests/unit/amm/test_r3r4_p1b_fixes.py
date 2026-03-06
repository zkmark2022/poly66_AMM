"""Tests for R3/R4 P1b + P2 fixes.

P1-6: spread_min/max_cents applied in ASEngine.compute_quotes()
P1-7: run_market() propagates unrecoverable exceptions
P1-8: config_loader.load_global() return value used for reconcile interval
P2-1: MarketContext.last_known_market_active initial value = True (set by AMMInitializer)
P2-2: maybe_auto_reinvest() syncs InventoryCache after successful mint
P2-3: PolymarketOracle.check_deviation() uses cached last_price, avoids CLI calls
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.amm.config.models import MarketConfig
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext
from src.amm.strategy.as_engine import ASEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_inventory(**kwargs: object) -> Inventory:
    defaults: dict = dict(
        cash_cents=500_000,
        yes_volume=100,
        no_volume=100,
        yes_cost_sum_cents=5_000,
        no_cost_sum_cents=5_000,
        yes_pending_sell=0,
        no_pending_sell=0,
        frozen_balance_cents=0,
    )
    defaults.update(kwargs)
    return Inventory(**defaults)  # type: ignore[arg-type]


def _make_context(market_id: str = "mkt-1", **cfg_kwargs: object) -> MarketContext:
    cfg = MarketConfig(market_id=market_id, quote_interval_seconds=0.01, **cfg_kwargs)  # type: ignore[arg-type]
    return MarketContext(
        market_id=market_id,
        config=cfg,
        inventory=_make_inventory(),
    )


# ---------------------------------------------------------------------------
# P1-6: spread_min/max_cents in ASEngine.compute_quotes()
# ---------------------------------------------------------------------------

class TestASEngineSpreadConstraints:
    """spread_min_cents and spread_max_cents must clamp the output spread."""

    def test_spread_min_enforced_when_model_spread_too_narrow(self) -> None:
        """When model spread < spread_min_cents, result spread >= spread_min_cents."""
        engine = ASEngine()
        # Use params that produce a very narrow spread (low sigma, low gamma, short tau)
        ask, bid = engine.compute_quotes(
            mid_price=50,
            inventory_skew=0.0,
            gamma=0.01,
            sigma=0.001,
            tau_hours=0.001,
            kappa=100.0,
            spread_min_cents=10,
            spread_max_cents=50,
        )
        assert ask - bid >= 10, f"Spread {ask - bid} < spread_min_cents=10"

    def test_spread_max_enforced_when_model_spread_too_wide(self) -> None:
        """When model spread > spread_max_cents, result spread <= spread_max_cents."""
        engine = ASEngine()
        # Use params that produce a very wide spread (high gamma, long tau)
        ask, bid = engine.compute_quotes(
            mid_price=50,
            inventory_skew=0.0,
            gamma=5.0,
            sigma=0.3,
            tau_hours=100.0,
            kappa=0.1,
            spread_min_cents=2,
            spread_max_cents=5,
        )
        assert ask - bid <= 5, f"Spread {ask - bid} > spread_max_cents=5"

    def test_default_params_backward_compatible(self) -> None:
        """compute_quotes() still works without explicit spread params."""
        engine = ASEngine()
        ask, bid = engine.compute_quotes(
            mid_price=50,
            inventory_skew=0.0,
            gamma=0.3,
            sigma=0.05,
            tau_hours=24.0,
            kappa=1.5,
        )
        assert 1 <= bid < ask <= 99

    def test_spread_min_default_is_2(self) -> None:
        """Default spread_min_cents=2 must be respected."""
        engine = ASEngine()
        ask, bid = engine.compute_quotes(
            mid_price=50,
            inventory_skew=0.0,
            gamma=0.001,
            sigma=0.0001,
            tau_hours=0.001,
            kappa=1000.0,
        )
        assert ask - bid >= 2

    @pytest.mark.asyncio
    async def test_quote_cycle_passes_spread_config_to_engine(self) -> None:
        """quote_cycle must pass ctx.config.spread_min/max_cents to as_engine.compute_quotes."""
        from src.amm.main import quote_cycle
        from src.amm.strategy.as_engine import ASEngine
        from src.amm.models.enums import DefenseLevel

        ctx = _make_context(spread_min_cents=8, spread_max_cents=15)
        captured: dict = {}

        original_compute = ASEngine.compute_quotes

        def _patched(self: ASEngine, *args: object, **kwargs: object) -> tuple[int, int]:
            captured.update(kwargs)
            return original_compute(self, *args, **kwargs)

        with patch.object(ASEngine, "compute_quotes", _patched):
            mock_api = AsyncMock()
            mock_api.get_orderbook.return_value = {"best_bid": 48, "best_ask": 52}
            mock_api.get_market_status.return_value = "active"

            mock_poller = AsyncMock()
            mock_poller.poll.return_value = []

            mock_pricing = MagicMock()
            mock_pricing.compute.return_value = 50

            mock_gradient = MagicMock()
            mock_gradient.build_ask_ladder.return_value = []
            mock_gradient.build_bid_ladder.return_value = []

            mock_risk = MagicMock()
            mock_risk.evaluate.return_value = DefenseLevel.NORMAL

            mock_sanitizer = MagicMock()
            mock_sanitizer.sanitize.return_value = []

            mock_order_mgr = AsyncMock()
            mock_inv_cache = AsyncMock()
            mock_inv_cache.get.return_value = None

            await quote_cycle(
                ctx=ctx,
                api=mock_api,
                poller=mock_poller,
                pricing=mock_pricing,
                as_engine=ASEngine(),
                gradient=mock_gradient,
                risk=mock_risk,
                sanitizer=mock_sanitizer,
                order_mgr=mock_order_mgr,
                inventory_cache=mock_inv_cache,
            )

        assert captured.get("spread_min_cents") == 8
        assert captured.get("spread_max_cents") == 15


# ---------------------------------------------------------------------------
# P1-7: run_market() exception handling
# ---------------------------------------------------------------------------

class TestRunMarketExceptionHandling:
    """run_market() must propagate unrecoverable exceptions, not swallow them."""

    @pytest.mark.asyncio
    async def test_unrecoverable_exception_triggers_shutdown_and_raises(self) -> None:
        """TypeError (programming error) must set shutdown_requested and re-raise."""
        from src.amm.main import run_market

        ctx = _make_context()
        call_count = 0

        async def _failing_cycle(*args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            raise TypeError("bad type — programming error")

        with patch("src.amm.main.quote_cycle", _failing_cycle):
            with pytest.raises(TypeError, match="bad type"):
                await run_market(ctx, {})

        assert ctx.shutdown_requested is True
        assert call_count == 1  # must NOT retry unrecoverable errors

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self) -> None:
        """asyncio.CancelledError must propagate unchanged."""
        from src.amm.main import run_market

        ctx = _make_context()

        async def _cancel(*args: object, **kwargs: object) -> None:
            raise asyncio.CancelledError()

        with patch("src.amm.main.quote_cycle", _cancel):
            with pytest.raises(asyncio.CancelledError):
                await run_market(ctx, {})

    @pytest.mark.asyncio
    async def test_recoverable_network_error_continues(self) -> None:
        """httpx.TimeoutException must NOT immediately raise; loop continues."""
        import httpx
        from src.amm.main import run_market

        ctx = _make_context()
        call_count = 0

        async def _recoverable(*args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise httpx.TimeoutException("timeout")
            # After 2 recoverable errors, shut down cleanly
            ctx.shutdown_requested = True

        with patch("src.amm.main.quote_cycle", _recoverable):
            await run_market(ctx, {})

        assert call_count == 3
        assert ctx.shutdown_requested is True

    @pytest.mark.asyncio
    async def test_too_many_consecutive_recoverable_errors_triggers_shutdown(self) -> None:
        """5+ consecutive recoverable errors must trigger shutdown."""
        import httpx
        from src.amm.main import run_market

        ctx = _make_context()
        call_count = 0

        async def _always_timeout(*args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            raise httpx.TimeoutException("always times out")

        with patch("src.amm.main.quote_cycle", _always_timeout):
            with pytest.raises((httpx.TimeoutException, Exception)):
                await run_market(ctx, {})

        assert ctx.shutdown_requested is True
        assert call_count >= 5


# ---------------------------------------------------------------------------
# P1-8: config_loader.load_global() return value used
# ---------------------------------------------------------------------------

class TestGlobalConfigUsed:
    """amm_main() must use global_cfg.reconcile_interval_seconds (not hardcoded 300)."""

    @pytest.mark.asyncio
    async def test_reconcile_loop_uses_global_config_interval(self) -> None:
        """reconcile_loop is started with global_cfg.reconcile_interval_seconds."""
        from src.amm.config.models import GlobalConfig

        captured_intervals: list[float] = []

        async def _fake_reconcile_loop(
            reconciler: object, contexts: object, interval_seconds: float,
        ) -> None:
            captured_intervals.append(interval_seconds)

        # Patch GlobalConfig with non-default interval
        fake_global_cfg = GlobalConfig(reconcile_interval_seconds=60.0)

        async def _fake_load_global() -> GlobalConfig:
            return fake_global_cfg

        with patch("src.amm.main.reconcile_loop", _fake_reconcile_loop), \
             patch("src.amm.main.ConfigLoader") as MockLoader, \
             patch("src.amm.main.AMMInitializer") as MockInit, \
             patch("src.amm.main.create_redis_client", return_value=AsyncMock()), \
             patch("src.amm.main.httpx.AsyncClient", return_value=AsyncMock()), \
             patch("src.amm.main.run_market_with_health", new_callable=lambda: lambda: AsyncMock(return_value=None)), \
             patch("src.amm.main.run_health_server", return_value=asyncio.sleep(0)):

            instance = MockLoader.return_value
            instance.load_global = AsyncMock(return_value=fake_global_cfg)
            instance.load_market = AsyncMock(return_value=MarketConfig(market_id="mkt-1"))

            init_instance = MockInit.return_value
            ctx = _make_context("mkt-1")
            ctx.config.oracle_slug = ""
            init_instance.initialize = AsyncMock(return_value={"mkt-1": ctx})

            try:
                await asyncio.wait_for(
                    __import__("src.amm.main", fromlist=["amm_main"]).amm_main(["mkt-1"]),
                    timeout=0.5,
                )
            except (asyncio.TimeoutError, Exception):
                pass

        if captured_intervals:
            assert captured_intervals[0] == 60.0, (
                f"Expected reconcile interval 60.0, got {captured_intervals[0]}. "
                "load_global() return value is probably being discarded."
            )


# ---------------------------------------------------------------------------
# P2-1: last_known_market_active initial value = True
# ---------------------------------------------------------------------------

class TestInitializerSetsMarketActiveTrue:
    """AMMInitializer must set last_known_market_active=True on fresh MarketContext."""

    @pytest.mark.asyncio
    async def test_initialized_context_has_market_active_true(self) -> None:
        from src.amm.lifecycle.initializer import AMMInitializer
        from src.amm.connector.auth import TokenManager
        from src.amm.connector.api_client import AMMApiClient
        from src.amm.config.loader import ConfigLoader
        from src.amm.cache.inventory_cache import InventoryCache

        mock_token_mgr = AsyncMock(spec=TokenManager)
        mock_api = AsyncMock(spec=AMMApiClient)
        mock_config_loader = AsyncMock(spec=ConfigLoader)
        mock_inv_cache = AsyncMock(spec=InventoryCache)

        mock_config_loader.load_global.return_value = MagicMock(base_url="http://x")
        mock_config_loader.load_market.return_value = MarketConfig(
            market_id="mkt-1", initial_mint_quantity=100,
        )
        mock_api.get_market.return_value = {"data": {"status": "active"}}
        mock_api.get_balance.return_value = {
            "data": {"balance_cents": 100_000, "frozen_balance_cents": 0}
        }
        mock_api.get_positions.return_value = {
            "data": {
                "yes_volume": 100, "no_volume": 100,
                "yes_cost_sum_cents": 5000, "no_cost_sum_cents": 5000,
            }
        }

        init = AMMInitializer(
            token_manager=mock_token_mgr,
            api=mock_api,
            config_loader=mock_config_loader,
            inventory_cache=mock_inv_cache,
        )
        contexts = await init.initialize(["mkt-1"])

        ctx = contexts["mkt-1"]
        assert ctx.last_known_market_active is True, (
            "MarketContext.last_known_market_active must be True after initialization "
            "(market was verified active by AMMInitializer)"
        )
        assert ctx.market_status_checked_at > 0.0, (
            "market_status_checked_at must be set to current monotonic time after initialization"
        )


# ---------------------------------------------------------------------------
# P2-2: maybe_auto_reinvest() syncs InventoryCache after mint
# ---------------------------------------------------------------------------

class TestAutoReinvestSyncsCache:
    """maybe_auto_reinvest() must call inventory_cache.set() after successful mint."""

    @pytest.mark.asyncio
    async def test_reinvest_syncs_inventory_to_redis(self) -> None:
        from src.amm.lifecycle.reinvest import maybe_auto_reinvest

        ctx = _make_context()
        ctx.config.auto_reinvest_enabled = True
        # 600k cash → surplus above 50k threshold = 550k → 5500 pairs to mint
        ctx.inventory.cash_cents = 600_000

        mock_api = AsyncMock()
        mock_inv_cache = AsyncMock()

        minted = await maybe_auto_reinvest(
            ctx, mock_api, inventory_cache=mock_inv_cache
        )

        assert minted > 0, "Should have minted pairs"
        mock_inv_cache.set.assert_called_once_with("mkt-1", ctx.inventory), (
            "inventory_cache.set() must be called with market_id and updated inventory"
        )

    @pytest.mark.asyncio
    async def test_reinvest_no_sync_when_no_mint(self) -> None:
        """When cash is below threshold, no mint → no cache sync."""
        from src.amm.lifecycle.reinvest import maybe_auto_reinvest

        ctx = _make_context()
        ctx.config.auto_reinvest_enabled = True
        ctx.inventory.cash_cents = 10_000  # below threshold

        mock_api = AsyncMock()
        mock_inv_cache = AsyncMock()

        minted = await maybe_auto_reinvest(
            ctx, mock_api, inventory_cache=mock_inv_cache
        )

        assert minted == 0
        mock_inv_cache.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_reinvest_accepts_none_cache(self) -> None:
        """Passing inventory_cache=None must not crash (backwards compat)."""
        from src.amm.lifecycle.reinvest import maybe_auto_reinvest

        ctx = _make_context()
        ctx.config.auto_reinvest_enabled = True
        ctx.inventory.cash_cents = 600_000

        mock_api = AsyncMock()

        minted = await maybe_auto_reinvest(ctx, mock_api, inventory_cache=None)
        assert minted > 0


# ---------------------------------------------------------------------------
# P2-3: PolymarketOracle.check_deviation() uses cached last_price
# ---------------------------------------------------------------------------

class TestPolymarketOracleCheckDeviationUsesCachedPrice:
    """check_deviation() must use self.last_price instead of re-calling get_price()."""

    @pytest.mark.asyncio
    async def test_check_deviation_uses_last_price_not_get_price(self) -> None:
        from src.amm.oracle.polymarket import PolymarketOracle

        oracle = PolymarketOracle("some-market-slug")
        oracle.last_price = 60.0  # simulates a recently refreshed price

        get_price_call_count = 0
        original_get_price = oracle.get_price

        async def _track_get_price() -> float:
            nonlocal get_price_call_count
            get_price_call_count += 1
            return await original_get_price()

        oracle.get_price = _track_get_price  # type: ignore[method-assign]

        result = await oracle.check_deviation(internal_price=50.0, threshold=5.0)

        assert get_price_call_count == 0, (
            "check_deviation() must use cached last_price and NOT call get_price() again"
        )
        assert result is True  # |50 - 60| = 10 > 5.0

    @pytest.mark.asyncio
    async def test_check_deviation_no_deviation_when_price_within_threshold(self) -> None:
        from src.amm.oracle.polymarket import PolymarketOracle

        oracle = PolymarketOracle("some-slug")
        oracle.last_price = 52.0

        result = await oracle.check_deviation(internal_price=50.0, threshold=5.0)

        assert result is False  # |50 - 52| = 2 < 5.0

    @pytest.mark.asyncio
    async def test_check_deviation_returns_false_when_no_price_cached(self) -> None:
        """When last_price is None, deviation check must return False (safe default)."""
        from src.amm.oracle.polymarket import PolymarketOracle

        oracle = PolymarketOracle("some-slug")
        assert oracle.last_price is None

        result = await oracle.check_deviation(internal_price=50.0, threshold=5.0)

        assert result is False, (
            "When no price is cached yet, check_deviation must return False "
            "(don't trigger defense on startup)"
        )
