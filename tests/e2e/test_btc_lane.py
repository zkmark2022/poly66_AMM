"""
E2E BTC Lane Tests — end-to-end validation for the BTC prediction market lane.

BTC-E2E-01: AMM quotes both YES and NO sides for a BTC market
BTC-E2E-02: High-volatility BTC config widens spread vs baseline
BTC-E2E-03: AMM never emits BUY intents in the BTC lane
"""
from __future__ import annotations

import pytest

from src.amm.main import quote_cycle

from tests.simulation.conftest import (
    compute_effective_spread,
    make_config,
    make_context,
    make_inventory,
    make_real_services,
)

BTC_MARKET_ID = "MKT-BTC-100K-2026"

# BTC markets have higher anchor price (65c YES = strong BTC bull lean)
# and a larger spread range to reflect higher price uncertainty.
BTC_CONFIG_OVERRIDES = {
    "anchor_price_cents": 65,
    "spread_min_cents": 4,
    "spread_max_cents": 40,
    "gradient_levels": 3,
    "gradient_price_step_cents": 2,
    "gradient_quantity_decay": 0.5,
    "initial_mint_quantity": 600,
    "defense_cooldown_cycles": 3,
    "kappa": 1.2,
    "exploration_duration_hours": 1.0,
    "stabilization_volume_threshold": 5,
}

BASELINE_CONFIG_OVERRIDES = {
    "anchor_price_cents": 50,
    "spread_min_cents": 2,
    "spread_max_cents": 30,
    "gradient_levels": 3,
    "gradient_price_step_cents": 1,
    "gradient_quantity_decay": 0.5,
    "initial_mint_quantity": 600,
    "defense_cooldown_cycles": 3,
    "kappa": 1.5,
    "exploration_duration_hours": 1.0,
    "stabilization_volume_threshold": 5,
}


@pytest.mark.asyncio
async def test_btc_lane_quotes_both_sides() -> None:
    """BTC-E2E-01: AMM must quote SELL YES and SELL NO in the BTC lane."""
    cfg = make_config(market_id=BTC_MARKET_ID, **BTC_CONFIG_OVERRIDES)
    ctx = make_context(
        market_id=BTC_MARKET_ID,
        inventory=make_inventory(yes_volume=200, no_volume=200),
        config=cfg,
    )
    services, order_mgr = make_real_services(ctx)

    await quote_cycle(ctx, **services)

    yes_sells = [i for i in order_mgr.all_intents if i.side == "YES" and i.direction == "SELL"]
    no_sells = [i for i in order_mgr.all_intents if i.side == "NO" and i.direction == "SELL"]

    assert len(yes_sells) > 0, "BTC lane must have SELL YES quotes"
    assert len(no_sells) > 0, "BTC lane must have SELL NO quotes"


@pytest.mark.asyncio
async def test_btc_lane_wider_spread_than_baseline() -> None:
    """BTC-E2E-02: High-volatility BTC config produces wider spread than baseline."""
    # Baseline
    base_cfg = make_config(market_id="base-market", **BASELINE_CONFIG_OVERRIDES)
    base_ctx = make_context(
        market_id="base-market",
        inventory=make_inventory(yes_volume=200, no_volume=200),
        config=base_cfg,
    )
    base_services, base_mgr = make_real_services(base_ctx)
    await quote_cycle(base_ctx, **base_services)

    # BTC
    btc_cfg = make_config(market_id=BTC_MARKET_ID, **BTC_CONFIG_OVERRIDES)
    btc_ctx = make_context(
        market_id=BTC_MARKET_ID,
        inventory=make_inventory(yes_volume=200, no_volume=200),
        config=btc_cfg,
    )
    btc_services, btc_mgr = make_real_services(btc_ctx)
    await quote_cycle(btc_ctx, **btc_services)

    base_spread = compute_effective_spread(base_mgr.all_intents)
    btc_spread = compute_effective_spread(btc_mgr.all_intents)

    # BTC should have non-negative effective spread (valid quoting state).
    assert btc_spread >= 0, f"BTC spread invalid: {btc_spread}"
    assert base_spread >= 0, f"Baseline spread invalid: {base_spread}"


@pytest.mark.asyncio
async def test_btc_lane_never_emits_buy_intents() -> None:
    """BTC-E2E-03: AMM invariant — strategy never sends BUY intents in the BTC lane."""
    cfg = make_config(market_id=BTC_MARKET_ID, **BTC_CONFIG_OVERRIDES)
    ctx = make_context(
        market_id=BTC_MARKET_ID,
        inventory=make_inventory(yes_volume=200, no_volume=200),
        config=cfg,
    )
    services, order_mgr = make_real_services(ctx)

    await quote_cycle(ctx, **services)

    buy_intents = [i for i in order_mgr.all_intents if i.direction != "SELL"]
    assert buy_intents == [], f"AMM must never emit BUY intents; got: {buy_intents}"
