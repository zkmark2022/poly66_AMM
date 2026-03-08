from __future__ import annotations

import pytest

from src.amm.main import quote_cycle
from src.amm.models.enums import DefenseLevel

from tests.simulation.conftest import (
    make_config,
    make_context,
    make_inventory,
    make_real_services,
)


class FlakyOracle:
    def __init__(self, fail_cycles: int) -> None:
        self.fail_cycles = fail_cycles
        self.calls = 0

    def evaluate(self, internal_price_cents: float):
        from src.amm.oracle.polymarket_oracle import OracleState

        self.calls += 1
        if self.calls <= self.fail_cycles:
            return OracleState.STALE
        return OracleState.NORMAL


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_cycles", [1, 3, 5])
async def test_oracle_recovers_from_repeated_stale_cycles_without_locking_one_side(
    fail_cycles: int,
) -> None:
    config = make_config(oracle_slug="test-oracle", defense_cooldown_cycles=2)
    ctx = make_context(
        inventory=make_inventory(yes_volume=258, no_volume=142),
        config=config,
    )
    services, order_mgr = make_real_services(ctx)
    oracle = FlakyOracle(fail_cycles=fail_cycles)

    for _ in range(fail_cycles):
        await quote_cycle(ctx, **services, oracle=oracle)

    stale_no_orders = [i for i in order_mgr.all_intents if i.side == "NO"]
    # During STALE + long-YES inventory, ONE_SIDE must suppress NO quotes.
    assert ctx.defense_level == DefenseLevel.ONE_SIDE
    assert stale_no_orders == []

    order_mgr.captured.clear()
    await quote_cycle(ctx, **services, oracle=oracle)
    yes_sells = [i for i in order_mgr.all_intents if i.side == "YES"]
    no_sells = [i for i in order_mgr.all_intents if i.side == "NO"]
    # Oracle recovery should immediately restore bilateral quoting on the next healthy cycle.
    assert ctx.defense_level == DefenseLevel.NORMAL
    assert len(yes_sells) == 3
    assert len(no_sells) == 3


@pytest.mark.asyncio
async def test_oracle_recovery_returns_to_normal_on_first_healthy_cycle() -> None:
    config = make_config(oracle_slug="test-oracle", defense_cooldown_cycles=3)
    ctx = make_context(
        inventory=make_inventory(yes_volume=258, no_volume=142),
        config=config,
    )
    services, order_mgr = make_real_services(ctx)
    oracle = FlakyOracle(fail_cycles=1)

    await quote_cycle(ctx, **services, oracle=oracle)
    assert ctx.defense_level == DefenseLevel.ONE_SIDE

    order_mgr.captured.clear()
    await quote_cycle(ctx, **services, oracle=oracle)
    yes_sells = [i for i in order_mgr.all_intents if i.side == "YES"]
    no_sells = [i for i in order_mgr.all_intents if i.side == "NO"]
    # Oracle defense is stateless across healthy cycles: recovery happens immediately, not via cooldown.
    assert ctx.defense_level == DefenseLevel.NORMAL
    assert len(yes_sells) == 3
    assert len(no_sells) == 3
