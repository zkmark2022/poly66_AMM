"""Tests for GracefulShutdown — SIGTERM → batch_cancel → clean exit."""
from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest

from src.amm.lifecycle.shutdown import GracefulShutdown
from src.amm.models.market_context import MarketContext
from src.amm.models.inventory import Inventory
from src.amm.config.models import MarketConfig
from src.amm.models.enums import Phase, DefenseLevel


def _make_ctx(market_id: str) -> MarketContext:
    return MarketContext(
        market_id=market_id,
        config=MarketConfig(market_id=market_id),
        inventory=Inventory(
            cash_cents=100_000, yes_volume=500, no_volume=500,
            yes_cost_sum_cents=25000, no_cost_sum_cents=25000,
            yes_pending_sell=0, no_pending_sell=0, frozen_balance_cents=0,
        ),
    )


class TestGracefulShutdown:
    async def test_execute_cancels_all_markets(self) -> None:
        api = AsyncMock()
        shutdown = GracefulShutdown(api=api)
        contexts = {
            "mkt-1": _make_ctx("mkt-1"),
            "mkt-2": _make_ctx("mkt-2"),
        }
        await shutdown.execute(contexts)
        assert api.batch_cancel.call_count == 2
        api.batch_cancel.assert_any_call("mkt-1", scope="ALL")
        api.batch_cancel.assert_any_call("mkt-2", scope="ALL")

    async def test_execute_closes_api_client(self) -> None:
        api = AsyncMock()
        shutdown = GracefulShutdown(api=api)
        await shutdown.execute({"mkt-1": _make_ctx("mkt-1")})
        api.close.assert_called_once()

    async def test_execute_sets_shutdown_flag(self) -> None:
        api = AsyncMock()
        shutdown = GracefulShutdown(api=api)
        ctx = _make_ctx("mkt-1")
        assert ctx.shutdown_requested is False
        await shutdown.execute({"mkt-1": ctx})
        assert ctx.shutdown_requested is True

    async def test_execute_empty_contexts(self) -> None:
        api = AsyncMock()
        shutdown = GracefulShutdown(api=api)
        # Should not raise
        await shutdown.execute({})
        api.batch_cancel.assert_not_called()
        api.close.assert_called_once()

    async def test_execute_continues_on_cancel_error(self) -> None:
        api = AsyncMock()
        api.batch_cancel.side_effect = [Exception("network error"), None]
        shutdown = GracefulShutdown(api=api)
        contexts = {
            "mkt-1": _make_ctx("mkt-1"),
            "mkt-2": _make_ctx("mkt-2"),
        }
        # Should not raise even if one cancel fails
        await shutdown.execute(contexts)
        assert api.batch_cancel.call_count == 2
        api.close.assert_called_once()

    async def test_execute_single_market(self) -> None:
        api = AsyncMock()
        shutdown = GracefulShutdown(api=api)
        await shutdown.execute({"mkt-1": _make_ctx("mkt-1")})
        api.batch_cancel.assert_called_once_with("mkt-1", scope="ALL")
