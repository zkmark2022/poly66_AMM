"""TDD tests for R2A main.py integration fixes.

Covers:
  FIX 1: oracle not duplicated in services dict
  FIX 2: session_pnl_cents updated each quote_cycle
  FIX 3: OracleState.KILL_SWITCH not downgraded by risk.evaluate()
  FIX 4: market_active=False triggers KILL_SWITCH
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.amm.config.models import MarketConfig
from src.amm.models.enums import DefenseLevel
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext
from src.amm.oracle.polymarket_oracle import OracleState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_inventory(yes: int = 100, no: int = 100, cash: int = 500_000) -> Inventory:
    return Inventory(
        cash_cents=cash,
        yes_volume=yes,
        no_volume=no,
        yes_cost_sum_cents=5_000,
        no_cost_sum_cents=5_000,
        yes_pending_sell=0,
        no_pending_sell=0,
        frozen_balance_cents=0,
    )


def _make_context(market_id: str = "mkt-1") -> MarketContext:
    return MarketContext(
        market_id=market_id,
        config=MarketConfig(market_id=market_id, quote_interval_seconds=0.01),
        inventory=_make_inventory(),
    )


def _make_services(
    mid_return: int = 50,
    ask: int = 52,
    bid: int = 48,
    market_status: str = "active",
) -> dict:
    """Build a minimal services dict with all required mocks."""
    api = AsyncMock()
    api.get_orderbook.return_value = {
        "data": {"best_bid": 49, "best_ask": 51, "bid_depth": 10, "ask_depth": 10}
    }
    api.get_market_status = AsyncMock(return_value=market_status)

    poller = AsyncMock()
    poller.poll.return_value = []

    pricing = MagicMock()
    pricing.compute.return_value = mid_return

    as_engine = MagicMock()
    as_engine.bernoulli_sigma.return_value = 0.5
    as_engine.get_gamma_for_age.return_value = 0.3
    as_engine.compute_quotes.return_value = (ask, bid)

    gradient = MagicMock()
    gradient.build_ask_ladder.return_value = []
    gradient.build_bid_ladder.return_value = []

    risk = MagicMock()
    risk.evaluate.return_value = DefenseLevel.NORMAL

    sanitizer = MagicMock()
    sanitizer.sanitize.return_value = []

    order_mgr = AsyncMock()

    inventory_cache = AsyncMock()
    inventory_cache.get.return_value = None

    phase_mgr = MagicMock()
    phase_mgr.update.return_value = MagicMock()

    return {
        "api": api,
        "poller": poller,
        "pricing": pricing,
        "as_engine": as_engine,
        "gradient": gradient,
        "risk": risk,
        "sanitizer": sanitizer,
        "order_mgr": order_mgr,
        "inventory_cache": inventory_cache,
        "phase_mgr": phase_mgr,
    }


# ---------------------------------------------------------------------------
# FIX 1: oracle must NOT appear in the services dict
# ---------------------------------------------------------------------------

class TestFix1OracleNotDuplicated:
    """oracle must not be in services dict — it is passed as a kwarg separately."""

    def test_oracle_not_in_services_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify amm_main() does NOT put oracle in the services dict."""
        captured_services: list[dict] = []

        async def fake_run_market_with_health(
            ctx: MarketContext,
            services: dict,
            health_state: object,
            oracle: object = None,
        ) -> None:
            captured_services.append(dict(services))
            ctx.shutdown_requested = True

        import src.amm.main as main_mod

        monkeypatch.setattr(main_mod, "run_market_with_health", fake_run_market_with_health)

        # Patch everything needed for amm_main to run
        mock_initializer = AsyncMock()
        ctx = _make_context()
        mock_initializer.initialize.return_value = {ctx.market_id: ctx}

        monkeypatch.setattr(main_mod, "AMMInitializer", lambda **kw: mock_initializer)
        monkeypatch.setattr(main_mod, "create_redis_client", lambda url: AsyncMock())
        monkeypatch.setattr(main_mod, "TokenManager", lambda *a, **kw: MagicMock())
        monkeypatch.setattr(main_mod, "AMMApiClient", lambda *a, **kw: AsyncMock())
        monkeypatch.setattr(main_mod, "InventoryCache", lambda r: MagicMock())
        config_loader = AsyncMock()
        from src.amm.config.models import GlobalConfig
        config_loader.load_global.return_value = GlobalConfig()
        monkeypatch.setattr(main_mod, "ConfigLoader", lambda **kw: config_loader)
        monkeypatch.setattr(main_mod, "GracefulShutdown", lambda api: AsyncMock())
        monkeypatch.setattr(main_mod, "AMMReconciler", lambda *a, **kw: AsyncMock())
        monkeypatch.setattr(main_mod, "run_health_server", AsyncMock())
        monkeypatch.setattr(main_mod, "TradePoller", lambda **kw: MagicMock())
        monkeypatch.setattr(main_mod, "ThreeLayerPricing", lambda **kw: MagicMock())
        monkeypatch.setattr(main_mod, "AnchorPricing", lambda p: MagicMock())
        monkeypatch.setattr(main_mod, "MicroPricing", lambda: MagicMock())
        monkeypatch.setattr(main_mod, "PosteriorPricing", lambda: MagicMock())
        monkeypatch.setattr(main_mod, "ASEngine", lambda: MagicMock())
        monkeypatch.setattr(main_mod, "GradientEngine", lambda: MagicMock())
        monkeypatch.setattr(main_mod, "DefenseStack", lambda cfg: MagicMock())
        monkeypatch.setattr(main_mod, "OrderSanitizer", lambda: MagicMock())
        monkeypatch.setattr(main_mod, "OrderManager", lambda **kw: MagicMock())
        monkeypatch.setattr(main_mod, "PhaseManager", lambda config: MagicMock())

        import types
        fake_loop = types.SimpleNamespace(add_signal_handler=lambda *a, **kw: None)
        monkeypatch.setattr(main_mod.asyncio, "get_event_loop", lambda: fake_loop)
        monkeypatch.setenv("AMM_MARKETS", ctx.market_id)

        asyncio.run(main_mod.amm_main([ctx.market_id]))

        assert len(captured_services) >= 1
        for svc in captured_services:
            assert "oracle" not in svc, (
                f"oracle must NOT be in services dict — it's passed as a separate kwarg. "
                f"Found keys: {list(svc.keys())}"
            )


# ---------------------------------------------------------------------------
# FIX 2: session_pnl_cents must be updated each quote_cycle
# ---------------------------------------------------------------------------

class TestFix2SessionPnlUpdates:
    """ctx.session_pnl_cents must be updated each quote_cycle iteration."""

    async def test_session_pnl_updates_each_cycle(self) -> None:
        from src.amm.main import quote_cycle

        ctx = _make_context()
        # Set initial_inventory_value_cents so pnl is computed relative to it
        ctx.initial_inventory_value_cents = 510_000

        services = _make_services(mid_return=50)

        assert ctx.session_pnl_cents == 0

        await quote_cycle(ctx, **services)

        # inventory.total_value_cents(50) = 500_000 + 100*50 + 100*50 = 510_000
        # pnl = 510_000 - 510_000 = 0 (no change yet, but it must have been computed)
        # The key assertion: session_pnl_cents was SET (not left at default 0 without computation)
        # Use a different initial value to confirm real computation happened
        ctx2 = _make_context()
        ctx2.initial_inventory_value_cents = 400_000
        services2 = _make_services(mid_return=50)
        await quote_cycle(ctx2, **services2)

        # inventory.total_value_cents(50) = 500_000 + 100*50 + 100*50 = 510_000
        # pnl = 510_000 - 400_000 = 110_000
        assert ctx2.session_pnl_cents == 110_000

    async def test_initial_inventory_value_cents_field_exists(self) -> None:
        """MarketContext must have initial_inventory_value_cents field."""
        ctx = _make_context()
        # This will raise AttributeError if field doesn't exist
        _ = ctx.initial_inventory_value_cents


# ---------------------------------------------------------------------------
# FIX 3: OracleState.KILL_SWITCH must not be downgraded
# ---------------------------------------------------------------------------

class TestFix3OracleKillSwitchNotDowngraded:
    """When oracle says KILL_SWITCH, risk.evaluate() returning NORMAL must not override it."""

    async def test_oracle_kill_switch_not_downgraded_by_normal_risk(self) -> None:
        from src.amm.main import quote_cycle

        ctx = _make_context()
        ctx.initial_inventory_value_cents = 0
        services = _make_services()
        # risk says NORMAL
        services["risk"].evaluate.return_value = DefenseLevel.NORMAL

        # oracle says KILL_SWITCH (via DEVIATION state)
        oracle = MagicMock()
        oracle.evaluate = MagicMock(return_value=OracleState.DEVIATION)
        ctx.config.oracle_slug = "test-market"

        cancel_called = False
        async def fake_cancel_all(market_id: str) -> None:
            nonlocal cancel_called
            cancel_called = True

        services["order_mgr"].cancel_all = AsyncMock(side_effect=fake_cancel_all)

        await quote_cycle(ctx, oracle=oracle, **services)

        # DEVIATION → KILL_SWITCH → should have cancelled all orders
        assert cancel_called, (
            "KILL_SWITCH from oracle DEVIATION must trigger order cancellation, "
            "not be overridden by risk NORMAL"
        )
        assert ctx.defense_level == DefenseLevel.KILL_SWITCH

    async def test_oracle_lvr_not_downgraded_to_one_side(self) -> None:
        from src.amm.main import quote_cycle

        ctx = _make_context()
        ctx.initial_inventory_value_cents = 0
        services = _make_services()
        services["risk"].evaluate.return_value = DefenseLevel.ONE_SIDE

        oracle = MagicMock()
        oracle.evaluate = MagicMock(return_value=OracleState.LVR)
        ctx.config.oracle_slug = "test-market"

        cancel_called = False
        async def fake_cancel_all(market_id: str) -> None:
            nonlocal cancel_called
            cancel_called = True

        services["order_mgr"].cancel_all = AsyncMock(side_effect=fake_cancel_all)

        await quote_cycle(ctx, oracle=oracle, **services)

        # LVR → KILL_SWITCH, overrides ONE_SIDE
        assert cancel_called
        assert ctx.defense_level == DefenseLevel.KILL_SWITCH

    async def test_oracle_normal_does_not_override_risk_widen(self) -> None:
        from src.amm.main import quote_cycle

        ctx = _make_context()
        ctx.initial_inventory_value_cents = 0
        services = _make_services()
        services["risk"].evaluate.return_value = DefenseLevel.WIDEN

        oracle = MagicMock()
        oracle.evaluate = MagicMock(return_value=OracleState.NORMAL)
        ctx.config.oracle_slug = "test-market"

        await quote_cycle(ctx, oracle=oracle, **services)

        # risk says WIDEN, oracle says NORMAL → final should be WIDEN
        assert ctx.defense_level == DefenseLevel.WIDEN


# ---------------------------------------------------------------------------
# FIX 4: market_active=False must trigger KILL_SWITCH
# ---------------------------------------------------------------------------

class TestFix4MarketInactiveTriggersKillSwitch:
    """When market status is not active, risk must receive market_active=False."""

    async def test_market_inactive_triggers_kill_switch(self) -> None:
        from src.amm.main import quote_cycle

        ctx = _make_context()
        ctx.initial_inventory_value_cents = 0
        # Market is inactive
        services = _make_services(market_status="closed")
        # DefenseStack.evaluate() with market_active=False returns KILL_SWITCH
        from src.amm.risk.defense_stack import DefenseStack
        real_risk = DefenseStack(ctx.config)
        services["risk"] = real_risk

        cancel_called = False
        async def fake_cancel_all(market_id: str) -> None:
            nonlocal cancel_called
            cancel_called = True

        services["order_mgr"].cancel_all = AsyncMock(side_effect=fake_cancel_all)

        await quote_cycle(ctx, **services)

        assert cancel_called, (
            "Inactive market must trigger KILL_SWITCH and cancel all orders"
        )

    async def test_market_active_status_passed_to_risk_evaluate(self) -> None:
        from src.amm.main import quote_cycle

        ctx = _make_context()
        ctx.initial_inventory_value_cents = 0
        services = _make_services(market_status="closed")

        risk_calls: list[dict] = []
        def capture_risk_evaluate(**kwargs: object) -> DefenseLevel:
            risk_calls.append(dict(kwargs))
            return DefenseLevel.KILL_SWITCH

        services["risk"].evaluate.side_effect = capture_risk_evaluate
        services["order_mgr"].cancel_all = AsyncMock()

        await quote_cycle(ctx, **services)

        assert risk_calls, "risk.evaluate() must be called"
        assert risk_calls[0].get("market_active") is False, (
            f"market_active must be False for closed market, got: {risk_calls[0]}"
        )
