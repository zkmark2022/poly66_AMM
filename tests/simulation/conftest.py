"""Shared fixtures for Layer 3 simulation tests.

Provides lightweight factory helpers so individual test files can compose
test scenarios without duplicating mock setup.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from src.amm.config.models import MarketConfig
from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.order_manager import OrderManager
from src.amm.connector.trade_poller import TradePoller
from src.amm.cache.inventory_cache import InventoryCache
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext
from src.amm.risk.defense_stack import DefenseStack
from src.amm.risk.sanitizer import OrderSanitizer
from src.amm.strategy.as_engine import ASEngine
from src.amm.strategy.gradient import GradientEngine
from src.amm.strategy.models import OrderIntent
from src.amm.strategy.phase_manager import PhaseManager
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.pricing.micro import MicroPricing
from src.amm.strategy.pricing.posterior import PosteriorPricing
from src.amm.strategy.pricing.three_layer import ThreeLayerPricing


# ---------------------------------------------------------------------------
# Inventory helpers
# ---------------------------------------------------------------------------

def make_inventory(
    yes_volume: int = 200,
    no_volume: int = 200,
    cash_cents: int = 500_000,
    yes_pending_sell: int = 0,
    no_pending_sell: int = 0,
) -> Inventory:
    return Inventory(
        cash_cents=cash_cents,
        yes_volume=yes_volume,
        no_volume=no_volume,
        yes_cost_sum_cents=yes_volume * 50,
        no_cost_sum_cents=no_volume * 50,
        yes_pending_sell=yes_pending_sell,
        no_pending_sell=no_pending_sell,
        frozen_balance_cents=0,
    )


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def make_config(
    market_id: str = "sim-mkt-test",
    remaining_hours: float | None = 24.0,
    **overrides: Any,
) -> MarketConfig:
    """Build a MarketConfig with simulation-friendly defaults.

    Any kwarg in *overrides* takes precedence over the built-in defaults,
    preventing keyword-collision errors when callers pass fields like
    ``inventory_skew_widen`` explicitly.
    """
    defaults: dict[str, Any] = {
        "remaining_hours_override": remaining_hours,
        "anchor_price_cents": 50,
        "spread_min_cents": 2,
        "spread_max_cents": 30,   # wider cap so WIDEN tests can see numeric diff
        "gradient_levels": 3,
        "gradient_price_step_cents": 1,
        "gradient_quantity_decay": 0.5,
        "initial_mint_quantity": 600,
        "defense_cooldown_cycles": 5,
        "kappa": 1.5,
    }
    defaults.update(overrides)
    return MarketConfig(market_id=market_id, **defaults)


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def make_context(
    market_id: str = "sim-mkt-test",
    inventory: Inventory | None = None,
    config: MarketConfig | None = None,
    market_active: bool = True,
) -> MarketContext:
    inv = inventory or make_inventory()
    cfg = config or make_config(market_id=market_id)
    ctx = MarketContext(
        market_id=market_id,
        config=cfg,
        inventory=inv,
        initial_inventory_value_cents=inv.total_value_cents(cfg.anchor_price_cents),
        last_known_market_active=market_active,
        # Set far in the past so market-status TTL triggers, but we mock the
        # API call so the result is deterministic.
        market_status_checked_at=0.0,
    )
    return ctx


# ---------------------------------------------------------------------------
# Mock service helpers
# ---------------------------------------------------------------------------

def make_mock_api() -> AsyncMock:
    """Return a mock AMMApiClient with sensible defaults."""
    api = AsyncMock(spec=AMMApiClient)
    api.get_orderbook.return_value = {
        "data": {
            "best_bid": 48,
            "best_ask": 52,
            "bid_depth": 10,
            "ask_depth": 10,
        }
    }
    api.get_market_status.return_value = "active"
    api.cancel_order.return_value = {}
    api.place_order.return_value = {"order_id": "mock-order-id"}
    api.batch_cancel.return_value = {}
    api.get_trades.return_value = {"data": {"trades": []}}
    return api


def make_mock_poller() -> AsyncMock:
    poller = AsyncMock(spec=TradePoller)
    poller.poll.return_value = []
    return poller


def make_mock_inventory_cache() -> AsyncMock:
    cache = AsyncMock(spec=InventoryCache)
    cache.get.return_value = None  # no cached update
    return cache


class CapturingOrderManager:
    """Thin wrapper around AsyncMock that records all intents passed to execute_intents."""

    def __init__(self) -> None:
        self._mock = AsyncMock(spec=OrderManager)
        self.captured: list[list[OrderIntent]] = []

        async def _capture(intents: list[OrderIntent], market_id: str) -> None:
            self.captured.append(list(intents))

        self._mock.execute_intents.side_effect = _capture
        self._mock.cancel_all = AsyncMock(return_value=None)

    # Delegate attribute access to the inner mock so quote_cycle sees
    # a compatible object.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._mock, name)

    @property
    def all_intents(self) -> list[OrderIntent]:
        """Flat list of every intent submitted across all execute_intents calls."""
        return [i for batch in self.captured for i in batch]


# ---------------------------------------------------------------------------
# Real strategy service builder
# ---------------------------------------------------------------------------

def make_real_services(
    ctx: MarketContext,
    api: AsyncMock | None = None,
    poller: AsyncMock | None = None,
    inventory_cache: AsyncMock | None = None,
    order_mgr: CapturingOrderManager | None = None,
    phase_mgr: PhaseManager | None = None,
) -> tuple[dict[str, Any], CapturingOrderManager]:
    """Build a services dict with real strategy/risk objects and mock I/O.

    Returns (services_dict, capturing_order_manager).
    """
    _api = api or make_mock_api()
    _poller = poller or make_mock_poller()
    _cache = inventory_cache or make_mock_inventory_cache()
    _order_mgr = order_mgr or CapturingOrderManager()
    _phase_mgr = phase_mgr or PhaseManager(config=ctx.config)

    pricing = ThreeLayerPricing(
        anchor=AnchorPricing(ctx.config.anchor_price_cents),
        micro=MicroPricing(),
        posterior=PosteriorPricing(),
        config=ctx.config,
    )

    services = {
        "api": _api,
        "poller": _poller,
        "pricing": pricing,
        "as_engine": ASEngine(),
        "gradient": GradientEngine(),
        "risk": DefenseStack(ctx.config),
        "sanitizer": OrderSanitizer(),
        "order_mgr": _order_mgr,
        "inventory_cache": _cache,
        "phase_mgr": _phase_mgr,
    }
    return services, _order_mgr


# ---------------------------------------------------------------------------
# Spread computation helper
# ---------------------------------------------------------------------------

def compute_effective_spread(intents: list[OrderIntent]) -> int | None:
    """Compute effective YES-price spread from submitted order intents.

    YES SELL intents  → ask side (price = YES ask)
    NO  SELL intents  → bid side (YES-equiv bid = 100 - NO_ask)

    Returns ask_yes - bid_yes_equiv, or None if either side is missing.
    """
    yes_prices = [i.price_cents for i in intents if i.side == "YES" and i.direction == "SELL"]
    no_prices = [i.price_cents for i in intents if i.side == "NO" and i.direction == "SELL"]
    if not yes_prices or not no_prices:
        return None
    effective_ask = min(yes_prices)
    effective_bid = 100 - min(no_prices)   # convert lowest NO price to YES bid
    return effective_ask - effective_bid
