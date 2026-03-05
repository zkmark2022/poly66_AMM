"""Phase 9 race-condition tests (T9.3, T9.4, T9.5)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import fakeredis.aioredis
import pytest
import respx

from src.amm.cache.inventory_cache import InventoryCache
from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.auth import TokenManager
from src.amm.connector.order_manager import OrderManager
from src.amm.connector.trade_poller import TradePoller
from src.amm.models.enums import QuoteAction
from src.amm.strategy.models import OrderIntent
from tests.integration.amm.conftest import BASE_URL, MARKET_ID


class TestPhase9RaceConditions:
    async def test_t93_replace_timeout_retry_does_not_create_duplicate_order(self) -> None:
        """T9.3: replace timeout retry should reuse the same idempotency key."""
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            route = router.post("/amm/orders/replace").mock(
                side_effect=[
                    httpx.TimeoutException("replace timeout"),
                    httpx.Response(200, json={"data": {"order_id": "replaced-1"}}),
                ]
            )

            async with httpx.AsyncClient(base_url=BASE_URL) as client:
                tm = TokenManager(base_url=BASE_URL, username="amm", password="x", client=client)
                tm.access_token = "amm-token"
                api = AMMApiClient(base_url=BASE_URL, token_manager=tm, http_client=client)

                resp = await api.replace_order(
                    old_order_id="ord-old-1",
                    new_order={
                        "market_id": MARKET_ID,
                        "side": "YES",
                        "direction": "SELL",
                        "price_cents": 56,
                        "quantity": 10,
                    },
                )

        assert resp["data"]["order_id"] == "replaced-1"
        assert route.call_count == 2
        first_body = json.loads(route.calls[0].request.content.decode())
        second_body = json.loads(route.calls[1].request.content.decode())
        assert first_body["idempotency_key"] == second_body["idempotency_key"]

    async def test_t94_recovery_flow_does_not_place_duplicate_order(self) -> None:
        """T9.4: after crash/restart, identical place intent should not place again."""
        api = AsyncMock()
        api.place_order.return_value = {"data": {"order_id": "ord-1"}}

        redis = fakeredis.aioredis.FakeRedis()
        cache = InventoryCache(redis=redis)

        mgr_before_crash = OrderManager(api=api, cache=cache)
        intent = OrderIntent(
            action=QuoteAction.PLACE,
            side="YES",
            direction="SELL",
            price_cents=55,
            quantity=100,
        )

        await mgr_before_crash.execute_intents([intent], MARKET_ID)

        # Simulate process crash + recovery: new OrderManager with empty in-memory state
        mgr_after_recovery = OrderManager(api=api, cache=cache)
        await mgr_after_recovery.execute_intents([intent], MARKET_ID)

        assert api.place_order.call_count == 1
        await redis.aclose()

    async def test_t95_duplicate_trade_not_double_counted(self) -> None:
        """T9.5: duplicate trade ID should only be applied once."""
        api = AsyncMock()
        dup_trade = {
            "id": "trade-dup-1",
            "scenario": "TRANSFER_YES",
            "quantity": 10,
            "price_cents": 50,
            "buy_user_id": "00000000-0000-4000-a000-000000000001",
            "sell_user_id": "other",
            "buyer_fee_cents": 0,
        }
        api.get_trades.return_value = {"data": {"trades": [dup_trade, dup_trade]}}

        cache = AsyncMock()
        poller = TradePoller(api=api, cache=cache)

        processed = await poller.poll(MARKET_ID)

        assert len(processed) == 1
        cache.adjust.assert_called_once()
