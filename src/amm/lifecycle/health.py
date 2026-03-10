"""AMM health check endpoint — FastAPI mini app on port 8001.

Endpoints:
  GET /health           — liveness probe (always 200 if process is alive)
  GET /readiness        — readiness probe (503 until fully initialized)
  GET /state            — full AMM state snapshot (all markets)
  GET /state/{market_id} — per-market state snapshot
"""
from __future__ import annotations

import logging
import os
import time
import uvicorn
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


@dataclass
class MarketStateSnapshot:
    """Point-in-time observability snapshot for a single market."""
    market_id: str
    phase: str
    defense_level: str
    inventory_skew: float
    active_orders_count: int
    session_pnl_cents: int
    uptime_seconds: float
    winding_down: bool
    last_updated_at: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "phase": self.phase,
            "defense_level": self.defense_level,
            "inventory_skew": round(self.inventory_skew, 4),
            "active_orders_count": self.active_orders_count,
            "session_pnl_cents": self.session_pnl_cents,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "winding_down": self.winding_down,
            "last_updated_at": round(self.last_updated_at, 3),
        }


@dataclass
class HealthState:
    """Shared mutable state between the AMM main loop and the health server."""
    ready: bool = False
    markets_active: int = 0
    uptime_seconds: float = 0.0
    market_states: dict[str, MarketStateSnapshot] = field(default_factory=dict)


def create_health_app(state: HealthState) -> FastAPI:
    """Create the FastAPI health check application."""
    app = FastAPI(title="AMM Health", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def liveness() -> JSONResponse:
        """Liveness probe — process is alive."""
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "markets_active": state.markets_active,
                "uptime_seconds": state.uptime_seconds,
            },
        )

    @app.get("/readiness")
    async def readiness() -> JSONResponse:
        """Readiness probe — AMM is initialized and accepting traffic."""
        if state.ready:
            return JSONResponse(
                status_code=200,
                content={"ready": True, "markets_active": state.markets_active},
            )
        return JSONResponse(
            status_code=503,
            content={"ready": False, "reason": "AMM not yet initialized"},
        )

    @app.get("/state")
    async def all_market_states() -> JSONResponse:
        """Full observability snapshot — all active markets."""
        return JSONResponse(
            status_code=200,
            content={
                "markets": [s.to_dict() for s in state.market_states.values()],
                "markets_active": state.markets_active,
                "uptime_seconds": round(state.uptime_seconds, 1),
            },
        )

    @app.get("/state/{market_id}")
    async def single_market_state(market_id: str) -> JSONResponse:
        """Per-market observability snapshot."""
        snapshot = state.market_states.get(market_id)
        if snapshot is None:
            return JSONResponse(
                status_code=404,
                content={"error": f"market {market_id!r} not found"},
            )
        return JSONResponse(status_code=200, content=snapshot.to_dict())

    return app


_DEFAULT_HEALTH_HOST = "127.0.0.1"


async def run_health_server(state: HealthState, port: int = 8001) -> None:
    """Run the health check server as an asyncio task."""
    host = os.environ.get("AMM_HEALTH_HOST", _DEFAULT_HEALTH_HOST)
    app = create_health_app(state)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    logger.info("AMM health server starting on port %d", port)
    await server.serve()
