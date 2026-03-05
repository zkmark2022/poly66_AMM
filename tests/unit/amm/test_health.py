"""Tests for AMM health check endpoint."""
from __future__ import annotations

from httpx import AsyncClient, ASGITransport

from src.amm.lifecycle.health import create_health_app, HealthState


class TestHealthEndpoint:
    async def test_health_returns_200_when_healthy(self) -> None:
        state = HealthState(ready=True, markets_active=2)
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    async def test_health_returns_200_even_when_not_ready(self) -> None:
        """Liveness probe always returns 200 unless the process is dead."""
        state = HealthState(ready=False, markets_active=0)
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")
        assert resp.status_code == 200

    async def test_readiness_returns_200_when_ready(self) -> None:
        state = HealthState(ready=True, markets_active=1)
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/readiness")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ready"] is True

    async def test_readiness_returns_503_when_not_ready(self) -> None:
        state = HealthState(ready=False, markets_active=0)
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/readiness")
        assert resp.status_code == 503
        data = resp.json()
        assert data["ready"] is False

    async def test_health_includes_markets_count(self) -> None:
        state = HealthState(ready=True, markets_active=3)
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/health")
        data = resp.json()
        assert data["markets_active"] == 3

    async def test_state_is_mutable(self) -> None:
        """Health state can be updated externally (from main loop)."""
        state = HealthState(ready=False, markets_active=0)
        app = create_health_app(state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r1 = await client.get("/readiness")
            assert r1.status_code == 503

            state.ready = True
            state.markets_active = 2

            r2 = await client.get("/readiness")
            assert r2.status_code == 200
