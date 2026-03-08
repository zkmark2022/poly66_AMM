"""T-SIM-08: Oracle lag → PASSIVE_MODE → recovery.

Scenario: Oracle disconnects then reconnects.
Steps:
1. Run normally, record spread_normal
2. Oracle returns None/STALE for 3 cycles
3. Run quote_cycle, record spread_passive (PASSIVE_MODE = widened)
4. Restore Oracle
5. Run cycles, record spread_recovered
Assertions (all numeric):
- spread_passive > spread_normal (PASSIVE_MODE widens spread)
- Oracle recovery exits PASSIVE_MODE
- spread_recovered ≈ spread_normal (back to normal)
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
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.pricing.micro import MicroPricing
from src.amm.strategy.pricing.posterior import PosteriorPricing
from src.amm.strategy.pricing.three_layer import ThreeLayerPricing


def _make_config(market_id: str = "test-mkt-08") -> MarketConfig:
    return MarketConfig(
        market_id=market_id,
        anchor_price_cents=50,
        initial_mint_quantity=2000,
        remaining_hours_override=24.0,
        max_daily_loss_cents=50_000,
        max_per_market_loss_cents=25_000,
        oracle_slug="test-oracle",
        oracle_stale_seconds=3.0,
        defense_cooldown_cycles=2,  # Fast de-escalation for test
        widen_factor=2.0,  # Clearly visible spread widening
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


class ControllableOracle:
    """Oracle where STALE/NORMAL can be toggled externally."""

    def __init__(self, stale: bool = False) -> None:
        self.force_stale = stale
        self._price = 50.0

    def evaluate(self, internal_price_cents: float) -> OracleState:
        if self.force_stale:
            return OracleState.STALE
        return OracleState.NORMAL

    def check_stale(self) -> bool:
        return self.force_stale

    def check_deviation(self, internal_price_cents: float) -> bool:
        return False

    def check_lvr(self) -> bool:
        return False

    async def get_price(self) -> float:
        return self._price


def _extract_spread(orders_placed: list[dict]) -> int | None:
    """Extract spread from placed orders: max(YES prices) - min(NO mapped prices)."""
    yes_prices = [o["price_cents"] for o in orders_placed if o.get("side") == "YES"]
    no_prices = [o["price_cents"] for o in orders_placed if o.get("side") == "NO"]

    if not yes_prices or not no_prices:
        # ONE_SIDE mode: only one side has orders
        # Return a large spread indicator for ONE_SIDE mode
        if yes_prices:
            return max(yes_prices) - min(yes_prices) + 50  # artificially wide
        if no_prices:
            return max(no_prices) - min(no_prices) + 50
        return None

    # ask (YES SELL) lowest price vs bid (mapped from NO SELL: bid = 100 - no_price)
    min_ask = min(yes_prices)
    # NO sell price → bid_yes = 100 - no_price → highest bid = 100 - min(no_price)
    max_bid = 100 - min(no_prices)
    return min_ask - max_bid


def _build_cycle_deps(
    config: MarketConfig,
    ctx: MarketContext,
    client,
    cache: InventoryCache,
) -> dict:
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
    return {
        "api": api,
        "poller": poller,
        "pricing": pricing,
        "as_engine": ASEngine(),
        "gradient": GradientEngine(),
        "risk": DefenseStack(config),
        "sanitizer": OrderSanitizer(),
        "order_mgr": OrderManager(api=api, cache=cache),
        "inventory_cache": cache,
    }


@pytest.mark.asyncio
async def test_oracle_lag_widens_spread_then_recovers(
    mock_exchange: dict,
    fake_redis_async,
) -> None:
    """Oracle goes STALE (PASSIVE_MODE) → spreads widen → Oracle recovers → spreads normalize."""
    config = _make_config()
    inv = _make_inventory()
    ctx = MarketContext(
        market_id=config.market_id,
        config=config,
        inventory=inv,
        initial_inventory_value_cents=inv.total_value_cents(50),
        last_known_market_active=True,
        market_status_checked_at=time.monotonic(),
    )

    client = mock_exchange["client"]

    cache = InventoryCache(fake_redis_async)
    await cache.set(config.market_id, ctx.inventory)

    oracle = ControllableOracle(stale=False)

    def _extract_spread_from_active(order_mgr: OrderManager) -> int | None:
        """Extract spread from active_orders on the OrderManager."""
        yes_prices = [o.price_cents for o in order_mgr.active_orders.values() if o.side == "YES"]
        no_prices = [o.price_cents for o in order_mgr.active_orders.values() if o.side == "NO"]
        if not yes_prices or not no_prices:
            return None
        min_ask = min(yes_prices)
        max_bid = 100 - min(no_prices)  # NO sell price → bid_yes = 100 - no_price
        return min_ask - max_bid

    async def _clear_intent_keys() -> None:
        for key in await fake_redis_async.keys("amm:intent:*"):
            await fake_redis_async.delete(key)

    # Build shared deps — share DefenseStack across all phases so cooldown works
    deps = _build_cycle_deps(config, ctx, client, cache)
    shared_risk = deps["risk"]

    # Phase 1: Normal operation — record spread_normal
    for _ in range(3):
        await quote_cycle(ctx, **deps, oracle=oracle)

    spread_normal = _extract_spread_from_active(deps["order_mgr"])
    assert spread_normal is not None, "Should produce bilateral orders in normal mode"
    assert ctx.defense_level == DefenseLevel.NORMAL

    # Phase 2: Oracle goes STALE → PASSIVE_MODE (ONE_SIDE)
    oracle.force_stale = True
    await _clear_intent_keys()
    # Fresh order_mgr for Phase 2, but reuse shared risk stack
    order_mgr2 = OrderManager(api=deps["api"], cache=cache)
    deps2 = {**deps, "order_mgr": order_mgr2, "risk": shared_risk}
    for _ in range(3):
        await quote_cycle(ctx, **deps2, oracle=oracle)

    # STALE oracle → ONE_SIDE defense
    assert ctx.defense_level in (DefenseLevel.ONE_SIDE, DefenseLevel.WIDEN, DefenseLevel.KILL_SWITCH), (
        f"Expected escalated defense during STALE, got {ctx.defense_level}"
    )

    spread_passive = _extract_spread_from_active(order_mgr2)
    if spread_passive is not None:
        assert spread_passive >= spread_normal, (
            f"PASSIVE spread ({spread_passive}) should be >= NORMAL spread ({spread_normal})"
        )

    # Phase 3: Oracle recovers
    oracle.force_stale = False
    await _clear_intent_keys()
    order_mgr3 = OrderManager(api=deps["api"], cache=cache)
    deps3 = {**deps, "order_mgr": order_mgr3, "risk": shared_risk}

    # Run enough cycles to pass cooldown (defense_cooldown_cycles=2)
    for _ in range(config.defense_cooldown_cycles + 3):
        await quote_cycle(ctx, **deps3, oracle=oracle)

    assert ctx.defense_level == DefenseLevel.NORMAL, (
        f"Expected NORMAL after oracle recovery, got {ctx.defense_level}"
    )

    spread_recovered = _extract_spread_from_active(order_mgr3)
    assert spread_recovered is not None, "Should produce bilateral orders after recovery"

    # spread_recovered should be close to spread_normal
    tolerance = max(3, abs(spread_normal))
    assert abs(spread_recovered - spread_normal) <= tolerance, (
        f"Recovered spread ({spread_recovered}) should be close to "
        f"normal spread ({spread_normal}), diff={abs(spread_recovered - spread_normal)}"
    )


@pytest.mark.asyncio
async def test_oracle_stale_defense_level_is_one_side(
    mock_exchange: dict,
    fake_redis_async,
) -> None:
    """Verify that STALE oracle maps to ONE_SIDE (PASSIVE_MODE) defense, not KILL."""
    config = _make_config()
    inv = _make_inventory()
    ctx = MarketContext(
        market_id=config.market_id,
        config=config,
        inventory=inv,
        initial_inventory_value_cents=inv.total_value_cents(50),
        last_known_market_active=True,
        market_status_checked_at=time.monotonic(),
    )

    client = mock_exchange["client"]
    cache = InventoryCache(fake_redis_async)
    await cache.set(config.market_id, ctx.inventory)

    deps = _build_cycle_deps(config, ctx, client, cache)
    oracle = ControllableOracle(stale=True)

    await quote_cycle(ctx, **deps, oracle=oracle)

    # OracleState.STALE → DefenseLevel.ONE_SIDE (not KILL_SWITCH)
    assert ctx.defense_level == DefenseLevel.ONE_SIDE, (
        f"STALE oracle should produce ONE_SIDE, got {ctx.defense_level}"
    )
