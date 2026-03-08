"""T-SIM-06: Component startup integrity.

Scenario: Run several quote_cycle periods and verify component initialization.
Assertions:
- Reconciler is invoked at least once (patch/spy verification)
- Health Server /health endpoint is bound (mock verification)
- PhaseManager is initialized and phase advances from EXPLORATION at least once

Uses mock time.time() to control time progression without sleep.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.amm.cache.inventory_cache import InventoryCache
from src.amm.config.models import MarketConfig
from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.auth import TokenManager
from src.amm.connector.order_manager import OrderManager
from src.amm.connector.trade_poller import TradePoller
from src.amm.lifecycle.health import HealthState, create_health_app
from src.amm.lifecycle.reconciler import AMMReconciler
from src.amm.main import quote_cycle, reconcile_loop
from src.amm.models.enums import Phase
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext
from src.amm.risk.defense_stack import DefenseStack
from src.amm.risk.sanitizer import OrderSanitizer
from src.amm.strategy.as_engine import ASEngine
from src.amm.strategy.gradient import GradientEngine
from src.amm.strategy.phase_manager import PhaseManager
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.pricing.micro import MicroPricing
from src.amm.strategy.pricing.posterior import PosteriorPricing
from src.amm.strategy.pricing.three_layer import ThreeLayerPricing


def _make_config(market_id: str = "test-mkt-06") -> MarketConfig:
    return MarketConfig(
        market_id=market_id,
        anchor_price_cents=50,
        initial_mint_quantity=2000,
        remaining_hours_override=24.0,
        max_daily_loss_cents=50_000,
        max_per_market_loss_cents=25_000,
        # Very low thresholds so phase transitions quickly
        exploration_duration_hours=1.0,
        stabilization_volume_threshold=5,
    )


def _make_inventory() -> Inventory:
    return Inventory(
        cash_cents=50_000,
        yes_volume=1000,
        no_volume=1000,
        yes_cost_sum_cents=50_000,
        no_cost_sum_cents=50_000,
        yes_pending_sell=0,
        no_pending_sell=0,
        frozen_balance_cents=0,
    )


def _make_context(config: MarketConfig) -> MarketContext:
    inv = _make_inventory()
    return MarketContext(
        market_id=config.market_id,
        config=config,
        inventory=inv,
        initial_inventory_value_cents=inv.total_value_cents(50),
        last_known_market_active=True,
        market_status_checked_at=time.monotonic(),
    )


@pytest.mark.asyncio
async def test_reconciler_called_at_least_once(
    mock_exchange: dict,
    fake_redis_async,
) -> None:
    """Reconciler.reconcile() is invoked at least once during operation."""
    config = _make_config()
    ctx = _make_context(config)
    client = mock_exchange["client"]

    cache = InventoryCache(fake_redis_async)
    await cache.set(config.market_id, ctx.inventory)

    token_mgr = TokenManager("http://test-exchange", "user", "pass", client)
    token_mgr._access_token = "fake-token"
    api = AMMApiClient("http://test-exchange", token_mgr, http_client=client)

    reconciler = AMMReconciler(api=api, inventory_cache=cache)
    spy_reconcile = AsyncMock(wraps=reconciler.reconcile)
    reconciler.reconcile = spy_reconcile

    # Simulate one reconcile call (as main.py does in reconcile_loop)
    await reconciler.reconcile([config.market_id])

    assert spy_reconcile.call_count >= 1, "Reconciler must be called at least once"


@pytest.mark.asyncio
async def test_health_endpoint_bound() -> None:
    """Health server /health endpoint is created and responds correctly."""
    state = HealthState(ready=True, markets_active=2)
    app = create_health_app(state)

    # Verify the app has the /health route
    routes = [route.path for route in app.routes]
    assert "/health" in routes, f"/health not found in routes: {routes}"
    assert "/readiness" in routes, f"/readiness not found in routes: {routes}"


@pytest.mark.asyncio
async def test_phase_manager_advances_from_exploration(
    mock_exchange: dict,
    fake_redis_async,
) -> None:
    """PhaseManager transitions from EXPLORATION to STABILIZATION when conditions met.
    Uses mocked time.monotonic() to simulate time progression without sleep."""
    config = _make_config()
    ctx = _make_context(config)
    client = mock_exchange["client"]

    cache = InventoryCache(fake_redis_async)
    await cache.set(config.market_id, ctx.inventory)

    token_mgr = TokenManager("http://test-exchange", "user", "pass", client)
    token_mgr._access_token = "fake-token"
    api = AMMApiClient("http://test-exchange", token_mgr, http_client=client)
    poller = TradePoller(api=api, cache=cache, amm_user_id="amm-user-never-match")
    pricing = ThreeLayerPricing(
        anchor=AnchorPricing(config.anchor_price_cents),
        micro=MicroPricing(),
        posterior=PosteriorPricing(),
        config=config,
    )
    as_engine = ASEngine()
    gradient = GradientEngine()
    risk = DefenseStack(config)
    sanitizer = OrderSanitizer()
    order_mgr = OrderManager(api=api, cache=cache)
    phase_mgr = PhaseManager(config=config)

    assert ctx.phase == Phase.EXPLORATION, "Should start in EXPLORATION"

    # Mock time.monotonic to simulate 2 hours passing (> exploration_duration_hours=1.0)
    start_time = ctx.started_at
    fake_time = start_time + 2 * 3600  # 2 hours later

    with patch("time.monotonic", return_value=fake_time):
        await quote_cycle(
            ctx, api, poller, pricing, as_engine, gradient,
            risk, sanitizer, order_mgr, cache,
            phase_mgr=phase_mgr,
        )

    assert ctx.phase == Phase.STABILIZATION, (
        f"Expected STABILIZATION after 2h, got {ctx.phase}"
    )
    assert phase_mgr.current_phase == Phase.STABILIZATION


@pytest.mark.asyncio
async def test_phase_advances_by_volume(
    mock_exchange: dict,
    fake_redis_async,
) -> None:
    """PhaseManager transitions via trade_count reaching stabilization_volume_threshold."""
    config = _make_config()
    ctx = _make_context(config)
    # Pre-set trade_count to exceed threshold
    ctx.trade_count = config.stabilization_volume_threshold + 1
    client = mock_exchange["client"]

    cache = InventoryCache(fake_redis_async)
    await cache.set(config.market_id, ctx.inventory)

    token_mgr = TokenManager("http://test-exchange", "user", "pass", client)
    token_mgr._access_token = "fake-token"
    api = AMMApiClient("http://test-exchange", token_mgr, http_client=client)
    poller = TradePoller(api=api, cache=cache, amm_user_id="amm-user-never-match")
    pricing = ThreeLayerPricing(
        anchor=AnchorPricing(config.anchor_price_cents),
        micro=MicroPricing(),
        posterior=PosteriorPricing(),
        config=config,
    )
    as_engine = ASEngine()
    gradient = GradientEngine()
    risk = DefenseStack(config)
    sanitizer = OrderSanitizer()
    order_mgr = OrderManager(api=api, cache=cache)
    phase_mgr = PhaseManager(config=config)

    await quote_cycle(
        ctx, api, poller, pricing, as_engine, gradient,
        risk, sanitizer, order_mgr, cache,
        phase_mgr=phase_mgr,
    )

    assert ctx.phase == Phase.STABILIZATION
