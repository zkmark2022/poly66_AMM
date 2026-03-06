"""AMM startup sequence — login → config → balance → positions → Redis → mint."""
from __future__ import annotations

import logging
import time

from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.auth import TokenManager
from src.amm.config.loader import ConfigLoader
from src.amm.cache.inventory_cache import InventoryCache
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext
from src.amm.models.enums import Phase, DefenseLevel

logger = logging.getLogger(__name__)


class AMMInitializer:
    def __init__(
        self,
        token_manager: TokenManager,
        api: AMMApiClient,
        config_loader: ConfigLoader,
        inventory_cache: InventoryCache,
    ) -> None:
        self._token_manager = token_manager
        self._api = api
        self._config_loader = config_loader
        self._inventory_cache = inventory_cache

    async def initialize(self, market_ids: list[str]) -> dict[str, MarketContext]:
        """Full AMM startup sequence. Returns dict[market_id → MarketContext]."""
        # Step 1: Login
        await self._token_manager.login()
        logger.info("AMM initialization started for %d markets", len(market_ids))

        # Step 2: Load global config
        global_config = await self._config_loader.load_global()
        logger.info("Global config loaded: %s", global_config.base_url)

        contexts: dict[str, MarketContext] = {}
        n_markets = max(1, len(market_ids))

        for market_id in market_ids:
            logger.info("Initializing market %s", market_id)

            # Step 3: Load market config
            market_config = await self._config_loader.load_market(market_id)
            try:
                market_resp = await self._api.get_market(market_id)
                market_data = market_resp.get("data", {})
                market_status = str(market_data.get("status", "")).lower()
                if market_status not in {"active", "open"}:
                    raise ValueError(
                        f"Market {market_id} is not active (status={market_data.get('status')})"
                    )

                # Step 4: Fetch current state from API (DB truth)
                balance_resp = await self._api.get_balance()
                positions_resp = await self._api.get_positions(market_id)

                # Step 5: Build inventory locally, then mint if needed
                inventory = self._build_inventory(balance_resp, positions_resp)
                if inventory.yes_volume == 0 and inventory.no_volume == 0:
                    idempotency_key = f"init_{market_id}_{int(time.time())}"
                    await self._api.mint(
                        market_id,
                        market_config.initial_mint_quantity,
                        idempotency_key,
                    )
                    logger.info("Minted %d shares for market %s",
                                market_config.initial_mint_quantity, market_id)
                    balance_resp = await self._api.get_balance()
                    positions_resp = await self._api.get_positions(market_id)
                    inventory = self._build_inventory(balance_resp, positions_resp)

                # Step 5.5: Allocate per-market cash share
                inventory.allocated_cash_cents = inventory.cash_cents // n_markets

                # Step 6: Persist only the final inventory
                await self._inventory_cache.set(market_id, inventory)

                # Step 7: Create MarketContext
                anchor = market_config.anchor_price_cents
                initial_inv_value = inventory.total_value_cents(anchor)
                ctx = MarketContext(
                    market_id=market_id,
                    config=market_config,
                    inventory=inventory,
                    phase=Phase.EXPLORATION,
                    defense_level=DefenseLevel.NORMAL,
                    initial_inventory_value_cents=initial_inv_value,
                    last_known_market_active=True,
                    market_status_checked_at=time.monotonic(),
                )
                contexts[market_id] = ctx
                logger.info("Market %s initialized", market_id)
            except Exception:
                await self._inventory_cache.delete(market_id)
                raise

        return contexts

    def _build_inventory(self, balance_resp: dict, positions_resp: dict) -> Inventory:
        """Build Inventory dataclass from API responses."""
        bal = balance_resp.get("data", {})
        pos = positions_resp.get("data", {})

        return Inventory(
            cash_cents=int(bal.get("balance_cents", 0)),
            yes_volume=int(pos.get("yes_volume", 0)),
            no_volume=int(pos.get("no_volume", 0)),
            yes_cost_sum_cents=int(pos.get("yes_cost_sum_cents", 0)),
            no_cost_sum_cents=int(pos.get("no_cost_sum_cents", 0)),
            yes_pending_sell=0,
            no_pending_sell=0,
            frozen_balance_cents=int(bal.get("frozen_balance_cents", 0)),
        )
