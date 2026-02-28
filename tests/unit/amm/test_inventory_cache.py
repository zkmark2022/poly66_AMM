"""Tests for Redis cache layer using fakeredis.

Covers InventoryCache and OrderCache CRUD operations.
"""
import pytest
import fakeredis.aioredis

from src.amm.cache.inventory_cache import InventoryCache
from src.amm.cache.order_cache import OrderCache
from src.amm.models.inventory import Inventory

MARKET = "mkt-abc"


@pytest.fixture
async def redis():
    server = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield server
    await server.aclose()


@pytest.fixture
async def inv_cache(redis):
    return InventoryCache(redis)


@pytest.fixture
async def order_cache(redis):
    return OrderCache(redis)


def make_inventory(**overrides) -> Inventory:
    defaults = dict(
        cash_cents=100_000,
        yes_volume=500,
        no_volume=500,
        yes_cost_sum_cents=25_000,
        no_cost_sum_cents=25_000,
        yes_pending_sell=0,
        no_pending_sell=0,
        frozen_balance_cents=0,
    )
    defaults.update(overrides)
    return Inventory(**defaults)


# ---------------------------------------------------------------------------
# InventoryCache
# ---------------------------------------------------------------------------

class TestInventoryCache:
    async def test_get_returns_none_when_missing(self, inv_cache: InventoryCache) -> None:
        result = await inv_cache.get("nonexistent-market")
        assert result is None

    async def test_set_and_get_roundtrip(self, inv_cache: InventoryCache) -> None:
        inv = make_inventory()
        await inv_cache.set(MARKET, inv)
        got = await inv_cache.get(MARKET)
        assert got is not None
        assert got.cash_cents == 100_000
        assert got.yes_volume == 500
        assert got.no_volume == 500
        assert got.yes_cost_sum_cents == 25_000
        assert got.no_cost_sum_cents == 25_000

    async def test_adjust_cash_delta(self, inv_cache: InventoryCache) -> None:
        await inv_cache.set(MARKET, make_inventory(cash_cents=100_000))
        await inv_cache.adjust(MARKET, cash_delta=-3_000)
        got = await inv_cache.get(MARKET)
        assert got is not None
        assert got.cash_cents == 97_000

    async def test_adjust_yes_delta(self, inv_cache: InventoryCache) -> None:
        await inv_cache.set(MARKET, make_inventory(yes_volume=500))
        await inv_cache.adjust(MARKET, yes_delta=100)
        got = await inv_cache.get(MARKET)
        assert got is not None
        assert got.yes_volume == 600

    async def test_adjust_no_delta_negative(self, inv_cache: InventoryCache) -> None:
        await inv_cache.set(MARKET, make_inventory(no_volume=500))
        await inv_cache.adjust(MARKET, no_delta=-50)
        got = await inv_cache.get(MARKET)
        assert got is not None
        assert got.no_volume == 450

    async def test_adjust_cost_deltas(self, inv_cache: InventoryCache) -> None:
        await inv_cache.set(MARKET, make_inventory(yes_cost_sum_cents=25_000, no_cost_sum_cents=25_000))
        await inv_cache.adjust(MARKET, yes_cost_delta=5_000, no_cost_delta=-2_000)
        got = await inv_cache.get(MARKET)
        assert got is not None
        assert got.yes_cost_sum_cents == 30_000
        assert got.no_cost_sum_cents == 23_000

    async def test_adjust_pending_sell(self, inv_cache: InventoryCache) -> None:
        await inv_cache.set(MARKET, make_inventory(yes_pending_sell=0, no_pending_sell=0))
        await inv_cache.adjust(MARKET, yes_pending_delta=100, no_pending_delta=50)
        got = await inv_cache.get(MARKET)
        assert got is not None
        assert got.yes_pending_sell == 100
        assert got.no_pending_sell == 50

    async def test_adjust_multiple_fields_atomically(self, inv_cache: InventoryCache) -> None:
        await inv_cache.set(MARKET, make_inventory())
        await inv_cache.adjust(
            MARKET,
            yes_delta=100,
            cash_delta=-5_000,
            yes_cost_delta=5_000,
        )
        got = await inv_cache.get(MARKET)
        assert got is not None
        assert got.yes_volume == 600
        assert got.cash_cents == 95_000
        assert got.yes_cost_sum_cents == 30_000

    async def test_delete_removes_key(self, inv_cache: InventoryCache) -> None:
        await inv_cache.set(MARKET, make_inventory())
        await inv_cache.delete(MARKET)
        result = await inv_cache.get(MARKET)
        assert result is None

    async def test_overwrite_with_set(self, inv_cache: InventoryCache) -> None:
        await inv_cache.set(MARKET, make_inventory(cash_cents=100_000))
        await inv_cache.set(MARKET, make_inventory(cash_cents=200_000))
        got = await inv_cache.get(MARKET)
        assert got is not None
        assert got.cash_cents == 200_000


# ---------------------------------------------------------------------------
# OrderCache
# ---------------------------------------------------------------------------

class TestOrderCache:
    async def test_get_all_empty(self, order_cache: OrderCache) -> None:
        result = await order_cache.get_all(MARKET)
        assert result == {}

    async def test_set_and_get_order(self, order_cache: OrderCache) -> None:
        order = {"id": "ord-1", "price_cents": 52, "quantity": 100, "side": "YES_BID"}
        await order_cache.set_order(MARKET, "ord-1", order)
        got = await order_cache.get_order(MARKET, "ord-1")
        assert got == order

    async def test_get_order_missing(self, order_cache: OrderCache) -> None:
        result = await order_cache.get_order(MARKET, "nonexistent")
        assert result is None

    async def test_get_all_multiple_orders(self, order_cache: OrderCache) -> None:
        orders = {
            "ord-1": {"id": "ord-1", "side": "YES_BID", "price_cents": 50},
            "ord-2": {"id": "ord-2", "side": "NO_BID", "price_cents": 48},
        }
        for oid, o in orders.items():
            await order_cache.set_order(MARKET, oid, o)
        result = await order_cache.get_all(MARKET)
        assert result == orders

    async def test_delete_order(self, order_cache: OrderCache) -> None:
        await order_cache.set_order(MARKET, "ord-1", {"id": "ord-1"})
        await order_cache.delete_order(MARKET, "ord-1")
        result = await order_cache.get_order(MARKET, "ord-1")
        assert result is None

    async def test_delete_all_clears_market(self, order_cache: OrderCache) -> None:
        for i in range(3):
            await order_cache.set_order(MARKET, f"ord-{i}", {"id": f"ord-{i}"})
        await order_cache.delete_all(MARKET)
        result = await order_cache.get_all(MARKET)
        assert result == {}

    async def test_upsert_overwrites_order(self, order_cache: OrderCache) -> None:
        await order_cache.set_order(MARKET, "ord-1", {"price_cents": 50})
        await order_cache.set_order(MARKET, "ord-1", {"price_cents": 55})
        got = await order_cache.get_order(MARKET, "ord-1")
        assert got is not None
        assert got["price_cents"] == 55
