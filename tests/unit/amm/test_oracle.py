"""Tests for the unified PolymarketOracle implementation."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.amm.config.models import MarketConfig
from src.amm.oracle.polymarket_oracle import PolymarketOracle


def _make_config(**kwargs: object) -> MarketConfig:
    defaults: dict[str, object] = {
        "market_id": "mkt-test",
        "oracle_slug": "test-market",
        "oracle_stale_seconds": 3.0,
        "oracle_deviation_cents": 20.0,
        "oracle_lvr_window_seconds": 0.5,
        "oracle_lvr_threshold": 0.2,
    }
    defaults.update(kwargs)
    return MarketConfig(**defaults)


class TestPolymarketOracleRefresh:
    def test_constructor_requires_market_config(self) -> None:
        """Only the MarketConfig constructor is supported."""
        with pytest.raises(TypeError):
            PolymarketOracle("will-btc-exceed-100000")

    @pytest.mark.asyncio
    async def test_refresh_is_awaitable_for_config_constructor(self) -> None:
        """refresh() must be async on the unified oracle implementation."""
        oracle = PolymarketOracle(_make_config())

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(
            json.dumps({"outcomePrices": ["0.52", "0.48"]}).encode(),
            b"",
        ))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await oracle.refresh()

        assert oracle.get_yes_price() == pytest.approx(52.0)

    def test_constructor_rejects_legacy_market_slug_keyword(self) -> None:
        """Explicit slug-only construction was removed with the legacy oracle path."""
        with pytest.raises(TypeError):
            PolymarketOracle(market_slug="will-btc-exceed-100000")

    @pytest.mark.asyncio
    async def test_refresh_raises_when_outcome_prices_missing(self) -> None:
        """refresh() should reject malformed CLI output without mutating cached state."""
        oracle = PolymarketOracle(_make_config())

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(
            json.dumps({"title": "some market"}).encode(),
            b"",
        ))

        with patch("src.amm.oracle.polymarket_oracle.asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="outcomePrices"):
                await oracle.refresh()

        assert oracle.check_stale() is True
        with pytest.raises(RuntimeError, match="No price data"):
            oracle.get_yes_price()

    @pytest.mark.asyncio
    async def test_refresh_timeout_keeps_oracle_stale(self) -> None:
        """refresh() timeout must not mark the oracle as freshly updated."""
        oracle = PolymarketOracle(_make_config())

        mock_proc = MagicMock()
        mock_proc.returncode = None  # process still running when timeout fires
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_proc.wait = AsyncMock()

        with patch("src.amm.oracle.polymarket_oracle.asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="timed out"):
                await oracle.refresh()

        mock_proc.kill.assert_called_once_with()
        mock_proc.wait.assert_awaited_once_with()
        assert oracle.check_stale() is True
        with pytest.raises(RuntimeError, match="No price data"):
            oracle.get_yes_price()

    @pytest.mark.asyncio
    async def test_refresh_rejects_missing_returncode_after_subprocess_completion(self) -> None:
        """A subprocess without a resolved return code should be treated as failed."""
        oracle = PolymarketOracle(_make_config())

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.communicate = AsyncMock(return_value=(
            json.dumps({"outcomePrices": ["0.65", "0.35"]}).encode(),
            b"",
        ))

        with patch("src.amm.oracle.polymarket_oracle.asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="did not exit cleanly"):
                await oracle.refresh()


class TestPolymarketOracleCheckDeviation:
    def test_returns_true_when_deviation_exceeds_threshold(self) -> None:
        """check_deviation() returns True when |internal - external| > threshold."""
        oracle = PolymarketOracle(_make_config(oracle_deviation_cents=20.0))
        oracle._price_history.append((1000.0, 70.0))
        internal_price = 45  # 内部价格 45¢ → 差值 25 > 20

        assert oracle.check_deviation(internal_price) is True

    def test_returns_false_when_deviation_within_threshold(self) -> None:
        """check_deviation() returns False when |internal - external| <= threshold."""
        oracle = PolymarketOracle(_make_config(oracle_deviation_cents=20.0))
        oracle._price_history.append((1000.0, 55.0))
        internal_price = 50  # 差值 5 < 20

        assert oracle.check_deviation(internal_price) is False

    def test_returns_false_when_deviation_equals_threshold(self) -> None:
        """check_deviation() returns False when |internal - external| == threshold."""
        oracle = PolymarketOracle(_make_config(oracle_deviation_cents=20.0))
        oracle._price_history.append((1000.0, 70.0))
        internal_price = 50  # 差值 20 == threshold → not exceeded

        assert oracle.check_deviation(internal_price) is False

    def test_uses_config_threshold(self) -> None:
        """check_deviation() should use the configured threshold."""
        oracle = PolymarketOracle(_make_config(oracle_deviation_cents=20.0))
        oracle._price_history.append((1000.0, 72.0))
        internal_price = 50  # 差值 22 > 20

        assert oracle.check_deviation(internal_price) is True

    def test_works_when_internal_price_is_higher(self) -> None:
        """check_deviation() detects when internal exceeds external by threshold."""
        oracle = PolymarketOracle(_make_config(oracle_deviation_cents=20.0))
        oracle._price_history.append((1000.0, 40.0))
        internal_price = 65  # 差值 25 > 20

        assert oracle.check_deviation(internal_price) is True


class TestPolymarketOracleCheckStale:
    def test_returns_true_when_never_refreshed(self) -> None:
        """check_stale() returns True when oracle has never refreshed."""
        oracle = PolymarketOracle(_make_config())
        assert oracle.check_stale() is True
