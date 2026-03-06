"""Tests for AMM main-loop background task orchestration."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.amm.config.models import GlobalConfig, MarketConfig
from src.amm.lifecycle.health import HealthState
from src.amm.main import amm_main, run_market_with_health
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext


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
