"""T-SIM-02: Oracle deadlock regression test.

Background: Known bug — maybe_auto_reinvest called between two inventory_lock
acquisitions caused a race condition. PR #31 fixed this by restructuring the
lock acquisition order. This test ensures the fix holds.

Scenario: Oracle last_update=None (system just started, no price fetched yet).
The oracle starts STALE (check_stale returns True → OracleState.STALE →
DefenseLevel.ONE_SIDE). After the first refresh(), it transitions to NORMAL
and produces bilateral quotes (both YES and NO sides).

Assertions:
- First quote_cycle does NOT permanently lock into ONE_SIDE
- After oracle refresh, bilateral quotes (YES + NO) are produced
- No exceptions thrown
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
from src.amm.oracle.polymarket_oracle import OracleState
from src.amm.risk.defense_stack import DefenseStack
from src.amm.risk.sanitizer import OrderSanitizer
from src.amm.strategy.as_engine import ASEngine
from src.amm.strategy.gradient import GradientEngine
from src.amm.strategy.phase_manager import PhaseManager
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.pricing.micro import MicroPricing
from src.amm.strategy.pricing.posterior import PosteriorPricing
from src.amm.strategy.pricing.three_layer import ThreeLayerPricing


def _make_config(market_id: str = "test-mkt-02") -> MarketConfig:
    return MarketConfig(
        market_id=market_id,
        anchor_price_cents=50,
        initial_mint_quantity=2000,
        remaining_hours_override=24.0,
        max_daily_loss_cents=50_000,
        max_per_market_loss_cents=25_000,
        oracle_slug="test-oracle",
        oracle_stale_seconds=3.0,
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


class FakeOracle:
    """Oracle that starts with no data (STALE) and becomes NORMAL after refresh."""

    def __init__(self, config: MarketConfig) -> None:
        self._config = config
        self._refreshed = False
        self._last_refresh_time: float | None = None
        self._price_history: list[tuple[float, float]] = []

    def refresh(self) -> None:
        now = time.monotonic()
        self._last_refresh_time = now
        self._price_history.append((now, 50.0))
        self._refreshed = True

    def check_stale(self) -> bool:
        if self._last_refresh_time is None:
            return True
        return (time.monotonic() - self._last_refresh_time) > self._config.oracle_stale_seconds

    def check_deviation(self, internal_price_cents: float) -> bool:
        if not self._price_history:
            return False
        return abs(self._price_history[-1][1] - internal_price_cents) > self._config.oracle_deviation_cents

    def check_lvr(self) -> bool:
        return False

    def evaluate(self, internal_price_cents: float) -> OracleState:
        if self.check_stale():
            return OracleState.STALE
        if self.check_deviation(internal_price_cents):
            return OracleState.DEVIATION
        return OracleState.NORMAL

    async def get_price(self) -> float | None:
        return 50.0


@pytest.mark.asyncio
async def test_oracle_stale_does_not_permanently_lock_one_side(
    mock_exchange: dict,
    fake_redis_async,
) -> None:
    """Regression: inventory_lock race condition — Oracle starts STALE (last_update=None),
    first quote_cycle enters ONE_SIDE via oracle defense, but must NOT stay there permanently
    once oracle is refreshed."""
    config = _make_config()
    ctx = _make_context(config)
    client = mock_exchange["client"]
    _orders_placed = mock_exchange["orders_placed"]

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

    oracle = FakeOracle(config)

    # Cycle 1: Oracle is STALE → defense escalates to ONE_SIDE
    await quote_cycle(
        ctx, api, poller, pricing, as_engine, gradient,
        risk, sanitizer, order_mgr, cache,
        oracle=oracle, phase_mgr=phase_mgr,
    )

    assert ctx.defense_level != DefenseLevel.KILL_SWITCH, (
        "STALE oracle should produce ONE_SIDE, not KILL_SWITCH"
    )

    # Refresh oracle to make it NORMAL
    oracle.refresh()

    # Use a fresh OrderManager so active_orders from cycle 1 don't mask new placements
    order_mgr2 = OrderManager(api=api, cache=cache)

    # Clear intent dedup keys (simulates TTL expiry between operational phases)
    for key in await fake_redis_async.keys("amm:intent:*"):
        await fake_redis_async.delete(key)

    # Run several more cycles — defense should de-escalate
    for _ in range(config.defense_cooldown_cycles + 2):
        await quote_cycle(
            ctx, api, poller, pricing, as_engine, gradient,
            risk, sanitizer, order_mgr2, cache,
            oracle=oracle, phase_mgr=phase_mgr,
        )

    # After cooldown, defense should be back to NORMAL
    assert ctx.defense_level == DefenseLevel.NORMAL, (
        f"Expected NORMAL after oracle recovery, got {ctx.defense_level}"
    )

    # Verify bilateral quotes: both YES and NO sides present in active orders
    yes_active = [o for o in order_mgr2.active_orders.values() if o.side == "YES"]
    no_active = [o for o in order_mgr2.active_orders.values() if o.side == "NO"]
    assert len(yes_active) > 0, "Expected YES-side active orders after oracle recovery"
    assert len(no_active) > 0, "Expected NO-side active orders after oracle recovery"


@pytest.mark.asyncio
async def test_oracle_deadlock_no_exceptions(
    mock_exchange: dict,
    fake_redis_async,
) -> None:
    """Regression: ensure no exceptions are raised during the oracle STALE → NORMAL transition,
    which previously caused deadlocks via inventory_lock contention in maybe_auto_reinvest."""
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

    oracle = FakeOracle(config)

    # Run cycle with STALE oracle — should not throw
    await quote_cycle(
        ctx, api, poller, pricing, as_engine, gradient,
        risk, sanitizer, order_mgr, cache,
        oracle=oracle,
    )

    # Refresh and run again — no exceptions
    oracle.refresh()
    await quote_cycle(
        ctx, api, poller, pricing, as_engine, gradient,
        risk, sanitizer, order_mgr, cache,
        oracle=oracle,
    )
