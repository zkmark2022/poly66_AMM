"""Polymarket CLI oracle — external price source for LVR/deviation detection."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class PolymarketOracle:
    """Async oracle that fetches prices from Polymarket CLI."""

    def __init__(self, market_slug: str) -> None:
        self.market_slug = market_slug
        self.last_price: Optional[float] = None
        self.last_update: Optional[float] = None

    async def get_price(self) -> float:
        """Fetch YES price in cents (0–100) via Polymarket CLI. Returns 50.0 on error.
        
        Uses async subprocess to avoid blocking the event loop (Gemini fix).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "polymarket", "-o", "json", "markets", "get", self.market_slug,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            data = json.loads(stdout.decode())
            if "outcomePrices" in data:
                price = float(data["outcomePrices"][0]) * 100
                self.last_price = price
                self.last_update = time.time()
                return price
        except asyncio.TimeoutError:
            logger.warning("PolymarketOracle.get_price timeout for %s", self.market_slug)
        except Exception as exc:
            logger.warning("PolymarketOracle.get_price failed for %s: %s", self.market_slug, exc)
        return 50.0

    async def check_deviation(self, internal_price: float, threshold: float = 20.0) -> bool:
        """Return True if |internal_price - external_price| > threshold (cents)."""
        external = await self.get_price()
        return abs(internal_price - external) > threshold

    def check_lag(self, threshold_seconds: float = 3.0) -> bool:
        """Return True if oracle has never updated or last update is older than threshold."""
        if self.last_update is None:
            return True
        return (time.time() - self.last_update) > threshold_seconds
