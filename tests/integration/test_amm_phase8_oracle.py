"""Phase 8 integration tests: LVR defense and Oracle price protection.

T8.1 — LVR rapid inventory loss: external price drops >20% within 500ms → LVR → KILL
T8.2 — Oracle stale: no price update >3s → STALE → PASSIVE_MODE (ONE_SIDE)
T8.3 — Oracle deviation: |internal - external| >20¢ → DEVIATION → AMM_PAUSE; auto-recovers
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.amm.config.models import MarketConfig
from src.amm.models.enums import DefenseLevel
from src.amm.oracle.polymarket_oracle import OracleState, PolymarketOracle

SLUG = "will-trump-win-2024"


def _oracle(
    slug: str = SLUG,
    stale_seconds: float = 3.0,
    deviation_cents: float = 20.0,
    lvr_window_seconds: float = 0.5,
    lvr_threshold: float = 0.20,
) -> PolymarketOracle:
    cfg = MarketConfig(
        market_id="test-market",
        oracle_slug=slug,
        oracle_stale_seconds=stale_seconds,
        oracle_deviation_cents=deviation_cents,
        oracle_lvr_window_seconds=lvr_window_seconds,
        oracle_lvr_threshold=lvr_threshold,
    )
    return PolymarketOracle(cfg)


def _proc_ok(yes_price_cents: float) -> MagicMock:
    """Mock async subprocess returning yes price."""
    proc = MagicMock()
    proc.returncode = 0
    no_price = 1.0 - yes_price_cents / 100.0
    proc.communicate = AsyncMock(return_value=(
        json.dumps({"outcomePrices": [str(yes_price_cents / 100.0), str(no_price)]}).encode(),
        b"",
    ))
    return proc


def _proc_error(message: str) -> MagicMock:
    proc = MagicMock()
    proc.returncode = 1
    proc.communicate = AsyncMock(return_value=(b"", message.encode()))
    return proc


# ---------------------------------------------------------------------------
# T8.1 — LVR Rapid Inventory Loss
# ---------------------------------------------------------------------------

class TestT81LVRRapidLoss:
    """T8.1: Price drops >20% within 500ms window → OracleState.LVR → KILL_SWITCH."""

    @pytest.mark.asyncio
    async def test_rapid_price_drop_triggers_lvr(self) -> None:
        """60¢ drops to 45¢ (25% drop) within 400ms → LVR state."""
        oracle = _oracle(lvr_window_seconds=0.5, lvr_threshold=0.20)
        t0 = 1000.0

        with patch("src.amm.oracle.polymarket_oracle.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _proc_ok(60.0)
            with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=t0):
                await oracle.refresh()

            mock_exec.return_value = _proc_ok(45.0)
            with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=t0 + 0.4):
                await oracle.refresh()
                state = oracle.evaluate(internal_price_cents=55.0)

        assert state == OracleState.LVR

    @pytest.mark.asyncio
    async def test_slow_price_drop_outside_window_stays_normal(self) -> None:
        """25% price drop but over 1.0s (outside 500ms window) → NORMAL."""
        oracle = _oracle(lvr_window_seconds=0.5, lvr_threshold=0.20)
        t0 = 1000.0

        with patch("src.amm.oracle.polymarket_oracle.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _proc_ok(60.0)
            with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=t0):
                await oracle.refresh()

            mock_exec.return_value = _proc_ok(45.0)
            with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=t0 + 1.0):
                await oracle.refresh()
                state = oracle.evaluate(internal_price_cents=55.0)

        assert state == OracleState.NORMAL

    @pytest.mark.asyncio
    async def test_small_price_move_within_window_stays_normal(self) -> None:
        """5% price drop within 400ms — below 20% threshold → NORMAL."""
        oracle = _oracle(lvr_window_seconds=0.5, lvr_threshold=0.20)
        t0 = 1000.0

        with patch("src.amm.oracle.polymarket_oracle.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _proc_ok(60.0)
            with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=t0):
                await oracle.refresh()

            mock_exec.return_value = _proc_ok(57.0)
            with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=t0 + 0.4):
                await oracle.refresh()
                state = oracle.evaluate(internal_price_cents=58.0)

        assert state == OracleState.NORMAL

    def test_lvr_maps_to_kill_switch_defense(self) -> None:
        """OracleState.LVR.defense_level == KILL_SWITCH (immediate halt)."""
        assert OracleState.LVR.defense_level == DefenseLevel.KILL_SWITCH


# ---------------------------------------------------------------------------
# T8.2 — Oracle Stale
# ---------------------------------------------------------------------------

class TestT82OracleStale:
    """T8.2: No price refresh for >3s → OracleState.STALE → PASSIVE_MODE (ONE_SIDE)."""

    @pytest.mark.asyncio
    async def test_stale_price_detected_after_threshold(self) -> None:
        """Refreshed at t0, checked at t0+4s (>3s threshold) → check_stale() True → STALE."""
        oracle = _oracle(stale_seconds=3.0)
        t0 = 1000.0

        with patch("src.amm.oracle.polymarket_oracle.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _proc_ok(50.0)
            with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=t0):
                await oracle.refresh()

        with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=t0 + 4.0):
            assert oracle.check_stale() is True
            state = oracle.evaluate(internal_price_cents=50.0)

        assert state == OracleState.STALE

    @pytest.mark.asyncio
    async def test_fresh_price_not_stale(self) -> None:
        """Refreshed 1s ago (< 3s threshold) → check_stale() False → NORMAL."""
        oracle = _oracle(stale_seconds=3.0)
        t0 = 1000.0

        with patch("src.amm.oracle.polymarket_oracle.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _proc_ok(50.0)
            with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=t0):
                await oracle.refresh()

        with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=t0 + 1.0):
            assert oracle.check_stale() is False
            state = oracle.evaluate(internal_price_cents=50.0)

        assert state == OracleState.NORMAL

    def test_never_refreshed_is_stale(self) -> None:
        """Oracle never called refresh() → check_stale() True (no data)."""
        oracle = _oracle(stale_seconds=3.0)
        assert oracle.check_stale() is True

    def test_stale_maps_to_one_side_defense(self) -> None:
        """OracleState.STALE.defense_level == ONE_SIDE (PASSIVE_MODE)."""
        assert OracleState.STALE.defense_level == DefenseLevel.ONE_SIDE


# ---------------------------------------------------------------------------
# T8.3 — Oracle Deviation + Auto-Recovery
# ---------------------------------------------------------------------------

class TestT83OracleDeviation:
    """T8.3: |internal - external| >20¢ → DEVIATION → AMM_PAUSE; auto-recovers when resolved."""

    @pytest.mark.asyncio
    async def test_deviation_exceeds_threshold_triggers_pause(self) -> None:
        """Internal=50¢, Oracle=75¢, deviation=25¢ > 20¢ → DEVIATION."""
        oracle = _oracle(deviation_cents=20.0)
        t0 = 1000.0

        with patch("src.amm.oracle.polymarket_oracle.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _proc_ok(75.0)
            with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=t0):
                await oracle.refresh()
                state = oracle.evaluate(internal_price_cents=50.0)

        assert state == OracleState.DEVIATION

    @pytest.mark.asyncio
    async def test_deviation_within_threshold_stays_normal(self) -> None:
        """Internal=50¢, Oracle=65¢, deviation=15¢ < 20¢ → NORMAL."""
        oracle = _oracle(deviation_cents=20.0)
        t0 = 1000.0

        with patch("src.amm.oracle.polymarket_oracle.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _proc_ok(65.0)
            with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=t0):
                await oracle.refresh()
                state = oracle.evaluate(internal_price_cents=50.0)

        assert state == OracleState.NORMAL

    @pytest.mark.asyncio
    async def test_auto_recovery_after_deviation_resolved(self) -> None:
        """Deviation → DEVIATION, then oracle converges → NORMAL (stateless: no sticky pause)."""
        oracle = _oracle(deviation_cents=20.0)
        t0 = 1000.0

        with patch("src.amm.oracle.polymarket_oracle.asyncio.create_subprocess_exec") as mock_exec:
            # Phase 1: large deviation → AMM_PAUSE
            mock_exec.return_value = _proc_ok(80.0)
            with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=t0):
                await oracle.refresh()
                state = oracle.evaluate(internal_price_cents=50.0)
            assert state == OracleState.DEVIATION

            # Phase 2: oracle recovers, prices converge
            mock_exec.return_value = _proc_ok(55.0)
            with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=t0 + 5.0):
                await oracle.refresh()
                state = oracle.evaluate(internal_price_cents=50.0)
        assert state == OracleState.NORMAL

    def test_deviation_maps_to_kill_switch_defense(self) -> None:
        """OracleState.DEVIATION.defense_level == KILL_SWITCH (AMM_PAUSE)."""
        assert OracleState.DEVIATION.defense_level == DefenseLevel.KILL_SWITCH


# ---------------------------------------------------------------------------
# Oracle CLI integration unit tests
# ---------------------------------------------------------------------------

class TestOracleCLI:
    """Unit tests for CLI invocation and price parsing."""

    @pytest.mark.asyncio
    async def test_get_yes_price_parses_outcome_prices(self) -> None:
        """get_yes_price() returns 52.0 when CLI outputs outcomePrices=['0.52', '0.48']."""
        oracle = _oracle()
        t0 = 1000.0

        with patch("src.amm.oracle.polymarket_oracle.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _proc_ok(52.0)
            with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=t0):
                await oracle.refresh()
                price = oracle.get_yes_price()

        assert price == pytest.approx(52.0)

    @pytest.mark.asyncio
    async def test_refresh_calls_correct_polymarket_command(self) -> None:
        """refresh() invokes the polymarket CLI with json output enabled."""
        oracle = _oracle(slug="my-test-slug")

        with patch("src.amm.oracle.polymarket_oracle.asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = _proc_ok(50.0)
            with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=1000.0):
                await oracle.refresh()

        mock_exec.assert_called_once_with(
            "polymarket",
            "-o",
            "json",
            "markets",
            "get",
            "my-test-slug",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    @pytest.mark.asyncio
    async def test_cli_non_zero_exit_keeps_oracle_stale(self) -> None:
        """CLI failures must not be recorded as a healthy neutral sample."""
        oracle = _oracle()

        with patch(
            "src.amm.oracle.polymarket_oracle.asyncio.create_subprocess_exec",
            return_value=_proc_error("market not found"),
        ):
            with patch("src.amm.oracle.polymarket_oracle.time.monotonic", return_value=1000.0):
                with pytest.raises(RuntimeError, match="market not found"):
                    await oracle.refresh()
        assert oracle.check_stale() is True
        with pytest.raises(RuntimeError, match="No price data"):
            oracle.get_yes_price()

    def test_get_yes_price_before_refresh_raises(self) -> None:
        """get_yes_price() before any refresh() raises RuntimeError (no data)."""
        oracle = _oracle()
        with pytest.raises(RuntimeError, match="No price data"):
            oracle.get_yes_price()
