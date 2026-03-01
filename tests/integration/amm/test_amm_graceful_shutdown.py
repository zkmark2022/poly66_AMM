"""Integration test — Task 23: Graceful shutdown on SIGTERM.

Verifies:
- All open orders are cancelled (batch_cancel called per market)
- Shutdown continues even if one market's cancel fails
- Multi-market shutdown cancels all markets
"""
import httpx
import pytest
import respx

from src.amm.config.models import MarketConfig
from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.auth import TokenManager
from src.amm.lifecycle.shutdown import GracefulShutdown
from src.amm.models.enums import DefenseLevel

from tests.integration.amm.conftest import BASE_URL, MARKET_ID, make_context


class TestGracefulShutdown:
    """GracefulShutdown cancels all orders then closes the API client."""

    async def test_batch_cancel_called_on_shutdown(self) -> None:
        """GracefulShutdown.execute() calls batch_cancel for each active market."""
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            cancel_route = router.post("/amm/orders/batch-cancel").mock(
                return_value=httpx.Response(
                    200,
                    json={"data": {"cancelled_count": 5, "market_id": MARKET_ID}},
                )
            )

            tm = TokenManager(base_url=BASE_URL)
            tm._access_token = "preset_token"
            api = AMMApiClient(base_url=BASE_URL, token_manager=tm)

            ctx = make_context(market_id=MARKET_ID)
            shutdown = GracefulShutdown(api=api)
            await shutdown.execute(contexts={MARKET_ID: ctx})

            assert cancel_route.called
            assert cancel_route.call_count == 1

            import json
            body = json.loads(cancel_route.calls.last.request.content)
            assert body["market_id"] == MARKET_ID
            assert body["cancel_scope"] == "ALL"

    async def test_multi_market_shutdown_cancels_all(self) -> None:
        """GracefulShutdown cancels orders for every market in contexts."""
        market_ids = ["mkt-a", "mkt-b", "mkt-c"]

        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            cancel_route = router.post("/amm/orders/batch-cancel").mock(
                return_value=httpx.Response(
                    200, json={"data": {"cancelled_count": 2}}
                )
            )

            tm = TokenManager(base_url=BASE_URL)
            tm._access_token = "preset_token"
            api = AMMApiClient(base_url=BASE_URL, token_manager=tm)

            contexts = {
                mid: make_context(
                    market_id=mid,
                    config=MarketConfig(market_id=mid),
                )
                for mid in market_ids
            }

            shutdown = GracefulShutdown(api=api)
            await shutdown.execute(contexts=contexts)

            # One cancel call per market
            assert cancel_route.call_count == len(market_ids)

            # Verify each market_id was included in a cancel request
            cancelled_markets = set()
            import json
            for call in cancel_route.calls:
                body = json.loads(call.request.content)
                cancelled_markets.add(body["market_id"])

            assert cancelled_markets == set(market_ids)

    async def test_shutdown_completes_despite_one_market_cancel_failure(self) -> None:
        """GracefulShutdown continues to next market even if one cancel fails."""
        market_ids = ["mkt-good-1", "mkt-bad", "mkt-good-2"]

        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            # Alternate: success, failure, success
            cancel_route = router.post("/amm/orders/batch-cancel").mock(
                side_effect=[
                    httpx.Response(200, json={"data": {"cancelled_count": 3}}),
                    httpx.Response(500, json={"error": "Internal Server Error"}),
                    httpx.Response(200, json={"data": {"cancelled_count": 1}}),
                ]
            )

            tm = TokenManager(base_url=BASE_URL)
            tm._access_token = "preset_token"
            api = AMMApiClient(base_url=BASE_URL, token_manager=tm)

            contexts = {
                mid: make_context(market_id=mid, config=MarketConfig(market_id=mid))
                for mid in market_ids
            }

            shutdown = GracefulShutdown(api=api)
            # Should not raise — GracefulShutdown handles errors per-market
            await shutdown.execute(contexts=contexts)

            # All three cancel attempts were made
            assert cancel_route.call_count == len(market_ids)

    async def test_clean_exit_after_shutdown(self) -> None:
        """GracefulShutdown.execute() returns None — no exception on clean exit."""
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            router.post("/amm/orders/batch-cancel").mock(
                return_value=httpx.Response(
                    200, json={"data": {"cancelled_count": 0}}
                )
            )

            tm = TokenManager(base_url=BASE_URL)
            tm._access_token = "preset_token"
            api = AMMApiClient(base_url=BASE_URL, token_manager=tm)

            ctx = make_context(market_id=MARKET_ID)
            shutdown = GracefulShutdown(api=api)

            result = await shutdown.execute(contexts={MARKET_ID: ctx})
            assert result is None  # clean exit

    async def test_empty_contexts_shutdown_is_noop(self) -> None:
        """GracefulShutdown with no markets completes without any API calls."""
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            cancel_route = router.post("/amm/orders/batch-cancel").mock(
                return_value=httpx.Response(200, json={"data": {"cancelled_count": 0}})
            )

            tm = TokenManager(base_url=BASE_URL)
            tm._access_token = "preset_token"
            api = AMMApiClient(base_url=BASE_URL, token_manager=tm)

            shutdown = GracefulShutdown(api=api)
            await shutdown.execute(contexts={})

            assert not cancel_route.called


class TestShutdownCancelScope:
    """Batch cancel always uses scope=ALL to ensure no orders are left behind."""

    async def test_batch_cancel_scope_is_all(self) -> None:
        """GracefulShutdown always sends cancel_scope=ALL (not partial)."""
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            cancel_route = router.post("/amm/orders/batch-cancel").mock(
                return_value=httpx.Response(
                    200, json={"data": {"cancelled_count": 10}}
                )
            )

            tm = TokenManager(base_url=BASE_URL)
            tm._access_token = "preset_token"
            api = AMMApiClient(base_url=BASE_URL, token_manager=tm)

            ctx = make_context(market_id=MARKET_ID)
            shutdown = GracefulShutdown(api=api)
            await shutdown.execute(contexts={MARKET_ID: ctx})

            import json
            body = json.loads(cancel_route.calls.last.request.content)
            assert body["cancel_scope"] == "ALL"
