"""PolyMarket CLI Oracle — external price feed for AMM defense.

Integrates with the polymarket CLI to detect:
- LVR (Loss-Versus-Rebalancing): price moves >threshold% within a short window
- Stale oracle: no price update for >stale_seconds
- Deviation: |internal - external| > deviation_cents threshold
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from enum import StrEnum

from src.amm.config.models import MarketConfig
from src.amm.models.enums import DefenseLevel

logger = logging.getLogger(__name__)
_POLYMARKET_BIN = os.environ.get("POLYMARKET_BIN", "/opt/homebrew/bin/polymarket")


class OracleState(StrEnum):
    """Oracle health states mapped to AMM defense actions."""

    NORMAL = "NORMAL"
    STALE = "STALE"        # price unchanged > stale_seconds → ONE_SIDE (PASSIVE_MODE)
    DEVIATION = "DEVIATION"  # |oracle - internal| > threshold → KILL_SWITCH (AMM_PAUSE)
    LVR = "LVR"            # price moved >threshold% in window → KILL_SWITCH (immediate halt)

    @property
    def defense_level(self) -> DefenseLevel:
        if self == OracleState.NORMAL:
            return DefenseLevel.NORMAL
        if self == OracleState.STALE:
            return DefenseLevel.ONE_SIDE
        return DefenseLevel.KILL_SWITCH  # DEVIATION or LVR


class PolymarketOracle:
    """External price oracle backed by the Polymarket CLI.

    Usage:
        oracle.refresh()                        # fetch current price from CLI
        state = oracle.evaluate(internal_mid)  # returns OracleState
    """

    def __init__(
        self,
        config: MarketConfig | str | None = None,
        *,
        market_slug: str | None = None,
        oracle_stale_seconds: float = 3.0,
        oracle_deviation_cents: float = 20.0,
        oracle_lvr_window_seconds: float = 0.5,
        oracle_lvr_threshold: float = 0.20,
    ) -> None:
        if config is not None and market_slug is not None:
            raise TypeError("config and market_slug are mutually exclusive")
        if isinstance(config, str):
            market_slug = config
            config = None
        if config is None:
            if market_slug is None:
                raise TypeError("PolymarketOracle requires config or market_slug")
            config = MarketConfig(
                market_id=market_slug,
                oracle_slug=market_slug,
                oracle_stale_seconds=oracle_stale_seconds,
                oracle_deviation_cents=oracle_deviation_cents,
                oracle_lvr_window_seconds=oracle_lvr_window_seconds,
                oracle_lvr_threshold=oracle_lvr_threshold,
            )

        self._config = config
        # (monotonic_timestamp, price_cents)
        self._price_history: list[tuple[float, float]] = []
        self._last_refresh_time: float | None = None
        self.last_price: float | None = None
        self.last_update: float | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def refresh(self) -> None:
        """Fetch price from Polymarket CLI and record it with current timestamp."""
        price = await self._fetch_price()
        now = time.monotonic()
        self._last_refresh_time = now
        self._price_history.append((now, price))
        self.last_price = price
        self.last_update = now
        # Prune history older than 60 seconds (keep memory bounded)
        cutoff = now - 60.0
        self._price_history = [(t, p) for t, p in self._price_history if t >= cutoff]

    async def get_price(self) -> float:
        """Refresh and return the latest YES price in cents.

        Raises RuntimeError if the CLI fails, times out, or returns malformed data.
        """
        await self.refresh()
        return self.get_yes_price()

    def get_yes_price(self) -> float:
        """Return latest cached YES price in cents (0–100).

        Raises RuntimeError if refresh() has never been called.
        """
        if not self._price_history:
            raise RuntimeError("No price data available — call refresh() first")
        return self._price_history[-1][1]

    def check_stale(self) -> bool:
        """Return True if oracle has never been refreshed or last refresh exceeded threshold."""
        if self._last_refresh_time is None:
            return True
        return (time.monotonic() - self._last_refresh_time) > self._config.oracle_stale_seconds

    def check_lag(self, threshold_seconds: float = 3.0) -> bool:
        """Compatibility shim for older callers using the previous lag API."""
        if self.last_update is None:
            return True
        return (time.monotonic() - self.last_update) > threshold_seconds

    def check_deviation(self, internal_price_cents: float) -> bool:
        """Return True if |oracle_price - internal_price| exceeds deviation threshold."""
        if not self._price_history:
            return False
        oracle_price = self._price_history[-1][1]
        return abs(oracle_price - internal_price_cents) > self._config.oracle_deviation_cents

    def check_lvr(self) -> bool:
        """Return True if price moved >lvr_threshold fraction within lvr_window_seconds."""
        if len(self._price_history) < 2:
            return False
        now = time.monotonic()
        window_start = now - self._config.oracle_lvr_window_seconds
        window_prices = [p for t, p in self._price_history if t >= window_start]
        if len(window_prices) < 2:
            return False
        oldest = window_prices[0]
        newest = window_prices[-1]
        if oldest == 0.0:
            return False
        change = abs(newest - oldest) / oldest
        return change > self._config.oracle_lvr_threshold

    def evaluate(self, internal_price_cents: float) -> OracleState:
        """Return current OracleState based on stale, LVR, and deviation checks.

        Priority: STALE > LVR > DEVIATION > NORMAL
        """
        if self.check_stale():
            return OracleState.STALE
        if self.check_lvr():
            return OracleState.LVR
        if self.check_deviation(internal_price_cents):
            return OracleState.DEVIATION
        return OracleState.NORMAL

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _fetch_price(self) -> float:
        """Call polymarket CLI and parse YES price from outcomePrices field."""
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                _POLYMARKET_BIN,
                "-o",
                "json",
                "markets",
                "get",
                self._config.oracle_slug,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            returncode = getattr(proc, "returncode", 0)
            if isinstance(returncode, int) and returncode != 0:
                raise RuntimeError(stderr.decode() if stderr else "unknown polymarket CLI error")
            data: dict[str, object] = json.loads(stdout.decode())
            outcome_prices = data.get("outcomePrices")
            if isinstance(outcome_prices, list) and outcome_prices:
                return float(outcome_prices[0]) * 100.0
            raise RuntimeError("polymarket CLI response missing outcomePrices")
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.wait()
            logger.warning("PolymarketOracle refresh timeout for %s", self._config.oracle_slug)
            raise RuntimeError(
                f"polymarket CLI timed out for {self._config.oracle_slug}"
            ) from None
        except RuntimeError:
            logger.warning("PolymarketOracle refresh failed for %s", self._config.oracle_slug)
            raise
        except Exception as exc:
            logger.warning(
                "PolymarketOracle refresh failed for %s: %s",
                self._config.oracle_slug,
                exc,
            )
            raise RuntimeError(
                f"polymarket CLI failed for {self._config.oracle_slug}"
            ) from exc
