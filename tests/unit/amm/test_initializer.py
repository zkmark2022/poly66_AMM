"""Tests for AMMInitializer startup sequence."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.amm.lifecycle.initializer import AMMInitializer
from src.amm.config.models import GlobalConfig, MarketConfig
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext


def _make_api(
    balance: dict | None = None,
    positions: dict | None = None,
    market: dict | None = None,
    mint_resp: dict | None = None,
) -> AsyncMock:
    api = AsyncMock()
    api.get_balance.return_value = balance or {
        "data": {"balance_cents": 1_000_000, "frozen_balance_cents": 0}
    }
    api.get_positions.return_value = positions or {
        "data": {"yes_volume": 0, "no_volume": 0,
                 "yes_cost_sum_cents": 0, "no_cost_sum_cents": 0}
    }
    api.get_market.return_value = market or {
        "data": {"id": "mkt-1", "status": "ACTIVE"}
    }
    api.mint.return_value = mint_resp or {
        "data": {"quantity": 1000}
    }
    return api


def _make_cache() -> AsyncMock:
    cache = AsyncMock()
    cache.get.return_value = Inventory(
        cash_cents=1_000_000, yes_volume=1000, no_volume=1000,
        yes_cost_sum_cents=50000, no_cost_sum_cents=50000,
        yes_pending_sell=0, no_pending_sell=0, frozen_balance_cents=0,
    )
    return cache


def _make_config_loader(
    global_cfg: GlobalConfig | None = None,
    market_cfg: MarketConfig | None = None,
) -> AsyncMock:
    loader = AsyncMock()
    loader.load_global.return_value = global_cfg or GlobalConfig()
    loader.load_market.return_value = market_cfg or MarketConfig(market_id="mkt-1")
    return loader


def _make_token_manager() -> AsyncMock:
    tm = AsyncMock()
    tm.access_token = "test-token"
    return tm


class TestAMMInitializer:
    async def test_initialize_calls_login(self) -> None:
        token_mgr = _make_token_manager()
        init = AMMInitializer(
            token_manager=token_mgr,
            api=_make_api(),
            config_loader=_make_config_loader(),
            inventory_cache=_make_cache(),
        )
        await init.initialize(["mkt-1"])
        token_mgr.login.assert_called_once()

    async def test_initialize_loads_global_config(self) -> None:
        loader = _make_config_loader()
        init = AMMInitializer(
            token_manager=_make_token_manager(),
            api=_make_api(),
            config_loader=loader,
            inventory_cache=_make_cache(),
        )
        await init.initialize(["mkt-1"])
        loader.load_global.assert_called_once()

    async def test_initialize_loads_market_config(self) -> None:
        loader = _make_config_loader()
        init = AMMInitializer(
            token_manager=_make_token_manager(),
            api=_make_api(),
            config_loader=loader,
            inventory_cache=_make_cache(),
        )
        await init.initialize(["mkt-1"])
        loader.load_market.assert_called_once_with("mkt-1")

    async def test_initialize_fetches_balance_and_positions(self) -> None:
        # Use existing positions so mint is skipped → exactly one fetch each
        api = _make_api(positions={
            "data": {"yes_volume": 500, "no_volume": 500,
                     "yes_cost_sum_cents": 25000, "no_cost_sum_cents": 25000}
        })
        init = AMMInitializer(
            token_manager=_make_token_manager(),
            api=api,
            config_loader=_make_config_loader(),
            inventory_cache=_make_cache(),
        )
        await init.initialize(["mkt-1"])
        api.get_balance.assert_called_once()
        api.get_positions.assert_called_once_with("mkt-1")
        api.get_market.assert_called_once_with("mkt-1")

    async def test_initialize_writes_inventory_to_redis(self) -> None:
        cache = _make_cache()
        init = AMMInitializer(
            token_manager=_make_token_manager(),
            api=_make_api(),
            config_loader=_make_config_loader(),
            inventory_cache=cache,
        )
        await init.initialize(["mkt-1"])
        cache.set.assert_called()

    async def test_initialize_mints_when_no_positions(self) -> None:
        api = _make_api(positions={
            "data": {"yes_volume": 0, "no_volume": 0,
                     "yes_cost_sum_cents": 0, "no_cost_sum_cents": 0}
        })
        init = AMMInitializer(
            token_manager=_make_token_manager(),
            api=api,
            config_loader=_make_config_loader(),
            inventory_cache=_make_cache(),
        )
        await init.initialize(["mkt-1"])
        api.mint.assert_called_once()

    async def test_initialize_refetches_inventory_from_api_after_mint(self) -> None:
        """After mint, balance+positions must be re-fetched from API, not from cache."""
        api = _make_api(positions={
            "data": {"yes_volume": 0, "no_volume": 0,
                     "yes_cost_sum_cents": 0, "no_cost_sum_cents": 0}
        })
        cache = _make_cache()
        init = AMMInitializer(
            token_manager=_make_token_manager(),
            api=api,
            config_loader=_make_config_loader(),
            inventory_cache=cache,
        )
        await init.initialize(["mkt-1"])
        # With yes_volume=0, mint fires → get_balance + get_positions called twice
        assert api.get_balance.call_count == 2
        assert api.get_positions.call_count == 2
        # Cache is updated once with the final post-mint inventory only
        assert cache.set.call_count == 1
        # Cache.get is NOT used for post-mint re-fetch
        cache.get.assert_not_called()

    async def test_initialize_skips_mint_when_positions_exist(self) -> None:
        api = _make_api(positions={
            "data": {"yes_volume": 500, "no_volume": 500,
                     "yes_cost_sum_cents": 25000, "no_cost_sum_cents": 25000}
        })
        init = AMMInitializer(
            token_manager=_make_token_manager(),
            api=api,
            config_loader=_make_config_loader(),
            inventory_cache=_make_cache(),
        )
        await init.initialize(["mkt-1"])
        api.mint.assert_not_called()

    async def test_initialize_returns_market_contexts(self) -> None:
        init = AMMInitializer(
            token_manager=_make_token_manager(),
            api=_make_api(),
            config_loader=_make_config_loader(),
            inventory_cache=_make_cache(),
        )
        contexts = await init.initialize(["mkt-1", "mkt-2"])
        assert "mkt-1" in contexts
        assert "mkt-2" in contexts
        assert isinstance(contexts["mkt-1"], MarketContext)

    async def test_initialize_multiple_markets(self) -> None:
        loader = _make_config_loader()
        loader.load_market.side_effect = lambda mid: MarketConfig(market_id=mid)
        init = AMMInitializer(
            token_manager=_make_token_manager(),
            api=_make_api(),
            config_loader=loader,
            inventory_cache=_make_cache(),
        )
        contexts = await init.initialize(["mkt-1", "mkt-2", "mkt-3"])
        assert len(contexts) == 3

    async def test_initialize_failure_after_position_fetch_rolls_back_cache(self) -> None:
        api = _make_api()
        api.mint.side_effect = RuntimeError("mint failed")
        cache = _make_cache()
        init = AMMInitializer(
            token_manager=_make_token_manager(),
            api=api,
            config_loader=_make_config_loader(),
            inventory_cache=cache,
        )

        with pytest.raises(RuntimeError, match="mint failed"):
            await init.initialize(["mkt-1"])

        cache.set.assert_not_called()
        cache.delete.assert_called_once_with("mkt-1")

    async def test_initialize_inactive_market_raises_value_error(self) -> None:
        api = _make_api(market={"data": {"id": "mkt-1", "status": "PAUSED"}})
        cache = _make_cache()
        init = AMMInitializer(
            token_manager=_make_token_manager(),
            api=api,
            config_loader=_make_config_loader(),
            inventory_cache=cache,
        )

        with pytest.raises(ValueError, match="not active"):
            await init.initialize(["mkt-1"])

        cache.set.assert_not_called()
