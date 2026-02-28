"""Tests for TradePoller and InventorySync.

Uses fakeredis for cache and unittest.mock for API client.
"""
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from src.amm.cache.inventory_cache import InventoryCache
from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.inventory_sync import InventorySync
from src.amm.connector.trade_poller import TradePoller
from src.amm.models.inventory import Inventory
from src.pm_account.domain.constants import AMM_USER_ID

MARKET = "mkt-test"
OTHER_USER = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def redis():
    server = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield server
    await server.aclose()


@pytest.fixture
async def inv_cache(redis):
    return InventoryCache(redis)


@pytest.fixture
def mock_api() -> AMMApiClient:
    api = MagicMock(spec=AMMApiClient)
    api.get_trades = AsyncMock()
    api.get_balance = AsyncMock()
    api.get_positions = AsyncMock()
    return api


@pytest.fixture
async def poller(mock_api, inv_cache) -> TradePoller:
    return TradePoller(mock_api, inv_cache)


@pytest.fixture
async def syncer(mock_api, inv_cache) -> InventorySync:
    return InventorySync(mock_api, inv_cache)


def make_inventory(**kw) -> Inventory:
    defaults = dict(
        cash_cents=1_000_000,
        yes_volume=1000,
        no_volume=1000,
        yes_cost_sum_cents=50_000,
        no_cost_sum_cents=50_000,
        yes_pending_sell=0,
        no_pending_sell=0,
        frozen_balance_cents=0,
    )
    defaults.update(kw)
    return Inventory(**defaults)


def trade(
    trade_id: str = "t1",
    scenario: str = "TRANSFER_YES",
    price_cents: int = 60,
    quantity: int = 100,
    buy_user_id: str = AMM_USER_ID,
    sell_user_id: str = OTHER_USER,
    buyer_fee_cents: int = 0,
    seller_fee_cents: int = 0,
) -> dict:
    return {
        "id": trade_id,
        "scenario": scenario,
        "price_cents": price_cents,
        "quantity": quantity,
        "buy_user_id": buy_user_id,
        "sell_user_id": sell_user_id,
        "buyer_fee_cents": buyer_fee_cents,
        "seller_fee_cents": seller_fee_cents,
    }


# ---------------------------------------------------------------------------
# TradePoller - volume / side tests
# ---------------------------------------------------------------------------

class TestTradePoller:
    async def test_poll_incremental_updates_redis(
        self, poller: TradePoller, inv_cache: InventoryCache
    ) -> None:
        await inv_cache.set(MARKET, make_inventory())
        poller._api.get_trades.return_value = {
            "data": {"trades": [trade("t1", "TRANSFER_YES", price_cents=60, quantity=100)]}
        }
        count = await poller.poll(MARKET)
        assert count == 1
        inv = await inv_cache.get(MARKET)
        assert inv is not None
        assert inv.yes_volume == 1100  # +100

    async def test_poll_deduplicates_trades(
        self, poller: TradePoller, inv_cache: InventoryCache
    ) -> None:
        await inv_cache.set(MARKET, make_inventory())
        poller._api.get_trades.return_value = {
            "data": {"trades": [trade("t1", "TRANSFER_YES", quantity=100)]}
        }
        await poller.poll(MARKET)
        count = await poller.poll(MARKET)  # same trade returned again
        assert count == 0
        inv = await inv_cache.get(MARKET)
        assert inv is not None
        assert inv.yes_volume == 1100  # applied only once

    async def test_poll_updates_cursor(self, poller: TradePoller, inv_cache: InventoryCache) -> None:
        await inv_cache.set(MARKET, make_inventory())
        poller._api.get_trades.return_value = {
            "data": {"trades": [trade("t42", "TRANSFER_YES", quantity=10)]}
        }
        await poller.poll(MARKET)
        assert poller._cursors[MARKET] == "t42"

    async def test_trade_buy_yes_increases_yes_volume(
        self, poller: TradePoller, inv_cache: InventoryCache
    ) -> None:
        await inv_cache.set(MARKET, make_inventory(yes_volume=500))
        poller._api.get_trades.return_value = {
            "data": {"trades": [trade("t1", "TRANSFER_YES", quantity=200, buy_user_id=AMM_USER_ID)]}
        }
        await poller.poll(MARKET)
        inv = await inv_cache.get(MARKET)
        assert inv is not None
        assert inv.yes_volume == 700

    async def test_trade_sell_yes_decreases_yes_volume(
        self, poller: TradePoller, inv_cache: InventoryCache
    ) -> None:
        await inv_cache.set(MARKET, make_inventory(yes_volume=500))
        poller._api.get_trades.return_value = {
            "data": {"trades": [trade("t1", "TRANSFER_YES", quantity=100,
                                      buy_user_id=OTHER_USER, sell_user_id=AMM_USER_ID)]}
        }
        await poller.poll(MARKET)
        inv = await inv_cache.get(MARKET)
        assert inv is not None
        assert inv.yes_volume == 400

    async def test_mint_trade_increases_both(
        self, poller: TradePoller, inv_cache: InventoryCache
    ) -> None:
        await inv_cache.set(MARKET, make_inventory(yes_volume=500, no_volume=500))
        poller._api.get_trades.return_value = {
            "data": {"trades": [trade("t1", "MINT", quantity=100)]}
        }
        await poller.poll(MARKET)
        inv = await inv_cache.get(MARKET)
        assert inv is not None
        assert inv.yes_volume == 600
        assert inv.no_volume == 600

    async def test_burn_trade_decreases_both(
        self, poller: TradePoller, inv_cache: InventoryCache
    ) -> None:
        await inv_cache.set(MARKET, make_inventory(yes_volume=500, no_volume=500))
        poller._api.get_trades.return_value = {
            "data": {"trades": [trade("t1", "BURN", quantity=50)]}
        }
        await poller.poll(MARKET)
        inv = await inv_cache.get(MARKET)
        assert inv is not None
        assert inv.yes_volume == 450
        assert inv.no_volume == 450

    # -----------------------------------------------------------------------
    # v1.0 Review Fix #2: fee and cost_sum tests
    # -----------------------------------------------------------------------

    async def test_buy_yes_deducts_fee_from_cash(
        self, poller: TradePoller, inv_cache: InventoryCache
    ) -> None:
        """BUY YES: cash_delta = -(price*qty + fee), not just -(price*qty)."""
        await inv_cache.set(MARKET, make_inventory(cash_cents=1_000_000))
        # price=60, qty=100 → trade_value=6000, fee=120
        poller._api.get_trades.return_value = {
            "data": {
                "trades": [
                    trade(
                        "t1",
                        "TRANSFER_YES",
                        price_cents=60,
                        quantity=100,
                        buy_user_id=AMM_USER_ID,
                        buyer_fee_cents=120,
                    )
                ]
            }
        }
        await poller.poll(MARKET)
        inv = await inv_cache.get(MARKET)
        assert inv is not None
        assert inv.cash_cents == 1_000_000 - 6000 - 120  # 993_880

    async def test_sell_yes_deducts_fee_from_revenue(
        self, poller: TradePoller, inv_cache: InventoryCache
    ) -> None:
        """SELL YES: cash_delta = +(price*qty - fee)."""
        await inv_cache.set(MARKET, make_inventory(cash_cents=1_000_000))
        # price=60, qty=100 → trade_value=6000, fee=120
        poller._api.get_trades.return_value = {
            "data": {
                "trades": [
                    trade(
                        "t1",
                        "TRANSFER_YES",
                        price_cents=60,
                        quantity=100,
                        buy_user_id=OTHER_USER,
                        sell_user_id=AMM_USER_ID,
                        seller_fee_cents=120,
                    )
                ]
            }
        }
        await poller.poll(MARKET)
        inv = await inv_cache.get(MARKET)
        assert inv is not None
        assert inv.cash_cents == 1_000_000 + 6000 - 120  # 1_005_880

    async def test_buy_yes_updates_cost_sum(
        self, poller: TradePoller, inv_cache: InventoryCache
    ) -> None:
        """BUY YES must increment yes_cost_sum_cents by trade_value."""
        await inv_cache.set(MARKET, make_inventory(yes_cost_sum_cents=0))
        # price=60, qty=100 → trade_value=6000
        poller._api.get_trades.return_value = {
            "data": {
                "trades": [
                    trade("t1", "TRANSFER_YES", price_cents=60, quantity=100,
                          buy_user_id=AMM_USER_ID)
                ]
            }
        }
        await poller.poll(MARKET)
        inv = await inv_cache.get(MARKET)
        assert inv is not None
        assert inv.yes_cost_sum_cents == 6000

    async def test_sell_no_updates_no_cost_sum(
        self, poller: TradePoller, inv_cache: InventoryCache
    ) -> None:
        """SELL NO must decrement no_cost_sum_cents."""
        await inv_cache.set(MARKET, make_inventory(no_cost_sum_cents=10_000))
        # YES price=60, so NO price=40, qty=50 → no_trade_value=2000
        poller._api.get_trades.return_value = {
            "data": {
                "trades": [
                    trade(
                        "t1",
                        "TRANSFER_NO",
                        price_cents=60,  # YES price; NO price = 40
                        quantity=50,
                        buy_user_id=OTHER_USER,
                        sell_user_id=AMM_USER_ID,
                    )
                ]
            }
        }
        await poller.poll(MARKET)
        inv = await inv_cache.get(MARKET)
        assert inv is not None
        assert inv.no_cost_sum_cents == 10_000 - 2000  # 8_000

    async def test_mint_deducts_cash_and_updates_cost_sums(
        self, poller: TradePoller, inv_cache: InventoryCache
    ) -> None:
        """MINT: cash -= qty*100, yes_cost += qty*50, no_cost += qty*50."""
        await inv_cache.set(
            MARKET,
            make_inventory(cash_cents=1_000_000, yes_cost_sum_cents=0, no_cost_sum_cents=0),
        )
        poller._api.get_trades.return_value = {
            "data": {"trades": [trade("t1", "MINT", quantity=100)]}
        }
        await poller.poll(MARKET)
        inv = await inv_cache.get(MARKET)
        assert inv is not None
        assert inv.cash_cents == 1_000_000 - 10_000  # 990_000
        assert inv.yes_cost_sum_cents == 5_000
        assert inv.no_cost_sum_cents == 5_000

    async def test_poll_no_trades_returns_zero(
        self, poller: TradePoller, inv_cache: InventoryCache
    ) -> None:
        await inv_cache.set(MARKET, make_inventory())
        poller._api.get_trades.return_value = {"data": {"trades": []}}
        count = await poller.poll(MARKET)
        assert count == 0


# ---------------------------------------------------------------------------
# InventorySync
# ---------------------------------------------------------------------------

class TestInventorySync:
    async def test_reconcile_writes_api_values(
        self, syncer: InventorySync, inv_cache: InventoryCache
    ) -> None:
        syncer._api.get_balance.return_value = {"cash_cents": 500_000}
        syncer._api.get_positions.return_value = {"yes_volume": 200, "no_volume": 150}

        inv = await syncer.reconcile(MARKET)
        assert inv.cash_cents == 500_000
        assert inv.yes_volume == 200
        assert inv.no_volume == 150

        cached = await inv_cache.get(MARKET)
        assert cached is not None
        assert cached.cash_cents == 500_000

    async def test_reconcile_preserves_existing_cost_sums(
        self, syncer: InventorySync, inv_cache: InventoryCache
    ) -> None:
        """Reconcile must not wipe yes_cost_sum / no_cost_sum (PnL tracking)."""
        existing = make_inventory(yes_cost_sum_cents=99_000, no_cost_sum_cents=88_000)
        await inv_cache.set(MARKET, existing)

        syncer._api.get_balance.return_value = {"cash_cents": 300_000}
        syncer._api.get_positions.return_value = {"yes_volume": 100, "no_volume": 100}

        inv = await syncer.reconcile(MARKET)
        assert inv.yes_cost_sum_cents == 99_000
        assert inv.no_cost_sum_cents == 88_000

    async def test_reconcile_resets_pending_sells(
        self, syncer: InventorySync, inv_cache: InventoryCache
    ) -> None:
        """Pending sell counts reset on reconcile (repopulated by order manager)."""
        existing = make_inventory(yes_pending_sell=200, no_pending_sell=100)
        await inv_cache.set(MARKET, existing)

        syncer._api.get_balance.return_value = {"cash_cents": 1_000_000}
        syncer._api.get_positions.return_value = {"yes_volume": 500, "no_volume": 500}

        inv = await syncer.reconcile(MARKET)
        assert inv.yes_pending_sell == 0
        assert inv.no_pending_sell == 0
