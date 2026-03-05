"""PolyMarket CLI Oracle — external price feed for AMM defense.

Integrates with the polymarket CLI to detect:
- LVR (Loss-Versus-Rebalancing): price moves >threshold% within a short window
- Stale oracle: no price update for >stale_seconds
- Deviation: |internal - external| > deviation_cents threshold
"""
from __future__ import annotations

import json
import subprocess
import time
from enum import StrEnum

from src.amm.config.models import MarketConfig
from src.amm.models.enums import DefenseLevel

_POLYMARKET_BIN = "/opt/homebrew/bin/polymarket"


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

    def __init__(self, config: MarketConfig) -> None:
        self._config = config
        # (monotonic_timestamp, price_cents)
        self._price_history: list[tuple[float, float]] = []
        self._last_refresh_time: float | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Fetch price from Polymarket CLI and record it with current timestamp."""
        price = self._fetch_price()
        now = time.monotonic()
        self._last_refresh_time = now
        self._price_history.append((now, price))
        # Prune history older than 60 seconds (keep memory bounded)
        cutoff = now - 60.0
        self._price_history = [(t, p) for t, p in self._price_history if t >= cutoff]

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

    def _fetch_price(self) -> float:
        """Call polymarket CLI and parse YES price from outcomePrices field."""
        result = subprocess.run(
            [_POLYMARKET_BIN, "-o", "json", "markets", "get", self._config.oracle_slug],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Polymarket CLI error: {result.stderr}")
        data: dict = json.loads(result.stdout)
        return float(data["outcomePrices"][0]) * 100.0
