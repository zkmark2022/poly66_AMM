"""AMM bot entry point — quote cycle orchestrator."""
import asyncio
import inspect
import logging
import os
import signal
from pathlib import Path
import time
from typing import Any, Callable, cast

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
from src.amm.risk.defense_stack import DEFENSE_SEVERITY, DefenseStack
from src.amm.risk.sanitizer import OrderSanitizer
from src.amm.strategy.as_engine import ASEngine
from src.amm.strategy.gradient import GradientEngine
from src.amm.strategy.phase_manager import PhaseManager
from src.amm.strategy.pricing.anchor import AnchorPricing
from src.amm.strategy.pricing.micro import MicroPricing
from src.amm.strategy.pricing.posterior import PosteriorPricing
from src.amm.oracle.polymarket_oracle import OracleState, PolymarketOracle
from src.amm.strategy.pricing.three_layer import ThreeLayerPricing

logger = logging.getLogger(__name__)
SHUTDOWN_TIMEOUT_SECONDS = 30.0

_DEFENSE_SEVERITY = DEFENSE_SEVERITY  # single source of truth; avoids silent divergence
_MARKET_STATUS_TTL = 30.0  # seconds between live market-status API fetches


def _build_signal_handler(
    contexts: dict[str, MarketContext],
    market_task_handles: list[asyncio.Task],
    background_tasks: list[asyncio.Task],
    shutdown_event: asyncio.Event,
) -> Callable[[], None]:
    def _signal_handler() -> None:
        logger.info("Shutdown signal received - initiating graceful shutdown")
        for ctx in contexts.values():
            ctx.shutdown_requested = True
        shutdown_event.set()
        for task in market_task_handles + background_tasks:
            task.cancel()

    return _signal_handler


def _shutdown_requested(contexts: dict[str, MarketContext]) -> bool:
    return any(ctx.shutdown_requested for ctx in contexts.values())


async def _wait_for_task_shutdown(tasks: list[asyncio.Task], timeout_seconds: float) -> None:
    if not tasks:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning("Shutdown timeout after %.0fs - forcing cancellation", timeout_seconds)
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            logger.error("Tasks did not exit after forced cancellation")


async def _oracle_refresh_loop(
    oracle: PolymarketOracle,
    interval_seconds: float,
    shutdown_contexts: dict[str, MarketContext],
) -> None:
    """Periodically refresh oracle price in background."""
    while not _shutdown_requested(shutdown_contexts):
        try:
            await _refresh_oracle(oracle)
            logger.debug("Oracle price refreshed: %.2f", getattr(oracle, "last_price", 0) or 0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Oracle background refresh failed: %s", e)

        if _shutdown_requested(shutdown_contexts):
            break

        await asyncio.sleep(interval_seconds)


async def _refresh_oracle(oracle: PolymarketOracle) -> None:
    refresh = getattr(oracle, "refresh", None)
    if callable(refresh):
        if inspect.iscoroutinefunction(refresh):
            await refresh()
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, refresh)
        return

    await oracle.get_price()


async def _evaluate_oracle_state(
    oracle: Any,
    ctx: MarketContext,
    internal_price_cents: float,
) -> OracleState:
    evaluate = getattr(oracle, "evaluate", None)
    if callable(evaluate):
        result = evaluate(internal_price_cents=internal_price_cents)
        if inspect.isawaitable(result):
            return cast(OracleState, await result)
        return cast(OracleState, result)

    check_stale = getattr(oracle, "check_stale", None)
    if callable(check_stale):
        if check_stale():
            return OracleState.STALE
        check_lvr = getattr(oracle, "check_lvr", None)
        if callable(check_lvr) and check_lvr():
            return OracleState.LVR
        deviation = oracle.check_deviation(internal_price_cents)
    else:
        if oracle.check_lag(threshold_seconds=ctx.oracle_lag_threshold):
            return OracleState.STALE
        deviation = oracle.check_deviation(
            internal_price_cents,
            threshold=ctx.oracle_deviation_threshold,
        )

    if inspect.isawaitable(deviation):
        deviation = await deviation
    if deviation:
        return OracleState.DEVIATION

    return OracleState.NORMAL


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

    # Step 2.5: Update session P&L — must happen before risk evaluation
    ctx.session_pnl_cents = ctx.inventory.total_value_cents(mid) - ctx.initial_inventory_value_cents

    # Step 3: Risk — oracle check then defense evaluation
    oracle_defense = DefenseLevel.NORMAL
    if oracle is not None and ctx.config.oracle_slug:
        oracle_state = await _evaluate_oracle_state(
            oracle,
            ctx,
            internal_price_cents=float(mid),
        )
        oracle_defense = oracle_state.defense_level
        if oracle_state != OracleState.NORMAL:
            logger.warning(
                "Oracle state %s for %s (internal mid=%.1f)",
                oracle_state, ctx.market_id, mid,
            )

    # Fetch live market status with TTL cache — avoids a REST call on every cycle
    now = time.monotonic()
    if now - ctx.market_status_checked_at >= _MARKET_STATUS_TTL:
        try:
            status = await api.get_market_status(ctx.market_id)
            ctx.last_known_market_active = status in {"active", "open"}
            ctx.market_status_checked_at = now
        except Exception:
            logger.warning(
                "Market status fetch failed for %s — using last-known active=%s",
                ctx.market_id, ctx.last_known_market_active,
            )
    market_is_active = ctx.last_known_market_active

    risk_defense = risk.evaluate(
        inventory_skew=ctx.inventory.inventory_skew,
        daily_pnl=ctx.session_pnl_cents,
        market_active=market_is_active,
    )
    defense = max(risk_defense, oracle_defense, key=lambda d: _DEFENSE_SEVERITY[d])
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
    cycle_services = dict(services)
    cycle_services.setdefault("oracle", oracle)
    while not ctx.shutdown_requested:
        try:
            await quote_cycle(ctx, **cycle_services)
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
    market_task_handles: list[asyncio.Task] = []
    background_tasks: list[asyncio.Task] = []
    _shutdown_event = asyncio.Event()

    _signal_handler = _build_signal_handler(
        contexts=contexts,
        market_task_handles=market_task_handles,
        background_tasks=background_tasks,
        shutdown_event=_shutdown_event,
    )

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    # Build per-market oracles — each market gets its own oracle instance
    # so different oracle_slug configs don't pollute each other.
    market_oracles: dict[str, PolymarketOracle | None] = {}
    for mid, ctx in contexts.items():
        if ctx.config.oracle_slug:
            market_oracle = PolymarketOracle(ctx.config)
            logger.info("Oracle enabled for %s: slug=%s", mid, ctx.config.oracle_slug)
            try:
                await market_oracle.refresh()
            except Exception as e:
                logger.warning("Oracle warm-up failed for %s: %s", mid, e)
            market_oracles[mid] = market_oracle
        else:
            market_oracles[mid] = None

    # Load oracle config (interval + thresholds) from global config file (optional)
    _project_root = Path(__file__).resolve().parent.parent.parent
    _oracle_cfg_path = _project_root / "config" / "oracle.yaml"
    _oracle_interval = 30.0
    _oracle_lag_threshold = 10.0
    _oracle_deviation_threshold = 20.0
    if _oracle_cfg_path.exists():
        with open(_oracle_cfg_path) as _f:
            _oracle_data = yaml.safe_load(_f).get("oracle", {})
        _oracle_interval = _oracle_data.get("update_interval_seconds", 30.0)
        _oracle_lag_threshold = _oracle_data.get("lag_threshold_seconds", 10.0)
        _oracle_deviation_threshold = _oracle_data.get("deviation_threshold_cents", 20.0)
    # Apply thresholds to contexts that have an oracle enabled
    for mid, ctx in contexts.items():
        if market_oracles.get(mid) is not None:
            ctx.oracle_lag_threshold = _oracle_lag_threshold
            ctx.oracle_deviation_threshold = _oracle_deviation_threshold

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
        market_oracle = market_oracles[ctx.market_id]

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
            "phase_mgr": phase_mgr,
        }
        tasks.append(asyncio.create_task(
            run_market_with_health(ctx, services, health_state, oracle=market_oracle),
            name=f"market-{ctx.market_id}",
        ))
    market_task_handles.extend(tasks)

    # Background tasks: reconciler + per-market oracle refresh loops + health server
    reconciler = AMMReconciler(api=api, inventory_cache=inventory_cache)
    background_tasks.append(asyncio.create_task(
        reconcile_loop(reconciler, contexts, 300.0),
        name="reconcile-loop",
    ))
    for mid, market_oracle in market_oracles.items():
        if market_oracle is not None:
            background_tasks.append(asyncio.create_task(
                _oracle_refresh_loop(market_oracle, _oracle_interval, {mid: contexts[mid]}),
                name=f"oracle-refresh-{mid}",
            ))
    background_tasks.append(asyncio.create_task(
        run_health_server(health_state),
        name="health-server",
    ))

    # FIX 6: Guard background tasks — log failures and trigger shutdown
    def _bg_task_guard(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error("Background task %s died: %s", task.get_name(), exc)
            for _ctx in contexts.values():
                _ctx.shutdown_requested = True

    for task in background_tasks:
        task.add_done_callback(_bg_task_guard)

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error("Market task %d failed: %s", i, result, exc_info=result)
    finally:
        health_state.ready = False
        if not _shutdown_event.is_set():
            for ctx in contexts.values():
                ctx.shutdown_requested = True
            for task in background_tasks:
                if not task.done():
                    task.cancel()
        await _wait_for_task_shutdown(tasks + background_tasks, SHUTDOWN_TIMEOUT_SECONDS)
        await shutdown.execute(contexts)
        await http_client.aclose()
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(amm_main())
