"""Regression tests for PR #9 bug fixes."""
from __future__ import annotations
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.amm.config.loader import ConfigLoader
from src.amm.config.models import GlobalConfig
from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.auth import TokenManager
from src.amm.connector.order_manager import ActiveOrder, OrderManager
from src.amm.connector.trade_poller import TradePoller
from src.amm.models.inventory import Inventory

AMM_USER_ID = "00000000-0000-4000-a000-000000000001"
OTHER_USER_ID = "ffffffff-ffff-4fff-afff-ffffffffffff"


# ─────────────────────────────────────────────────────────
# Bug 1: Config Loader — dataclasses.fields() not hasattr()
# ─────────────────────────────────────────────────────────

class TestConfigLoaderFields:
    async def test_load_global_ignores_unknown_yaml_keys(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "cfg.yaml"
        yaml_file.write_text(
            "global:\n"
            "  quote_interval_seconds: 5.0\n"
            "  unknown_key_not_in_model: 999\n"
        )
        loader = ConfigLoader(yaml_path=yaml_file)
        cfg = await loader.load_global()
        assert cfg.quote_interval_seconds == 5.0
        assert not hasattr(cfg, "unknown_key_not_in_model")

    async def test_load_market_ignores_unknown_yaml_keys(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "cfg.yaml"
        yaml_file.write_text(
            "markets:\n"
            "  default:\n"
            "    kappa: 2.0\n"
            "    ghost_field: bad_value\n"
        )
        loader = ConfigLoader(yaml_path=yaml_file)
        cfg = await loader.load_market("mkt-x")
        assert cfg.kappa == 2.0
        assert not hasattr(cfg, "ghost_field")

    async def test_load_global_valid_fields_are_applied(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "cfg.yaml"
        yaml_file.write_text(
            "global:\n"
            "  log_level: DEBUG\n"
            "  max_concurrent_markets: 10\n"
        )
        loader = ConfigLoader(yaml_path=yaml_file)
        cfg = await loader.load_global()
        assert cfg.log_level == "DEBUG"
        assert cfg.max_concurrent_markets == 10

    async def test_load_global_falls_back_to_defaults_on_empty_yaml(
        self, tmp_path: Path
    ) -> None:
        yaml_file = tmp_path / "cfg.yaml"
        yaml_file.write_text("{}")
        loader = ConfigLoader(yaml_path=yaml_file)
        cfg = await loader.load_global()
        assert isinstance(cfg, GlobalConfig)
        assert cfg.quote_interval_seconds == 2.0

    async def test_load_market_market_id_not_duplicated(self, tmp_path: Path) -> None:
        """market_id must not appear twice in constructor call."""
        yaml_file = tmp_path / "cfg.yaml"
        yaml_file.write_text(
            "markets:\n"
            "  default:\n"
            "    market_id: should-be-ignored\n"
            "    kappa: 1.5\n"
        )
        loader = ConfigLoader(yaml_path=yaml_file)
        # Should not raise TypeError about duplicate 'market_id'
        cfg = await loader.load_market("mkt-real")
        assert cfg.market_id == "mkt-real"


# ─────────────────────────────────────────────────────────────────────
# Bug 2: Cost Basis — seller uses avg acquisition cost, not sale price
# ─────────────────────────────────────────────────────────────────────

def _make_inventory(
    yes: int = 1000, no: int = 1000,
    yes_cost: int = 50_000, no_cost: int = 50_000,
) -> Inventory:
    return Inventory(
        cash_cents=500_000, yes_volume=yes, no_volume=no,
        yes_cost_sum_cents=yes_cost, no_cost_sum_cents=no_cost,
        yes_pending_sell=0, no_pending_sell=0, frozen_balance_cents=0,
    )


def _sell_yes_trade(price: int = 60, quantity: int = 100) -> dict:
    return {
        "id": "t1",
        "scenario": "TRANSFER_YES",
        "quantity": quantity,
        "price_cents": price,
        "buy_user_id": OTHER_USER_ID,  # AMM is seller
        "sell_user_id": AMM_USER_ID,
        "seller_fee_cents": 0,
    }


def _buy_yes_trade(price: int = 40, quantity: int = 100) -> dict:
    return {
        "id": "t2",
        "scenario": "TRANSFER_YES",
        "quantity": quantity,
        "price_cents": price,
        "buy_user_id": AMM_USER_ID,  # AMM is buyer
        "sell_user_id": OTHER_USER_ID,
        "buyer_fee_cents": 0,
    }


def _sell_no_trade(yes_price: int = 40, quantity: int = 100) -> dict:
    """no_price = 100 - yes_price."""
    return {
        "id": "t3",
        "scenario": "TRANSFER_NO",
        "quantity": quantity,
        "price_cents": yes_price,
        "buy_user_id": OTHER_USER_ID,
        "sell_user_id": AMM_USER_ID,
        "seller_fee_cents": 0,
    }


class TestCostBasisCalculation:
    async def test_sell_yes_uses_avg_cost_not_sale_price(self) -> None:
        """When AMM sells YES, cost_delta must be avg_cost*qty, not sale_price*qty."""
        # Inventory: 1000 YES shares at avg cost 50¢ each (total cost=50000)
        inv = _make_inventory(yes=1000, yes_cost=50_000)
        cache = AsyncMock()
        cache.get.return_value = inv
        api = AsyncMock()
        poller = TradePoller(api=api, cache=cache)

        trade = _sell_yes_trade(price=60, quantity=100)
        await poller._apply_trade("mkt-1", trade)

        # yes_cost_delta should be -(50000/1000)*100 = -5000 (avg cost), NOT -6000 (sale price)
        adjust_call = cache.adjust.call_args
        yes_cost_delta = adjust_call.kwargs.get("yes_cost_delta",
                         adjust_call[1].get("yes_cost_delta"))
        assert yes_cost_delta == -5000

    async def test_sell_yes_cash_delta_is_sale_proceeds(self) -> None:
        """Cash received = price * qty regardless of cost basis fix."""
        inv = _make_inventory(yes=1000, yes_cost=50_000)
        cache = AsyncMock()
        cache.get.return_value = inv
        api = AsyncMock()
        poller = TradePoller(api=api, cache=cache)

        trade = _sell_yes_trade(price=60, quantity=100)
        await poller._apply_trade("mkt-1", trade)

        adjust_call = cache.adjust.call_args
        cash_delta = adjust_call.kwargs.get("cash_delta",
                     adjust_call[1].get("cash_delta"))
        assert cash_delta == 6000  # 60 * 100

    async def test_buy_yes_cost_delta_equals_purchase_price(self) -> None:
        """When AMM buys YES, yes_cost_delta = price * qty (correct, unchanged)."""
        cache = AsyncMock()
        api = AsyncMock()
        poller = TradePoller(api=api, cache=cache)

        trade = _buy_yes_trade(price=40, quantity=200)
        await poller._apply_trade("mkt-1", trade)

        adjust_call = cache.adjust.call_args
        yes_cost_delta = adjust_call.kwargs.get("yes_cost_delta",
                         adjust_call[1].get("yes_cost_delta"))
        assert yes_cost_delta == 8000  # 40 * 200

    async def test_sell_no_uses_avg_cost_not_sale_price(self) -> None:
        """When AMM sells NO, no_cost_delta must be avg_cost*qty, not sale_price*qty."""
        # Inventory: 1000 NO shares at avg cost 50¢ each
        inv = _make_inventory(no=1000, no_cost=50_000)
        cache = AsyncMock()
        cache.get.return_value = inv
        api = AsyncMock()
        poller = TradePoller(api=api, cache=cache)

        # AMM sells NO; YES price=40 → NO price=60
        trade = _sell_no_trade(yes_price=40, quantity=100)
        await poller._apply_trade("mkt-1", trade)

        adjust_call = cache.adjust.call_args
        no_cost_delta = adjust_call.kwargs.get("no_cost_delta",
                        adjust_call[1].get("no_cost_delta"))
        # avg_cost = 50000/1000 = 50, cost_basis = 50*100 = 5000 → delta = -5000
        assert no_cost_delta == -5000

    async def test_sell_yes_fallback_when_inventory_unavailable(self) -> None:
        """If inventory can't be read, fall back to trade_value."""
        cache = AsyncMock()
        cache.get.return_value = None  # no inventory in Redis
        api = AsyncMock()
        poller = TradePoller(api=api, cache=cache)

        trade = _sell_yes_trade(price=60, quantity=100)
        await poller._apply_trade("mkt-1", trade)

        adjust_call = cache.adjust.call_args
        yes_cost_delta = adjust_call.kwargs.get("yes_cost_delta",
                         adjust_call[1].get("yes_cost_delta"))
        # Fallback: -(60 * 100) = -6000
        assert yes_cost_delta == -6000


# ──────────────────────────────────────────────────────────────────────────
# Bug 3: Order Execution — direction in key; get_pending_sells filters SELL
# ──────────────────────────────────────────────────────────────────────────

def _make_order(
    oid: str, side: str, direction: str, price: int, qty: int
) -> ActiveOrder:
    return ActiveOrder(
        order_id=oid, side=side, direction=direction,
        price_cents=price, remaining_quantity=qty,
    )


class TestOrderManagerPendingSells:
    def test_get_pending_sells_counts_only_sell_direction(self) -> None:
        mgr = OrderManager(api=AsyncMock(), cache=AsyncMock())
        mgr.active_orders = {
            "o1": _make_order("o1", "YES", "SELL", 55, 100),
            "o2": _make_order("o2", "YES", "BUY", 45, 200),  # should NOT count
            "o3": _make_order("o3", "NO", "SELL", 48, 50),
        }
        yes_p, no_p = mgr.get_pending_sells()
        assert yes_p == 100  # only o1
        assert no_p == 50    # only o3

    def test_get_pending_sells_zero_when_only_buy_orders(self) -> None:
        mgr = OrderManager(api=AsyncMock(), cache=AsyncMock())
        mgr.active_orders = {
            "o1": _make_order("o1", "YES", "BUY", 45, 300),
            "o2": _make_order("o2", "NO", "BUY", 55, 200),
        }
        yes_p, no_p = mgr.get_pending_sells()
        assert yes_p == 0
        assert no_p == 0

    async def test_execute_intents_key_includes_direction(self) -> None:
        """SELL YES@55 and BUY YES@55 must not collide as the same key."""
        api = AsyncMock()
        api.place_order.return_value = {"data": {"order_id": "new-order"}}
        cache = AsyncMock()
        mgr = OrderManager(api=api, cache=cache)

        # Pre-existing SELL order
        mgr.active_orders["existing"] = _make_order("existing", "YES", "SELL", 55, 100)

        from src.amm.strategy.models import OrderIntent
        from src.amm.models.enums import QuoteAction

        # Place a BUY order at the same price — should NOT match the existing SELL
        intents = [OrderIntent(
            action=QuoteAction.PLACE, side="YES", direction="BUY",
            price_cents=55, quantity=100,
        )]
        await mgr.execute_intents(intents, "mkt-1")

        # existing SELL should have been cancelled (not in target_keys which is BUY)
        api.cancel_order.assert_called_once_with("existing")
        # new BUY order should have been placed
        api.place_order.assert_called_once()

    async def test_execute_intents_same_side_price_direction_not_replaced(self) -> None:
        """Identical (side, direction, price) intent keeps existing order."""
        api = AsyncMock()
        cache = AsyncMock()
        mgr = OrderManager(api=api, cache=cache)

        mgr.active_orders["existing"] = _make_order("existing", "YES", "SELL", 55, 100)

        from src.amm.strategy.models import OrderIntent
        from src.amm.models.enums import QuoteAction

        intents = [OrderIntent(
            action=QuoteAction.PLACE, side="YES", direction="SELL",
            price_cents=55, quantity=100,
        )]
        await mgr.execute_intents(intents, "mkt-1")

        api.cancel_order.assert_not_called()
        api.place_order.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# Bug 4: HTTP Client Sharing
# ─────────────────────────────────────────────────────────────────────

class TestHttpClientSharing:
    def test_api_client_accepts_external_http_client(self) -> None:
        shared = httpx.AsyncClient(timeout=10.0)
        tm = MagicMock(spec=TokenManager)
        tm.access_token = "tok"
        api = AMMApiClient("http://localhost", tm, http_client=shared)
        assert api._client is shared
        shared.aclose  # check it exists (not closing in teardown)

    def test_api_client_creates_own_client_when_none_given(self) -> None:
        tm = MagicMock(spec=TokenManager)
        tm.access_token = "tok"
        api = AMMApiClient("http://localhost", tm)
        assert isinstance(api._client, httpx.AsyncClient)
        assert api._owns_client is True

    def test_api_client_does_not_own_external_client(self) -> None:
        shared = httpx.AsyncClient(timeout=10.0)
        tm = MagicMock(spec=TokenManager)
        tm.access_token = "tok"
        api = AMMApiClient("http://localhost", tm, http_client=shared)
        assert api._owns_client is False

    async def test_close_does_not_close_shared_client(self) -> None:
        shared = AsyncMock(spec=httpx.AsyncClient)
        tm = MagicMock(spec=TokenManager)
        tm.access_token = "tok"
        api = AMMApiClient("http://localhost", tm, http_client=shared)
        await api.close()
        shared.aclose.assert_not_called()

    async def test_close_closes_owned_client(self) -> None:
        tm = MagicMock(spec=TokenManager)
        tm.access_token = "tok"
        api = AMMApiClient("http://localhost", tm)
        api._client = AsyncMock(spec=httpx.AsyncClient)
        await api.close()
        api._client.aclose.assert_called_once()


class TestApiClientRetryStrategy:
    async def test_get_retries_on_503_with_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        request = httpx.Request("GET", "http://localhost/account/balance")
        retryable = httpx.Response(503, request=request)
        success = httpx.Response(
            200,
            request=request,
            json={"data": {"balance_cents": 100}},
        )

        client = AsyncMock(spec=httpx.AsyncClient)
        client.request.side_effect = [retryable, success]
        tm = MagicMock(spec=TokenManager)
        tm.access_token = "tok"
        api = AMMApiClient("http://localhost", tm, http_client=client)

        sleep_calls: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr("src.amm.connector.api_client.asyncio.sleep", fake_sleep)

        result = await api.get_balance()

        assert result == {"data": {"balance_cents": 100}}
        assert client.request.await_count == 2
        assert len(sleep_calls) == 1
        assert 1 <= sleep_calls[0] <= 2

    async def test_get_retries_on_timeout_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        request = httpx.Request("GET", "http://localhost/account/balance")
        success = httpx.Response(
            200,
            request=request,
            json={"data": {"balance_cents": 100}},
        )

        client = AsyncMock(spec=httpx.AsyncClient)
        client.request.side_effect = [
            httpx.TimeoutException("timed out"),
            success,
        ]
        tm = MagicMock(spec=TokenManager)
        tm.access_token = "tok"
        api = AMMApiClient("http://localhost", tm, http_client=client)

        sleep_calls: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        monkeypatch.setattr("src.amm.connector.api_client.asyncio.sleep", fake_sleep)

        result = await api.get_balance()

        assert result == {"data": {"balance_cents": 100}}
        assert client.request.await_count == 2
        assert sleep_calls == [1]

    async def test_post_does_not_retry_on_503(self, monkeypatch: pytest.MonkeyPatch) -> None:
        request = httpx.Request("POST", "http://localhost/orders")
        retryable = httpx.Response(503, request=request)

        client = AsyncMock(spec=httpx.AsyncClient)
        client.request.return_value = retryable
        tm = MagicMock(spec=TokenManager)
        tm.access_token = "tok"
        api = AMMApiClient("http://localhost", tm, http_client=client)

        sleep = AsyncMock()
        monkeypatch.setattr("src.amm.connector.api_client.asyncio.sleep", sleep)

        with pytest.raises(httpx.HTTPStatusError):
            await api.place_order({"market_id": "mkt-1", "side": "YES"})

        client.request.assert_awaited_once()
        sleep.assert_not_awaited()


class TestTokenRefreshRotation:
    async def test_refresh_updates_rotated_refresh_token(self) -> None:
        client = AsyncMock(spec=httpx.AsyncClient)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "data": {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
            }
        }
        client.post.return_value = response

        tm = TokenManager("http://localhost", "amm", "secret", client=client)
        tm._refresh_token = "old-refresh"

        await tm.refresh()

        assert tm.access_token == "new-access"
        assert tm._refresh_token == "new-refresh"
