"""Tests for AMM main-loop background task orchestration."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.amm.config.models import GlobalConfig, MarketConfig
from src.amm.lifecycle.health import HealthState
from src.amm.main import (
    _build_signal_handler,
    _evaluate_oracle_state,
    _oracle_refresh_loop,
    _wait_for_task_shutdown,
    amm_main,
    run_market,
    run_market_with_health,
)
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext
from src.amm.oracle.polymarket_oracle import OracleState


def _make_context(market_id: str = "mkt-1") -> MarketContext:
    return MarketContext(
        market_id=market_id,
        config=MarketConfig(market_id=market_id, quote_interval_seconds=0.01),
        inventory=Inventory(
            cash_cents=500_000,
            yes_volume=100,
            no_volume=100,
            yes_cost_sum_cents=5_000,
            no_cost_sum_cents=5_000,
            yes_pending_sell=0,
            no_pending_sell=0,
            frozen_balance_cents=0,
        ),
    )


class TestAMMMain:
    async def test_amm_main_uses_full_oracle_config_and_reconciler_inventory_cache(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = _make_context()
        ctx.config.oracle_slug = "full-oracle"
        ctx.config.oracle_stale_seconds = 9.0
        ctx.config.oracle_deviation_cents = 13.0
        ctx.config.oracle_lvr_window_seconds = 2.5
        ctx.config.oracle_lvr_threshold = 0.15

        global_cfg = GlobalConfig(reconcile_interval_seconds=0.01)
        redis_client = AsyncMock()
        http_client = AsyncMock()
        token_mgr = object()
        api = object()
        inventory_cache = object()
        config_loader = AsyncMock()
        initializer = AsyncMock()
        initializer.initialize.return_value = {ctx.market_id: ctx}
        shutdown = AsyncMock()
        oracle_ctor_args: list[tuple[tuple[object, ...], dict[str, object]]] = []
        reconciler_ctor_args: list[tuple[tuple[object, ...], dict[str, object]]] = []

        class FakeOracle:
            def __init__(self, *args: object, **kwargs: object) -> None:
                oracle_ctor_args.append((args, kwargs))

            async def get_price(self) -> float:
                return 50.0

            async def refresh(self) -> None:
                return None

        class FakeReconciler:
            def __init__(self, *args: object, **kwargs: object) -> None:
                reconciler_ctor_args.append((args, kwargs))

            async def reconcile(self, _market_ids: list[str]) -> dict[str, dict[str, object]]:
                ctx.shutdown_requested = True
                return {}

        async def fake_run_market(
            market_ctx: MarketContext,
            services: dict[str, object],
            state: HealthState,
            **kwargs: object,
        ) -> None:
            assert market_ctx is ctx
            assert state.ready is True
            assert kwargs["oracle"] is not None
            market_ctx.shutdown_requested = True

        async def fake_run_health_server(_state: HealthState, port: int = 8001) -> None:
            assert port == 8001
            while not ctx.shutdown_requested:
                await asyncio.sleep(0)

        monkeypatch.setattr("src.amm.main.create_redis_client", lambda url: redis_client)
        monkeypatch.setattr("src.amm.main.httpx.AsyncClient", lambda **kwargs: http_client)
        monkeypatch.setattr("src.amm.main.TokenManager", lambda *args, **kwargs: token_mgr)
        monkeypatch.setattr("src.amm.main.AMMApiClient", lambda *args, **kwargs: api)
        monkeypatch.setattr("src.amm.main.InventoryCache", lambda redis: inventory_cache)
        monkeypatch.setattr(
            "src.amm.main.ConfigLoader",
            lambda redis_client=None: config_loader,
        )
        monkeypatch.setattr("src.amm.main.AMMInitializer", lambda **kwargs: initializer)
        monkeypatch.setattr("src.amm.main.GracefulShutdown", lambda api: shutdown)
        monkeypatch.setattr("src.amm.main.AMMReconciler", FakeReconciler)
        monkeypatch.setattr("src.amm.main.run_market_with_health", fake_run_market)
        monkeypatch.setattr("src.amm.main.run_health_server", fake_run_health_server)
        monkeypatch.setattr("src.amm.main.TradePoller", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.ThreeLayerPricing", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.AnchorPricing", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.MicroPricing", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.PosteriorPricing", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.ASEngine", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.GradientEngine", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.DefenseStack", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.OrderSanitizer", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.OrderManager", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.PhaseManager", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.PolymarketOracle", FakeOracle)

        fake_loop = SimpleNamespace(add_signal_handler=lambda *args, **kwargs: None)
        monkeypatch.setattr("src.amm.main.asyncio.get_event_loop", lambda: fake_loop)
        monkeypatch.setenv("AMM_BASE_URL", "http://test/api/v1")
        monkeypatch.setenv("AMM_REDIS_URL", "redis://test")
        monkeypatch.setenv("AMM_USERNAME", "amm")
        monkeypatch.setenv("AMM_PASSWORD", "secret")
        monkeypatch.setenv("AMM_MARKETS", ctx.market_id)
        config_loader.load_global.return_value = global_cfg

        await amm_main()

        assert oracle_ctor_args == [((ctx.config,), {})]
        assert reconciler_ctor_args == [(
            (),
            {"api": api, "inventory_cache": inventory_cache},
        )]

    async def test_amm_main_applies_global_oracle_thresholds_before_oracle_construction(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = _make_context()
        ctx.config.oracle_slug = "full-oracle"
        ctx.config.oracle_stale_seconds = 3.0
        ctx.config.oracle_deviation_cents = 20.0

        global_cfg = GlobalConfig(reconcile_interval_seconds=0.01)
        redis_client = AsyncMock()
        http_client = AsyncMock()
        token_mgr = object()
        api = object()
        inventory_cache = object()
        config_loader = AsyncMock()
        initializer = AsyncMock()
        initializer.initialize.return_value = {ctx.market_id: ctx}
        shutdown = AsyncMock()
        observed_configs: list[MarketConfig] = []

        class FakeOracle:
            def __init__(self, config: MarketConfig) -> None:
                observed_configs.append(config)

            async def refresh(self) -> None:
                return None

        class FakeReconciler:
            def __init__(self, *args: object, **kwargs: object) -> None:
                return None

            async def reconcile(self, _market_ids: list[str]) -> dict[str, dict[str, object]]:
                ctx.shutdown_requested = True
                return {}

        async def fake_run_market(
            market_ctx: MarketContext,
            services: dict[str, object],
            state: HealthState,
            **kwargs: object,
        ) -> None:
            assert market_ctx is ctx
            assert state.ready is True
            assert kwargs["oracle"] is not None
            market_ctx.shutdown_requested = True

        async def fake_run_health_server(_state: HealthState, port: int = 8001) -> None:
            assert port == 8001
            while not ctx.shutdown_requested:
                await asyncio.sleep(0)

        monkeypatch.setattr("src.amm.main.create_redis_client", lambda url: redis_client)
        monkeypatch.setattr("src.amm.main.httpx.AsyncClient", lambda **kwargs: http_client)
        monkeypatch.setattr("src.amm.main.TokenManager", lambda *args, **kwargs: token_mgr)
        monkeypatch.setattr("src.amm.main.AMMApiClient", lambda *args, **kwargs: api)
        monkeypatch.setattr("src.amm.main.InventoryCache", lambda redis: inventory_cache)
        monkeypatch.setattr(
            "src.amm.main.ConfigLoader",
            lambda redis_client=None: config_loader,
        )
        monkeypatch.setattr("src.amm.main.AMMInitializer", lambda **kwargs: initializer)
        monkeypatch.setattr("src.amm.main.GracefulShutdown", lambda api: shutdown)
        monkeypatch.setattr("src.amm.main.AMMReconciler", FakeReconciler)
        monkeypatch.setattr("src.amm.main.run_market_with_health", fake_run_market)
        monkeypatch.setattr("src.amm.main.run_health_server", fake_run_health_server)
        monkeypatch.setattr("src.amm.main.TradePoller", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.ThreeLayerPricing", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.AnchorPricing", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.MicroPricing", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.PosteriorPricing", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.ASEngine", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.GradientEngine", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.DefenseStack", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.OrderSanitizer", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.OrderManager", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.PhaseManager", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.PolymarketOracle", FakeOracle)
        fake_loop = SimpleNamespace(add_signal_handler=lambda *args, **kwargs: None)
        monkeypatch.setattr("src.amm.main.asyncio.get_event_loop", lambda: fake_loop)
        monkeypatch.setenv("AMM_BASE_URL", "http://test/api/v1")
        monkeypatch.setenv("AMM_REDIS_URL", "redis://test")
        monkeypatch.setenv("AMM_USERNAME", "amm")
        monkeypatch.setenv("AMM_PASSWORD", "secret")
        monkeypatch.setenv("AMM_MARKETS", ctx.market_id)
        config_loader.load_global.return_value = global_cfg

        with patch(
            "yaml.safe_load",
            return_value={"oracle": {"lag_threshold_seconds": 11.0, "deviation_threshold_cents": 17.0}},
        ):
            await amm_main()

        assert len(observed_configs) == 1
        assert observed_configs[0].oracle_stale_seconds == 11.0
        assert observed_configs[0].oracle_deviation_cents == 17.0

    async def test_run_market_with_health_decrements_markets_active_on_exit(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = _make_context()
        state = HealthState(ready=True, markets_active=1)

        async def fake_run_market(market_ctx: MarketContext, services: dict[str, object], **kwargs: object) -> None:
            assert market_ctx is ctx
            raise RuntimeError("stop")

        monkeypatch.setattr("src.amm.main.run_market", fake_run_market)

        with pytest.raises(RuntimeError, match="stop"):
            await run_market_with_health(ctx, {}, state)

        assert state.markets_active == 0

    async def test_run_market_accepts_oracle_from_services_without_duplicate_kwargs(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = _make_context()
        ctx.shutdown_requested = False
        oracle = object()
        observed_calls: list[tuple[MarketContext, object | None, object | None]] = []

        async def fake_quote_cycle(
            market_ctx: MarketContext,
            **kwargs: object,
        ) -> None:
            observed_calls.append((market_ctx, kwargs.get("oracle"), kwargs.get("phase_mgr")))
            market_ctx.shutdown_requested = True

        async def fake_sleep(_seconds: float) -> None:
            ctx.shutdown_requested = True

        monkeypatch.setattr("src.amm.main.quote_cycle", fake_quote_cycle)
        monkeypatch.setattr("src.amm.main.asyncio.sleep", fake_sleep)

        services = {
            "api": object(),
            "poller": object(),
            "pricing": object(),
            "as_engine": object(),
            "gradient": object(),
            "risk": object(),
            "sanitizer": object(),
            "order_mgr": object(),
            "inventory_cache": object(),
            "oracle": oracle,
            "phase_mgr": "phase-manager",
        }

        await run_market(ctx, services, oracle=oracle)

        assert observed_calls == [(ctx, oracle, "phase-manager")]

    @pytest.mark.skip(reason="TODO: amm_main integration test needs rework after merge conflicts")
    async def test_amm_main_starts_health_server_marks_ready_and_reconciles_periodically(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctx = _make_context()
        global_cfg = GlobalConfig(reconcile_interval_seconds=0.01)
        redis_client = AsyncMock()
        http_client = AsyncMock()
        token_mgr = object()
        api = object()
        inventory_cache = object()
        config_loader = AsyncMock()
        initializer = AsyncMock()
        initializer.initialize.return_value = {ctx.market_id: ctx}
        shutdown = AsyncMock()

        reconcile_calls: list[list[str]] = []
        observed_health: list[tuple[bool, int]] = []

        class FakeReconciler:
            async def reconcile(self, market_ids: list[str]) -> None:
                reconcile_calls.append(market_ids)
                if len(reconcile_calls) >= 2:
                    ctx.shutdown_requested = True

        async def fake_run_market(
            market_ctx: MarketContext,
            services: dict[str, object],
            state: HealthState,
            **kwargs: object,
        ) -> None:
            assert market_ctx is ctx
            assert state.ready is True
            assert state.markets_active == 1
            while not market_ctx.shutdown_requested:
                await asyncio.sleep(0)

        async def fake_run_health_server(state: HealthState, port: int = 8001) -> None:
            assert port == 8001
            observed_health.append((state.ready, state.markets_active))
            while not ctx.shutdown_requested:
                await asyncio.sleep(0)

        monkeypatch.setattr("src.amm.main.create_redis_client", lambda url: redis_client)
        monkeypatch.setattr("src.amm.main.httpx.AsyncClient", lambda **kwargs: http_client)
        monkeypatch.setattr("src.amm.main.TokenManager", lambda *args, **kwargs: token_mgr)
        monkeypatch.setattr("src.amm.main.AMMApiClient", lambda *args, **kwargs: api)
        monkeypatch.setattr("src.amm.main.InventoryCache", lambda redis: inventory_cache)
        monkeypatch.setattr(
            "src.amm.main.ConfigLoader",
            lambda redis_client=None: config_loader,
        )
        monkeypatch.setattr("src.amm.main.AMMInitializer", lambda **kwargs: initializer)
        monkeypatch.setattr("src.amm.main.GracefulShutdown", lambda api: shutdown)
        monkeypatch.setattr("src.amm.main.AMMReconciler", lambda *args, **kwargs: FakeReconciler())
        monkeypatch.setattr("src.amm.main.run_market_with_health", fake_run_market)
        monkeypatch.setattr("src.amm.main.run_health_server", fake_run_health_server)
        monkeypatch.setattr("src.amm.main.TradePoller", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.ThreeLayerPricing", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.AnchorPricing", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.MicroPricing", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.PosteriorPricing", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.ASEngine", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.GradientEngine", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.DefenseStack", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.OrderSanitizer", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.OrderManager", lambda *args, **kwargs: object())
        monkeypatch.setattr("src.amm.main.PolymarketOracle", lambda *args, **kwargs: object())

        fake_loop = SimpleNamespace(add_signal_handler=lambda *args, **kwargs: None)
        monkeypatch.setattr("src.amm.main.asyncio.get_event_loop", lambda: fake_loop)
        monkeypatch.setenv("AMM_BASE_URL", "http://test/api/v1")
        monkeypatch.setenv("AMM_REDIS_URL", "redis://test")
        monkeypatch.setenv("AMM_USERNAME", "amm")
        monkeypatch.setenv("AMM_PASSWORD", "secret")
        monkeypatch.setenv("AMM_MARKETS", ctx.market_id)
        config_loader.load_global.return_value = global_cfg

        await amm_main()

        assert len(reconcile_calls) >= 2
        assert reconcile_calls[0] == [ctx.market_id]
        assert observed_health == [(True, 1)]
        shutdown.execute.assert_awaited_once()
        redis_client.aclose.assert_awaited_once()
        http_client.aclose.assert_awaited_once()

    async def test_signal_handler_marks_shutdown_and_cancels_tasks(self) -> None:
        contexts = {"mkt-1": _make_context()}
        shutdown_event = asyncio.Event()

        started = asyncio.Event()

        async def sleeper() -> None:
            started.set()
            await asyncio.sleep(3600)

        market_task = asyncio.create_task(sleeper())
        background_task = asyncio.create_task(sleeper())
        await started.wait()

        handler = _build_signal_handler(
            contexts=contexts,
            market_task_handles=[market_task],
            background_tasks=[background_task],
            shutdown_event=shutdown_event,
        )
        handler()

        results = await asyncio.gather(market_task, background_task, return_exceptions=True)

        assert contexts["mkt-1"].shutdown_requested is True
        assert shutdown_event.is_set() is True
        assert all(isinstance(result, asyncio.CancelledError) for result in results)

    async def test_wait_for_task_shutdown_force_cancels_after_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        task = MagicMock(spec=asyncio.Task)
        task.done.return_value = False

        gather_calls = 0
        fake_gather = AsyncMock(return_value=[asyncio.CancelledError()])

        async def fake_wait_for(awaitable: Awaitable[object], timeout: float) -> object:
            nonlocal gather_calls
            gather_calls += 1
            if gather_calls == 1:
                close = getattr(awaitable, "close", None)
                if callable(close):
                    close()
                raise asyncio.TimeoutError
            return await awaitable

        monkeypatch.setattr("src.amm.main.asyncio.wait_for", fake_wait_for)
        monkeypatch.setattr("src.amm.main.asyncio.gather", fake_gather)

        await _wait_for_task_shutdown([task], timeout_seconds=0.01)

        task.cancel.assert_called_once()

    async def test_oracle_refresh_loop_refreshes_in_background(self, monkeypatch: pytest.MonkeyPatch) -> None:
        contexts = {"mkt-1": _make_context()}
        oracle = MagicMock()
        oracle.last_price = 52.0
        refresh_calls = 0
        original_sleep = asyncio.sleep

        async def fake_sleep(_seconds: float) -> None:
            await original_sleep(0)

        def refresh() -> None:
            nonlocal refresh_calls
            refresh_calls += 1
            if refresh_calls >= 2:
                contexts["mkt-1"].shutdown_requested = True

        oracle.refresh.side_effect = refresh
        monkeypatch.setattr("src.amm.main.asyncio.sleep", fake_sleep)

        await _oracle_refresh_loop(oracle, 0.01, contexts)

        assert oracle.refresh.call_count == 2

    async def test_oracle_refresh_loop_refreshes_before_first_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        contexts = {"mkt-1": _make_context()}
        oracle = MagicMock()
        sleep_calls = 0

        async def fake_sleep(_seconds: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1

        def refresh() -> None:
            contexts["mkt-1"].shutdown_requested = True

        oracle.refresh.side_effect = refresh
        monkeypatch.setattr("src.amm.main.asyncio.sleep", fake_sleep)

        await _oracle_refresh_loop(oracle, 0.01, contexts)

        oracle.refresh.assert_called_once()
        assert sleep_calls == 0

    async def test_oracle_refresh_loop_runs_sync_refresh_in_executor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        contexts = {"mkt-1": _make_context()}
        oracle = SimpleNamespace(refresh=MagicMock(), last_price=52.0)
        executor_calls: list[tuple[object | None, object]] = []

        class FakeLoop:
            async def run_in_executor(
                self,
                executor: object | None,
                func: Callable[[], object],
            ) -> None:
                executor_calls.append((executor, func))
                contexts["mkt-1"].shutdown_requested = True
                func()

        async def fake_sleep(_seconds: float) -> None:
            raise AssertionError("sleep should not run before the initial refresh")

        monkeypatch.setattr("src.amm.main.asyncio.get_running_loop", lambda: FakeLoop())
        monkeypatch.setattr("src.amm.main.asyncio.sleep", fake_sleep)

        await _oracle_refresh_loop(oracle, 0.01, contexts)

        assert executor_calls == [(None, oracle.refresh)]
        oracle.refresh.assert_called_once()

    async def test_oracle_refresh_loop_logs_refresh_failures(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        contexts = {"mkt-1": _make_context()}
        original_sleep = asyncio.sleep

        async def fake_sleep(_seconds: float) -> None:
            await original_sleep(0)

        oracle = MagicMock()
        oracle.last_price = None

        def refresh() -> None:
            contexts["mkt-1"].shutdown_requested = True
            raise ValueError("oracle unavailable")

        oracle.refresh.side_effect = refresh
        monkeypatch.setattr("src.amm.main.asyncio.sleep", fake_sleep)

        await _oracle_refresh_loop(oracle, 0.01, contexts)

        assert "Oracle background refresh failed: oracle unavailable" in caplog.text

    async def test_evaluate_oracle_state_uses_current_oracle_interface(self) -> None:
        ctx = _make_context()
        oracle = MagicMock()
        oracle.evaluate.return_value = OracleState.NORMAL

        state = await _evaluate_oracle_state(oracle, ctx, internal_price_cents=51.0)

        oracle.evaluate.assert_called_once_with(internal_price_cents=51.0)
        assert state is OracleState.NORMAL
