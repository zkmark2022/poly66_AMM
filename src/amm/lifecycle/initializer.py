"""AMM startup initializer: login → config → inventory sync → initial mint."""
import logging
import time
from copy import deepcopy

from src.amm.cache.inventory_cache import InventoryCache
from src.amm.config.models import MarketConfig
from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.auth import TokenManager
from src.amm.models.enums import DefenseLevel, Phase
from src.amm.models.inventory import Inventory
from src.amm.models.market_context import MarketContext

logger = logging.getLogger(__name__)


class AMMInitializer:
    """Orchestrates the full AMM startup sequence."""

    def __init__(
        self,
        api: AMMApiClient,
        token_manager: TokenManager,
        inventory_cache: InventoryCache,
    ) -> None:
        self._api = api
        self._token_manager = token_manager
        self._inventory_cache = inventory_cache

    async def initialize(
        self,
        market_ids: list[str],
        market_configs: dict[str, MarketConfig],
    ) -> dict[str, MarketContext]:
        """Full AMM startup sequence.

        Steps per market:
        1. Login (JWT tokens)
        2. Fetch balance + positions from DB via API
        3. Build Inventory from DB state and write to Redis
        4. Mint initial shares if inventory is empty
        5. Return populated MarketContext
        """
        logger.info("AMM startup: logging in...")
        await self._token_manager.login()

        contexts: dict[str, MarketContext] = {}

        for market_id in market_ids:
            logger.info("Initializing market %s", market_id)
            cfg = market_configs.get(market_id, MarketConfig(market_id=market_id))

            # Fetch current state from matching engine
            balance_resp = await self._api.get_balance()
            positions_resp = await self._api.get_positions(market_id)
            await self._api.get_market(market_id)  # validates market is accessible

            balance_data = balance_resp.get("data", {})
            positions_data = positions_resp.get("data", {})

            inventory = Inventory(
                cash_cents=int(balance_data.get("available_balance", 0)),  # type: ignore[arg-type]
                yes_volume=int(positions_data.get("yes_volume", 0)),  # type: ignore[arg-type]
                no_volume=int(positions_data.get("no_volume", 0)),  # type: ignore[arg-type]
                yes_cost_sum_cents=int(positions_data.get("yes_cost_sum_cents", 0)),  # type: ignore[arg-type]
                no_cost_sum_cents=int(positions_data.get("no_cost_sum_cents", 0)),  # type: ignore[arg-type]
                yes_pending_sell=0,
                no_pending_sell=0,
                frozen_balance_cents=int(balance_data.get("frozen_balance", 0)),  # type: ignore[arg-type]
            )

            # Write to Redis cache
            await self._inventory_cache.set(market_id, inventory)

            # Initial mint if no positions exist
            if inventory.yes_volume == 0 and inventory.no_volume == 0:
                mint_key = f"init_{market_id}_{int(time.time())}"
                logger.info(
                    "No inventory for market %s — minting %d shares",
                    market_id,
                    cfg.initial_mint_quantity,
                )
                await self._api.mint(market_id, cfg.initial_mint_quantity, mint_key)

            now = time.monotonic()
            ctx = MarketContext(
                market_id=market_id,
                config=cfg,
                inventory=inventory,
                phase=Phase.EXPLORATION,
                mid_price=cfg.anchor_price_cents,
                reservation_price=float(cfg.anchor_price_cents),
                optimal_spread=float(cfg.spread_min_cents),
                active_orders={},
                defense_level=DefenseLevel.NORMAL,
                daily_pnl_cents=0,
                session_start_inventory=deepcopy(inventory),
                last_quote_at=now,
                last_reconcile_at=now,
            )
            contexts[market_id] = ctx
            logger.info("Market %s initialized (phase=%s)", market_id, ctx.phase)

        return contexts
