from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.amm.lifecycle.health import HealthState, create_health_app
from src.amm.lifecycle.reconciler import AMMReconciler
from src.amm.main import quote_cycle, reconcile_loop
from src.amm.models.enums import Phase
from src.amm.strategy.phase_manager import PhaseManager

from tests.simulation.conftest import make_context, make_inventory, make_real_services


@pytest.mark.asyncio
async def test_reconcile_loop_updates_context_from_drifted_cache() -> None:
    ctx = make_context(inventory=make_inventory(yes_volume=200, no_volume=200))
    reconciler = AsyncMock(spec=AMMReconciler)
    reconciler.fetch_balance.return_value = {"data": {"balance_cents": 100_000, "frozen_balance_cents": 0}}
    reconciler.reconcile.return_value = {ctx.market_id: {"drifted": True, "fields": ["yes_volume"]}}
    inventory_cache = AsyncMock()
    inventory_cache.get.return_value = make_inventory(yes_volume=275, no_volume=125)

    async def one_pass_sleep(_: float) -> None:
        ctx.shutdown_requested = True

    with patch("src.amm.main.asyncio.sleep", side_effect=one_pass_sleep):
        await reconcile_loop(
            reconciler,
            {ctx.market_id: ctx},
            interval_seconds=0.01,
            inventory_cache=inventory_cache,
        )

    # Startup reconciliation must refresh in-memory context when Redis drift correction occurs.
    assert ctx.inventory.yes_volume == 275
    assert ctx.inventory.no_volume == 125


def test_health_app_reports_liveness_and_readiness_transitions() -> None:
    state = HealthState(ready=False, markets_active=2, uptime_seconds=15.0)
    client = TestClient(create_health_app(state))

    health = client.get("/health")
    readiness_before = client.get("/readiness")
    state.ready = True
    readiness_after = client.get("/readiness")

    # Liveness must always be 200; readiness should flip from 503 to 200 after init.
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "markets_active": 2, "uptime_seconds": 15.0}
    assert readiness_before.status_code == 503
    assert readiness_after.status_code == 200
    assert readiness_after.json() == {"ready": True, "markets_active": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trade_count", "elapsed_hours", "expected_phase"),
    [
        (0, 0.5, Phase.EXPLORATION),
        (6, 0.5, Phase.STABILIZATION),
        (0, 2.0, Phase.STABILIZATION),
    ],
)
async def test_phase_manager_advances_on_volume_or_elapsed_time(
    trade_count: int,
    elapsed_hours: float,
    expected_phase: Phase,
) -> None:
    ctx = make_context(inventory=make_inventory(yes_volume=200, no_volume=200))
    ctx.trade_count = trade_count
    phase_mgr = PhaseManager(config=ctx.config)
    services, _ = make_real_services(ctx, phase_mgr=phase_mgr)
    fake_time = ctx.started_at + (elapsed_hours * 3600)

    with patch("src.amm.main.time.monotonic", return_value=fake_time):
        await quote_cycle(ctx, **services)

    # Startup integrity requires ctx.phase and PhaseManager internal phase to remain in sync.
    assert ctx.phase == expected_phase
    assert phase_mgr.current_phase == expected_phase
