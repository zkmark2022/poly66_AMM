"""Integration tests for AMM Test Plan v2.0 - Phase 7 (Funds & Minting)."""
from __future__ import annotations

from unittest.mock import AsyncMock

from src.amm.config.models import MarketConfig
from src.amm.lifecycle.reinvest import maybe_auto_reinvest
from src.amm.lifecycle.winding_down import handle_winding_down
from src.amm.main import quote_cycle
from src.amm.models.enums import DefenseLevel, Phase
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


class TestPhase7Funds:
    async def test_t71_auto_reinvest_mints_when_cash_above_500(self) -> None:
        """T7.1: cash > $500 triggers auto reinvest mint."""
        ctx = _make_context(cash_cents=50_900, yes=100, no=100)
        api = AsyncMock()

        minted = await maybe_auto_reinvest(ctx, api)

        assert minted == 9
        api.mint.assert_called_once()
        assert ctx.inventory.cash_cents == 50_000

    async def test_t72_mint_consistency(self) -> None:
        """T7.2: yes+=N, no+=N, reserve+=N*100 after mint."""
        ctx = _make_context(
            cash_cents=50_800,
            yes=120,
            no=130,
            yes_cost_sum=6_000,
            no_cost_sum=6_500,
        )
        api = AsyncMock()

        minted = await maybe_auto_reinvest(ctx, api)

        assert minted == 8
        assert ctx.inventory.yes_volume == 128
        assert ctx.inventory.no_volume == 138
        reserve_before = 6_000 + 6_500
        reserve_after = ctx.inventory.yes_cost_sum_cents + ctx.inventory.no_cost_sum_cents
        assert reserve_after - reserve_before == minted * 100

    async def test_t73_cash_depleted_stops_buy_orders(self) -> None:
        """T7.3: when cash=0, stop BUY-side quoting intents."""
        ctx = _make_context(cash_cents=0, yes=500, no=500)
        services = _make_services_for_quote_cycle(ctx)

        await quote_cycle(ctx, **services)

        services["order_mgr"].execute_intents.assert_called_once()
        intents = services["order_mgr"].execute_intents.call_args[0][0]
        assert len(intents) > 0
        assert all(intent.side == "YES" for intent in intents)

    async def test_t74_winding_down_burns_all_holdings(self) -> None:
        """T7.4: market ended -> burn all YES/NO holdings."""
        ctx = _make_context(cash_cents=1_000, yes=300, no=300)
        api = AsyncMock()
        order_mgr = AsyncMock()

        burned = await handle_winding_down(
            ctx=ctx,
            api=api,
            market_status="RESOLVED",
            order_mgr=order_mgr,
        )

        assert burned == 300
        order_mgr.cancel_all.assert_called_once_with(ctx.market_id)
        api.burn.assert_called_once()
        assert ctx.inventory.yes_volume == 0
        assert ctx.inventory.no_volume == 0
        assert ctx.inventory.cash_cents == 31_000
        assert ctx.shutdown_requested is True


def _make_context(
    cash_cents: int,
    yes: int,
    no: int,
    yes_cost_sum: int | None = None,
    no_cost_sum: int | None = None,
) -> MarketContext:
    yes_cost = yes_cost_sum if yes_cost_sum is not None else yes * 50
    no_cost = no_cost_sum if no_cost_sum is not None else no * 50

    return MarketContext(
        market_id="mkt-phase7",
        config=MarketConfig(market_id="mkt-phase7", remaining_hours_override=24.0),
        inventory=Inventory(
            cash_cents=cash_cents,
            yes_volume=yes,
            no_volume=no,
            yes_cost_sum_cents=yes_cost,
            no_cost_sum_cents=no_cost,
            yes_pending_sell=0,
            no_pending_sell=0,
            frozen_balance_cents=0,
        ),
        phase=Phase.STABILIZATION,
        defense_level=DefenseLevel.NORMAL,
    )


def _make_services_for_quote_cycle(ctx: MarketContext) -> dict:
    poller = AsyncMock()
    poller.poll.return_value = []

    api = AsyncMock()
    api.get_orderbook.return_value = {"data": {"best_bid": 48, "best_ask": 52}}

    inventory_cache = AsyncMock()
    inventory_cache.get.return_value = ctx.inventory

    order_mgr = AsyncMock()
    order_mgr.execute_intents = AsyncMock()
    order_mgr.cancel_all = AsyncMock()

    return {
        "api": api,
        "poller": poller,
        "pricing": ThreeLayerPricing(
            anchor=AnchorPricing(ctx.config.anchor_price_cents),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
        ),
        "as_engine": ASEngine(),
        "gradient": GradientEngine(),
        "risk": DefenseStack(ctx.config),
        "sanitizer": OrderSanitizer(),
        "order_mgr": order_mgr,
        "inventory_cache": inventory_cache,
    }
