"""T-SIM-04: KILL trigger stops quoting.

Scenario: Inventory loss exceeds KILL threshold.
Steps:
1. Construct inventory state that triggers KILL_SWITCH
2. Run quote_cycle 3 times
Assertions:
- DefenseLevel enters KILL_SWITCH
- KILL-triggered quote_cycles produce NO new orders (orders_placed count frozen)
- Existing orders are cancelled (batch-cancel is called)
"""
from __future__ import annotations

import time

import pytest

from src.amm.cache.inventory_cache import InventoryCache
from src.amm.config.models import MarketConfig
from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.auth import TokenManager
from src.amm.connector.order_manager import OrderManager
from src.amm.connector.trade_poller import TradePoller
from src.amm.main import quote_cycle
from src.amm.models.enums import DefenseLevel
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext
from src.amm.risk.defense_stack import DefenseStack
from src.amm.risk.sanitizer import OrderSanitizer
from src.amm.strategy.as_engine import ASEngine
from src.amm.strategy.gradient import GradientEngine
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.pricing.micro import MicroPricing
from src.amm.strategy.pricing.posterior import PosteriorPricing
from src.amm.strategy.pricing.three_layer import ThreeLayerPricing


def _make_kill_config(market_id: str = "test-mkt-04") -> MarketConfig:
    return MarketConfig(
        market_id=market_id,
        anchor_price_cents=50,
        initial_mint_quantity=2000,
        remaining_hours_override=24.0,
        max_daily_loss_cents=50_000,
        max_per_market_loss_cents=10_000,  # Low threshold to trigger KILL
        inventory_skew_kill=0.8,
    )


def _make_skewed_inventory() -> Inventory:
    """Inventory with extreme skew (>0.8) to trigger KILL_SWITCH."""
    return Inventory(
        cash_cents=50_000,
        yes_volume=1800,
        no_volume=200,
        yes_cost_sum_cents=90_000,
        no_cost_sum_cents=10_000,
        yes_pending_sell=0,
        no_pending_sell=0,
        frozen_balance_cents=0,
    )


def _make_context(config: MarketConfig) -> MarketContext:
    inv = _make_skewed_inventory()
    return MarketContext(
        market_id=config.market_id,
        config=config,
        inventory=inv,
        initial_inventory_value_cents=inv.total_value_cents(50),
        last_known_market_active=True,
        market_status_checked_at=time.monotonic(),
    )


@pytest.mark.asyncio
async def test_kill_switch_stops_quoting(
    mock_exchange: dict,
    fake_redis_async,
) -> None:
    """When inventory skew exceeds kill threshold, defense escalates to KILL_SWITCH,
    all existing orders are cancelled, and no new orders are placed."""
    config = _make_kill_config()
    ctx = _make_context(config)
    client = mock_exchange["client"]
    orders_placed = mock_exchange["orders_placed"]
    call_log = mock_exchange["call_log"]

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

    # Pre-KILL: Run one normal cycle with balanced inventory so order_mgr has active orders.
    # This makes the subsequent batch-cancel call meaningful (cancelling real pre-existing orders).
    balanced_inv = Inventory(
        cash_cents=50_000,
        yes_volume=1000,
        no_volume=1000,
        yes_cost_sum_cents=50_000,
        no_cost_sum_cents=50_000,
        yes_pending_sell=0,
        no_pending_sell=0,
        frozen_balance_cents=0,
    )
    from src.amm.models.market_context import MarketContext as _MC
    import time as _time
    balanced_ctx = _MC(
        market_id=config.market_id,
        config=config,
        inventory=balanced_inv,
        initial_inventory_value_cents=balanced_inv.total_value_cents(50),
        last_known_market_active=True,
        market_status_checked_at=_time.monotonic(),
    )
    await cache.set(config.market_id, balanced_inv)
    await quote_cycle(
        balanced_ctx, api, poller, pricing, as_engine, gradient,
        risk, sanitizer, order_mgr, cache,
    )
    assert len(order_mgr.active_orders) > 0, (
        "Pre-existing orders must exist before KILL_SWITCH scenario"
    )
    # Reset cache to skewed inventory for the KILL cycles
    await cache.set(config.market_id, ctx.inventory)

    # Snapshot order count before KILL cycles to isolate assertions to KILL phase
    orders_before_kill = len(orders_placed)

    # Run 3 cycles with KILL-triggering inventory
    for _ in range(3):
        await quote_cycle(
            ctx, api, poller, pricing, as_engine, gradient,
            risk, sanitizer, order_mgr, cache,
        )

    # Assert 1: Defense level is KILL_SWITCH
    assert ctx.defense_level == DefenseLevel.KILL_SWITCH, (
        f"Expected KILL_SWITCH, got {ctx.defense_level}"
    )

    # Assert 2: No NEW orders placed during KILL (KILL returns before execute_intents)
    orders_during_kill = len(orders_placed) - orders_before_kill
    assert orders_during_kill == 0, (
        f"Expected 0 orders placed during KILL, got {orders_during_kill}"
    )

    # Assert 3: Batch-cancel was called (existing orders cancelled)
    cancel_calls = [
        c for c in call_log
        if c["path"] == "/amm/orders/batch-cancel"
    ]
    assert len(cancel_calls) >= 1, (
        "Expected at least one batch-cancel call during KILL_SWITCH"
    )


@pytest.mark.asyncio
async def test_kill_from_pnl_loss(
    mock_exchange: dict,
    fake_redis_async,
) -> None:
    """KILL_SWITCH triggers when session P&L exceeds max_per_market_loss_cents."""
    config = MarketConfig(
        market_id="test-mkt-04-pnl",
        anchor_price_cents=50,
        initial_mint_quantity=2000,
        remaining_hours_override=24.0,
        max_daily_loss_cents=50_000,
        max_per_market_loss_cents=5_000,
    )
    # Balanced inventory but set initial_inventory_value_cents high to create
    # a large negative session_pnl_cents
    inv = Inventory(
        cash_cents=50_000,
        yes_volume=1000,
        no_volume=1000,
        yes_cost_sum_cents=50_000,
        no_cost_sum_cents=50_000,
        yes_pending_sell=0,
        no_pending_sell=0,
        frozen_balance_cents=0,
    )
    ctx = MarketContext(
        market_id=config.market_id,
        config=config,
        inventory=inv,
        # Set initial value much higher than current value to trigger P&L KILL
        initial_inventory_value_cents=inv.total_value_cents(50) + 10_000,
        last_known_market_active=True,
        market_status_checked_at=time.monotonic(),
    )

    client = mock_exchange["client"]
    orders_placed = mock_exchange["orders_placed"]

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

    await quote_cycle(
        ctx, api, poller, pricing, as_engine, gradient,
        risk, sanitizer, order_mgr, cache,
    )

    assert ctx.defense_level == DefenseLevel.KILL_SWITCH
    assert len(orders_placed) == 0

    call_log = mock_exchange["call_log"]
    cancel_calls = [c for c in call_log if c["path"] == "/amm/orders/batch-cancel"]
    assert len(cancel_calls) >= 1, (
        "KILL_SWITCH triggered by P&L loss must call batch-cancel to clear existing orders"
    )
