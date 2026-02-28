"""AMM bot entry point — quote cycle orchestrator."""
import asyncio
import logging
import signal
from typing import Any

import httpx

from src.amm.cache.inventory_cache import InventoryCache
from src.amm.cache.redis_client import create_redis_client
from src.amm.config.loader import ConfigLoader
from src.amm.config.models import MarketConfig
from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.auth import TokenManager
from src.amm.connector.order_manager import OrderManager
from src.amm.connector.trade_poller import TradePoller
from src.amm.lifecycle.initializer import AMMInitializer
from src.amm.lifecycle.shutdown import GracefulShutdown
from src.amm.models.enums import DefenseLevel
from src.amm.models.market_context import MarketContext
from src.amm.risk.defense_stack import DefenseStack
from src.amm.risk.sanitizer import OrderSanitizer
from src.amm.strategy.as_engine import ASEngine
from src.amm.strategy.gradient import GradientEngine
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.pricing.micro import MicroPricing
from src.amm.strategy.pricing.posterior import PosteriorPricing
from src.amm.strategy.pricing.three_layer import ThreeLayerPricing

logger = logging.getLogger(__name__)


async def quote_cycle(
    ctx: MarketContext,
    api: AMMApiClient,
    poller: TradePoller,
    pricing: ThreeLayerPricing,
    as_engine: ASEngine,
    gradient: GradientEngine,
    risk: DefenseStack,
    sanitizer: OrderSanitizer,
    order_mgr: OrderManager,
    inventory_cache: InventoryCache,
) -> None:
    """Single quote cycle for one market: Sync → Strategy → Risk → Execute."""

    # Step 1: Sync — poll new trades, refresh pending_sell
    recent_trades = await poller.poll(ctx.market_id)
    fresh = await inventory_cache.get(ctx.market_id)
    if fresh is not None:
        ctx.inventory = fresh

    # Step 2: Strategy — fetch live orderbook, then compute mid-price
    best_bid = ctx.config.anchor_price_cents - 5
    best_ask = ctx.config.anchor_price_cents + 5
    try:
        ob = await api.get_orderbook(ctx.market_id)
        ob_data = ob.get("data", ob)
        best_bid = int(ob_data.get("best_bid", best_bid))
        best_ask = int(ob_data.get("best_ask", best_ask))
    except Exception:
        logger.warning("Orderbook fetch failed for %s — using anchor fallback", ctx.market_id)

    mid = pricing.compute(
        phase=ctx.phase.value,
        anchor_price=ctx.config.anchor_price_cents,
        best_bid=best_bid,
        best_ask=best_ask,
        recent_trades=recent_trades,
    )

    tau = ctx.config.remaining_hours_override or 24.0
    sigma = as_engine.bernoulli_sigma(mid)
    gamma = ctx.config.gamma
    kappa = ctx.config.kappa

    ask, bid = as_engine.compute_quotes(
        mid_price=mid,
        inventory_skew=ctx.inventory.inventory_skew,
        gamma=gamma,
        sigma=sigma,
        tau_hours=tau,
        kappa=kappa,
    )

    base_qty = max(1, ctx.config.initial_mint_quantity // (ctx.config.gradient_levels * 2))
    ask_ladder = gradient.build_ask_ladder(ask, ctx.config, base_qty)
    bid_ladder = gradient.build_bid_ladder(bid, ctx.config, base_qty)

    # Step 3: Risk — defense evaluation
    defense = risk.evaluate(
        inventory_skew=ctx.inventory.inventory_skew,
        daily_pnl=ctx.daily_pnl_cents,
        market_active=True,
    )
    ctx.defense_level = defense

    if not defense.is_quoting_active:
        logger.warning("KILL_SWITCH active for %s — cancelling all orders", ctx.market_id)
        await order_mgr.cancel_all(ctx.market_id)
        return

    intents = sanitizer.sanitize(ask_ladder + bid_ladder, defense, ctx)

    # Step 4: Execute — send order diff to API
    await order_mgr.execute_intents(intents, ctx.market_id)


async def run_market(
    ctx: MarketContext,
    services: dict[str, Any],
) -> None:
    """Run quote cycles for a single market until shutdown requested."""
    while not ctx.shutdown_requested:
        try:
            await quote_cycle(ctx, **services)
        except Exception as e:
            logger.error("Quote cycle error for %s: %s", ctx.market_id, e, exc_info=True)
        await asyncio.sleep(ctx.config.quote_interval_seconds)


async def amm_main(market_ids: list[str] | None = None) -> None:
    """AMM service entry point."""
    logging.basicConfig(level=logging.INFO)

    import os
    base_url = os.environ.get("AMM_BASE_URL", "http://localhost:8000/api/v1")
    redis_url = os.environ.get("AMM_REDIS_URL", "redis://localhost:6379/0")
    username = os.environ.get("AMM_USERNAME", "amm_market_maker")
    password = os.environ.get("AMM_PASSWORD", "")
    market_ids = market_ids or os.environ.get("AMM_MARKETS", "mkt-1").split(",")

    redis_client = create_redis_client(redis_url)
    http_client = httpx.AsyncClient(base_url=base_url, timeout=10.0)
    token_mgr = TokenManager(base_url, username, password, http_client)
    api = AMMApiClient(base_url, token_mgr)
    inventory_cache = InventoryCache(redis_client)
    config_loader = ConfigLoader(redis_client=redis_client)

    # Initialize
    initializer = AMMInitializer(
        token_manager=token_mgr,
        api=api,
        config_loader=config_loader,
        inventory_cache=inventory_cache,
    )
    contexts = await initializer.initialize(market_ids)

    # Shutdown handler
    shutdown = GracefulShutdown(api=api)

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        for ctx in contexts.values():
            ctx.shutdown_requested = True

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    # Build per-market services
    tasks = []
    for ctx in contexts.values():
        poller = TradePoller(api=api, cache=inventory_cache)
        pricing = ThreeLayerPricing(
            anchor=AnchorPricing(ctx.config.anchor_price_cents),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
        )
        as_engine = ASEngine()
        gradient = GradientEngine()
        risk = DefenseStack(ctx.config)
        sanitizer = OrderSanitizer()
        order_mgr = OrderManager(api=api, cache=inventory_cache)

        services = {
            "api": api,
            "poller": poller,
            "pricing": pricing,
            "as_engine": as_engine,
            "gradient": gradient,
            "risk": risk,
            "sanitizer": sanitizer,
            "order_mgr": order_mgr,
            "inventory_cache": inventory_cache,
        }
        tasks.append(asyncio.create_task(run_market(ctx, services)))

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await shutdown.execute(contexts)
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(amm_main())
