# API Completion Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Complete all remaining MVP API endpoints and missing infrastructure: fee collection, trade persistence, ORDER_FREEZE ledger, positions API, trades API, and admin APIs.

**Architecture:** Each task is a focused vertical slice (migration → service → API → tests). Clearing scenarios are modified to return `(buy_pnl, sell_pnl)` tuples; the engine coordinates fee collection and trade persistence after each fill. Admin APIs are added in a new `pm_admin` module.

**Tech Stack:** FastAPI, SQLAlchemy async, PostgreSQL, pytest-asyncio, uv

---

## Deviation Table

| Task | Deviation | Reason |
|------|-----------|--------|
| Task 2 | `check_and_freeze` keeps TAKER_FEE_BPS=20 for pre-freeze; only MarketState adds taker_fee_bps for actual fee | Avoids restructuring risk-check phase; seed data has taker_fee_bps=20 so values match |

---

## Task 1: Migration 013 — trades UUID→VARCHAR(64)

**Context:** Migration 006 defined `buy_order_id UUID`, `sell_order_id UUID`, `maker_order_id UUID`, `taker_order_id UUID` in the trades table. But orders.id was changed to VARCHAR(64) in migration 012. Any INSERT into trades will fail with type mismatch.

**Files:**
- Create: `alembic/versions/013_alter_trades_order_ids_to_varchar.py`

**Step 1: Create the migration file**

```python
"""013: alter trades order ID columns from UUID to VARCHAR(64)

orders.id was changed to VARCHAR(64) in migration 012.
Trades columns referencing order IDs must match.

Revision ID: 013
Revises: 012
Create Date: 2026-02-21
"""
from typing import Sequence, Union
from alembic import op

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for col in ("buy_order_id", "sell_order_id", "maker_order_id", "taker_order_id"):
        op.execute(f"ALTER TABLE trades ALTER COLUMN {col} TYPE VARCHAR(64) USING {col}::TEXT;")


def downgrade() -> None:
    for col in ("buy_order_id", "sell_order_id", "maker_order_id", "taker_order_id"):
        op.execute(f"ALTER TABLE trades ALTER COLUMN {col} TYPE UUID USING {col}::UUID;")
```

**Step 2: Apply and verify**

```bash
uv run alembic upgrade head
```
Expected: `Running upgrade 012 -> 013, alter trades order ID columns from UUID to VARCHAR(64)`

Verify column types:
```bash
docker exec pm-postgres psql -U pm_user -d predict_market -c \
  "\d trades" | grep order_id
```
Expected: all four `*_order_id` columns show `character varying(64)`.

**Step 3: Commit**

```bash
git add alembic/versions/013_alter_trades_order_ids_to_varchar.py
git commit -m "feat(db): migration 013 — trades order ID columns UUID→VARCHAR(64)"
```

---

## Task 2: Add taker_fee_bps to MarketState

**Context:** `MarketState` in `engine.py` is populated from `_GET_MARKET_SQL`. The `taker_fee_bps` column exists in the `markets` table (seeded as 20) but is not read. We need it to calculate actual fees after each trade.

**Files:**
- Modify: `src/pm_matching/engine/engine.py:29-54`

**Step 1: Write failing test**

Create `tests/unit/test_market_state.py`:

```python
# tests/unit/test_market_state.py
"""Unit tests for MarketState construction."""
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.pm_matching.engine.engine import MarketState


def _make_row(**kwargs: Any) -> MagicMock:
    row = MagicMock()
    row.id = kwargs.get("id", "mkt-1")
    row.status = kwargs.get("status", "ACTIVE")
    row.reserve_balance = kwargs.get("reserve_balance", 0)
    row.pnl_pool = kwargs.get("pnl_pool", 0)
    row.total_yes_shares = kwargs.get("total_yes_shares", 0)
    row.total_no_shares = kwargs.get("total_no_shares", 0)
    row.taker_fee_bps = kwargs.get("taker_fee_bps", 20)
    return row


def test_market_state_reads_taker_fee_bps() -> None:
    row = _make_row(taker_fee_bps=15)
    ms = MarketState(row)
    assert ms.taker_fee_bps == 15


def test_market_state_default_fields() -> None:
    row = _make_row()
    ms = MarketState(row)
    assert ms.id == "mkt-1"
    assert ms.status == "ACTIVE"
    assert ms.taker_fee_bps == 20
```

**Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/test_market_state.py -v
```
Expected: `FAILED` — `MarketState has no attribute taker_fee_bps`

**Step 3: Implement — modify engine.py**

In `src/pm_matching/engine/engine.py`, change `_GET_MARKET_SQL` and `MarketState`:

```python
_GET_MARKET_SQL = text("""
    SELECT id, status, reserve_balance, pnl_pool,
           total_yes_shares, total_no_shares,
           taker_fee_bps
    FROM markets WHERE id = :market_id FOR UPDATE
""")


class MarketState:
    """In-memory view of market row; mutated during clearing, flushed at end."""

    def __init__(self, row: Any) -> None:
        self.id: str = row.id
        self.status: str = row.status
        self.reserve_balance: int = row.reserve_balance
        self.pnl_pool: int = row.pnl_pool
        self.total_yes_shares: int = row.total_yes_shares
        self.total_no_shares: int = row.total_no_shares
        self.taker_fee_bps: int = row.taker_fee_bps
```

**Step 4: Run tests**

```bash
uv run pytest tests/unit/test_market_state.py -v
```
Expected: `2 passed`

Run full suite to confirm no regressions:
```bash
uv run pytest tests/unit/ -q
```
Expected: all pass.

**Step 5: Commit**

```bash
git add src/pm_matching/engine/engine.py tests/unit/test_market_state.py
git commit -m "feat(engine): add taker_fee_bps to MarketState"
```

---

## Task 3: Clearing scenarios return (buy_pnl, sell_pnl)

**Context:** The four clearing scenarios compute `cost_released` and `proceeds` internally but return `None`. To write these values to the `trades` table we need them propagated back to the engine. Each scenario returns `tuple[int | None, int | None]` — `(buy_realized_pnl, sell_realized_pnl)`.

**PnL rules:**
- **MINT**: both sides open → `(None, None)`
- **TRANSFER_YES**: buyer opens, seller (NATIVE_SELL) closes → `(None, proceeds - cost_released)`
- **TRANSFER_NO**: sell_user opens (SYNTHETIC_SELL), buy_user closes (SYNTHETIC_BUY) → `(no_price*qty - cost_released, None)`
- **BURN**: both close → `(no_proceeds - no_cost_rel, yes_proceeds - yes_cost_rel)`

**Files:**
- Modify: `src/pm_clearing/domain/scenarios/mint.py`
- Modify: `src/pm_clearing/domain/scenarios/transfer_yes.py`
- Modify: `src/pm_clearing/domain/scenarios/transfer_no.py`
- Modify: `src/pm_clearing/domain/scenarios/burn.py`
- Modify: `src/pm_clearing/domain/service.py`

**Step 1: Write failing tests**

Create `tests/unit/test_clearing_pnl.py`:

```python
# tests/unit/test_clearing_pnl.py
"""Unit tests — clearing scenarios return (buy_pnl, sell_pnl)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.pm_matching.domain.models import TradeResult


def _make_trade(**kwargs: int | str) -> TradeResult:
    return TradeResult(
        buy_order_id=str(kwargs.get("buy_order_id", "b1")),
        sell_order_id=str(kwargs.get("sell_order_id", "s1")),
        buy_user_id=str(kwargs.get("buy_user_id", "user-b")),
        sell_user_id=str(kwargs.get("sell_user_id", "user-s")),
        market_id=str(kwargs.get("market_id", "mkt-1")),
        price=int(kwargs.get("price", 60)),
        quantity=int(kwargs.get("quantity", 10)),
        buy_book_type=str(kwargs.get("buy_book_type", "NATIVE_BUY")),
        sell_book_type=str(kwargs.get("sell_book_type", "NATIVE_SELL")),
        buy_original_price=int(kwargs.get("buy_original_price", 60)),
        maker_order_id=str(kwargs.get("maker_order_id", "s1")),
        taker_order_id=str(kwargs.get("taker_order_id", "b1")),
    )


@pytest.mark.asyncio
async def test_mint_returns_none_pnl() -> None:
    from src.pm_clearing.domain.scenarios.mint import clear_mint
    trade = _make_trade(buy_book_type="NATIVE_BUY", sell_book_type="SYNTHETIC_SELL")
    market = MagicMock()
    db = AsyncMock()
    result = await clear_mint(trade, market, db)
    assert result == (None, None)


@pytest.mark.asyncio
async def test_transfer_yes_returns_sell_pnl() -> None:
    from src.pm_clearing.domain.scenarios.transfer_yes import clear_transfer_yes
    trade = _make_trade(price=60, quantity=10)
    market = MagicMock()
    db = AsyncMock()
    # mock: seller has yes_volume=10, yes_cost_sum=500 (avg cost 50), pending=10
    row_mock = MagicMock()
    row_mock.fetchone.return_value = (10, 500, 10)  # yes_vol, yes_cost, pending
    db.execute.return_value = row_mock
    buy_pnl, sell_pnl = await clear_transfer_yes(trade, market, db)
    assert buy_pnl is None
    proceeds = 60 * 10  # 600
    cost_released = 500  # full close
    assert sell_pnl == proceeds - cost_released  # 100


@pytest.mark.asyncio
async def test_transfer_no_returns_buy_pnl() -> None:
    from src.pm_clearing.domain.scenarios.transfer_no import clear_transfer_no
    # buy_user = SYNTHETIC_BUY (sell NO), closing position
    trade = _make_trade(price=60, quantity=10,
                        buy_book_type="SYNTHETIC_BUY", sell_book_type="SYNTHETIC_SELL")
    market = MagicMock()
    db = AsyncMock()
    # mock: buyer NO position: no_volume=10, no_cost_sum=400, pending=10
    row_mock = MagicMock()
    row_mock.fetchone.return_value = (10, 400, 10)
    db.execute.return_value = row_mock
    buy_pnl, sell_pnl = await clear_transfer_no(trade, market, db)
    assert sell_pnl is None
    no_price = 100 - 60  # 40
    proceeds = no_price * 10  # 400
    cost_released = 400  # full close
    assert buy_pnl == proceeds - cost_released  # 0


@pytest.mark.asyncio
async def test_burn_returns_both_pnl() -> None:
    from src.pm_clearing.domain.scenarios.burn import clear_burn
    trade = _make_trade(price=70, quantity=5,
                        buy_book_type="SYNTHETIC_BUY", sell_book_type="NATIVE_SELL")
    market = MagicMock()
    db = AsyncMock()
    yes_row = MagicMock()
    yes_row.fetchone.return_value = (5, 250, 5)  # yes_vol=5, yes_cost=250, pending=5
    no_row = MagicMock()
    no_row.fetchone.return_value = (5, 150, 5)   # no_vol=5, no_cost=150, pending=5
    db.execute.side_effect = [yes_row, no_row,
                               AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()]
    buy_pnl, sell_pnl = await clear_burn(trade, market, db)
    yes_proceeds = 70 * 5     # 350
    yes_cost_rel = 250        # full close
    no_proceeds = 30 * 5      # 150
    no_cost_rel = 150         # full close
    assert sell_pnl == yes_proceeds - yes_cost_rel   # 100
    assert buy_pnl == no_proceeds - no_cost_rel      # 0
```

**Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/test_clearing_pnl.py -v
```
Expected: `FAILED` — functions return `None` instead of tuples.

**Step 3: Implement**

**`src/pm_clearing/domain/scenarios/mint.py`** — change return type:

```python
async def clear_mint(
    trade: TradeResult, market: object, db: AsyncSession
) -> tuple[int | None, int | None]:
    """MINT: NATIVE_BUY + SYNTHETIC_SELL — create YES/NO contract pair."""
    # ... (all existing SQL executes unchanged) ...
    market.reserve_balance += trade.quantity * 100  # type: ignore[attr-defined]
    market.total_yes_shares += trade.quantity  # type: ignore[attr-defined]
    market.total_no_shares += trade.quantity  # type: ignore[attr-defined]
    return (None, None)  # opening positions — no realized PnL
```

**`src/pm_clearing/domain/scenarios/transfer_yes.py`** — return `(None, sell_pnl)`:

```python
async def clear_transfer_yes(
    trade: TradeResult, market: object, db: AsyncSession
) -> tuple[int | None, int | None]:
    # ... (all existing code unchanged until the pnl_pool line) ...
    market.pnl_pool -= proceeds - cost_released  # type: ignore[attr-defined]
    sell_realized_pnl = proceeds - cost_released
    return (None, sell_realized_pnl)
```

**`src/pm_clearing/domain/scenarios/transfer_no.py`** — return `(buy_pnl, None)`:

```python
async def clear_transfer_no(
    trade: TradeResult, market: object, db: AsyncSession
) -> tuple[int | None, int | None]:
    # ... (all existing code unchanged until the pnl_pool line) ...
    market.pnl_pool -= proceeds - cost_released  # type: ignore[attr-defined]
    buy_realized_pnl = proceeds - cost_released
    return (buy_realized_pnl, None)
```

**`src/pm_clearing/domain/scenarios/burn.py`** — return `(buy_pnl, sell_pnl)`:

```python
async def clear_burn(
    trade: TradeResult, market: object, db: AsyncSession
) -> tuple[int | None, int | None]:
    # ... (all existing code unchanged until final pnl_pool lines) ...
    market.pnl_pool -= yes_proceeds - yes_cost_rel  # type: ignore[attr-defined]
    market.pnl_pool -= no_proceeds - no_cost_rel    # type: ignore[attr-defined]
    sell_realized_pnl = yes_proceeds - yes_cost_rel  # NATIVE_SELL closes YES
    buy_realized_pnl = no_proceeds - no_cost_rel     # SYNTHETIC_BUY closes NO
    return (buy_realized_pnl, sell_realized_pnl)
```

**`src/pm_clearing/domain/service.py`** — propagate return value:

```python
async def settle_trade(
    trade: TradeResult,
    market: object,
    db: AsyncSession,
    fee_bps: int,
) -> tuple[int | None, int | None]:
    """Determine scenario, dispatch, and return (buy_pnl, sell_pnl)."""
    scenario = determine_scenario(trade.buy_book_type, trade.sell_book_type)
    if scenario == TradeScenario.MINT:
        return await clear_mint(trade, market, db)
    elif scenario == TradeScenario.TRANSFER_YES:
        return await clear_transfer_yes(trade, market, db)
    elif scenario == TradeScenario.TRANSFER_NO:
        return await clear_transfer_no(trade, market, db)
    else:
        return await clear_burn(trade, market, db)
```

**Step 4: Run tests**

```bash
uv run pytest tests/unit/test_clearing_pnl.py -v
```
Expected: `4 passed`

```bash
uv run pytest tests/unit/ -q
```
Expected: all pass.

**Step 5: Commit**

```bash
git add src/pm_clearing/domain/scenarios/ src/pm_clearing/domain/service.py \
        tests/unit/test_clearing_pnl.py
git commit -m "feat(clearing): return realized pnl from clearing scenarios"
```

---

## Task 4: Fee Collection + Trade Persistence

**Context:** After each fill, the engine must: (1) collect the taker fee from frozen funds or proceeds and credit PLATFORM_FEE account; (2) write a row to the `trades` table. This connects the pre-frozen fee buffer (set by `check_and_freeze`) with the actual fee charged per market config.

**Fee mechanism:**
- Taker with FUNDS order (NATIVE_BUY or SYNTHETIC_SELL): frozen buffer = max_fee(20 bps). After fill, `frozen` still holds this buffer. Collect `actual_fee` from frozen, return `(max_fee - actual_fee)` to available.
- Taker with SHARES order (NATIVE_SELL or SYNTHETIC_BUY): receives `proceeds` credited to available. Collect `actual_fee` by debiting available.

**Files:**
- Create: `src/pm_clearing/infrastructure/fee_collector.py`
- Create: `src/pm_clearing/infrastructure/trades_writer.py`
- Modify: `src/pm_matching/engine/engine.py` (import + call both)

**Step 1: Write failing tests**

Create `tests/unit/test_fee_collector.py`:

```python
# tests/unit/test_fee_collector.py
"""Unit tests for fee collection helpers."""
from unittest.mock import AsyncMock, MagicMock, call

import pytest


@pytest.mark.asyncio
async def test_collect_fee_from_frozen_executes_two_updates() -> None:
    from src.pm_clearing.infrastructure.fee_collector import collect_fee_from_frozen
    db = AsyncMock()
    await collect_fee_from_frozen("user-1", actual_fee=10, max_fee=20, db=db)
    # Expect 2 SQL executions: deduct from frozen + credit PLATFORM_FEE
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_collect_fee_from_proceeds_executes_two_updates() -> None:
    from src.pm_clearing.infrastructure.fee_collector import collect_fee_from_proceeds
    db = AsyncMock()
    await collect_fee_from_proceeds("user-1", actual_fee=10, db=db)
    # Expect 2 SQL executions: deduct from available + credit PLATFORM_FEE
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_write_trade_executes_insert() -> None:
    from src.pm_clearing.infrastructure.trades_writer import write_trade
    from src.pm_matching.domain.models import TradeResult
    db = AsyncMock()
    trade = TradeResult(
        buy_order_id="b1", sell_order_id="s1",
        buy_user_id="user-b", sell_user_id="user-s",
        market_id="mkt-1", price=60, quantity=10,
        buy_book_type="NATIVE_BUY", sell_book_type="NATIVE_SELL",
        buy_original_price=60,
        maker_order_id="s1", taker_order_id="b1",
    )
    await write_trade(trade, scenario="TRANSFER_YES",
                      maker_fee=0, taker_fee=12,
                      buy_pnl=None, sell_pnl=100, db=db)
    db.execute.assert_awaited_once()
```

**Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/test_fee_collector.py -v
```
Expected: `FAILED` — modules not found.

**Step 3: Create fee_collector.py**

```python
# src/pm_clearing/infrastructure/fee_collector.py
"""Taker fee collection — debit taker, credit PLATFORM_FEE account."""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PLATFORM_FEE_USER_ID = "PLATFORM_FEE"

_DEDUCT_FROZEN_SQL = text("""
    UPDATE accounts
    SET frozen_balance    = frozen_balance    - :actual_fee,
        available_balance = available_balance + :refund,
        version = version + 1, updated_at = NOW()
    WHERE user_id = :user_id
""")

_DEDUCT_AVAILABLE_SQL = text("""
    UPDATE accounts
    SET available_balance = available_balance - :actual_fee,
        version = version + 1, updated_at = NOW()
    WHERE user_id = :user_id
""")

_CREDIT_PLATFORM_SQL = text("""
    UPDATE accounts
    SET available_balance = available_balance + :amount,
        version = version + 1, updated_at = NOW()
    WHERE user_id = :user_id
""")


async def collect_fee_from_frozen(
    taker_user_id: str,
    actual_fee: int,
    max_fee: int,
    db: AsyncSession,
) -> None:
    """Collect fee from pre-frozen funds buffer (NATIVE_BUY or SYNTHETIC_SELL taker)."""
    refund = max_fee - actual_fee
    await db.execute(
        _DEDUCT_FROZEN_SQL,
        {"user_id": taker_user_id, "actual_fee": actual_fee, "refund": refund},
    )
    await db.execute(
        _CREDIT_PLATFORM_SQL,
        {"user_id": PLATFORM_FEE_USER_ID, "amount": actual_fee},
    )


async def collect_fee_from_proceeds(
    taker_user_id: str,
    actual_fee: int,
    db: AsyncSession,
) -> None:
    """Collect fee from proceeds (NATIVE_SELL or SYNTHETIC_BUY taker)."""
    await db.execute(
        _DEDUCT_AVAILABLE_SQL,
        {"user_id": taker_user_id, "actual_fee": actual_fee},
    )
    await db.execute(
        _CREDIT_PLATFORM_SQL,
        {"user_id": PLATFORM_FEE_USER_ID, "amount": actual_fee},
    )
```

**Create trades_writer.py:**

```python
# src/pm_clearing/infrastructure/trades_writer.py
"""Persist a single trade row to the trades table."""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_common.datetime_utils import utc_now
from src.pm_common.id_generator import generate_id
from src.pm_matching.domain.models import TradeResult

_INSERT_TRADE_SQL = text("""
    INSERT INTO trades (
        trade_id, market_id, scenario,
        buy_order_id, sell_order_id,
        buy_user_id, sell_user_id,
        buy_book_type, sell_book_type,
        price, quantity,
        maker_order_id, taker_order_id,
        maker_fee, taker_fee,
        buy_realized_pnl, sell_realized_pnl,
        executed_at
    ) VALUES (
        :trade_id, :market_id, :scenario,
        :buy_order_id, :sell_order_id,
        :buy_user_id, :sell_user_id,
        :buy_book_type, :sell_book_type,
        :price, :quantity,
        :maker_order_id, :taker_order_id,
        :maker_fee, :taker_fee,
        :buy_realized_pnl, :sell_realized_pnl,
        :executed_at
    )
""")


async def write_trade(
    trade: TradeResult,
    scenario: str,
    maker_fee: int,
    taker_fee: int,
    buy_pnl: int | None,
    sell_pnl: int | None,
    db: AsyncSession,
) -> None:
    """Insert one row into the trades table."""
    await db.execute(
        _INSERT_TRADE_SQL,
        {
            "trade_id": generate_id(),
            "market_id": trade.market_id,
            "scenario": scenario,
            "buy_order_id": trade.buy_order_id,
            "sell_order_id": trade.sell_order_id,
            "buy_user_id": trade.buy_user_id,
            "sell_user_id": trade.sell_user_id,
            "buy_book_type": trade.buy_book_type,
            "sell_book_type": trade.sell_book_type,
            "price": trade.price,
            "quantity": trade.quantity,
            "maker_order_id": trade.maker_order_id,
            "taker_order_id": trade.taker_order_id,
            "maker_fee": maker_fee,
            "taker_fee": taker_fee,
            "buy_realized_pnl": buy_pnl,
            "sell_realized_pnl": sell_pnl,
            "executed_at": utc_now(),
        },
    )
```

**Modify engine.py `_place_order_inner`:**

Add imports at top of file:
```python
from src.pm_clearing.domain.fee import calc_fee, get_fee_trade_value
from src.pm_clearing.infrastructure.fee_collector import (
    collect_fee_from_frozen,
    collect_fee_from_proceeds,
)
from src.pm_clearing.infrastructure.trades_writer import write_trade
from src.pm_matching.engine.scenario import determine_scenario
```

In `_place_order_inner`, replace the trade loop:

```python
        # Clear each fill
        trades_db: list[TradeResult] = []
        netting_qty = 0
        for tr in trade_results:
            buy_pnl, sell_pnl = await settle_trade(tr, market, db, fee_bps=market.taker_fee_bps)
            _sync_frozen_amount(order, order.remaining_quantity)
            await repo.update_status(order, db)
            await _update_maker_status(tr, repo, db)

            # Fee collection
            taker_is_buyer = tr.taker_order_id == tr.buy_order_id
            taker_book_type = tr.buy_book_type if taker_is_buyer else tr.sell_book_type
            taker_user_id = tr.buy_user_id if taker_is_buyer else tr.sell_user_id
            fee_base = get_fee_trade_value(
                taker_book_type, tr.price, tr.quantity, tr.buy_original_price
            )
            actual_fee = calc_fee(fee_base, market.taker_fee_bps)
            from src.pm_risk.rules.balance_check import _calc_max_fee
            if taker_book_type in ("NATIVE_BUY", "SYNTHETIC_SELL"):
                max_fee = _calc_max_fee(fee_base)
                await collect_fee_from_frozen(taker_user_id, actual_fee, max_fee, db)
            else:
                await collect_fee_from_proceeds(taker_user_id, actual_fee, db)

            # Persist trade
            scenario = determine_scenario(tr.buy_book_type, tr.sell_book_type)
            await write_trade(tr, scenario.value, 0, actual_fee, buy_pnl, sell_pnl, db)

            # Netting
            nq = await execute_netting_if_needed(tr.buy_user_id, order.market_id, market, db)
            netting_qty += nq
            await write_wal_event(
                "ORDER_MATCHED", order.id, order.market_id, order.user_id,
                {"trade_qty": tr.quantity}, db,
            )
            trades_db.append(tr)
```

**Step 4: Run tests**

```bash
uv run pytest tests/unit/test_fee_collector.py -v
```
Expected: `3 passed`

```bash
uv run pytest tests/ -q
```
Expected: all pass.

**Step 5: Commit**

```bash
git add src/pm_clearing/infrastructure/fee_collector.py \
        src/pm_clearing/infrastructure/trades_writer.py \
        src/pm_matching/engine/engine.py \
        tests/unit/test_fee_collector.py
git commit -m "feat(engine): collect taker fee and persist trades after each fill"
```

---

## Task 5: ORDER_FREEZE / ORDER_UNFREEZE Ledger Entries

**Context:** Per the design doc, every freeze/unfreeze of funds must be recorded in `ledger_entries` with `entry_type = 'ORDER_FREEZE'` or `'ORDER_UNFREEZE'`. Currently these entries are missing — `check_and_freeze` and `_unfreeze_remainder` don't call `write_ledger`.

**Files:**
- Modify: `src/pm_risk/rules/balance_check.py`
- Modify: `src/pm_matching/engine/engine.py` (`_unfreeze_remainder`)

**Step 1: Write failing tests**

Create `tests/unit/test_ledger_entries.py`:

```python
# tests/unit/test_ledger_entries.py
"""Unit tests — ORDER_FREEZE and ORDER_UNFREEZE ledger entries are written."""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.pm_order.domain.models import Order


def _make_order(**kwargs: object) -> Order:
    return Order(
        id=str(kwargs.get("id", "ord-1")),
        client_order_id=str(kwargs.get("client_order_id", "c1")),
        market_id=str(kwargs.get("market_id", "mkt-1")),
        user_id=str(kwargs.get("user_id", "user-1")),
        original_side=str(kwargs.get("original_side", "YES")),
        original_direction=str(kwargs.get("original_direction", "BUY")),
        original_price=int(kwargs.get("original_price", 60)),
        book_type=str(kwargs.get("book_type", "NATIVE_BUY")),
        book_direction=str(kwargs.get("book_direction", "BUY")),
        book_price=int(kwargs.get("book_price", 60)),
        quantity=int(kwargs.get("quantity", 10)),
        frozen_amount=int(kwargs.get("frozen_amount", 612)),
        frozen_asset_type=str(kwargs.get("frozen_asset_type", "FUNDS")),
        time_in_force=str(kwargs.get("time_in_force", "GTC")),
        status=str(kwargs.get("status", "OPEN")),
    )


@pytest.mark.asyncio
async def test_check_and_freeze_writes_order_freeze_ledger() -> None:
    from src.pm_risk.rules.balance_check import check_and_freeze
    db = AsyncMock()
    # Mock successful freeze (returns a row)
    result_mock = MagicMock()
    result_mock.fetchone.return_value = MagicMock()  # row exists → freeze succeeded
    db.execute.return_value = result_mock
    order = _make_order()
    with patch("src.pm_risk.rules.balance_check.write_ledger") as mock_ledger:
        await check_and_freeze(order, db)
    mock_ledger.assert_awaited_once()
    call_kwargs = mock_ledger.call_args
    assert call_kwargs.kwargs["entry_type"] == "ORDER_FREEZE"


@pytest.mark.asyncio
async def test_unfreeze_remainder_writes_order_unfreeze_ledger() -> None:
    from src.pm_matching.engine.engine import MatchingEngine
    engine = MatchingEngine()
    db = AsyncMock()
    order = _make_order(frozen_asset_type="FUNDS", frozen_amount=612, remaining_quantity=10)
    with patch("src.pm_matching.engine.engine.write_ledger") as mock_ledger:
        await engine._unfreeze_remainder(order, db)
    mock_ledger.assert_awaited_once()
    call_kwargs = mock_ledger.call_args
    assert call_kwargs.kwargs["entry_type"] == "ORDER_UNFREEZE"
```

**Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/test_ledger_entries.py -v
```
Expected: `FAILED` — `write_ledger` is never called.

**Step 3: Implement**

**`src/pm_risk/rules/balance_check.py`** — add ledger write after successful freeze:

Add import:
```python
from src.pm_clearing.infrastructure.ledger import write_ledger
```

In `check_and_freeze`, after `order.frozen_amount = freeze_amount` (FUNDS branch):
```python
        order.frozen_amount = freeze_amount
        order.frozen_asset_type = "FUNDS"
        await write_ledger(
            user_id=order.user_id,
            entry_type="ORDER_FREEZE",
            amount=-freeze_amount,
            balance_after=0,  # exact balance unknown here; use 0 for MVP
            reference_type="ORDER",
            reference_id=order.id,
            db=db,
        )
```

For SHARES orders (NATIVE_SELL, SYNTHETIC_BUY) we don't freeze funds — no ledger entry needed (positions changes are tracked via position history, not ledger).

**`src/pm_matching/engine/engine.py`** — add ledger write in `_unfreeze_remainder`:

Add import at top:
```python
from src.pm_clearing.infrastructure.ledger import write_ledger
```

In `_unfreeze_remainder`, for the FUNDS branch, after the UPDATE:
```python
        if order.frozen_asset_type == "FUNDS":
            await db.execute(...)
            await write_ledger(
                user_id=order.user_id,
                entry_type="ORDER_UNFREEZE",
                amount=order.frozen_amount,
                balance_after=0,
                reference_type="ORDER",
                reference_id=order.id,
                db=db,
            )
```

**Step 4: Run tests**

```bash
uv run pytest tests/unit/test_ledger_entries.py -v
```
Expected: `2 passed`

```bash
uv run pytest tests/ -q
```
Expected: all pass.

**Step 5: Commit**

```bash
git add src/pm_risk/rules/balance_check.py src/pm_matching/engine/engine.py \
        tests/unit/test_ledger_entries.py
git commit -m "feat(ledger): add ORDER_FREEZE/UNFREEZE entries on fund freeze/unfreeze"
```

---

## Task 6: GET /positions + GET /positions/{market_id}

**Context:** Users need to view their YES/NO share positions. The `positions` table has `user_id, market_id, yes_volume, yes_cost_sum, no_volume, no_cost_sum`. Two endpoints: list all positions for a user, and detail for a specific market.

**API Design:**
- `GET /api/v1/positions` — list all positions (non-zero volume) for current user
- `GET /api/v1/positions/{market_id}` — single market position (404 if not found)

**Files:**
- Create: `src/pm_account/infrastructure/positions_repository.py`
- Create: `src/pm_account/application/positions_schemas.py`
- Create: `src/pm_account/api/positions_router.py`
- Modify: `src/main.py`

**Step 1: Write failing tests**

Create `tests/unit/test_positions.py`:

```python
# tests/unit/test_positions.py
"""Unit tests for positions infrastructure."""
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_pos_row(**kwargs: Any) -> MagicMock:
    row = MagicMock()
    row.market_id = kwargs.get("market_id", "mkt-1")
    row.yes_volume = kwargs.get("yes_volume", 10)
    row.yes_cost_sum = kwargs.get("yes_cost_sum", 600)
    row.no_volume = kwargs.get("no_volume", 0)
    row.no_cost_sum = kwargs.get("no_cost_sum", 0)
    return row


@pytest.mark.asyncio
async def test_list_positions_returns_non_zero_rows() -> None:
    from src.pm_account.infrastructure.positions_repository import PositionsRepository
    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.fetchall.return_value = [_make_pos_row(), _make_pos_row(market_id="mkt-2")]
    db.execute.return_value = result_mock
    repo = PositionsRepository()
    positions = await repo.list_by_user("user-1", db)
    assert len(positions) == 2
    assert positions[0]["market_id"] == "mkt-1"


@pytest.mark.asyncio
async def test_get_position_returns_none_when_missing() -> None:
    from src.pm_account.infrastructure.positions_repository import PositionsRepository
    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.fetchone.return_value = None
    db.execute.return_value = result_mock
    repo = PositionsRepository()
    pos = await repo.get_by_market("user-1", "mkt-missing", db)
    assert pos is None


@pytest.mark.asyncio
async def test_get_position_returns_data_when_found() -> None:
    from src.pm_account.infrastructure.positions_repository import PositionsRepository
    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.fetchone.return_value = _make_pos_row(yes_volume=5)
    db.execute.return_value = result_mock
    repo = PositionsRepository()
    pos = await repo.get_by_market("user-1", "mkt-1", db)
    assert pos is not None
    assert pos["yes_volume"] == 5
```

**Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/test_positions.py -v
```
Expected: `FAILED` — `PositionsRepository` not found.

**Step 3: Create positions_repository.py**

```python
# src/pm_account/infrastructure/positions_repository.py
"""Read-only positions queries."""
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_LIST_SQL = text("""
    SELECT market_id, yes_volume, yes_cost_sum, no_volume, no_cost_sum
    FROM positions
    WHERE user_id = :user_id
      AND (yes_volume > 0 OR no_volume > 0)
    ORDER BY market_id
""")

_GET_SQL = text("""
    SELECT market_id, yes_volume, yes_cost_sum, no_volume, no_cost_sum
    FROM positions
    WHERE user_id = :user_id AND market_id = :market_id
""")


class PositionsRepository:
    async def list_by_user(
        self, user_id: str, db: AsyncSession
    ) -> list[dict[str, Any]]:
        rows = (await db.execute(_LIST_SQL, {"user_id": user_id})).fetchall()
        return [_row_to_dict(r) for r in rows]

    async def get_by_market(
        self, user_id: str, market_id: str, db: AsyncSession
    ) -> dict[str, Any] | None:
        row = (
            await db.execute(_GET_SQL, {"user_id": user_id, "market_id": market_id})
        ).fetchone()
        if row is None:
            return None
        return _row_to_dict(row)


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "market_id": row.market_id,
        "yes_volume": row.yes_volume,
        "yes_cost_sum": row.yes_cost_sum,
        "no_volume": row.no_volume,
        "no_cost_sum": row.no_cost_sum,
    }
```

**Create positions_schemas.py:**

```python
# src/pm_account/application/positions_schemas.py
"""Pydantic schemas for positions API."""
from pydantic import BaseModel


class PositionResponse(BaseModel):
    market_id: str
    yes_volume: int
    yes_cost_sum: int
    no_volume: int
    no_cost_sum: int


class PositionListResponse(BaseModel):
    items: list[PositionResponse]
    total: int
```

**Create positions_router.py:**

```python
# src/pm_account/api/positions_router.py
"""Positions REST API — 2 endpoints."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_account.application.positions_schemas import (
    PositionListResponse,
    PositionResponse,
)
from src.pm_account.infrastructure.positions_repository import PositionsRepository
from src.pm_common.database import get_db_session
from src.pm_common.response import ApiResponse, success_response
from src.pm_gateway.auth.dependencies import get_current_user
from src.pm_gateway.user.db_models import UserModel

router = APIRouter(prefix="/positions", tags=["positions"])
_repo = PositionsRepository()


@router.get("")
async def list_positions(
    current_user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse:
    items = await _repo.list_by_user(str(current_user.id), db)
    data = PositionListResponse(
        items=[PositionResponse(**p) for p in items],
        total=len(items),
    )
    return success_response(data.model_dump())


@router.get("/{market_id}")
async def get_position(
    market_id: str,
    current_user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse:
    pos = await _repo.get_by_market(str(current_user.id), market_id, db)
    if pos is None:
        raise HTTPException(status_code=404, detail="Position not found")
    return success_response(PositionResponse(**pos).model_dump())
```

**Modify main.py** — add import and include_router:

```python
from src.pm_account.api.positions_router import router as positions_router
# ...
app.include_router(positions_router, prefix="/api/v1")
```

**Step 4: Run tests**

```bash
uv run pytest tests/unit/test_positions.py -v
```
Expected: `3 passed`

```bash
uv run pytest tests/ -q
```
Expected: all pass.

**Step 5: Commit**

```bash
git add src/pm_account/infrastructure/positions_repository.py \
        src/pm_account/application/positions_schemas.py \
        src/pm_account/api/positions_router.py \
        src/main.py \
        tests/unit/test_positions.py
git commit -m "feat(positions): add GET /positions and GET /positions/{market_id} endpoints"
```

---

## Task 7: GET /trades Endpoint

**Context:** Users need to view their trade history. Trades are now persisted in Task 4. This endpoint queries the trades table filtered by user (either buy_user_id or sell_user_id) with cursor pagination.

**API Design:**
- `GET /api/v1/trades?market_id=&limit=20&cursor=` — paginated list of trades for current user

**Files:**
- Create: `src/pm_clearing/infrastructure/trades_repository.py`
- Create: `src/pm_clearing/application/trades_schemas.py`
- Create: `src/pm_clearing/api/trades_router.py`
- Modify: `src/main.py`

**Step 1: Write failing tests**

Create `tests/unit/test_trades_repository.py`:

```python
# tests/unit/test_trades_repository.py
"""Unit tests for TradesRepository."""
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_trade_row(**kwargs: Any) -> MagicMock:
    row = MagicMock()
    row.trade_id = kwargs.get("trade_id", "t1")
    row.market_id = kwargs.get("market_id", "mkt-1")
    row.scenario = kwargs.get("scenario", "MINT")
    row.buy_order_id = kwargs.get("buy_order_id", "b1")
    row.sell_order_id = kwargs.get("sell_order_id", "s1")
    row.buy_user_id = kwargs.get("buy_user_id", "user-b")
    row.sell_user_id = kwargs.get("sell_user_id", "user-s")
    row.buy_book_type = kwargs.get("buy_book_type", "NATIVE_BUY")
    row.sell_book_type = kwargs.get("sell_book_type", "SYNTHETIC_SELL")
    row.price = kwargs.get("price", 60)
    row.quantity = kwargs.get("quantity", 10)
    row.maker_order_id = kwargs.get("maker_order_id", "s1")
    row.taker_order_id = kwargs.get("taker_order_id", "b1")
    row.taker_fee = kwargs.get("taker_fee", 12)
    row.maker_fee = kwargs.get("maker_fee", 0)
    row.buy_realized_pnl = kwargs.get("buy_realized_pnl", None)
    row.sell_realized_pnl = kwargs.get("sell_realized_pnl", None)
    row.executed_at = kwargs.get("executed_at", datetime.now(UTC))
    return row


@pytest.mark.asyncio
async def test_list_by_user_returns_empty() -> None:
    from src.pm_clearing.infrastructure.trades_repository import TradesRepository
    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.fetchall.return_value = []
    db.execute.return_value = result_mock
    repo = TradesRepository()
    trades = await repo.list_by_user("user-1", None, 20, None, db)
    assert trades == []


@pytest.mark.asyncio
async def test_list_by_user_returns_rows() -> None:
    from src.pm_clearing.infrastructure.trades_repository import TradesRepository
    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.fetchall.return_value = [_make_trade_row(), _make_trade_row(trade_id="t2")]
    db.execute.return_value = result_mock
    repo = TradesRepository()
    trades = await repo.list_by_user("user-b", None, 20, None, db)
    assert len(trades) == 2
    assert trades[0]["trade_id"] == "t1"
```

**Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/test_trades_repository.py -v
```
Expected: `FAILED` — `TradesRepository` not found.

**Step 3: Create trades_repository.py**

```python
# src/pm_clearing/infrastructure/trades_repository.py
"""Read-only trades queries — user perspective."""
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_LIST_SQL = text("""
    SELECT trade_id, market_id, scenario,
           buy_order_id, sell_order_id,
           buy_user_id, sell_user_id,
           buy_book_type, sell_book_type,
           price, quantity,
           maker_order_id, taker_order_id,
           maker_fee, taker_fee,
           buy_realized_pnl, sell_realized_pnl,
           executed_at
    FROM trades
    WHERE (buy_user_id = :user_id OR sell_user_id = :user_id)
      AND (CAST(:market_id AS TEXT) IS NULL OR market_id = :market_id)
      AND (:cursor_id IS NULL OR trade_id < :cursor_id)
    ORDER BY executed_at DESC, trade_id DESC
    LIMIT :limit
""")


class TradesRepository:
    async def list_by_user(
        self,
        user_id: str,
        market_id: str | None,
        limit: int,
        cursor_id: str | None,
        db: AsyncSession,
    ) -> list[dict[str, Any]]:
        rows = (
            await db.execute(
                _LIST_SQL,
                {
                    "user_id": user_id,
                    "market_id": market_id,
                    "limit": limit,
                    "cursor_id": cursor_id,
                },
            )
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "trade_id": row.trade_id,
        "market_id": row.market_id,
        "scenario": row.scenario,
        "buy_order_id": row.buy_order_id,
        "sell_order_id": row.sell_order_id,
        "buy_user_id": row.buy_user_id,
        "sell_user_id": row.sell_user_id,
        "buy_book_type": row.buy_book_type,
        "sell_book_type": row.sell_book_type,
        "price": row.price,
        "quantity": row.quantity,
        "maker_order_id": row.maker_order_id,
        "taker_order_id": row.taker_order_id,
        "maker_fee": row.maker_fee,
        "taker_fee": row.taker_fee,
        "buy_realized_pnl": row.buy_realized_pnl,
        "sell_realized_pnl": row.sell_realized_pnl,
        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
    }
```

**Create trades_schemas.py and trades_router.py:**

```python
# src/pm_clearing/application/trades_schemas.py
from pydantic import BaseModel
from datetime import datetime


class TradeResponse(BaseModel):
    trade_id: str
    market_id: str
    scenario: str
    price: int
    quantity: int
    buy_user_id: str
    sell_user_id: str
    taker_fee: int
    buy_realized_pnl: int | None
    sell_realized_pnl: int | None
    executed_at: str | None


class TradeListResponse(BaseModel):
    items: list[TradeResponse]
    has_more: bool
    next_cursor: str | None
```

```python
# src/pm_clearing/api/trades_router.py
"""Trades REST API."""
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_clearing.application.trades_schemas import TradeListResponse, TradeResponse
from src.pm_clearing.infrastructure.trades_repository import TradesRepository
from src.pm_common.database import get_db_session
from src.pm_common.response import ApiResponse, success_response
from src.pm_gateway.auth.dependencies import get_current_user
from src.pm_gateway.user.db_models import UserModel

router = APIRouter(prefix="/trades", tags=["trades"])
_repo = TradesRepository()


@router.get("")
async def list_trades(
    current_user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    market_id: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
) -> ApiResponse:
    items = await _repo.list_by_user(str(current_user.id), market_id, limit + 1, cursor, db)
    has_more = len(items) > limit
    if has_more:
        items = items[:limit]
    next_cursor = items[-1]["trade_id"] if has_more and items else None
    data = TradeListResponse(
        items=[TradeResponse(**t) for t in items],
        has_more=has_more,
        next_cursor=next_cursor,
    )
    return success_response(data.model_dump())
```

**Modify main.py:**

```python
from src.pm_clearing.api.trades_router import router as trades_router
# ...
app.include_router(trades_router, prefix="/api/v1")
```

**Step 4: Run tests**

```bash
uv run pytest tests/unit/test_trades_repository.py -v
```
Expected: `2 passed`

```bash
uv run pytest tests/ -q
```
Expected: all pass.

**Step 5: Commit**

```bash
git add src/pm_clearing/infrastructure/trades_repository.py \
        src/pm_clearing/application/trades_schemas.py \
        src/pm_clearing/api/trades_router.py \
        src/main.py \
        tests/unit/test_trades_repository.py
git commit -m "feat(trades): add GET /trades endpoint with cursor pagination"
```

---

## Task 8: POST /admin/markets/{id}/resolve

**Context:** Admin endpoint to resolve a market (mark outcome as YES or NO). Steps: (1) verify market is ACTIVE, (2) cancel all open/partially-filled orders and unfreeze their assets, (3) call `settle_market()` to pay winners and zero the market.

**API Design:**
- `POST /api/v1/admin/markets/{market_id}/resolve` body: `{"outcome": "YES" | "NO"}`
- Returns: `{"market_id": ..., "outcome": ..., "payouts_count": ..., "cancelled_orders": ...}`
- Auth: requires JWT (MVP — no role check, any authenticated user can resolve for simplicity)

**Files:**
- Create: `src/pm_admin/__init__.py`
- Create: `src/pm_admin/application/service.py`
- Create: `src/pm_admin/api/router.py`
- Modify: `src/main.py`

**Step 1: Write failing tests**

Create `tests/unit/test_admin_resolve.py`:

```python
# tests/unit/test_admin_resolve.py
"""Unit tests for AdminService.resolve_market."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_resolve_active_market_calls_settle() -> None:
    from src.pm_admin.application.service import AdminService
    db = AsyncMock()
    # Mock market row: status=ACTIVE
    market_mock = MagicMock()
    market_mock.fetchone.return_value = MagicMock(status="ACTIVE", id="mkt-1")
    # Mock open orders (empty for simplicity)
    orders_mock = MagicMock()
    orders_mock.fetchall.return_value = []
    db.execute.side_effect = [market_mock, orders_mock]
    with patch("src.pm_admin.application.service.settle_market") as mock_settle:
        svc = AdminService()
        result = await svc.resolve_market("mkt-1", "YES", db)
    mock_settle.assert_awaited_once_with("mkt-1", "YES", db)
    assert result["outcome"] == "YES"
    assert result["cancelled_orders"] == 0


@pytest.mark.asyncio
async def test_resolve_non_active_market_raises() -> None:
    from src.pm_admin.application.service import AdminService
    from src.pm_common.errors import AppError
    db = AsyncMock()
    market_mock = MagicMock()
    market_mock.fetchone.return_value = MagicMock(status="SETTLED", id="mkt-1")
    db.execute.return_value = market_mock
    svc = AdminService()
    with pytest.raises(AppError):
        await svc.resolve_market("mkt-1", "YES", db)
```

**Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/test_admin_resolve.py -v
```
Expected: `FAILED` — `AdminService` not found.

**Step 3: Create pm_admin module**

```python
# src/pm_admin/__init__.py
```

```python
# src/pm_admin/application/service.py
"""Admin application service — market resolution."""
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_clearing.domain.settlement import settle_market
from src.pm_common.errors import AppError

_GET_MARKET_SQL = text("SELECT id, status FROM markets WHERE id = :market_id")
_GET_OPEN_ORDERS_SQL = text("""
    SELECT id, user_id, frozen_amount, frozen_asset_type, remaining_quantity, market_id
    FROM orders
    WHERE market_id = :market_id AND status IN ('OPEN', 'PARTIALLY_FILLED')
""")
_CANCEL_ORDER_SQL = text("""
    UPDATE orders SET status = 'CANCELLED', cancel_reason = 'MARKET_RESOLVED',
    updated_at = NOW()
    WHERE id = :order_id
""")
_UNFREEZE_FUNDS_SQL = text("""
    UPDATE accounts SET available_balance = available_balance + :amount,
    frozen_balance = frozen_balance - :amount,
    version = version + 1, updated_at = NOW()
    WHERE user_id = :user_id
""")
_UNFREEZE_YES_SQL = text("""
    UPDATE positions SET yes_pending_sell = yes_pending_sell - :qty, updated_at = NOW()
    WHERE user_id = :user_id AND market_id = :market_id
""")
_UNFREEZE_NO_SQL = text("""
    UPDATE positions SET no_pending_sell = no_pending_sell - :qty, updated_at = NOW()
    WHERE user_id = :user_id AND market_id = :market_id
""")


class AdminService:
    async def resolve_market(
        self, market_id: str, outcome: str, db: AsyncSession
    ) -> dict[str, Any]:
        row = (await db.execute(_GET_MARKET_SQL, {"market_id": market_id})).fetchone()
        if row is None:
            raise AppError(3001, "Market not found", http_status=404)
        if row.status != "ACTIVE":
            raise AppError(3002, f"Market is not ACTIVE (status={row.status})", http_status=422)

        # Cancel all open orders and unfreeze
        orders = (
            await db.execute(_GET_OPEN_ORDERS_SQL, {"market_id": market_id})
        ).fetchall()
        for o in orders:
            await db.execute(_CANCEL_ORDER_SQL, {"order_id": o.id})
            if o.frozen_asset_type == "FUNDS":
                await db.execute(
                    _UNFREEZE_FUNDS_SQL, {"user_id": o.user_id, "amount": o.frozen_amount}
                )
            elif o.frozen_asset_type == "YES_SHARES":
                await db.execute(
                    _UNFREEZE_YES_SQL,
                    {"user_id": o.user_id, "market_id": market_id, "qty": o.remaining_quantity},
                )
            else:
                await db.execute(
                    _UNFREEZE_NO_SQL,
                    {"user_id": o.user_id, "market_id": market_id, "qty": o.remaining_quantity},
                )

        await settle_market(market_id, outcome, db)
        return {
            "market_id": market_id,
            "outcome": outcome,
            "cancelled_orders": len(orders),
        }
```

```python
# src/pm_admin/api/router.py
"""Admin REST API."""
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_admin.application.service import AdminService
from src.pm_common.database import get_db_session
from src.pm_common.response import ApiResponse, success_response
from src.pm_gateway.auth.dependencies import get_current_user
from src.pm_gateway.user.db_models import UserModel

router = APIRouter(prefix="/admin", tags=["admin"])
_service = AdminService()


class ResolveRequest(BaseModel):
    outcome: str  # "YES" | "NO"


@router.post("/markets/{market_id}/resolve")
async def resolve_market(
    market_id: str,
    body: ResolveRequest,
    current_user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse:
    result = await _service.resolve_market(market_id, body.outcome, db)
    return success_response(result)
```

**Modify main.py:**

```python
from src.pm_admin.api.router import router as admin_router
# ...
app.include_router(admin_router, prefix="/api/v1")
```

**Step 4: Run tests**

```bash
uv run pytest tests/unit/test_admin_resolve.py -v
```
Expected: `2 passed`

```bash
uv run pytest tests/ -q
```
Expected: all pass.

**Step 5: Commit**

```bash
git add src/pm_admin/ src/main.py tests/unit/test_admin_resolve.py
git commit -m "feat(admin): add POST /admin/markets/{id}/resolve endpoint"
```

---

## Task 9: POST /admin/verify-invariants

**Context:** Admin endpoint to verify global and per-market invariants. The per-market invariants (INV-1/2/3) already exist in `invariants.py`. Add: INV-G (global zero-sum: sum of all user balances + sum of all market reserves == sum of deposits - sum of withdrawals). The endpoint runs checks across all ACTIVE markets.

**API Design:**
- `POST /api/v1/admin/verify-invariants` — runs all invariant checks
- Returns: `{"ok": bool, "violations": [...]}`

**Files:**
- Create: `src/pm_clearing/domain/global_invariants.py`
- Modify: `src/pm_admin/api/router.py`
- Modify: `src/pm_admin/application/service.py`

**Step 1: Write failing tests**

Create `tests/unit/test_global_invariants.py`:

```python
# tests/unit/test_global_invariants.py
"""Unit tests for global invariant checks."""
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_verify_global_no_violations_returns_empty() -> None:
    from src.pm_clearing.domain.global_invariants import verify_global_invariants
    db = AsyncMock()
    # user balance sum = 1000, market reserve sum = 200, total = 1200
    # deposits - withdrawals = 1200 → balanced
    balance_row = MagicMock()
    balance_row.scalar_one.return_value = 1000
    reserve_row = MagicMock()
    reserve_row.scalar_one.return_value = 200
    platform_row = MagicMock()
    platform_row.scalar_one.return_value = 50
    deposit_row = MagicMock()
    deposit_row.scalar_one.return_value = 1250  # 1000 + 200 + 50
    db.execute.side_effect = [balance_row, reserve_row, platform_row, deposit_row]
    violations = await verify_global_invariants(db)
    assert violations == []


@pytest.mark.asyncio
async def test_verify_global_with_violation_returns_message() -> None:
    from src.pm_clearing.domain.global_invariants import verify_global_invariants
    db = AsyncMock()
    balance_row = MagicMock()
    balance_row.scalar_one.return_value = 1000
    reserve_row = MagicMock()
    reserve_row.scalar_one.return_value = 200
    platform_row = MagicMock()
    platform_row.scalar_one.return_value = 50
    deposit_row = MagicMock()
    deposit_row.scalar_one.return_value = 1300  # mismatch: 50 missing
    db.execute.side_effect = [balance_row, reserve_row, platform_row, deposit_row]
    violations = await verify_global_invariants(db)
    assert len(violations) == 1
    assert "INV-G" in violations[0]
```

**Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/test_global_invariants.py -v
```
Expected: `FAILED` — module not found.

**Step 3: Create global_invariants.py**

```python
# src/pm_clearing/domain/global_invariants.py
"""Global zero-sum invariant check."""
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_USER_BALANCE_SQL = text("""
    SELECT COALESCE(SUM(available_balance + frozen_balance), 0)
    FROM accounts
    WHERE user_id NOT IN ('SYSTEM_RESERVE', 'PLATFORM_FEE')
""")
_MARKET_RESERVE_SQL = text("SELECT COALESCE(SUM(reserve_balance), 0) FROM markets")
_PLATFORM_FEE_SQL = text(
    "SELECT COALESCE(available_balance, 0) FROM accounts WHERE user_id = 'PLATFORM_FEE'"
)
_NET_DEPOSIT_SQL = text("""
    SELECT COALESCE(SUM(CASE WHEN entry_type = 'DEPOSIT' THEN amount
                             WHEN entry_type = 'WITHDRAWAL' THEN -amount ELSE 0 END), 0)
    FROM ledger_entries
""")


async def verify_global_invariants(db: AsyncSession) -> list[str]:
    """Check INV-G: total user funds == net deposits.

    Returns list of violation messages (empty = all OK).
    """
    violations: list[str] = []
    user_bal = (await db.execute(_USER_BALANCE_SQL)).scalar_one()
    market_reserve = (await db.execute(_MARKET_RESERVE_SQL)).scalar_one()
    platform_fee = (await db.execute(_PLATFORM_FEE_SQL)).scalar_one()
    net_deposits = (await db.execute(_NET_DEPOSIT_SQL)).scalar_one()

    total_assets = user_bal + market_reserve + platform_fee
    if total_assets != net_deposits:
        msg = (
            f"INV-G violated: user_balances({user_bal}) + market_reserves({market_reserve}) "
            f"+ platform_fee({platform_fee}) = {total_assets} != net_deposits={net_deposits}"
        )
        violations.append(msg)
        logger.error(msg)
    return violations
```

**Modify `src/pm_admin/application/service.py`** — add verify_invariants method:

```python
from src.pm_clearing.domain.global_invariants import verify_global_invariants
from src.pm_clearing.domain.invariants import verify_invariants_after_trade

_LIST_ACTIVE_MARKETS_SQL = text(
    "SELECT id, status, reserve_balance, pnl_pool, total_yes_shares, total_no_shares "
    "FROM markets WHERE status = 'ACTIVE'"
)

class AdminService:
    # ... existing resolve_market ...

    async def verify_all_invariants(self, db: AsyncSession) -> dict[str, object]:
        violations: list[str] = []
        # Per-market invariants
        rows = (await db.execute(_LIST_ACTIVE_MARKETS_SQL)).fetchall()
        for row in rows:
            try:
                from src.pm_matching.engine.engine import MarketState
                # Build a minimal MarketState-like object
                ms = _MarketStateShim(row)
                await verify_invariants_after_trade(ms, db)
            except AssertionError as e:
                violations.append(str(e))
        # Global invariant
        global_violations = await verify_global_invariants(db)
        violations.extend(global_violations)
        return {"ok": len(violations) == 0, "violations": violations}


class _MarketStateShim:
    """Minimal duck-typed MarketState for invariant checks."""
    def __init__(self, row: Any) -> None:
        self.id: str = row.id
        self.reserve_balance: int = row.reserve_balance
        self.pnl_pool: int = row.pnl_pool
        self.total_yes_shares: int = row.total_yes_shares
        self.total_no_shares: int = row.total_no_shares
```

**Modify `src/pm_admin/api/router.py`** — add endpoint:

```python
@router.post("/verify-invariants")
async def verify_invariants(
    current_user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse:
    result = await _service.verify_all_invariants(db)
    return success_response(result)
```

**Step 4: Run tests**

```bash
uv run pytest tests/unit/test_global_invariants.py -v
```
Expected: `2 passed`

```bash
uv run pytest tests/ -q
```
Expected: all pass.

**Step 5: Commit**

```bash
git add src/pm_clearing/domain/global_invariants.py \
        src/pm_admin/application/service.py \
        src/pm_admin/api/router.py \
        tests/unit/test_global_invariants.py
git commit -m "feat(admin): add POST /admin/verify-invariants with global zero-sum check"
```

---

## Task 10: GET /admin/markets/{id}/stats

**Context:** Admin endpoint to view market trading statistics: total trades, total volume, total fees collected, and unique trader count. Reads from the `trades` table (requires Task 4 to have persisted trades).

**API Design:**
- `GET /api/v1/admin/markets/{market_id}/stats`
- Returns: `{"market_id", "total_trades", "total_volume", "total_fees", "unique_traders", "status"}`

**Files:**
- Modify: `src/pm_admin/application/service.py`
- Modify: `src/pm_admin/api/router.py`

**Step 1: Write failing tests**

Create `tests/unit/test_admin_stats.py`:

```python
# tests/unit/test_admin_stats.py
"""Unit tests for AdminService.get_market_stats."""
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_get_market_stats_returns_aggregates() -> None:
    from src.pm_admin.application.service import AdminService
    db = AsyncMock()
    # market row
    market_mock = MagicMock()
    market_mock.fetchone.return_value = MagicMock(status="ACTIVE", id="mkt-1")
    # stats row: total_trades=5, total_volume=100, total_fees=12, unique_traders=3
    stats_mock = MagicMock()
    stats_row = MagicMock()
    stats_row.total_trades = 5
    stats_row.total_volume = 100
    stats_row.total_fees = 12
    stats_row.unique_traders = 3
    stats_mock.fetchone.return_value = stats_row
    db.execute.side_effect = [market_mock, stats_mock]
    svc = AdminService()
    result = await svc.get_market_stats("mkt-1", db)
    assert result["total_trades"] == 5
    assert result["total_volume"] == 100
    assert result["unique_traders"] == 3


@pytest.mark.asyncio
async def test_get_market_stats_raises_when_not_found() -> None:
    from src.pm_admin.application.service import AdminService
    from src.pm_common.errors import AppError
    db = AsyncMock()
    market_mock = MagicMock()
    market_mock.fetchone.return_value = None
    db.execute.return_value = market_mock
    svc = AdminService()
    with pytest.raises(AppError):
        await svc.get_market_stats("mkt-missing", db)
```

**Step 2: Run to verify failure**

```bash
uv run pytest tests/unit/test_admin_stats.py -v
```
Expected: `FAILED` — `get_market_stats` not found.

**Step 3: Implement**

**Add to `src/pm_admin/application/service.py`:**

```python
_GET_MARKET_STATUS_SQL = text("SELECT id, status FROM markets WHERE id = :market_id")
_STATS_SQL = text("""
    SELECT
        COUNT(*) AS total_trades,
        COALESCE(SUM(quantity), 0) AS total_volume,
        COALESCE(SUM(taker_fee + maker_fee), 0) AS total_fees,
        COUNT(DISTINCT buy_user_id) + COUNT(DISTINCT sell_user_id) AS unique_traders
    FROM trades
    WHERE market_id = :market_id
""")

class AdminService:
    # ... existing methods ...

    async def get_market_stats(self, market_id: str, db: AsyncSession) -> dict[str, Any]:
        row = (await db.execute(_GET_MARKET_STATUS_SQL, {"market_id": market_id})).fetchone()
        if row is None:
            raise AppError(3001, "Market not found", http_status=404)
        stats = (await db.execute(_STATS_SQL, {"market_id": market_id})).fetchone()
        return {
            "market_id": market_id,
            "status": row.status,
            "total_trades": stats.total_trades if stats else 0,
            "total_volume": stats.total_volume if stats else 0,
            "total_fees": stats.total_fees if stats else 0,
            "unique_traders": stats.unique_traders if stats else 0,
        }
```

**Add to `src/pm_admin/api/router.py`:**

```python
@router.get("/markets/{market_id}/stats")
async def get_market_stats(
    market_id: str,
    current_user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ApiResponse:
    result = await _service.get_market_stats(market_id, db)
    return success_response(result)
```

**Step 4: Run tests**

```bash
uv run pytest tests/unit/test_admin_stats.py -v
```
Expected: `2 passed`

Full suite:
```bash
uv run pytest tests/ -q
uv run ruff check src/ tests/
uv run mypy src/
```
Expected: all pass, no lint errors, no type errors.

**Step 5: Commit**

```bash
git add src/pm_admin/application/service.py src/pm_admin/api/router.py \
        tests/unit/test_admin_stats.py
git commit -m "feat(admin): add GET /admin/markets/{id}/stats endpoint"
```

---

## Final Verification

After all 10 tasks complete:

```bash
# Full test suite
uv run pytest tests/ -v

# Lint + type checks
uv run ruff check src/ tests/
uv run mypy src/

# Integration smoke test (requires running PostgreSQL)
uv run pytest tests/integration/ -v
```

Expected: all 282+ tests pass, ruff clean, mypy clean.

## API Summary (After Completion)

| # | Method | Path | Module | Status |
|---|--------|------|--------|--------|
| 1 | POST | /auth/register | pm_gateway | ✅ Done |
| 2 | POST | /auth/login | pm_gateway | ✅ Done |
| 3 | POST | /auth/refresh | pm_gateway | ✅ Done |
| 4 | GET | /account/balance | pm_account | ✅ Done |
| 5 | POST | /account/deposit | pm_account | ✅ Done |
| 6 | POST | /account/withdraw | pm_account | ✅ Done |
| 7 | GET | /account/ledger | pm_account | ✅ Done |
| 8 | GET | /markets | pm_market | ✅ Done |
| 9 | GET | /markets/{id} | pm_market | ✅ Done |
| 10 | GET | /markets/{id}/orderbook | pm_market | ✅ Done |
| 11 | POST | /orders | pm_order | ✅ Done |
| 12 | GET | /orders | pm_order | ✅ Done |
| 13 | DELETE | /orders/{id} | pm_order | ✅ Done |
| 14 | GET | /orders/{id} | pm_order | ✅ Done |
| 15 | GET | /positions | pm_account | Task 6 |
| 16 | GET | /positions/{market_id} | pm_account | Task 6 |
| 17 | GET | /trades | pm_clearing | Task 7 |
| 18 | POST | /admin/verify-invariants | pm_admin | Task 9 |
| 19 | GET | /admin/markets/{id}/stats | pm_admin | Task 10 |
| 20 | POST | /admin/markets/{id}/resolve | pm_admin | Task 8 |
