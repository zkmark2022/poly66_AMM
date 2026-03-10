"""AMM health check endpoint — FastAPI mini app on port 8001.

Endpoints:
  GET /health    — liveness probe (always 200 if process is alive)
  GET /readiness — readiness probe (503 until fully initialized)
  GET /state     — machine-readable AMM runtime state per market (BUG-007)
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from src.amm.models.market_context import MarketContext

logger = logging.getLogger(__name__)


@dataclass
class HealthState:
    """Shared mutable state between the AMM main loop and the health server."""
    ready: bool = False
    markets_active: int = 0
    uptime_seconds: float = 0.0
    contexts: dict[str, MarketContext] = field(default_factory=dict)


def _build_market_state(ctx: MarketContext, now: float) -> dict[str, Any]:
    """Snapshot observable state for one market."""
    elapsed_hours = (now - ctx.started_at) / 3600.0

    if ctx.config.remaining_hours_override is not None:
        hours_remaining: float = ctx.config.remaining_hours_override
    else:
        hours_remaining = max(0.0, ctx.config.exploration_duration_hours - elapsed_hours)

    last_requote_at = ctx.last_requote_at
    last_requote_ms: float | None = (
        (now - last_requote_at) * 1000.0 if last_requote_at > 0.0 else None
    )

    return {
        "defense_level": ctx.defense_level.value,
        "kill_switch": not ctx.defense_level.is_quoting_active,
        "inventory_skew": ctx.inventory.inventory_skew,
        "phase": ctx.phase.value,
        "hours_remaining": hours_remaining,
        "last_requote_ms": last_requote_ms,
        "elapsed_hours": elapsed_hours,
        "session_pnl_cents": ctx.session_pnl_cents,
        "trade_count": ctx.trade_count,
    }


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
    async def amm_state() -> JSONResponse:
        """Machine-readable AMM runtime state for all markets.

        Returns per-market observability fields needed for E2E validation:
        - defense_level: current risk defense level (NORMAL/WIDEN/ONE_SIDE/KILL_SWITCH)
        - kill_switch: boolean shorthand for defense_level == KILL_SWITCH
        - inventory_skew: (yes - no) / (yes + no), range [-1, 1]
        - phase: EXPLORATION or STABILIZATION
        - hours_remaining: remaining_hours_override if set, else max(0, exploration_duration - elapsed)
        - last_requote_ms: ms since last completed quote cycle, null if never run
        - elapsed_hours: hours since AMM session started
        - session_pnl_cents: unrealized P&L since session start
        - trade_count: trades processed this session
        """
        now = time.monotonic()
        markets = {
            market_id: _build_market_state(ctx, now)
            for market_id, ctx in state.contexts.items()
        }
        return JSONResponse(status_code=200, content={"markets": markets})

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
