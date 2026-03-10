"""Tests for AMM runtime state observability — BUG-007.

Verifies GET /state exposes: defense_level, kill_switch, inventory_skew,
phase, hours_remaining, last_requote_ms, and that quote_cycle updates
last_requote_at on the MarketContext.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.amm.config.models import MarketConfig
from src.amm.lifecycle.health import HealthState, create_health_app
from src.amm.models.enums import DefenseLevel, Phase
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext


def _make_inventory(yes: int = 100, no: int = 100) -> Inventory:
    return Inventory(
        cash_cents=500_000,
        yes_volume=yes,
        no_volume=no,
        yes_cost_sum_cents=5_000,
        no_cost_sum_cents=5_000,
        yes_pending_sell=0,
        no_pending_sell=0,
        frozen_balance_cents=0,
    )


def _make_context(
    market_id: str = "mkt-btc",
    defense_level: DefenseLevel = DefenseLevel.NORMAL,
    phase: Phase = Phase.EXPLORATION,
    yes_volume: int = 100,
    no_volume: int = 100,
    remaining_hours_override: float | None = None,
    last_requote_at: float = 0.0,
) -> MarketContext:
    config = MarketConfig(
        market_id=market_id,
        remaining_hours_override=remaining_hours_override,
        exploration_duration_hours=24.0,
    )
    ctx = MarketContext(
        market_id=market_id,
        config=config,
        inventory=_make_inventory(yes=yes_volume, no=no_volume),
        phase=phase,
        defense_level=defense_level,
        last_requote_at=last_requote_at,
    )
    return ctx


class TestStateEndpointEmpty:
    async def test_state_returns_200_with_empty_markets(self) -> None:
        state = HealthState()
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/state")
        assert resp.status_code == 200
        assert resp.json() == {"markets": {}}

    async def test_state_always_returns_200(self) -> None:
        """State endpoint is always available, even when not ready."""
        state = HealthState(ready=False)
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/state")
        assert resp.status_code == 200


class TestStateEndpointFields:
    async def test_state_returns_defense_level(self) -> None:
        ctx = _make_context(defense_level=DefenseLevel.WIDEN)
        state = HealthState(contexts={"mkt-btc": ctx})
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            data = (await client.get("/state")).json()
        assert data["markets"]["mkt-btc"]["defense_level"] == "WIDEN"

    async def test_state_returns_kill_switch_false_when_normal(self) -> None:
        ctx = _make_context(defense_level=DefenseLevel.NORMAL)
        state = HealthState(contexts={"mkt-btc": ctx})
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            data = (await client.get("/state")).json()
        assert data["markets"]["mkt-btc"]["kill_switch"] is False

    async def test_state_returns_kill_switch_true_when_kill_switch_level(self) -> None:
        ctx = _make_context(defense_level=DefenseLevel.KILL_SWITCH)
        state = HealthState(contexts={"mkt-btc": ctx})
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            data = (await client.get("/state")).json()
        assert data["markets"]["mkt-btc"]["kill_switch"] is True

    async def test_state_returns_inventory_skew(self) -> None:
        # yes=150, no=50 → skew = (150-50)/(150+50) = 0.5
        ctx = _make_context(yes_volume=150, no_volume=50)
        state = HealthState(contexts={"mkt-btc": ctx})
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            data = (await client.get("/state")).json()
        assert abs(data["markets"]["mkt-btc"]["inventory_skew"] - 0.5) < 1e-6

    async def test_state_returns_phase(self) -> None:
        ctx = _make_context(phase=Phase.STABILIZATION)
        state = HealthState(contexts={"mkt-btc": ctx})
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            data = (await client.get("/state")).json()
        assert data["markets"]["mkt-btc"]["phase"] == "STABILIZATION"

    async def test_state_returns_hours_remaining_from_override(self) -> None:
        ctx = _make_context(remaining_hours_override=6.5)
        state = HealthState(contexts={"mkt-btc": ctx})
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            data = (await client.get("/state")).json()
        assert data["markets"]["mkt-btc"]["hours_remaining"] == pytest.approx(6.5, abs=0.01)

    async def test_state_computes_hours_remaining_when_no_override(self) -> None:
        """When no remaining_hours_override, hours_remaining = exploration_duration - elapsed."""
        ctx = _make_context(remaining_hours_override=None)
        # started_at is set to time.monotonic() at creation, so elapsed ≈ 0
        state = HealthState(contexts={"mkt-btc": ctx})
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            data = (await client.get("/state")).json()
        hours_remaining = data["markets"]["mkt-btc"]["hours_remaining"]
        # Should be close to 24.0 (exploration_duration_hours) since just created
        assert 23.0 < hours_remaining <= 24.0

    async def test_state_hours_remaining_never_negative(self) -> None:
        """hours_remaining clamps to 0 when elapsed exceeds exploration_duration."""
        ctx = _make_context(remaining_hours_override=None)
        # Backdate started_at by 30 hours to simulate expired market
        ctx.started_at = time.monotonic() - 30 * 3600
        state = HealthState(contexts={"mkt-btc": ctx})
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            data = (await client.get("/state")).json()
        assert data["markets"]["mkt-btc"]["hours_remaining"] == 0.0

    async def test_state_returns_last_requote_ms_none_when_never_requoted(self) -> None:
        ctx = _make_context(last_requote_at=0.0)
        state = HealthState(contexts={"mkt-btc": ctx})
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            data = (await client.get("/state")).json()
        assert data["markets"]["mkt-btc"]["last_requote_ms"] is None

    async def test_state_returns_last_requote_ms_positive_when_recently_requoted(self) -> None:
        ctx = _make_context(last_requote_at=time.monotonic() - 0.5)  # 500ms ago
        state = HealthState(contexts={"mkt-btc": ctx})
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            data = (await client.get("/state")).json()
        ms = data["markets"]["mkt-btc"]["last_requote_ms"]
        assert ms is not None
        assert 0 < ms < 5000  # should be between 0 and 5 seconds

    async def test_state_returns_multiple_markets(self) -> None:
        ctx1 = _make_context(market_id="mkt-btc", defense_level=DefenseLevel.NORMAL)
        ctx2 = _make_context(market_id="mkt-eth", defense_level=DefenseLevel.WIDEN)
        state = HealthState(contexts={"mkt-btc": ctx1, "mkt-eth": ctx2})
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            data = (await client.get("/state")).json()
        assert "mkt-btc" in data["markets"]
        assert "mkt-eth" in data["markets"]
        assert data["markets"]["mkt-btc"]["defense_level"] == "NORMAL"
        assert data["markets"]["mkt-eth"]["defense_level"] == "WIDEN"


class TestMarketContextLastRequoteAt:
    def test_last_requote_at_defaults_to_zero(self) -> None:
        ctx = _make_context()
        assert ctx.last_requote_at == 0.0

    def test_last_requote_at_can_be_set(self) -> None:
        ctx = _make_context()
        ctx.last_requote_at = time.monotonic()
        assert ctx.last_requote_at > 0.0


class TestQuoteCycleUpdatesLastRequoteAt:
    async def test_quote_cycle_updates_last_requote_at(self) -> None:
        """After quote_cycle completes, ctx.last_requote_at must be > 0."""
        from src.amm.main import quote_cycle
        from src.amm.risk.defense_stack import DefenseStack
        from src.amm.risk.sanitizer import OrderSanitizer
        from src.amm.strategy.as_engine import ASEngine
        from src.amm.strategy.gradient import GradientEngine
        from src.amm.strategy.phase_manager import PhaseManager
        from src.amm.strategy.pricing.three_layer import ThreeLayerPricing
        from src.amm.strategy.pricing.anchor import AnchorPricing
        from src.amm.strategy.pricing.micro import MicroPricing
        from src.amm.strategy.pricing.posterior import PosteriorPricing

        ctx = _make_context()
        assert ctx.last_requote_at == 0.0

        config = ctx.config
        pricing = ThreeLayerPricing(
            anchor=AnchorPricing(config.anchor_price_cents),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
            config=config,
        )

        api = AsyncMock()
        api.get_orderbook = AsyncMock(return_value={"data": {"best_bid": 48, "best_ask": 52, "bid_depth": 100, "ask_depth": 100}})
        api.get_market_status = AsyncMock(return_value="active")
        api.cancel_all = AsyncMock()

        poller = AsyncMock()
        poller.poll = AsyncMock(return_value=[])

        inventory_cache = AsyncMock()
        inventory_cache.get = AsyncMock(return_value=None)

        order_mgr = AsyncMock()
        order_mgr.execute_intents = AsyncMock()
        order_mgr.cancel_all = AsyncMock()

        before = time.monotonic()
        await quote_cycle(
            ctx=ctx,
            api=api,
            poller=poller,
            pricing=pricing,
            as_engine=ASEngine(),
            gradient=GradientEngine(),
            risk=DefenseStack(config),
            sanitizer=OrderSanitizer(),
            order_mgr=order_mgr,
            inventory_cache=inventory_cache,
            oracle=None,
            phase_mgr=PhaseManager(config=config),
        )
        after = time.monotonic()

        assert ctx.last_requote_at > 0.0
        assert before <= ctx.last_requote_at <= after
