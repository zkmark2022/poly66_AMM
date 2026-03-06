"""Tests for PolymarketOracle — external price fetching and deviation detection."""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.amm.oracle.polymarket import PolymarketOracle


class TestPolymarketOracleGetPrice:
    @pytest.mark.asyncio
    async def test_returns_yes_price_from_outcome_prices(self) -> None:
        """get_price() converts outcomePrices[0] from fraction to cents."""
        oracle = PolymarketOracle("test-market")
        
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(
            json.dumps({"outcomePrices": ["0.52", "0.48"]}).encode(),
            b""
        ))
        
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            price = await oracle.get_price()

        assert price == pytest.approx(52.0)

    @pytest.mark.asyncio
    async def test_returns_default_50_when_no_outcome_prices(self) -> None:
        """get_price() returns 50.0 when outcomePrices key is missing."""
        oracle = PolymarketOracle("test-market")
        
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(
            json.dumps({"title": "some market"}).encode(),
            b""
        ))
        
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            price = await oracle.get_price()

        assert price == 50.0

    @pytest.mark.asyncio
    async def test_calls_polymarket_cli_with_correct_args(self) -> None:
        """get_price() invokes polymarket CLI with json output flag."""
        oracle = PolymarketOracle("will-btc-exceed-100000")
        
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(
            json.dumps({"outcomePrices": ["0.65", "0.35"]}).encode(),
            b""
        ))
        
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await oracle.get_price()

        mock_exec.assert_called_once_with(
            "polymarket", "-o", "json", "markets", "get", "will-btc-exceed-100000",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    @pytest.mark.asyncio
    async def test_updates_last_price_and_last_update_after_fetch(self) -> None:
        """get_price() stores fetched price and timestamp."""
        oracle = PolymarketOracle("test-market")
        
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(
            json.dumps({"outcomePrices": ["0.70", "0.30"]}).encode(),
            b""
        ))
        
        before = time.time()
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await oracle.get_price()
        after = time.time()

        assert oracle.last_price == pytest.approx(70.0)
        assert oracle.last_update is not None
        assert before <= oracle.last_update <= after

    @pytest.mark.asyncio
    async def test_returns_default_50_on_timeout(self) -> None:
        """get_price() returns 50.0 if subprocess times out."""
        oracle = PolymarketOracle("test-market")
        
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            price = await oracle.get_price()

        assert price == 50.0


class TestPolymarketOracleCheckDeviation:
    def test_returns_true_when_deviation_exceeds_threshold(self) -> None:
        """check_deviation() returns True when |internal - external| > threshold."""
        oracle = PolymarketOracle("test-market")
        oracle.last_price = 70.0  # 外部价格 70¢ (cached by refresh loop)
        internal_price = 45  # 内部价格 45¢ → 差值 25 > 20

        assert oracle.check_deviation(internal_price, threshold=20.0) is True

    def test_returns_false_when_deviation_within_threshold(self) -> None:
        """check_deviation() returns False when |internal - external| <= threshold."""
        oracle = PolymarketOracle("test-market")
        oracle.last_price = 55.0  # 外部价格 55¢
        internal_price = 50  # 差值 5 < 20

        assert oracle.check_deviation(internal_price, threshold=20.0) is False

    def test_returns_false_when_deviation_equals_threshold(self) -> None:
        """check_deviation() returns False when |internal - external| == threshold."""
        oracle = PolymarketOracle("test-market")
        oracle.last_price = 70.0
        internal_price = 50  # 差值 20 == threshold → not exceeded

        assert oracle.check_deviation(internal_price, threshold=20.0) is False

    def test_uses_default_threshold_of_20(self) -> None:
        """check_deviation() defaults to 20.0 cent threshold."""
        oracle = PolymarketOracle("test-market")
        oracle.last_price = 72.0
        internal_price = 50  # 差值 22 > 20

        assert oracle.check_deviation(internal_price) is True

    def test_works_when_internal_price_is_higher(self) -> None:
        """check_deviation() detects when internal exceeds external by threshold."""
        oracle = PolymarketOracle("test-market")
        oracle.last_price = 40.0
        internal_price = 65  # 差值 25 > 20

        assert oracle.check_deviation(internal_price, threshold=20.0) is True

    def test_returns_false_when_no_cached_price(self) -> None:
        """check_deviation() returns False when last_price is None (startup safe default)."""
        oracle = PolymarketOracle("test-market")
        assert oracle.last_price is None

        assert oracle.check_deviation(internal_price=50.0) is False


class TestPolymarketOracleCheckLag:
    def test_returns_true_when_no_last_update(self) -> None:
        """check_lag() returns True when oracle has never been updated."""
        oracle = PolymarketOracle("test-market")
        assert oracle.last_update is None
        assert oracle.check_lag(threshold_seconds=3.0) is True

    def test_returns_true_when_last_update_is_stale(self) -> None:
        """check_lag() returns True when last update is older than threshold."""
        oracle = PolymarketOracle("test-market")
        oracle.last_update = time.time() - 5.0  # 5 seconds ago, threshold 3s

        assert oracle.check_lag(threshold_seconds=3.0) is True

    def test_returns_false_when_last_update_is_recent(self) -> None:
        """check_lag() returns False when last update is within threshold."""
        oracle = PolymarketOracle("test-market")
        oracle.last_update = time.time() - 1.0  # 1 second ago, threshold 3s

        assert oracle.check_lag(threshold_seconds=3.0) is False
