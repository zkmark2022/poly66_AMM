"""Shared fixtures for AMM integration tests."""
import httpx
import pytest
import respx

import fakeredis.aioredis

from src.amm.cache.inventory_cache import InventoryCache
from src.amm.config.models import GlobalConfig, MarketConfig
from src.amm.models.enums import DefenseLevel, Phase
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext

BASE_URL = "http://localhost:8000/api/v1"
MARKET_ID = "mkt-test-1"

# ---------------------------------------------------------------------------
# Standard mock response payloads
# ---------------------------------------------------------------------------

LOGIN_RESPONSE = {
    "data": {
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
    }
}

REFRESH_RESPONSE = {
    "data": {
        "access_token": "refreshed_access_token",
        "refresh_token": "refreshed_refresh_token",
    }
}

BALANCE_RESPONSE = {
    "data": {"balance_cents": 1_000_000, "frozen_balance_cents": 0}
}

EMPTY_POSITIONS_RESPONSE = {
    "data": {
        "yes_volume": 0,
        "no_volume": 0,
        "yes_cost_sum_cents": 0,
        "no_cost_sum_cents": 0,
    }
}

EXISTING_POSITIONS_RESPONSE = {
    "data": {
        "yes_volume": 1000,
        "no_volume": 1000,
        "yes_cost_sum_cents": 50_000,
        "no_cost_sum_cents": 50_000,
    }
}

MARKET_ACTIVE_RESPONSE = {
    "data": {"id": MARKET_ID, "status": "ACTIVE"}
}

MINT_RESPONSE = {
    "data": {"yes_shares": 1000, "no_shares": 1000}
}

BATCH_CANCEL_RESPONSE = {
    "data": {"cancelled_count": 5, "market_id": MARKET_ID}
}


def setup_default_routes(
    router: respx.Router,
    market_id: str = MARKET_ID,
    positions_response: dict | None = None,
) -> None:
    """Register standard mock routes onto a respx router.

    Individual tests can override or add routes after calling this helper.
    """
    if positions_response is None:
        positions_response = EMPTY_POSITIONS_RESPONSE

    router.post("/auth/login").mock(
        return_value=httpx.Response(200, json=LOGIN_RESPONSE)
    )
    router.post("/auth/refresh").mock(
        return_value=httpx.Response(200, json=REFRESH_RESPONSE)
    )
    router.get("/account/balance").mock(
        return_value=httpx.Response(200, json=BALANCE_RESPONSE)
    )
    # API client calls GET /positions (list endpoint), not /positions/{market_id}
    router.get("/positions").mock(
        return_value=httpx.Response(200, json={
            "data": {"items": [{"market_id": market_id, **positions_response.get("data", {})}]}
        })
    )
    router.get(f"/positions/{market_id}").mock(
        return_value=httpx.Response(200, json=positions_response)
    )
    router.get(f"/markets/{market_id}").mock(
        return_value=httpx.Response(200, json=MARKET_ACTIVE_RESPONSE)
    )
    router.post("/amm/mint").mock(
        return_value=httpx.Response(200, json=MINT_RESPONSE)
    )
    router.post("/amm/orders/batch-cancel").mock(
        return_value=httpx.Response(200, json=BATCH_CANCEL_RESPONSE)
    )
    router.post("/orders").mock(
        return_value=httpx.Response(200, json={"data": {"order_id": "order-1"}})
    )
    router.post("/amm/orders/replace").mock(
        return_value=httpx.Response(200, json={"data": {"order_id": "replaced-1"}})
    )


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def market_config() -> MarketConfig:
    return MarketConfig(
        market_id=MARKET_ID,
        initial_mint_quantity=100,
        max_daily_loss_cents=10_000,
        max_per_market_loss_cents=5_000,
        inventory_skew_widen=0.3,
        inventory_skew_one_side=0.6,
        inventory_skew_kill=0.8,
        defense_cooldown_cycles=3,
    )


@pytest.fixture
def global_config() -> GlobalConfig:
    return GlobalConfig(base_url=BASE_URL)


@pytest.fixture
async def fake_redis():  # type: ignore[return]
    """In-memory Redis replacement (fakeredis async)."""
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.aclose()


@pytest.fixture
async def inventory_cache(fake_redis) -> InventoryCache:  # type: ignore[return]
    return InventoryCache(redis=fake_redis)


@pytest.fixture
def zero_inventory() -> Inventory:
    """Inventory with no positions — triggers initial mint."""
    return Inventory(
        cash_cents=1_000_000,
        yes_volume=0,
        no_volume=0,
        yes_cost_sum_cents=0,
        no_cost_sum_cents=0,
        yes_pending_sell=0,
        no_pending_sell=0,
        frozen_balance_cents=0,
    )


@pytest.fixture
def balanced_inventory() -> Inventory:
    """Inventory with balanced yes/no positions."""
    return Inventory(
        cash_cents=500_000,
        yes_volume=1000,
        no_volume=1000,
        yes_cost_sum_cents=50_000,
        no_cost_sum_cents=50_000,
        yes_pending_sell=0,
        no_pending_sell=0,
        frozen_balance_cents=0,
    )


def make_context(
    market_id: str = MARKET_ID,
    inventory: Inventory | None = None,
    defense_level: DefenseLevel = DefenseLevel.NORMAL,
    session_pnl_cents: int = 0,
    config: MarketConfig | None = None,
) -> MarketContext:
    """Helper to build a MarketContext for tests."""
    if inventory is None:
        inventory = Inventory(
            cash_cents=500_000,
            yes_volume=500,
            no_volume=500,
            yes_cost_sum_cents=25_000,
            no_cost_sum_cents=25_000,
            yes_pending_sell=0,
            no_pending_sell=0,
            frozen_balance_cents=0,
        )
    if config is None:
        config = MarketConfig(market_id=market_id)
    return MarketContext(
        market_id=market_id,
        config=config,
        inventory=inventory,
        phase=Phase.STABILIZATION,
        defense_level=defense_level,
        session_pnl_cents=session_pnl_cents,
    )
