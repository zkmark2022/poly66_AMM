"""Tests for PR #11 fixes: TradePoller market_id filter, ownership check, ID sanitization."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from src.amm.connector.trade_poller import TradePoller

AMM_USER_ID = "00000000-0000-4000-a000-000000000001"
OTHER_USER_ID = "ffffffff-ffff-4fff-afff-ffffffffffff"
MARKET_ID = "mkt-abc123"


def _api_resp(trades: list[dict]) -> dict:
    return {"data": {"trades": trades}}


def _amm_buy_trade(trade_id: str = "t1", scenario: str = "TRANSFER_YES",
                   price: int = 50, qty: int = 100) -> dict:
    return {
        "id": trade_id,
        "scenario": scenario,
        "quantity": qty,
        "price_cents": price,
        "buy_user_id": AMM_USER_ID,
        "sell_user_id": OTHER_USER_ID,
        "buyer_fee_cents": 0,
    }


def _amm_sell_trade(trade_id: str = "t2", scenario: str = "TRANSFER_YES",
                    price: int = 60, qty: int = 100) -> dict:
    return {
        "id": trade_id,
        "scenario": scenario,
        "quantity": qty,
        "price_cents": price,
        "buy_user_id": OTHER_USER_ID,
        "sell_user_id": AMM_USER_ID,
        "seller_fee_cents": 0,
    }


def _third_party_trade(trade_id: str = "t3") -> dict:
    return {
        "id": trade_id,
        "scenario": "TRANSFER_YES",
        "quantity": 50,
        "price_cents": 55,
        "buy_user_id": OTHER_USER_ID,
        "sell_user_id": "other-user-2",
        "buyer_fee_cents": 0,
    }


# ─────────────────────────────────────────────────────
# Fix 1: poll() must pass market_id to get_trades()
# ─────────────────────────────────────────────────────

class TestPollPassesMarketIdToApi:
    async def test_poll_passes_market_id_to_get_trades(self) -> None:
        api = AsyncMock()
        api.get_trades.return_value = _api_resp([])
        poller = TradePoller(api=api, cache=AsyncMock())

        await poller.poll(MARKET_ID)

        api.get_trades.assert_called_once_with(
            market_id=MARKET_ID, cursor="", limit=50
        )

    async def test_poll_passes_cursor_from_previous_call(self) -> None:
        api = AsyncMock()
        trade = _amm_buy_trade("t1")
        api.get_trades.side_effect = [
            _api_resp([trade]),
            _api_resp([]),
        ]
        cache = AsyncMock()
        cache.get.return_value = None
        poller = TradePoller(api=api, cache=cache)

        await poller.poll(MARKET_ID)
        await poller.poll(MARKET_ID)

        second_call = api.get_trades.call_args_list[1]
        # cursor should be the last trade id from previous response
        cursor = second_call.kwargs.get("cursor") or second_call[1].get("cursor")
        assert cursor == "t1"

    async def test_empty_response_returns_empty_list(self) -> None:
        api = AsyncMock()
        api.get_trades.return_value = _api_resp([])
        poller = TradePoller(api=api, cache=AsyncMock())

        result = await poller.poll(MARKET_ID)

        assert result == []


# ─────────────────────────────────────────────────────
# Fix 2: ownership check — ignore third-party trades
# ─────────────────────────────────────────────────────

class TestOwnershipFilter:
    async def test_amm_as_buyer_trade_is_applied(self) -> None:
        api = AsyncMock()
        api.get_trades.return_value = _api_resp([_amm_buy_trade()])
        cache = AsyncMock()
        cache.get.return_value = None
        poller = TradePoller(api=api, cache=cache)

        result = await poller.poll(MARKET_ID)

        assert len(result) == 1
        cache.adjust.assert_called_once()

    async def test_amm_as_seller_trade_is_applied(self) -> None:
        api = AsyncMock()
        api.get_trades.return_value = _api_resp([_amm_sell_trade()])
        cache = AsyncMock()
        cache.get.return_value = None  # fallback cost basis path
        poller = TradePoller(api=api, cache=cache)

        result = await poller.poll(MARKET_ID)

        assert len(result) == 1
        cache.adjust.assert_called()

    async def test_third_party_trade_is_ignored(self) -> None:
        api = AsyncMock()
        api.get_trades.return_value = _api_resp([_third_party_trade()])
        cache = AsyncMock()
        poller = TradePoller(api=api, cache=cache)

        result = await poller.poll(MARKET_ID)

        assert result == []
        cache.adjust.assert_not_called()

    async def test_mixed_batch_only_amm_trades_returned_and_applied(self) -> None:
        api = AsyncMock()
        trades = [
            _amm_buy_trade("t1"),
            _third_party_trade("t2"),
            _amm_sell_trade("t3"),
        ]
        api.get_trades.return_value = _api_resp(trades)
        cache = AsyncMock()
        cache.get.return_value = None
        poller = TradePoller(api=api, cache=cache)

        result = await poller.poll(MARKET_ID)

        assert len(result) == 2
        assert all(t["id"] in ("t1", "t3") for t in result)
        # cache.adjust called once per AMM trade
        assert cache.adjust.call_count == 2

    def test_is_amm_trade_as_buyer(self) -> None:
        poller = TradePoller(api=AsyncMock(), cache=AsyncMock())
        assert poller._is_amm_trade(
            {"buy_user_id": AMM_USER_ID, "sell_user_id": OTHER_USER_ID}
        ) is True

    def test_is_amm_trade_as_seller(self) -> None:
        poller = TradePoller(api=AsyncMock(), cache=AsyncMock())
        assert poller._is_amm_trade(
            {"buy_user_id": OTHER_USER_ID, "sell_user_id": AMM_USER_ID}
        ) is True

    def test_is_not_amm_trade_when_both_third_party(self) -> None:
        poller = TradePoller(api=AsyncMock(), cache=AsyncMock())
        assert poller._is_amm_trade(
            {"buy_user_id": OTHER_USER_ID, "sell_user_id": "yet-another"}
        ) is False

    async def test_custom_amm_user_id_is_respected(self) -> None:
        custom_id = "custom-amm-user"
        api = AsyncMock()
        api.get_trades.return_value = _api_resp([{
            "id": "t1",
            "scenario": "TRANSFER_YES",
            "quantity": 10,
            "price_cents": 50,
            "buy_user_id": custom_id,
            "sell_user_id": OTHER_USER_ID,
            "buyer_fee_cents": 0,
        }])
        cache = AsyncMock()
        cache.get.return_value = None
        poller = TradePoller(api=api, cache=cache, amm_user_id=custom_id)

        result = await poller.poll(MARKET_ID)
        assert len(result) == 1


# ─────────────────────────────────────────────────────
# Fix 3: ID sanitization before API calls
# ─────────────────────────────────────────────────────

class TestIdSanitization:
    async def test_path_traversal_market_id_raises(self) -> None:
        poller = TradePoller(api=AsyncMock(), cache=AsyncMock())
        with pytest.raises(ValueError, match="Invalid ID format"):
            await poller.poll("../../etc/passwd")

    async def test_market_id_with_spaces_raises(self) -> None:
        poller = TradePoller(api=AsyncMock(), cache=AsyncMock())
        with pytest.raises(ValueError, match="Invalid ID format"):
            await poller.poll("market id with spaces")

    async def test_market_id_with_special_chars_raises(self) -> None:
        poller = TradePoller(api=AsyncMock(), cache=AsyncMock())
        with pytest.raises(ValueError, match="Invalid ID format"):
            await poller.poll("mkt?evil=true")

    async def test_valid_alphanumeric_market_id_passes(self) -> None:
        api = AsyncMock()
        api.get_trades.return_value = _api_resp([])
        poller = TradePoller(api=api, cache=AsyncMock())
        # Should not raise
        result = await poller.poll("valid-mkt-ID_123")
        assert result == []


# ─────────────────────────────────────────────────────
# Deduplication (regression)
# ─────────────────────────────────────────────────────

class TestDeduplication:
    async def test_same_trade_id_not_processed_twice(self) -> None:
        api = AsyncMock()
        trade = _amm_buy_trade("dup-trade")
        api.get_trades.return_value = _api_resp([trade])
        cache = AsyncMock()
        cache.get.return_value = None
        poller = TradePoller(api=api, cache=cache)

        r1 = await poller.poll(MARKET_ID)
        r2 = await poller.poll(MARKET_ID)

        assert len(r1) == 1
        assert r2 == []
        assert cache.adjust.call_count == 1
