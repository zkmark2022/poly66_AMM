"""Integration test — Task 21: AMM full startup sequence.

Verifies:
- AMM can log in and receive JWT tokens
- AMM loads market config during initialization
- AMM mints initial shares when inventory is empty
- AMM skips mint when inventory already exists
- Inventory is written to Redis cache after startup
"""
import httpx
import pytest
import respx

from src.amm.cache.inventory_cache import InventoryCache
from src.amm.config.models import MarketConfig
from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.auth import TokenManager
from src.amm.lifecycle.initializer import AMMInitializer
from src.amm.models.enums import DefenseLevel, Phase

from tests.integration.amm.conftest import (
    BASE_URL,
    MARKET_ID,
    setup_default_routes,
)


class TestAMMLogin:
    """AMM can authenticate and obtain JWT tokens."""

    async def test_login_obtains_access_token(self) -> None:
        """POST /auth/login returns tokens that are stored in TokenManager."""
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            router.post("/auth/login").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": {
                            "access_token": "my_access_token",
                            "refresh_token": "my_refresh_token",
                        }
                    },
                )
            )

            tm = TokenManager(base_url=BASE_URL, username="amm", password="secret")
            await tm.login()

            assert tm.access_token == "my_access_token"
            await tm.close()

    async def test_login_used_as_auth_header(self, fake_redis) -> None:
        """After login, subsequent API calls include the Bearer token."""
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            router.post("/auth/login").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": {
                            "access_token": "bearer_token_xyz",
                            "refresh_token": "refresh_xyz",
                        }
                    },
                )
            )
            balance_route = router.get("/account/balance").mock(
                return_value=httpx.Response(
                    200, json={"data": {"available_balance": 500_000, "frozen_balance": 0}}
                )
            )

            tm = TokenManager(base_url=BASE_URL)
            await tm.login()

            api = AMMApiClient(base_url=BASE_URL, token_manager=tm)
            await api.get_balance()

            # Verify Bearer token was attached to the balance request
            last_request = balance_route.calls.last.request
            assert last_request.headers["authorization"] == "Bearer bearer_token_xyz"
            await api.close()
            await tm.close()


class TestAMMInitialMint:
    """AMM mints initial YES/NO shares when inventory is empty."""

    async def test_mint_called_when_no_inventory(
        self, fake_redis, market_config: MarketConfig
    ) -> None:
        """AMMInitializer.initialize() calls mint when position volumes are 0."""
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            setup_default_routes(router, positions_response={
                "data": {
                    "yes_volume": 0,
                    "no_volume": 0,
                    "yes_cost_sum_cents": 0,
                    "no_cost_sum_cents": 0,
                }
            })
            mint_route = router.post("/amm/mint").mock(
                return_value=httpx.Response(
                    200, json={"data": {"yes_shares": 100, "no_shares": 100}}
                )
            )

            tm = TokenManager(base_url=BASE_URL)
            api = AMMApiClient(base_url=BASE_URL, token_manager=tm)
            cache = InventoryCache(redis=fake_redis)
            initializer = AMMInitializer(api=api, token_manager=tm, inventory_cache=cache)

            await initializer.initialize(
                market_ids=[MARKET_ID],
                market_configs={MARKET_ID: market_config},
            )

            assert mint_route.called
            mint_body = mint_route.calls.last.request.content
            import json
            body = json.loads(mint_body)
            assert body["market_id"] == MARKET_ID
            assert body["quantity"] == market_config.initial_mint_quantity
            assert "idempotency_key" in body

            await api.close()
            await tm.close()

    async def test_mint_skipped_when_inventory_exists(
        self, fake_redis, market_config: MarketConfig
    ) -> None:
        """AMMInitializer.initialize() skips mint when positions already exist."""
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            setup_default_routes(router, positions_response={
                "data": {
                    "yes_volume": 1000,
                    "no_volume": 1000,
                    "yes_cost_sum_cents": 50_000,
                    "no_cost_sum_cents": 50_000,
                }
            })
            mint_route = router.post("/amm/mint").mock(
                return_value=httpx.Response(
                    200, json={"data": {"yes_shares": 100, "no_shares": 100}}
                )
            )

            tm = TokenManager(base_url=BASE_URL)
            api = AMMApiClient(base_url=BASE_URL, token_manager=tm)
            cache = InventoryCache(redis=fake_redis)
            initializer = AMMInitializer(api=api, token_manager=tm, inventory_cache=cache)

            contexts = await initializer.initialize(
                market_ids=[MARKET_ID],
                market_configs={MARKET_ID: market_config},
            )

            assert not mint_route.called
            ctx = contexts[MARKET_ID]
            assert ctx.inventory.yes_volume == 1000
            assert ctx.inventory.no_volume == 1000

            await api.close()
            await tm.close()


class TestAMMStartupContext:
    """AMMInitializer returns correctly populated MarketContext."""

    async def test_context_has_correct_market_id(
        self, fake_redis, market_config: MarketConfig
    ) -> None:
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            setup_default_routes(router)

            tm = TokenManager(base_url=BASE_URL)
            api = AMMApiClient(base_url=BASE_URL, token_manager=tm)
            cache = InventoryCache(redis=fake_redis)
            initializer = AMMInitializer(api=api, token_manager=tm, inventory_cache=cache)

            contexts = await initializer.initialize(
                market_ids=[MARKET_ID],
                market_configs={MARKET_ID: market_config},
            )

            assert MARKET_ID in contexts
            ctx = contexts[MARKET_ID]
            assert ctx.market_id == MARKET_ID

            await api.close()
            await tm.close()

    async def test_context_starts_in_exploration_phase(
        self, fake_redis, market_config: MarketConfig
    ) -> None:
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            setup_default_routes(router)

            tm = TokenManager(base_url=BASE_URL)
            api = AMMApiClient(base_url=BASE_URL, token_manager=tm)
            cache = InventoryCache(redis=fake_redis)
            initializer = AMMInitializer(api=api, token_manager=tm, inventory_cache=cache)

            contexts = await initializer.initialize(
                market_ids=[MARKET_ID],
                market_configs={MARKET_ID: market_config},
            )

            ctx = contexts[MARKET_ID]
            assert ctx.phase == Phase.EXPLORATION
            assert ctx.defense_level == DefenseLevel.NORMAL
            assert ctx.daily_pnl_cents == 0

            await api.close()
            await tm.close()

    async def test_inventory_written_to_redis(
        self, fake_redis, market_config: MarketConfig
    ) -> None:
        """After startup, inventory is persisted in Redis cache."""
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            setup_default_routes(router, positions_response={
                "data": {
                    "yes_volume": 500,
                    "no_volume": 600,
                    "yes_cost_sum_cents": 25_000,
                    "no_cost_sum_cents": 30_000,
                }
            })

            tm = TokenManager(base_url=BASE_URL)
            api = AMMApiClient(base_url=BASE_URL, token_manager=tm)
            cache = InventoryCache(redis=fake_redis)
            initializer = AMMInitializer(api=api, token_manager=tm, inventory_cache=cache)

            await initializer.initialize(
                market_ids=[MARKET_ID],
                market_configs={MARKET_ID: market_config},
            )

            # Verify Redis was populated
            cached = await cache.get(MARKET_ID)
            assert cached is not None
            assert cached.yes_volume == 500
            assert cached.no_volume == 600
            assert cached.yes_cost_sum_cents == 25_000

            await api.close()
            await tm.close()

    async def test_full_startup_multi_market(
        self, fake_redis, market_config: MarketConfig
    ) -> None:
        """AMM initializes multiple markets in a single call."""
        market_id_2 = "mkt-test-2"
        config_2 = MarketConfig(market_id=market_id_2, initial_mint_quantity=200)

        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            setup_default_routes(router)
            # Add routes for second market
            router.get(f"/positions/{market_id_2}").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": {
                            "yes_volume": 0,
                            "no_volume": 0,
                            "yes_cost_sum_cents": 0,
                            "no_cost_sum_cents": 0,
                        }
                    },
                )
            )
            router.get(f"/markets/{market_id_2}").mock(
                return_value=httpx.Response(
                    200, json={"data": {"id": market_id_2, "status": "ACTIVE"}}
                )
            )

            tm = TokenManager(base_url=BASE_URL)
            api = AMMApiClient(base_url=BASE_URL, token_manager=tm)
            cache = InventoryCache(redis=fake_redis)
            initializer = AMMInitializer(api=api, token_manager=tm, inventory_cache=cache)

            contexts = await initializer.initialize(
                market_ids=[MARKET_ID, market_id_2],
                market_configs={
                    MARKET_ID: market_config,
                    market_id_2: config_2,
                },
            )

            assert MARKET_ID in contexts
            assert market_id_2 in contexts
            assert contexts[MARKET_ID].config.initial_mint_quantity == 100
            assert contexts[market_id_2].config.initial_mint_quantity == 200

            await api.close()
            await tm.close()
