"""Integration tests for the AMM quote cycle orchestrator."""
from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.amm.main import quote_cycle, run_market
from src.amm.models.market_context import MarketContext
from src.amm.models.inventory import Inventory
from src.amm.config.models import MarketConfig
from src.amm.models.enums import Phase, DefenseLevel
from src.amm.oracle.polymarket_oracle import OracleState
from src.amm.strategy.pricing.three_layer import ThreeLayerPricing
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.pricing.micro import MicroPricing
from src.amm.strategy.pricing.posterior import PosteriorPricing
from src.amm.strategy.as_engine import ASEngine
from src.amm.strategy.gradient import GradientEngine
from src.amm.risk.defense_stack import DefenseStack
from src.amm.risk.sanitizer import OrderSanitizer


def _make_inventory(yes: int = 500, no: int = 500) -> Inventory:
    return Inventory(
        cash_cents=500_000,
        yes_volume=yes, no_volume=no,
        yes_cost_sum_cents=yes * 50, no_cost_sum_cents=no * 50,
        yes_pending_sell=0, no_pending_sell=0, frozen_balance_cents=0,
    )


def _make_ctx(market_id: str = "mkt-1", defense: DefenseLevel = DefenseLevel.NORMAL) -> MarketContext:
    return MarketContext(
        market_id=market_id,
        config=MarketConfig(market_id=market_id, remaining_hours_override=24.0),
        inventory=_make_inventory(),
        phase=Phase.EXPLORATION,
        defense_level=defense,
    )


def _make_api(
    orderbook: dict | None = None,
    market_status: str = "active",
) -> AsyncMock:
    api = AsyncMock()
    api.get_orderbook.return_value = orderbook or {
        "data": {"best_bid": 48, "best_ask": 52}
    }
    api.get_market_status.return_value = market_status
    return api


def _make_services(
    inventory: Inventory | None = None,
    api_orders: list[dict] | None = None,
    api: AsyncMock | None = None,
) -> dict:
    poller = AsyncMock()
    poller.poll.return_value = []

    pricing = ThreeLayerPricing(
        anchor=AnchorPricing(50),
        micro=MicroPricing(),
        posterior=PosteriorPricing(),
    )
    as_engine = ASEngine()
    gradient = GradientEngine()

    inventory_cache = AsyncMock()
    inventory_cache.get.return_value = inventory or _make_inventory()

    order_mgr = AsyncMock()
    order_mgr.active_orders = {}
    order_mgr.cancel_all = AsyncMock()
    order_mgr.execute_intents = AsyncMock()

    return {
        "api": api or _make_api(),
        "poller": poller,
        "pricing": pricing,
        "as_engine": as_engine,
        "gradient": gradient,
        "risk": DefenseStack(MarketConfig(market_id="mkt-1")),
        "sanitizer": OrderSanitizer(),
        "order_mgr": order_mgr,
        "inventory_cache": inventory_cache,
    }


class TestQuoteCycle:
    async def test_single_cycle_produces_orders(self) -> None:
        """One quote cycle: sync → strategy → risk → execute with orders."""
        ctx = _make_ctx()
        services = _make_services()
        await quote_cycle(ctx, **services)
        services["order_mgr"].execute_intents.assert_called_once()
        # Should have produced some intents (ask + bid ladders)
        intents = services["order_mgr"].execute_intents.call_args[0][0]
        assert len(intents) > 0

    async def test_cycle_polls_trades_first(self) -> None:
        ctx = _make_ctx()
        services = _make_services()
        await quote_cycle(ctx, **services)
        services["poller"].poll.assert_called_once_with("mkt-1")

    async def test_cycle_fetches_live_orderbook(self) -> None:
        """quote_cycle must call get_orderbook to feed live bid/ask into pricing."""
        ctx = _make_ctx()
        api = _make_api(orderbook={"data": {"best_bid": 44, "best_ask": 56}})
        services = _make_services(api=api)
        await quote_cycle(ctx, **services)
        api.get_orderbook.assert_called_once_with("mkt-1")

    async def test_cycle_uses_orderbook_fallback_on_error(self) -> None:
        """If orderbook fetch fails, cycle continues using anchor fallback."""
        ctx = _make_ctx()
        api = AsyncMock()
        api.get_orderbook.side_effect = Exception("network error")
        api.get_market_status.return_value = "active"
        services = _make_services(api=api)
        # Should not raise; fallback kicks in
        await quote_cycle(ctx, **services)
        services["order_mgr"].execute_intents.assert_called_once()

    async def test_cycle_refreshes_inventory_from_cache(self) -> None:
        ctx = _make_ctx()
        fresh = _make_inventory(yes=600, no=400)
        services = _make_services(inventory=fresh)
        await quote_cycle(ctx, **services)
        assert ctx.inventory.yes_volume == 600

    async def test_cycle_respects_kill_switch(self) -> None:
        """KILL_SWITCH defense level cancels all and stops quoting."""
        ctx = _make_ctx()
        # Force extreme skew to trigger KILL_SWITCH
        ctx.inventory = _make_inventory(yes=900, no=100)
        services = _make_services(inventory=_make_inventory(yes=900, no=100))
        # Override risk with a config that trips KILL_SWITCH at this skew
        cfg = MarketConfig(market_id="mkt-1", inventory_skew_kill=0.7,
                           remaining_hours_override=24.0)
        ctx.config = cfg
        services["risk"] = DefenseStack(cfg)

        await quote_cycle(ctx, **services)

        assert ctx.defense_level == DefenseLevel.KILL_SWITCH
        services["order_mgr"].cancel_all.assert_called_once_with("mkt-1")
        services["order_mgr"].execute_intents.assert_not_called()

    async def test_cycle_handles_recoverable_api_error_gracefully(self) -> None:
        """Recoverable API failure (network transport) is tolerated — run_market doesn't crash."""
        import httpx

        ctx = _make_ctx()
        ctx.config = MarketConfig(market_id="mkt-1", quote_interval_seconds=0.01,
                                  remaining_hours_override=24.0)
        services = _make_services()
        services["order_mgr"].execute_intents.side_effect = httpx.TransportError("network error")

        # poll sets shutdown_requested after first call so loop exits
        call_count = 0

        async def _poll_then_stop(market_id: str) -> list[dict]:
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                ctx.shutdown_requested = True
            return []

        services["poller"].poll.side_effect = _poll_then_stop

        # Recoverable network error: run_market should not crash
        await run_market(ctx, services)

    async def test_cycle_raises_on_unrecoverable_error(self) -> None:
        """Programming errors (TypeError etc.) must propagate and set shutdown_requested."""
        ctx = _make_ctx()
        ctx.config = MarketConfig(market_id="mkt-1", quote_interval_seconds=0.01,
                                  remaining_hours_override=24.0)
        services = _make_services()
        services["order_mgr"].execute_intents.side_effect = TypeError("bug: bad type")

        with pytest.raises(TypeError, match="bug"):
            await run_market(ctx, services)

        assert ctx.shutdown_requested is True

    async def test_cycle_updates_defense_level_on_context(self) -> None:
        ctx = _make_ctx()
        services = _make_services()
        await quote_cycle(ctx, **services)
        # Normal conditions → NORMAL defense
        assert ctx.defense_level == DefenseLevel.NORMAL

    async def test_cycle_widen_defense_reduces_but_does_not_stop_quoting(self) -> None:
        ctx = _make_ctx()
        ctx.inventory = _make_inventory(yes=650, no=350)
        cfg = MarketConfig(market_id="mkt-1", inventory_skew_widen=0.2,
                           remaining_hours_override=24.0)
        ctx.config = cfg
        services = _make_services(inventory=_make_inventory(yes=650, no=350))
        services["risk"] = DefenseStack(cfg)

        await quote_cycle(ctx, **services)

        assert ctx.defense_level == DefenseLevel.WIDEN
        # execute_intents still called (WIDEN doesn't stop quoting)
        services["order_mgr"].execute_intents.assert_called_once()

    async def test_cycle_evaluates_oracle_without_refreshing_every_cycle(self) -> None:
        ctx = _make_ctx()
        ctx.config = MarketConfig(
            market_id="mkt-1",
            oracle_slug="test-oracle",
            remaining_hours_override=24.0,
        )
        services = _make_services()
        oracle = SimpleNamespace(
            refresh=MagicMock(),
            evaluate=MagicMock(return_value=OracleState.NORMAL),
        )

        await quote_cycle(ctx, oracle=oracle, **services)

        oracle.refresh.assert_not_called()
        oracle.evaluate.assert_called_once()
