"""Tests for AMM state observability endpoints — GET /state and GET /state/{market_id}."""
from __future__ import annotations

import time

import pytest
from httpx import AsyncClient, ASGITransport

from src.amm.lifecycle.health import (
    create_health_app,
    HealthState,
    MarketStateSnapshot,
)


def _make_snapshot(market_id: str = "mkt-1") -> MarketStateSnapshot:
    return MarketStateSnapshot(
        market_id=market_id,
        phase="EXPLORATION",
        defense_level="NORMAL",
        inventory_skew=0.25,
        active_orders_count=4,
        session_pnl_cents=-50,
        uptime_seconds=120.5,
        winding_down=False,
    )


class TestMarketStateSnapshot:
    def test_to_dict_has_expected_keys(self) -> None:
        snap = _make_snapshot("mkt-1")
        d = snap.to_dict()
        assert d["market_id"] == "mkt-1"
        assert d["phase"] == "EXPLORATION"
        assert d["defense_level"] == "NORMAL"
        assert d["inventory_skew"] == 0.25
        assert d["active_orders_count"] == 4
        assert d["session_pnl_cents"] == -50
        assert d["winding_down"] is False

    def test_inventory_skew_is_rounded(self) -> None:
        snap = _make_snapshot()
        snap.inventory_skew = 0.123456789
        assert snap.to_dict()["inventory_skew"] == round(0.123456789, 4)

    def test_uptime_seconds_is_rounded(self) -> None:
        snap = _make_snapshot()
        snap.uptime_seconds = 42.9999
        assert snap.to_dict()["uptime_seconds"] == round(42.9999, 1)

    def test_last_updated_at_is_set_on_creation(self) -> None:
        before = time.monotonic()
        snap = _make_snapshot()
        after = time.monotonic()
        assert before <= snap.last_updated_at <= after


class TestStateEndpointEmpty:
    async def test_state_returns_empty_when_no_markets(self) -> None:
        state = HealthState(ready=True, markets_active=0)
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["markets"] == []
        assert data["markets_active"] == 0

    async def test_state_single_market_returns_404_when_missing(self) -> None:
        state = HealthState(ready=True, markets_active=0)
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/state/mkt-999")
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"]


class TestStateEndpointWithMarkets:
    async def test_state_returns_all_snapshots(self) -> None:
        state = HealthState(ready=True, markets_active=2)
        state.market_states["mkt-1"] = _make_snapshot("mkt-1")
        state.market_states["mkt-2"] = _make_snapshot("mkt-2")
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/state")
        data = resp.json()
        assert data["markets_active"] == 2
        ids = {m["market_id"] for m in data["markets"]}
        assert ids == {"mkt-1", "mkt-2"}

    async def test_state_single_market_returns_snapshot(self) -> None:
        state = HealthState(ready=True, markets_active=1)
        state.market_states["mkt-1"] = _make_snapshot("mkt-1")
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/state/mkt-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["market_id"] == "mkt-1"
        assert data["phase"] == "EXPLORATION"
        assert data["active_orders_count"] == 4

    async def test_state_snapshot_reflects_live_updates(self) -> None:
        """Snapshot updates are immediately visible via the endpoint."""
        state = HealthState(ready=True, markets_active=1)
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r1 = await client.get("/state/mkt-1")
            assert r1.status_code == 404

            state.market_states["mkt-1"] = _make_snapshot("mkt-1")

            r2 = await client.get("/state/mkt-1")
            assert r2.status_code == 200
            assert r2.json()["defense_level"] == "NORMAL"

    async def test_state_winding_down_market(self) -> None:
        state = HealthState(ready=True, markets_active=1)
        snap = _make_snapshot("mkt-1")
        snap.winding_down = True
        state.market_states["mkt-1"] = snap
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/state/mkt-1")
        assert resp.json()["winding_down"] is True

    async def test_state_includes_uptime_seconds(self) -> None:
        state = HealthState(ready=True, markets_active=1, uptime_seconds=99.9)
        state.market_states["mkt-1"] = _make_snapshot("mkt-1")
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/state")
        assert resp.json()["uptime_seconds"] == 99.9
