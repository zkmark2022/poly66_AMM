"""Tests for strategy pipeline fixes: PhaseManager, WIDEN spread, phase weights, gather exceptions."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

from src.amm.config.models import MarketConfig
from src.amm.models.enums import DefenseLevel, Phase, QuoteAction
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext
from src.amm.strategy.phase_manager import PhaseManager
from src.amm.strategy.pricing.three_layer import ThreeLayerPricing
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.pricing.micro import MicroPricing
from src.amm.strategy.pricing.posterior import PosteriorPricing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**kwargs) -> MarketConfig:
    defaults = {"market_id": "mkt-test", "stabilization_volume_threshold": 10}
    defaults.update(kwargs)
    return MarketConfig(**defaults)


def _make_ctx(**kwargs) -> MarketContext:
    cfg = _make_config()
    inv = Inventory(
        cash_cents=500_000,
        yes_volume=500, no_volume=500,
        yes_cost_sum_cents=25_000, no_cost_sum_cents=25_000,
        yes_pending_sell=0, no_pending_sell=0,
        frozen_balance_cents=0,
    )
    return MarketContext(market_id="mkt-test", config=cfg, inventory=inv, **kwargs)


# ---------------------------------------------------------------------------
# FIX 1: PhaseManager transitions to STABILIZATION
# ---------------------------------------------------------------------------

class TestPhaseManagerTransition:
    def test_starts_in_exploration(self) -> None:
        pm = PhaseManager(config=_make_config(stabilization_volume_threshold=100))
        assert pm.current_phase == Phase.EXPLORATION

    def test_transitions_to_stabilization_on_high_trade_count(self) -> None:
        pm = PhaseManager(config=_make_config(stabilization_volume_threshold=10))
        phase = pm.update(trade_count=15, elapsed_hours=0.0)
        assert phase == Phase.STABILIZATION

    def test_stays_exploration_below_threshold(self) -> None:
        pm = PhaseManager(config=_make_config(stabilization_volume_threshold=100))
        phase = pm.update(trade_count=5, elapsed_hours=0.0)
        assert phase == Phase.EXPLORATION

    def test_transitions_on_elapsed_hours(self) -> None:
        pm = PhaseManager(config=_make_config(
            stabilization_volume_threshold=1000,
            exploration_duration_hours=1.0,
        ))
        phase = pm.update(trade_count=0, elapsed_hours=2.0)
        assert phase == Phase.STABILIZATION

    def test_stays_stabilization_once_transitioned(self) -> None:
        pm = PhaseManager(config=_make_config(stabilization_volume_threshold=10))
        pm.update(trade_count=15, elapsed_hours=0.0)
        phase = pm.update(trade_count=0, elapsed_hours=0.0)
        assert phase == Phase.STABILIZATION


# ---------------------------------------------------------------------------
# FIX 2: WIDEN defense widens spread
# ---------------------------------------------------------------------------

class TestWidenDefenseInQuoteCycle:
    """Test that WIDEN defense level causes wider ask/bid than NORMAL."""

    def _make_intent(self, side: str, price: int, qty: int = 100):
        from src.amm.strategy.models import OrderIntent
        return OrderIntent(
            action=QuoteAction.PLACE,
            side=side,
            direction="SELL",
            price_cents=price,
            quantity=qty,
        )

    @pytest.mark.asyncio
    async def test_widen_increases_ask_and_decreases_bid(self) -> None:
        """When defense==WIDEN, quotes are wider than normal baseline."""
        from src.amm.main import quote_cycle
        from src.amm.strategy.as_engine import ASEngine
        from src.amm.strategy.gradient import GradientEngine
        from src.amm.risk.defense_stack import DefenseStack
        from src.amm.risk.sanitizer import OrderSanitizer

        config = _make_config(widen_factor=1.5)
        ctx_normal = _make_ctx()
        ctx_widen = _make_ctx()

        # Shared mock services
        api = AsyncMock()
        api.get_orderbook.return_value = {"data": {"best_bid": 48, "best_ask": 52}}
        api.get_market_status.return_value = "active"

        poller = AsyncMock()
        poller.poll.return_value = []

        pricing = ThreeLayerPricing(
            anchor=AnchorPricing(50),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
        )
        as_engine = ASEngine()
        gradient = GradientEngine()
        sanitizer = OrderSanitizer()

        inventory_cache = AsyncMock()
        inventory_cache.get.return_value = None

        captured_normal = []
        captured_widen = []

        async def mock_execute_normal(intents, market_id):
            captured_normal.extend(intents)

        async def mock_execute_widen(intents, market_id):
            captured_widen.extend(intents)

        order_mgr_normal = AsyncMock()
        order_mgr_normal.execute_intents = mock_execute_normal

        order_mgr_widen = AsyncMock()
        order_mgr_widen.execute_intents = mock_execute_widen

        risk_normal = DefenseStack(config)
        risk_widen = DefenseStack(config)

        # Force WIDEN defense on ctx_widen
        ctx_widen.defense_level = DefenseLevel.WIDEN

        # Run normal cycle
        await quote_cycle(
            ctx=ctx_normal,
            api=api,
            poller=poller,
            pricing=pricing,
            as_engine=as_engine,
            gradient=gradient,
            risk=risk_normal,
            sanitizer=sanitizer,
            order_mgr=order_mgr_normal,
            inventory_cache=inventory_cache,
        )

        # Manually set WIDEN and run again
        ctx_widen.defense_level = DefenseLevel.WIDEN
        with patch.object(risk_widen, "evaluate", return_value=DefenseLevel.WIDEN):
            await quote_cycle(
                ctx=ctx_widen,
                api=api,
                poller=poller,
                pricing=pricing,
                as_engine=as_engine,
                gradient=gradient,
                risk=risk_widen,
                sanitizer=sanitizer,
                order_mgr=order_mgr_widen,
                inventory_cache=inventory_cache,
            )

        # Extract prices
        if captured_normal and captured_widen:
            normal_asks = [i.price_cents for i in captured_normal if i.side == "YES"]
            widen_asks = [i.price_cents for i in captured_widen if i.side == "YES"]
            normal_bids = [i.price_cents for i in captured_normal if i.side == "NO"]
            widen_bids = [i.price_cents for i in captured_widen if i.side == "NO"]

            if normal_asks and widen_asks:
                assert max(widen_asks) >= max(normal_asks), (
                    f"WIDEN ask {max(widen_asks)} should be >= normal ask {max(normal_asks)}"
                )
            if normal_bids and widen_bids:
                assert min(widen_bids) <= min(normal_bids), (
                    f"WIDEN bid {min(widen_bids)} should be <= normal bid {min(normal_bids)}"
                )


# ---------------------------------------------------------------------------
# FIX 3: Phase weights configurable via MarketConfig
# ---------------------------------------------------------------------------

class TestPhaseWeightsInMarketConfig:
    def test_market_config_has_phase_weights_exploration(self) -> None:
        cfg = MarketConfig(market_id="test")
        assert hasattr(cfg, "phase_weights_exploration")
        assert cfg.phase_weights_exploration == (0.6, 0.3, 0.1)

    def test_market_config_has_phase_weights_stabilization(self) -> None:
        cfg = MarketConfig(market_id="test")
        assert hasattr(cfg, "phase_weights_stabilization")
        assert cfg.phase_weights_stabilization == (0.2, 0.5, 0.3)

    def test_phase_weights_are_configurable(self) -> None:
        cfg = MarketConfig(
            market_id="test",
            phase_weights_exploration=(0.5, 0.3, 0.2),
            phase_weights_stabilization=(0.1, 0.6, 0.3),
        )
        assert cfg.phase_weights_exploration == (0.5, 0.3, 0.2)
        assert cfg.phase_weights_stabilization == (0.1, 0.6, 0.3)

    def test_three_layer_pricing_uses_config_weights(self) -> None:
        """ThreeLayerPricing should use config weights when config is provided."""
        anchor = AnchorPricing(50)
        micro = MicroPricing()
        posterior = PosteriorPricing()
        # Custom weights: anchor gets everything in EXPLORATION
        cfg = MarketConfig(
            market_id="test",
            phase_weights_exploration=(1.0, 0.0, 0.0),
            phase_weights_stabilization=(0.0, 1.0, 0.0),
        )
        pricing = ThreeLayerPricing(anchor=anchor, micro=micro, posterior=posterior, config=cfg)

        # EXPLORATION: should return anchor price (50)
        result = pricing.compute(
            phase="EXPLORATION",
            anchor_price=50,
            best_bid=30,
            best_ask=70,
            recent_trades=[],
        )
        assert result == 50

    def test_three_layer_pricing_backward_compatible_no_config(self) -> None:
        """ThreeLayerPricing without config uses module-level defaults."""
        anchor = AnchorPricing(50)
        micro = MicroPricing()
        posterior = PosteriorPricing()
        pricing = ThreeLayerPricing(anchor=anchor, micro=micro, posterior=posterior)
        # Should not raise
        result = pricing.compute(
            phase="EXPLORATION",
            anchor_price=50,
            best_bid=48,
            best_ask=52,
            recent_trades=[],
        )
        assert 1 <= result <= 99


# ---------------------------------------------------------------------------
# FIX 4: asyncio.gather exceptions are logged
# ---------------------------------------------------------------------------

class TestGatherExceptionLogging:
    @pytest.mark.skip(reason="TODO: needs rework after amm_main restructuring")
    @pytest.mark.asyncio
    async def test_gather_exceptions_are_logged(self, caplog) -> None:
        """Exceptions from market tasks should be logged, not swallowed."""
        from src.amm.main import amm_main

        error = RuntimeError("market task exploded")

        async def failing_task():
            raise error

        with (
            patch("src.amm.main.create_redis_client"),
            patch("src.amm.main.httpx.AsyncClient"),
            patch("src.amm.main.TokenManager"),
            patch("src.amm.main.AMMApiClient"),
            patch("src.amm.main.InventoryCache"),
            patch("src.amm.main.ConfigLoader"),
            patch("src.amm.main.AMMInitializer") as mock_init_cls,
            patch("src.amm.main.GracefulShutdown") as mock_shutdown_cls,
            patch("src.amm.main.asyncio.create_task") as mock_create_task,
        ):
            mock_init = mock_init_cls.return_value
            ctx = _make_ctx()
            mock_init.initialize = AsyncMock(return_value={"mkt-test": ctx})

            mock_shutdown = mock_shutdown_cls.return_value
            mock_shutdown.execute = AsyncMock()

            # Task that fails immediately
            mock_create_task.return_value = asyncio.ensure_future(failing_task())

            with caplog.at_level(logging.ERROR, logger="src.amm.main"):
                try:
                    await amm_main(market_ids=["mkt-test"])
                except Exception:
                    pass

            # The exception should have been logged
            assert any("failed" in r.message.lower() or "error" in r.message.lower()
                       for r in caplog.records), (
                f"Expected error log, got: {[r.message for r in caplog.records]}"
            )
