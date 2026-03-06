"""AMM bot entry point — quote cycle orchestrator."""
import asyncio
import logging
import os
import signal
from pathlib import Path
import time
from typing import Any
from typing import cast

import httpx

from src.amm.cache.inventory_cache import InventoryCache
from src.amm.cache.protocols import AsyncRedisLike
from src.amm.cache.redis_client import create_redis_client
from src.amm.config.loader import ConfigLoader
from src.amm.connector.api_client import AMMApiClient
from src.amm.connector.auth import TokenManager
from src.amm.connector.order_manager import OrderManager
from src.amm.connector.trade_poller import TradePoller
from src.amm.lifecycle.health import HealthState, run_health_server
from src.amm.lifecycle.initializer import AMMInitializer
from src.amm.lifecycle.reinvest import (
    drop_buy_side_intents_when_cash_depleted,
    maybe_auto_reinvest,
)
from src.amm.lifecycle.reconciler import AMMReconciler
from src.amm.lifecycle.shutdown import GracefulShutdown
from src.amm.models.enums import DefenseLevel, Phase
from src.amm.models.market_context import MarketContext
from src.amm.risk.defense_stack import DefenseStack
from src.amm.risk.sanitizer import OrderSanitizer
from src.amm.strategy.as_engine import ASEngine
from src.amm.strategy.gradient import GradientEngine
from src.amm.strategy.phase_manager import PhaseManager
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.pricing.micro import MicroPricing
from src.amm.strategy.pricing.posterior import PosteriorPricing
from src.amm.oracle.polymarket import PolymarketOracle
from src.amm.oracle.polymarket_oracle import OracleState
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
    oracle: PolymarketOracle | None = None,
    phase_mgr: PhaseManager | None = None,
) -> None:
    """Single quote cycle for one market: Sync → Strategy → Risk → Execute."""

    # Step 1: Sync — poll new trades, refresh pending_sell
    recent_trades = await poller.poll(ctx.market_id)
    ctx.trade_count += len(recent_trades)
    fresh = await inventory_cache.get(ctx.market_id)
    if fresh is not None:
        ctx.inventory = fresh
    if ctx.phase == Phase.STABILIZATION:
        await maybe_auto_reinvest(ctx, api)

    # Step 1.5: Update phase based on trade count and elapsed time
    if phase_mgr is not None:
        elapsed_hours = (time.monotonic() - ctx.started_at) / 3600.0
        ctx.phase = phase_mgr.update(trade_count=ctx.trade_count, elapsed_hours=elapsed_hours)

    # Step 2: Strategy — fetch live orderbook, then compute mid-price
    best_bid = ctx.config.anchor_price_cents - 5
    best_ask = ctx.config.anchor_price_cents + 5
    bid_depth = 0
    ask_depth = 0
    try:
        ob = await api.get_orderbook(ctx.market_id)
        ob_data = ob.get("data", ob)
        best_bid = int(ob_data.get("best_bid", best_bid))
        best_ask = int(ob_data.get("best_ask", best_ask))
        bid_depth = int(ob_data.get("bid_depth", 0))
        ask_depth = int(ob_data.get("ask_depth", 0))
    except Exception:
        logger.warning("Orderbook fetch failed for %s — using anchor fallback", ctx.market_id)

    mid = pricing.compute(
        phase=ctx.phase.value,
        anchor_price=ctx.config.anchor_price_cents,
        best_bid=best_bid,
        best_ask=best_ask,
        recent_trades=recent_trades,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
    )

    tau = ctx.config.remaining_hours_override if ctx.config.remaining_hours_override is not None else 24.0
    sigma = as_engine.bernoulli_sigma(mid)
    gamma = as_engine.get_gamma_for_age(ctx.config)
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

    # Step 3: Risk — oracle check then defense evaluation
    oracle_passive = False
    if oracle is not None and ctx.config.oracle_slug:
        try:
            oracle.refresh()
        except RuntimeError as e:
            logger.warning("Oracle refresh failed for %s: %s", ctx.market_id, e)
        oracle_state = oracle.evaluate(internal_price_cents=float(mid))
        if oracle_state != OracleState.NORMAL:
            ctx.defense_level = oracle_state.defense_level
            logger.warning(
                "Oracle price deviation detected for %s (internal mid=%.1f) — forcing ONE_SIDE",
                ctx.market_id, mid,
            )
            oracle_passive = True

    defense = risk.evaluate(
        inventory_skew=ctx.inventory.inventory_skew,
        daily_pnl=ctx.daily_pnl_cents,
        market_active=True,
    )
    if oracle_passive and defense == DefenseLevel.NORMAL:
        defense = DefenseLevel.ONE_SIDE
    ctx.defense_level = defense

    if not defense.is_quoting_active:
        logger.warning("KILL_SWITCH active for %s — cancelling all orders", ctx.market_id)
        await order_mgr.cancel_all(ctx.market_id)
        return

    # Step 3.5: Apply WIDEN spread if defense level requires it
    if defense == DefenseLevel.WIDEN:
        widen_factor = ctx.config.widen_factor
        mid_price = (ask + bid) // 2
        ask = min(99, round(mid_price + (ask - mid_price) * widen_factor))
        bid = max(1, round(mid_price - (mid_price - bid) * widen_factor))
        ask = max(ask, bid + 1)  # ensure ask > bid

    base_qty = max(1, ctx.config.initial_mint_quantity // (ctx.config.gradient_levels * 2))
    ask_ladder = gradient.build_ask_ladder(ask, ctx.config, base_qty)
    bid_ladder = gradient.build_bid_ladder(bid, ctx.config, base_qty)

    intents = sanitizer.sanitize(ask_ladder + bid_ladder, defense, ctx)
    intents = drop_buy_side_intents_when_cash_depleted(intents, ctx.inventory.cash_cents)

    # Step 4: Execute — send order diff to API
    await order_mgr.execute_intents(intents, ctx.market_id)


async def run_market(
    ctx: MarketContext,
    services: dict[str, Any],
    oracle: PolymarketOracle | None = None,
) -> None:
    """Run quote cycles for a single market until shutdown requested."""
    while not ctx.shutdown_requested:
        try:
            await quote_cycle(ctx, oracle=oracle, **services)
        except Exception as e:
            logger.error("Quote cycle error for %s: %s", ctx.market_id, e, exc_info=True)
        await asyncio.sleep(ctx.config.quote_interval_seconds)


async def run_market_with_health(
    ctx: MarketContext,
    services: dict[str, Any],
    health_state: HealthState,
    oracle: PolymarketOracle | None = None,
) -> None:
    try:
        await run_market(ctx, services, oracle=oracle)
    finally:
        health_state.markets_active = max(0, health_state.markets_active - 1)


async def reconcile_loop(
    reconciler: AMMReconciler,
    contexts: dict[str, MarketContext],
    interval_seconds: float,
) -> None:
    market_ids = list(contexts)
    while not any(ctx.shutdown_requested for ctx in contexts.values()):
        await reconciler.reconcile(market_ids)
        await asyncio.sleep(interval_seconds)


async def amm_main(market_ids: list[str] | None = None) -> None:
    """AMM service entry point."""
    logging.basicConfig(level=logging.INFO)

    import yaml

    base_url = os.environ.get("AMM_BASE_URL", "http://localhost:8000/api/v1")
    redis_url = os.environ.get("AMM_REDIS_URL", "redis://localhost:6379/0")
    username = os.environ.get("AMM_USERNAME", "amm_market_maker")
    password = os.environ.get("AMM_PASSWORD", "")
    market_ids = market_ids or os.environ.get("AMM_MARKETS", "mkt-1").split(",")

    redis_client = create_redis_client(redis_url)
    typed_redis_client = cast(AsyncRedisLike, redis_client)
    http_client = httpx.AsyncClient(base_url=base_url, timeout=10.0)
    token_mgr = TokenManager(base_url, username, password, http_client)
    api = AMMApiClient(base_url, token_mgr, http_client=http_client)
    inventory_cache = InventoryCache(typed_redis_client)
    config_loader = ConfigLoader(redis_client=typed_redis_client)
    await config_loader.load_global()
    health_state = HealthState()

    # Initialize
    initializer = AMMInitializer(
        token_manager=token_mgr,
        api=api,
        config_loader=config_loader,
        inventory_cache=inventory_cache,
    )
    contexts = await initializer.initialize(market_ids)
    health_state.markets_active = len(contexts)
    health_state.ready = True

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
    # Load oracle config
    # Gemini fix: use project root detection instead of fragile .parent chain
    _project_root = Path(__file__).resolve().parent.parent.parent  # src/amm/main.py -> project root
    _oracle_cfg_path = _project_root / "config" / "oracle.yaml"
    oracle: PolymarketOracle | None = None
    _oracle_lag_threshold = 10.0
    _oracle_deviation_threshold = 20.0
    if _oracle_cfg_path.exists():
        with open(_oracle_cfg_path) as _f:
            _oracle_data = yaml.safe_load(_f).get("oracle", {})
        _slug = os.environ.get("ORACLE_MARKET_SLUG", _oracle_data.get("market_slug", ""))
        # Gemini fix: load thresholds from config
        _oracle_lag_threshold = _oracle_data.get("lag_threshold_seconds", 10.0)
        _oracle_deviation_threshold = _oracle_data.get("deviation_threshold_cents", 20.0)
        if _slug:
            oracle = PolymarketOracle(_slug)
            logger.info("Oracle enabled: market_slug=%s, lag_threshold=%s, deviation_threshold=%s", 
                       _slug, _oracle_lag_threshold, _oracle_deviation_threshold)
            # Warm-up: fetch price once so check_lag() doesn't deadlock on first cycle
            try:
                await oracle.get_price()
            except Exception as e:
                logger.warning("Oracle warm-up fetch failed (will retry in cycle): %s", e)
            # Store thresholds in contexts
            for ctx in contexts.values():
                ctx.oracle_lag_threshold = _oracle_lag_threshold
                ctx.oracle_deviation_threshold = _oracle_deviation_threshold
    else:
        logger.info("No oracle config found at %s — oracle disabled", _oracle_cfg_path)

    tasks = []
    for ctx in contexts.values():
        poller = TradePoller(api=api, cache=inventory_cache)
        pricing = ThreeLayerPricing(
            anchor=AnchorPricing(ctx.config.anchor_price_cents),
            micro=MicroPricing(),
            posterior=PosteriorPricing(),
            config=ctx.config,
        )
        as_engine = ASEngine()
        gradient = GradientEngine()
        risk = DefenseStack(ctx.config)
        sanitizer = OrderSanitizer()
        order_mgr = OrderManager(api=api, cache=inventory_cache)
        phase_mgr = PhaseManager(config=ctx.config)

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

            "oracle": oracle,
            "phase_mgr": phase_mgr,
        }
        tasks.append(asyncio.create_task(run_market_with_health(ctx, services, health_state, oracle=oracle)))

    # Background tasks: reconciler + health server
    reconciler = AMMReconciler(api=api, cache=inventory_cache)
    background_tasks: list[asyncio.Task] = []

    background_tasks.append(asyncio.create_task(
        reconcile_loop(reconciler, contexts, 300.0)
    ))
    background_tasks.append(asyncio.create_task(
        run_health_server(health_state)
    ))

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error("Market task %d failed: %s", i, result, exc_info=result)
    finally:
        health_state.ready = False
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await shutdown.execute(contexts)
        await http_client.aclose()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(amm_main())
