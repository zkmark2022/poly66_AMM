"""AMM health check endpoint — FastAPI mini app on port 8001.

Endpoints:
  GET /health    — liveness probe (always 200 if process is alive)
  GET /readiness — readiness probe (503 until fully initialized)
"""
from __future__ import annotations

import logging
import uvicorn
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


@dataclass
class HealthState:
    """Shared mutable state between the AMM main loop and the health server."""
    ready: bool = False
    markets_active: int = 0
    uptime_seconds: float = 0.0


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

    return app


async def run_health_server(state: HealthState, port: int = 8001) -> None:
    """Run the health check server as an asyncio task."""
    app = create_health_app(state)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    logger.info("AMM health server starting on port %d", port)
    await server.serve()
