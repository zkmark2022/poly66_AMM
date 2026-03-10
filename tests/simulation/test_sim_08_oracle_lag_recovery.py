from __future__ import annotations

import pytest

from src.amm.main import quote_cycle
from src.amm.models.enums import DefenseLevel
from src.amm.oracle.polymarket_oracle import OracleState

from tests.simulation.conftest import (
    compute_effective_spread,
    make_config,
    make_context,
    make_inventory,
    make_real_services,
)


class ToggleOracle:
    def __init__(self) -> None:
        self.state = OracleState.NORMAL

    def evaluate(self, internal_price_cents: float) -> OracleState:
        return self.state


@pytest.mark.asyncio
async def test_oracle_stale_enters_one_side_and_recovers_to_normal_spread() -> None:
    config = make_config(
        oracle_slug="test-oracle", defense_cooldown_cycles=2,
        gamma_tier="MATURE", kappa=10.0,
    )
    ctx = make_context(
        inventory=make_inventory(yes_volume=258, no_volume=142),
        config=config,
    )
    services, order_mgr = make_real_services(ctx)
    oracle = ToggleOracle()

    await quote_cycle(ctx, **services, oracle=oracle)
    normal_spread = compute_effective_spread(order_mgr.all_intents)
    assert ctx.defense_level == DefenseLevel.NORMAL

    order_mgr.captured.clear()
    oracle.state = OracleState.STALE
    await quote_cycle(ctx, **services, oracle=oracle)
    stale_yes = [i for i in order_mgr.all_intents if i.side == "YES"]
    stale_no = [i for i in order_mgr.all_intents if i.side == "NO"]

    # Oracle lag should force passive one-sided quoting on the heavy side.
    assert ctx.defense_level == DefenseLevel.ONE_SIDE
    assert len(stale_yes) == 3
    assert stale_no == []

    order_mgr.captured.clear()
    oracle.state = OracleState.NORMAL
    await quote_cycle(ctx, **services, oracle=oracle)
    recovered_spread = compute_effective_spread(order_mgr.all_intents)
    recovered_no = [i for i in order_mgr.all_intents if i.side == "NO"]
    # Once the oracle is healthy again, bilateral quoting and the original spread should return immediately.
    assert ctx.defense_level == DefenseLevel.NORMAL
    assert len(recovered_no) == 3
    assert recovered_spread == normal_spread


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("oracle_state", "expected_level"),
    [
        (OracleState.STALE, DefenseLevel.ONE_SIDE),
        (OracleState.DEVIATION, DefenseLevel.KILL_SWITCH),
    ],
)
async def test_oracle_states_map_to_expected_defense_levels(
    oracle_state: OracleState,
    expected_level: DefenseLevel,
) -> None:
    config = make_config(oracle_slug="test-oracle")
    ctx = make_context(
        inventory=make_inventory(yes_volume=258, no_volume=142),
        config=config,
    )
    services, _ = make_real_services(ctx)
    oracle = ToggleOracle()
    oracle.state = oracle_state

    await quote_cycle(ctx, **services, oracle=oracle)

    # Oracle state translation is the contract that drives downstream defense behavior.
    assert ctx.defense_level == expected_level
