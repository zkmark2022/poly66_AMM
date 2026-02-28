# pm_order / pm_risk / pm_matching / pm_clearing Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the full order placement chain: REST API → transform → risk check → in-memory matching → clearing → netting → WAL, plus market settlement.

**Architecture:** MatchingEngine (pm_matching) is the central orchestrator, holding per-market `asyncio.Lock` and in-memory `OrderBook` dict. pm_order exposes REST API and persists orders. pm_risk provides pure rule functions + atomic DB freeze. pm_clearing handles 4 clearing scenarios, netting, WAL writes, and settle_market. All within a single DB transaction per order placement.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy (async), PostgreSQL, asyncio.Lock, uv, pytest-asyncio

**Key facts before you start:**
- All enums (`BookType`, `TradeScenario`, `FrozenAssetType`, `OrderStatus`, `TimeInForce`, `LedgerEntryType`) are **already in `src/pm_common/enums.py`** — import, don't redefine
- `Position` dataclass is in `src/pm_account/domain/models.py`
- `PositionORM` and `AccountORM` are in `src/pm_account/infrastructure/db_models.py`
- All 9 DB tables already exist (Alembic migrations applied) — **no new migrations needed**
- Run tests with: `uv run pytest <path> -v`
- All test functions must have `-> None` return type
- Use `StrEnum` (already used in pm_common), `from datetime import UTC`, `raise X from None`
- `TAKER_FEE_BPS = 20` (0.2%, design doc default)
- Design doc reference: `Planning/Implementation/2026-02-21-pm-order-design.md`

---

## Task 1: pm_order Domain Model

**Files:**
- Create: `src/pm_order/domain/models.py`
- Test: `tests/unit/test_order_domain_models.py`

**Step 1: Write failing test**

```python
# tests/unit/test_order_domain_models.py
from datetime import UTC, datetime
from src.pm_order.domain.models import Order


def _make_order(**kwargs) -> Order:
    defaults = dict(
        id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        client_order_id="client-1",
        market_id="mkt-1",
        user_id="user-1",
        original_side="YES",
        original_direction="BUY",
        original_price=65,
        book_type="NATIVE_BUY",
        book_direction="BUY",
        book_price=65,
        quantity=100,
        frozen_amount=6600,
        frozen_asset_type="FUNDS",
        time_in_force="GTC",
        status="OPEN",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    defaults.update(kwargs)
    return Order(**defaults)


class TestOrderModel:
    def test_initial_remaining_quantity(self) -> None:
        order = _make_order(quantity=100)
        assert order.remaining_quantity == 100

    def test_is_active_open(self) -> None:
        order = _make_order(status="OPEN")
        assert order.is_active is True

    def test_is_active_partially_filled(self) -> None:
        order = _make_order(status="PARTIALLY_FILLED")
        assert order.is_active is True

    def test_is_active_filled(self) -> None:
        order = _make_order(status="FILLED")
        assert order.is_active is False

    def test_is_cancellable(self) -> None:
        order = _make_order(status="OPEN")
        assert order.is_cancellable is True

    def test_is_not_cancellable_filled(self) -> None:
        order = _make_order(status="FILLED")
        assert order.is_cancellable is False
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_order_domain_models.py -v
# Expected: ModuleNotFoundError or ImportError
```

**Step 3: Implement**

```python
# src/pm_order/domain/models.py
"""Order domain model — pure dataclass, no SQLAlchemy dependency."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Order:
    id: str
    client_order_id: str
    market_id: str
    user_id: str
    # User original intent
    original_side: str           # YES / NO
    original_direction: str      # BUY / SELL
    original_price: int          # 1–99
    # Orderbook view (after transform)
    book_type: str               # NATIVE_BUY / NATIVE_SELL / SYNTHETIC_BUY / SYNTHETIC_SELL
    book_direction: str          # BUY / SELL
    book_price: int              # 1–99
    # Quantity tracking
    quantity: int                # original order size
    # Freeze info
    frozen_amount: int = 0
    frozen_asset_type: str = ""  # FUNDS / YES_SHARES / NO_SHARES
    # Control
    time_in_force: str = "GTC"
    status: str = "OPEN"
    filled_quantity: int = 0
    remaining_quantity: int = field(init=False)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        self.remaining_quantity = self.quantity - self.filled_quantity

    @property
    def is_active(self) -> bool:
        return self.status in ("OPEN", "PARTIALLY_FILLED")

    @property
    def is_cancellable(self) -> bool:
        return self.status in ("OPEN", "PARTIALLY_FILLED")
```

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_order_domain_models.py -v
# Expected: 6 passed
```

**Step 5: Commit**
```bash
git add src/pm_order/domain/models.py tests/unit/test_order_domain_models.py
git commit -m "feat(pm_order): add Order domain model"
```

---

## Task 2: Order Transformer

**Files:**
- Create: `src/pm_order/domain/transformer.py`
- Test: `tests/unit/test_order_transformer.py`

**Step 1: Write failing test**

```python
# tests/unit/test_order_transformer.py
import pytest
from src.pm_order.domain.transformer import transform_order


@pytest.mark.parametrize("side,direction,price,exp_type,exp_dir,exp_price", [
    ("YES", "BUY",  65, "NATIVE_BUY",     "BUY",  65),
    ("YES", "SELL", 67, "NATIVE_SELL",    "SELL", 67),
    ("NO",  "BUY",  35, "SYNTHETIC_SELL", "SELL", 65),  # 100-35=65
    ("NO",  "SELL", 40, "SYNTHETIC_BUY",  "BUY",  60),  # 100-40=60
    ("NO",  "BUY",   1, "SYNTHETIC_SELL", "SELL", 99),  # boundary
    ("NO",  "BUY",  99, "SYNTHETIC_SELL", "SELL",  1),  # boundary
    ("NO",  "SELL",  1, "SYNTHETIC_BUY",  "BUY",  99),  # boundary
    ("NO",  "SELL", 99, "SYNTHETIC_BUY",  "BUY",   1),  # boundary
])
def test_transform_order(
    side: str, direction: str, price: int,
    exp_type: str, exp_dir: str, exp_price: int,
) -> None:
    book_type, book_dir, book_price = transform_order(side, direction, price)
    assert book_type == exp_type
    assert book_dir == exp_dir
    assert book_price == exp_price
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_order_transformer.py -v
```

**Step 3: Implement**

```python
# src/pm_order/domain/transformer.py
"""Order transformation layer: maps user intent (YES/NO BUY/SELL) to single YES orderbook view."""


def transform_order(side: str, direction: str, price: int) -> tuple[str, str, int]:
    """
    Transform user intent into orderbook representation.

    Returns: (book_type, book_direction, book_price)
    - YES BUY  → NATIVE_BUY,     BUY,  price
    - YES SELL → NATIVE_SELL,    SELL, price
    - NO  BUY  → SYNTHETIC_SELL, SELL, 100 - price
    - NO  SELL → SYNTHETIC_BUY,  BUY,  100 - price
    """
    if side == "YES":
        if direction == "BUY":
            return ("NATIVE_BUY", "BUY", price)
        return ("NATIVE_SELL", "SELL", price)
    # NO side
    if direction == "BUY":
        return ("SYNTHETIC_SELL", "SELL", 100 - price)
    return ("SYNTHETIC_BUY", "BUY", 100 - price)
```

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_order_transformer.py -v
# Expected: 8 passed
```

**Step 5: Commit**
```bash
git add src/pm_order/domain/transformer.py tests/unit/test_order_transformer.py
git commit -m "feat(pm_order): add order transformer (NO→YES mapping)"
```

---

## Task 3: pm_risk Pure Logic Rules

**Files:**
- Create: `src/pm_risk/rules/price_range.py`
- Create: `src/pm_risk/rules/order_limit.py`
- Create: `src/pm_risk/rules/self_trade.py`
- Test: `tests/unit/test_risk_rules.py`

**Step 1: Write failing test**

```python
# tests/unit/test_risk_rules.py
import pytest
from src.pm_risk.rules.price_range import check_price_range
from src.pm_risk.rules.order_limit import check_order_limit, MAX_ORDER_QUANTITY
from src.pm_risk.rules.self_trade import is_self_trade
from src.pm_common.errors import AppError


class TestPriceRange:
    def test_valid_price(self) -> None:
        check_price_range(50)  # no exception

    def test_boundary_low(self) -> None:
        check_price_range(1)

    def test_boundary_high(self) -> None:
        check_price_range(99)

    def test_price_zero_raises(self) -> None:
        with pytest.raises(AppError) as exc_info:
            check_price_range(0)
        assert exc_info.value.code == 4001

    def test_price_100_raises(self) -> None:
        with pytest.raises(AppError) as exc_info:
            check_price_range(100)
        assert exc_info.value.code == 4001


class TestOrderLimit:
    def test_valid_quantity(self) -> None:
        check_order_limit(1000)

    def test_max_quantity(self) -> None:
        check_order_limit(MAX_ORDER_QUANTITY)

    def test_exceeds_limit_raises(self) -> None:
        with pytest.raises(AppError) as exc_info:
            check_order_limit(MAX_ORDER_QUANTITY + 1)
        assert exc_info.value.code == 4002

    def test_zero_raises(self) -> None:
        with pytest.raises(AppError) as exc_info:
            check_order_limit(0)
        assert exc_info.value.code == 4002


class TestSelfTrade:
    def test_same_user_is_self_trade(self) -> None:
        assert is_self_trade("user-A", "user-A") is True

    def test_different_user_is_not(self) -> None:
        assert is_self_trade("user-A", "user-B") is False
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_risk_rules.py -v
```

**Step 3: Implement**

```python
# src/pm_risk/rules/price_range.py
from src.pm_common.errors import AppError

def check_price_range(price: int) -> None:
    if not (1 <= price <= 99):
        raise AppError(4001, f"Price {price} out of range [1, 99]", http_status=400)
```

```python
# src/pm_risk/rules/order_limit.py
from src.pm_common.errors import AppError

MAX_ORDER_QUANTITY = 100_000

def check_order_limit(quantity: int) -> None:
    if not (1 <= quantity <= MAX_ORDER_QUANTITY):
        raise AppError(4002, f"Quantity {quantity} must be in [1, {MAX_ORDER_QUANTITY}]", http_status=400)
```

```python
# src/pm_risk/rules/self_trade.py
def is_self_trade(incoming_user_id: str, resting_user_id: str) -> bool:
    """Predicate used by matching_algo to skip self-trade fills."""
    return incoming_user_id == resting_user_id
```

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_risk_rules.py -v
# Expected: 10 passed
```

**Step 5: Commit**
```bash
git add src/pm_risk/rules/ tests/unit/test_risk_rules.py
git commit -m "feat(pm_risk): add pure logic risk rules (price_range, order_limit, self_trade)"
```

---
## Task 4: pm_risk market_status + balance_check (DB freeze)

**Files:**
- Create: `src/pm_risk/rules/market_status.py`
- Create: `src/pm_risk/rules/balance_check.py`
- Modify: `tests/unit/test_risk_rules.py`

**Step 1: Add DB-dependent tests to test_risk_rules.py**

```python
# Append to tests/unit/test_risk_rules.py
from unittest.mock import AsyncMock
import pytest
from src.pm_risk.rules.market_status import check_market_active
from src.pm_risk.rules.balance_check import check_and_freeze, TAKER_FEE_BPS
from src.pm_order.domain.models import Order
from src.pm_common.errors import AppError

def _make_order(**kwargs) -> Order:
    defaults = dict(
        id="01ARZ", client_order_id="c1", market_id="mkt-1", user_id="u1",
        original_side="YES", original_direction="BUY", original_price=65,
        book_type="NATIVE_BUY", book_direction="BUY", book_price=65,
        quantity=100, frozen_amount=0, frozen_asset_type="", time_in_force="GTC", status="OPEN",
    )
    defaults.update(kwargs)
    return Order(**defaults)

class TestMarketStatus:
    async def test_active_market_ok(self) -> None:
        mock_db = AsyncMock()
        mock_db.execute.return_value.scalar_one_or_none.return_value = "ACTIVE"
        await check_market_active("mkt-1", mock_db)  # no exception

    async def test_suspended_market_raises_4004(self) -> None:
        mock_db = AsyncMock()
        mock_db.execute.return_value.scalar_one_or_none.return_value = "SUSPENDED"
        with pytest.raises(AppError) as exc_info:
            await check_market_active("mkt-1", mock_db)
        assert exc_info.value.code == 4004

    async def test_missing_market_raises_4004(self) -> None:
        mock_db = AsyncMock()
        mock_db.execute.return_value.scalar_one_or_none.return_value = None
        with pytest.raises(AppError) as exc_info:
            await check_market_active("mkt-1", mock_db)
        assert exc_info.value.code == 4004

class TestBalanceCheck:
    async def test_native_buy_freeze_sets_frozen_amount_with_fee_buffer(self) -> None:
        order = _make_order(book_type="NATIVE_BUY", original_price=65, quantity=100)
        mock_db = AsyncMock()
        mock_db.execute.return_value.fetchone.return_value = ("row",)
        await check_and_freeze(order, mock_db)
        trade_value = 65 * 100  # 6500
        expected_fee = (trade_value * TAKER_FEE_BPS + 9999) // 10000  # 13
        assert order.frozen_amount == trade_value + expected_fee
        assert order.frozen_asset_type == "FUNDS"

    async def test_synthetic_sell_uses_original_no_price(self) -> None:
        # Buy NO @35 → SYNTHETIC_SELL, freeze based on NO price 35
        order = _make_order(book_type="SYNTHETIC_SELL", original_price=35, book_price=65, quantity=100)
        mock_db = AsyncMock()
        mock_db.execute.return_value.fetchone.return_value = ("row",)
        await check_and_freeze(order, mock_db)
        trade_value = 35 * 100  # NO price
        expected_fee = (trade_value * TAKER_FEE_BPS + 9999) // 10000
        assert order.frozen_amount == trade_value + expected_fee
        assert order.frozen_asset_type == "FUNDS"

    async def test_funds_insufficient_raises_5001(self) -> None:
        order = _make_order(book_type="NATIVE_BUY", original_price=65, quantity=100)
        mock_db = AsyncMock()
        mock_db.execute.return_value.fetchone.return_value = None
        with pytest.raises(AppError) as exc_info:
            await check_and_freeze(order, mock_db)
        assert exc_info.value.code == 5001

    async def test_native_sell_freezes_yes_shares(self) -> None:
        order = _make_order(book_type="NATIVE_SELL", quantity=50)
        mock_db = AsyncMock()
        mock_db.execute.return_value.fetchone.return_value = ("row",)
        await check_and_freeze(order, mock_db)
        assert order.frozen_amount == 50
        assert order.frozen_asset_type == "YES_SHARES"

    async def test_synthetic_buy_freezes_no_shares(self) -> None:
        order = _make_order(book_type="SYNTHETIC_BUY", quantity=80)
        mock_db = AsyncMock()
        mock_db.execute.return_value.fetchone.return_value = ("row",)
        await check_and_freeze(order, mock_db)
        assert order.frozen_amount == 80
        assert order.frozen_asset_type == "NO_SHARES"
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_risk_rules.py::TestMarketStatus tests/unit/test_risk_rules.py::TestBalanceCheck -v
```

**Step 3: Implement**

```python
# src/pm_risk/rules/market_status.py
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.pm_common.errors import AppError

_SQL = text("SELECT status FROM markets WHERE id = :market_id")

async def check_market_active(market_id: str, db: AsyncSession) -> None:
    result = await db.execute(_SQL, {"market_id": market_id})
    status = result.scalar_one_or_none()
    if status != "ACTIVE":
        raise AppError(4004, f"Market {market_id} not found or not ACTIVE", http_status=404)
```

```python
# src/pm_risk/rules/balance_check.py
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.pm_order.domain.models import Order
from src.pm_common.errors import AppError

TAKER_FEE_BPS: int = 20  # 0.2%, design doc default

def _calc_max_fee(trade_value: int) -> int:
    """Ceiling division: max taker fee for a given trade value."""
    return (trade_value * TAKER_FEE_BPS + 9999) // 10000

_FREEZE_FUNDS_SQL = text("""
    UPDATE accounts
    SET available_balance = available_balance - :amount,
        frozen_balance     = frozen_balance   + :amount,
        version = version + 1, updated_at = NOW()
    WHERE user_id = :user_id AND available_balance >= :amount
    RETURNING id
""")

_FREEZE_YES_SQL = text("""
    UPDATE positions
    SET yes_pending_sell = yes_pending_sell + :qty, updated_at = NOW()
    WHERE user_id = :user_id AND market_id = :market_id
      AND (yes_volume - yes_pending_sell) >= :qty
    RETURNING id
""")

_FREEZE_NO_SQL = text("""
    UPDATE positions
    SET no_pending_sell = no_pending_sell + :qty, updated_at = NOW()
    WHERE user_id = :user_id AND market_id = :market_id
      AND (no_volume - no_pending_sell) >= :qty
    RETURNING id
""")

async def check_and_freeze(order: Order, db: AsyncSession) -> None:
    """Freeze funds or shares atomically. Mutates order.frozen_amount and order.frozen_asset_type."""
    if order.book_type in ("NATIVE_BUY", "SYNTHETIC_SELL"):
        trade_value = order.original_price * order.quantity
        freeze_amount = trade_value + _calc_max_fee(trade_value)
        row = (await db.execute(_FREEZE_FUNDS_SQL, {"user_id": order.user_id, "amount": freeze_amount})).fetchone()
        if row is None:
            raise AppError(5001, "Insufficient funds", http_status=402)
        order.frozen_amount = freeze_amount
        order.frozen_asset_type = "FUNDS"
    elif order.book_type == "NATIVE_SELL":
        row = (await db.execute(_FREEZE_YES_SQL, {"user_id": order.user_id, "market_id": order.market_id, "qty": order.quantity})).fetchone()
        if row is None:
            raise AppError(5001, "Insufficient YES shares", http_status=402)
        order.frozen_amount = order.quantity
        order.frozen_asset_type = "YES_SHARES"
    else:  # SYNTHETIC_BUY
        row = (await db.execute(_FREEZE_NO_SQL, {"user_id": order.user_id, "market_id": order.market_id, "qty": order.quantity})).fetchone()
        if row is None:
            raise AppError(5001, "Insufficient NO shares", http_status=402)
        order.frozen_amount = order.quantity
        order.frozen_asset_type = "NO_SHARES"
```

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_risk_rules.py -v
```

**Step 5: Commit**
```bash
git add src/pm_risk/rules/market_status.py src/pm_risk/rules/balance_check.py tests/unit/test_risk_rules.py
git commit -m "feat(pm_risk): add market_status check and atomic balance/position freeze"
```

---

## Task 5: pm_matching OrderBook + Domain Models

**Files:**
- Create: `src/pm_matching/domain/models.py`
- Create: `src/pm_matching/engine/order_book.py`
- Test: `tests/unit/test_order_book.py`

**Step 1: Write failing test**

```python
# tests/unit/test_order_book.py
from datetime import UTC, datetime
from src.pm_matching.domain.models import BookOrder
from src.pm_matching.engine.order_book import OrderBook

def _bo(order_id: str, user_id: str = "u1", book_type: str = "NATIVE_BUY", qty: int = 100) -> BookOrder:
    return BookOrder(order_id=order_id, user_id=user_id, book_type=book_type,
                     quantity=qty, created_at=datetime.now(UTC))

class TestOrderBookBids:
    def test_add_bid_updates_best_bid(self) -> None:
        ob = OrderBook(market_id="mkt-1")
        ob.add_order(_bo("o1"), price=65, side="BUY")
        assert ob.best_bid == 65

    def test_best_bid_is_highest_of_multiple(self) -> None:
        ob = OrderBook(market_id="mkt-1")
        ob.add_order(_bo("o1"), price=60, side="BUY")
        ob.add_order(_bo("o2"), price=65, side="BUY")
        assert ob.best_bid == 65

    def test_empty_book_best_bid_zero(self) -> None:
        assert OrderBook(market_id="m").best_bid == 0

class TestOrderBookAsks:
    def test_add_ask_updates_best_ask(self) -> None:
        ob = OrderBook(market_id="mkt-1")
        ob.add_order(_bo("o1", book_type="NATIVE_SELL"), price=70, side="SELL")
        assert ob.best_ask == 70

    def test_best_ask_is_lowest_of_multiple(self) -> None:
        ob = OrderBook(market_id="mkt-1")
        ob.add_order(_bo("o1", book_type="NATIVE_SELL"), price=70, side="SELL")
        ob.add_order(_bo("o2", book_type="NATIVE_SELL"), price=65, side="SELL")
        assert ob.best_ask == 65

    def test_empty_book_best_ask_100(self) -> None:
        assert OrderBook(market_id="m").best_ask == 100

class TestOrderBookCancel:
    def test_cancel_removes_from_index(self) -> None:
        ob = OrderBook(market_id="mkt-1")
        ob.add_order(_bo("o1"), price=65, side="BUY")
        ob.cancel_order("o1")
        assert "o1" not in ob._order_index

    def test_cancel_sole_bid_resets_best_bid(self) -> None:
        ob = OrderBook(market_id="mkt-1")
        ob.add_order(_bo("o1"), price=65, side="BUY")
        ob.cancel_order("o1")
        assert ob.best_bid == 0

    def test_cancel_nonexistent_is_noop(self) -> None:
        ob = OrderBook(market_id="mkt-1")
        ob.cancel_order("ghost")  # must not raise
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_order_book.py -v
```

**Step 3: Implement**

```python
# src/pm_matching/domain/models.py
from dataclasses import dataclass
from datetime import datetime

@dataclass
class BookOrder:
    """In-memory orderbook entry."""
    order_id: str
    user_id: str
    book_type: str    # for determine_scenario + self-trade check
    quantity: int     # remaining matchable quantity
    created_at: datetime

@dataclass
class TradeResult:
    """Single fill passed from matching to clearing."""
    buy_order_id: str
    sell_order_id: str
    buy_user_id: str
    sell_user_id: str
    market_id: str
    price: int               # YES trade price (maker price)
    quantity: int
    buy_book_type: str
    sell_book_type: str
    buy_original_price: int  # for Synthetic fee calc (NO price)
    maker_order_id: str
    taker_order_id: str
```

```python
# src/pm_matching/engine/order_book.py
from collections import deque
from dataclasses import dataclass, field
from src.pm_matching.domain.models import BookOrder

@dataclass
class OrderBook:
    """Single YES orderbook per prediction market. Indices 1–99 = price cents."""
    market_id: str
    bids: list[deque] = field(default_factory=lambda: [deque() for _ in range(100)])
    asks: list[deque] = field(default_factory=lambda: [deque() for _ in range(100)])
    best_bid: int = 0    # 0 = no bids
    best_ask: int = 100  # 100 = no asks
    _order_index: dict[str, tuple[str, int]] = field(default_factory=dict)
    # _order_index[order_id] = (side, price)

    def add_order(self, book_order: BookOrder, price: int, side: str) -> None:
        if side == "BUY":
            self.bids[price].append(book_order)
            if price > self.best_bid:
                self.best_bid = price
        else:
            self.asks[price].append(book_order)
            if price < self.best_ask:
                self.best_ask = price
        self._order_index[book_order.order_id] = (side, price)

    def cancel_order(self, order_id: str) -> None:
        if order_id not in self._order_index:
            return
        side, price = self._order_index.pop(order_id)
        queue = self.bids[price] if side == "BUY" else self.asks[price]
        for i, bo in enumerate(queue):
            if bo.order_id == order_id:
                del queue[i]
                break
        if side == "BUY" and price == self.best_bid:
            self._refresh_best_bid()
        elif side == "SELL" and price == self.best_ask:
            self._refresh_best_ask()

    def _refresh_best_bid(self) -> None:
        for p in range(99, 0, -1):
            if self.bids[p]:
                self.best_bid = p
                return
        self.best_bid = 0

    def _refresh_best_ask(self) -> None:
        for p in range(1, 100):
            if self.asks[p]:
                self.best_ask = p
                return
        self.best_ask = 100
```

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_order_book.py -v
# Expected: 9 passed
```

**Step 5: Commit**
```bash
git add src/pm_matching/domain/models.py src/pm_matching/engine/order_book.py tests/unit/test_order_book.py
git commit -m "feat(pm_matching): add BookOrder/TradeResult models and OrderBook data structure"
```

---
## Task 6: pm_matching Scenario Determination

**Files:**
- Create: `src/pm_matching/engine/scenario.py`
- Test: `tests/unit/test_matching_scenario.py`

**Step 1: Write failing test**
```python
# tests/unit/test_matching_scenario.py
import pytest
from src.pm_matching.engine.scenario import determine_scenario
from src.pm_common.enums import TradeScenario

@pytest.mark.parametrize("buy_type,sell_type,expected", [
    ("NATIVE_BUY",    "SYNTHETIC_SELL", TradeScenario.MINT),
    ("NATIVE_BUY",    "NATIVE_SELL",    TradeScenario.TRANSFER_YES),
    ("SYNTHETIC_BUY", "SYNTHETIC_SELL", TradeScenario.TRANSFER_NO),
    ("SYNTHETIC_BUY", "NATIVE_SELL",    TradeScenario.BURN),
])
def test_determine_scenario(buy_type: str, sell_type: str, expected: TradeScenario) -> None:
    assert determine_scenario(buy_type, sell_type) == expected
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_matching_scenario.py -v
```

**Step 3: Implement**
```python
# src/pm_matching/engine/scenario.py
from src.pm_common.enums import TradeScenario

def determine_scenario(buy_book_type: str, sell_book_type: str) -> TradeScenario:
    buy_native = buy_book_type == "NATIVE_BUY"
    sell_native = sell_book_type == "NATIVE_SELL"
    if buy_native and not sell_native:
        return TradeScenario.MINT
    if buy_native and sell_native:
        return TradeScenario.TRANSFER_YES
    if not buy_native and not sell_native:
        return TradeScenario.TRANSFER_NO
    return TradeScenario.BURN
```

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_matching_scenario.py -v
# Expected: 4 passed
```

**Step 5: Commit**
```bash
git add src/pm_matching/engine/scenario.py tests/unit/test_matching_scenario.py
git commit -m "feat(pm_matching): add determine_scenario (4 trade scenarios)"
```

---

## Task 7: pm_matching MatchingAlgo

**Files:**
- Create: `src/pm_matching/engine/matching_algo.py`
- Test: `tests/unit/test_matching_algo.py`

This is the pure in-memory matching algorithm. It takes an Order and an OrderBook, returns a list of TradeResult. No DB access — all DB work is done by clearing.

**Step 1: Write failing test (key scenarios)**
```python
# tests/unit/test_matching_algo.py
from datetime import UTC, datetime
from src.pm_matching.domain.models import BookOrder, TradeResult
from src.pm_matching.engine.order_book import OrderBook
from src.pm_matching.engine.matching_algo import match_order
from src.pm_order.domain.models import Order

def _order(user: str, side: str, direction: str, price: int,
           qty: int = 100, tif: str = "GTC") -> Order:
    from src.pm_order.domain.transformer import transform_order
    book_type, book_dir, book_price = transform_order(side, direction, price)
    return Order(id=f"o-{user}-{price}", client_order_id=f"c-{user}", market_id="mkt-1",
                 user_id=user, original_side=side, original_direction=direction,
                 original_price=price, book_type=book_type, book_direction=book_dir,
                 book_price=book_price, quantity=qty, time_in_force=tif, status="OPEN",
                 created_at=datetime.now(UTC))

def _resting(order_id: str, user: str, book_type: str, price: int, qty: int = 100) -> BookOrder:
    return BookOrder(order_id=order_id, user_id=user, book_type=book_type,
                     quantity=qty, created_at=datetime.now(UTC))

class TestMatchBuyOrder:
    def test_buy_yes_matches_sell_yes(self) -> None:
        # TRANSFER_YES scenario: Buy YES @65 taker, Sell YES @60 maker
        ob = OrderBook(market_id="mkt-1")
        maker = _resting("maker-1", "user-B", "NATIVE_SELL", 60)
        ob.add_order(maker, price=60, side="SELL")
        incoming = _order("user-A", "YES", "BUY", 65)
        trades = match_order(incoming, ob)
        assert len(trades) == 1
        assert trades[0].price == 60        # maker price
        assert trades[0].quantity == 100
        assert trades[0].buy_book_type == "NATIVE_BUY"
        assert trades[0].sell_book_type == "NATIVE_SELL"

    def test_no_match_when_prices_do_not_cross(self) -> None:
        ob = OrderBook(market_id="mkt-1")
        maker = _resting("maker-1", "user-B", "NATIVE_SELL", 70)
        ob.add_order(maker, price=70, side="SELL")
        incoming = _order("user-A", "YES", "BUY", 65)
        trades = match_order(incoming, ob)
        assert len(trades) == 0
        assert incoming.remaining_quantity == 100  # untouched

    def test_self_trade_is_skipped(self) -> None:
        ob = OrderBook(market_id="mkt-1")
        maker = _resting("maker-1", "user-A", "NATIVE_SELL", 60)  # same user!
        ob.add_order(maker, price=60, side="SELL")
        incoming = _order("user-A", "YES", "BUY", 65)
        trades = match_order(incoming, ob)
        assert len(trades) == 0  # skipped, not rejected

    def test_partial_fill_updates_remaining(self) -> None:
        ob = OrderBook(market_id="mkt-1")
        maker = _resting("maker-1", "user-B", "NATIVE_SELL", 60, qty=50)
        ob.add_order(maker, price=60, side="SELL")
        incoming = _order("user-A", "YES", "BUY", 65, qty=100)
        trades = match_order(incoming, ob)
        assert len(trades) == 1
        assert trades[0].quantity == 50
        assert incoming.remaining_quantity == 50
        assert incoming.filled_quantity == 50

    def test_buy_no_mint_scenario(self) -> None:
        # Buy NO @35 → SYNTHETIC_SELL @65; Buy YES @65 maker
        ob = OrderBook(market_id="mkt-1")
        maker = _resting("maker-1", "user-B", "NATIVE_BUY", 65)
        ob.add_order(maker, price=65, side="BUY")
        # incoming: Buy NO @35 → SYNTHETIC_SELL @65
        incoming = _order("user-A", "NO", "BUY", 35)
        trades = match_order(incoming, ob)
        assert len(trades) == 1
        assert trades[0].buy_book_type == "NATIVE_BUY"
        assert trades[0].sell_book_type == "SYNTHETIC_SELL"
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_matching_algo.py -v
```

**Step 3: Implement**

```python
# src/pm_matching/engine/matching_algo.py
"""Price-time priority matching algorithm for the single YES orderbook."""
from src.pm_matching.domain.models import BookOrder, TradeResult
from src.pm_matching.engine.order_book import OrderBook
from src.pm_order.domain.models import Order
from src.pm_risk.rules.self_trade import is_self_trade


def match_order(incoming: Order, ob: OrderBook) -> list[TradeResult]:
    if incoming.book_direction == "BUY":
        return _match_buy(incoming, ob)
    return _match_sell(incoming, ob)


def _match_buy(incoming: Order, ob: OrderBook) -> list[TradeResult]:
    """Match a BUY order against resting asks (price-time priority)."""
    trades: list[TradeResult] = []
    while incoming.remaining_quantity > 0 and ob.best_ask <= incoming.book_price:
        price = ob.best_ask
        queue = ob.asks[price]
        while queue and incoming.remaining_quantity > 0:
            resting: BookOrder = queue[0]
            if is_self_trade(incoming.user_id, resting.user_id):
                queue.rotate(-1)  # skip, try next at same price
                # if we've rotated all the way, stop
                if queue[0].user_id == incoming.user_id:
                    break
                continue
            fill_qty = min(incoming.remaining_quantity, resting.quantity)
            trades.append(_make_trade(incoming, resting, price, fill_qty))
            _apply_fill(incoming, resting, fill_qty)
            if resting.quantity == 0:
                queue.popleft()
                ob._order_index.pop(resting.order_id, None)
        if not queue:
            ob._refresh_best_ask()
            if ob.best_ask > incoming.book_price:
                break
    return trades


def _match_sell(incoming: Order, ob: OrderBook) -> list[TradeResult]:
    """Match a SELL order against resting bids (price-time priority)."""
    trades: list[TradeResult] = []
    while incoming.remaining_quantity > 0 and ob.best_bid >= incoming.book_price:
        price = ob.best_bid
        queue = ob.bids[price]
        while queue and incoming.remaining_quantity > 0:
            resting: BookOrder = queue[0]
            if is_self_trade(incoming.user_id, resting.user_id):
                queue.rotate(-1)
                if queue[0].user_id == incoming.user_id:
                    break
                continue
            fill_qty = min(incoming.remaining_quantity, resting.quantity)
            # For SELL: buy side is the resting BUY order
            trades.append(_make_trade_sell_incoming(incoming, resting, price, fill_qty))
            _apply_fill(incoming, resting, fill_qty)
            if resting.quantity == 0:
                queue.popleft()
                ob._order_index.pop(resting.order_id, None)
        if not queue:
            ob._refresh_best_bid()
            if ob.best_bid < incoming.book_price:
                break
    return trades


def _make_trade(buy_incoming: Order, sell_resting: BookOrder,
                price: int, qty: int) -> TradeResult:
    """incoming is BUY, resting is SELL."""
    return TradeResult(
        buy_order_id=buy_incoming.id,
        sell_order_id=sell_resting.order_id,
        buy_user_id=buy_incoming.user_id,
        sell_user_id=sell_resting.user_id,
        market_id=buy_incoming.market_id,
        price=price,
        quantity=qty,
        buy_book_type=buy_incoming.book_type,
        sell_book_type=sell_resting.book_type,
        buy_original_price=buy_incoming.original_price,
        maker_order_id=sell_resting.order_id,  # resting = maker
        taker_order_id=buy_incoming.id,
    )


def _make_trade_sell_incoming(sell_incoming: Order, buy_resting: BookOrder,
                               price: int, qty: int) -> TradeResult:
    """incoming is SELL, resting is BUY."""
    return TradeResult(
        buy_order_id=buy_resting.order_id,
        sell_order_id=sell_incoming.id,
        buy_user_id=buy_resting.user_id,
        sell_user_id=sell_incoming.user_id,
        market_id=sell_incoming.market_id,
        price=price,
        quantity=qty,
        buy_book_type=buy_resting.book_type,
        sell_book_type=sell_incoming.book_type,
        buy_original_price=0,  # resting BUY: original_price not needed (NO price only for SYNTHETIC_SELL)
        maker_order_id=buy_resting.order_id,  # resting = maker
        taker_order_id=sell_incoming.id,
    )


def _apply_fill(incoming: Order, resting: BookOrder, qty: int) -> None:
    incoming.filled_quantity += qty
    incoming.remaining_quantity -= qty
    resting.quantity -= qty
    if incoming.remaining_quantity == 0:
        incoming.status = "FILLED"
    else:
        incoming.status = "PARTIALLY_FILLED"
```

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_matching_algo.py -v
# Expected: 5 passed
```

**Step 5: Add remaining 11 test scenarios (matching all 16 from main plan)**
Add to `tests/unit/test_matching_algo.py`:
- Scenario 3: Transfer NO (Sell NO taker vs Buy NO maker)
- Scenario 4: Burn (Sell NO taker vs Sell YES maker)
- Scenario 5: Mint with price improvement (Taker pays less than book_price)
- Scenario 6: Partial fill, GTC remainder
- Scenario 7: IOC cancel remainder
- Scenario 8: No cross, no fill
- Scenario 9: Time priority (same price, two makers)
- Scenario 10: Boundary price=99
- Scenario 12: Multi-fill from one order
- Scenario 14: Burn releases reserve (scenario check only)
- Scenario 15: Add to empty book

```bash
uv run pytest tests/unit/test_matching_algo.py -v
# Expected: 16 passed
```

**Step 6: Commit**
```bash
git add src/pm_matching/engine/matching_algo.py tests/unit/test_matching_algo.py
git commit -m "feat(pm_matching): add price-time priority matching algorithm (16 scenarios)"
```

---

## Task 8: pm_clearing Fee Calculation

**Files:**
- Create: `src/pm_clearing/domain/fee.py`
- Test: `tests/unit/test_fee_calculation.py`

**Step 1: Write failing test**
```python
# tests/unit/test_fee_calculation.py
from src.pm_clearing.domain.fee import get_fee_trade_value, calc_fee

class TestGetFeeTradeValue:
    def test_native_buy_uses_yes_price(self) -> None:
        # NATIVE_BUY @ YES price 65, qty 100
        assert get_fee_trade_value("NATIVE_BUY", 65, 100, 0) == 65 * 100

    def test_native_sell_uses_yes_price(self) -> None:
        assert get_fee_trade_value("NATIVE_SELL", 65, 100, 0) == 65 * 100

    def test_synthetic_sell_uses_no_price(self) -> None:
        # Buy NO @35 → SYNTHETIC_SELL, YES trade price=65, NO original_price=35
        assert get_fee_trade_value("SYNTHETIC_SELL", 65, 100, 35) == 35 * 100

    def test_synthetic_buy_uses_no_price(self) -> None:
        # Sell NO @40 → SYNTHETIC_BUY, YES trade price=60, NO price = 100-60=40
        assert get_fee_trade_value("SYNTHETIC_BUY", 60, 100, 0) == 40 * 100

class TestCalcFee:
    def test_ceiling_division(self) -> None:
        # fee = (100 * 20 + 9999) // 10000 = 10119 // 10000 = 1
        assert calc_fee(100, 20) == 1

    def test_exact_division(self) -> None:
        # fee = (10000 * 20 + 9999) // 10000 = 209999 // 10000 = 20
        assert calc_fee(10000, 20) == 20

    def test_large_value(self) -> None:
        # 6500 cents * 20 bps ceiling
        assert calc_fee(6500, 20) == (6500 * 20 + 9999) // 10000
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_fee_calculation.py -v
```

**Step 3: Implement**
```python
# src/pm_clearing/domain/fee.py
"""Fee calculation — distinguishes NATIVE vs SYNTHETIC for fee base."""

def get_fee_trade_value(
    book_type: str, trade_price: int, quantity: int, buy_original_price: int
) -> int:
    """Return the trade value used as fee base.
    NATIVE orders: YES price × qty.
    SYNTHETIC_SELL (Buy NO): NO price × qty (buy_original_price).
    SYNTHETIC_BUY  (Sell NO): (100 - trade_price) × qty = NO price.
    """
    if book_type in ("NATIVE_BUY", "NATIVE_SELL"):
        return trade_price * quantity
    if book_type == "SYNTHETIC_SELL":
        return buy_original_price * quantity        # NO price stored in TradeResult
    # SYNTHETIC_BUY
    return (100 - trade_price) * quantity           # NO price = 100 - YES trade price


def calc_fee(trade_value: int, fee_bps: int) -> int:
    """Ceiling division fee: (trade_value × fee_bps + 9999) // 10000."""
    return (trade_value * fee_bps + 9999) // 10000
```

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_fee_calculation.py -v
# Expected: 7 passed
```

**Step 5: Commit**
```bash
git add src/pm_clearing/domain/fee.py tests/unit/test_fee_calculation.py
git commit -m "feat(pm_clearing): add fee calculation (NATIVE/SYNTHETIC, ceiling division)"
```

---
## Task 9: pm_clearing Ledger + WAL Infrastructure

**Files:**
- Create: `src/pm_clearing/infrastructure/ledger.py`
- Test: `tests/unit/test_clearing_ledger.py`

**Step 1: Write failing test**
```python
# tests/unit/test_clearing_ledger.py
from unittest.mock import AsyncMock, call
from src.pm_clearing.infrastructure.ledger import write_ledger, write_wal_event

class TestWriteLedger:
    async def test_executes_insert(self) -> None:
        mock_db = AsyncMock()
        await write_ledger(
            user_id="u1", entry_type="MINT_COST", amount=-6500,
            balance_after=93500, reference_type="TRADE", reference_id="t1", db=mock_db,
        )
        mock_db.execute.assert_called_once()

class TestWriteWalEvent:
    async def test_executes_insert(self) -> None:
        mock_db = AsyncMock()
        await write_wal_event(
            event_type="ORDER_ACCEPTED", order_id="o1",
            market_id="mkt-1", user_id="u1", payload={}, db=mock_db,
        )
        mock_db.execute.assert_called_once()
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_clearing_ledger.py -v
```

**Step 3: Implement**
```python
# src/pm_clearing/infrastructure/ledger.py
"""DB helpers for ledger_entries and wal_events — called from MatchingEngine within a transaction."""
import json
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.pm_common.id_generator import generate_id
from src.pm_common.datetime_utils import utc_now

_INSERT_LEDGER_SQL = text("""
    INSERT INTO ledger_entries
        (user_id, entry_type, amount, balance_after, reference_type, reference_id)
    VALUES (:user_id, :entry_type, :amount, :balance_after, :reference_type, :reference_id)
""")

_INSERT_WAL_SQL = text("""
    INSERT INTO wal_events (id, event_type, order_id, market_id, user_id, payload, created_at)
    VALUES (:id, :event_type, :order_id, :market_id, :user_id, :payload, :created_at)
""")

async def write_ledger(
    user_id: str, entry_type: str, amount: int, balance_after: int,
    reference_type: str, reference_id: str, db: AsyncSession,
) -> None:
    await db.execute(_INSERT_LEDGER_SQL, {
        "user_id": user_id, "entry_type": entry_type,
        "amount": amount, "balance_after": balance_after,
        "reference_type": reference_type, "reference_id": reference_id,
    })

async def write_wal_event(
    event_type: str, order_id: str, market_id: str,
    user_id: str, payload: dict, db: AsyncSession,
) -> None:
    await db.execute(_INSERT_WAL_SQL, {
        "id": generate_id(),
        "event_type": event_type,
        "order_id": order_id,
        "market_id": market_id,
        "user_id": user_id,
        "payload": json.dumps(payload),
        "created_at": utc_now(),
    })
```

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_clearing_ledger.py -v
```

**Step 5: Commit**
```bash
git add src/pm_clearing/infrastructure/ledger.py tests/unit/test_clearing_ledger.py
git commit -m "feat(pm_clearing): add ledger + WAL write helpers"
```

---

## Task 10: pm_clearing Scenarios + Dispatcher

**Files:**
- Create: `src/pm_clearing/domain/scenarios/mint.py`
- Create: `src/pm_clearing/domain/scenarios/transfer_yes.py`
- Create: `src/pm_clearing/domain/scenarios/transfer_no.py`
- Create: `src/pm_clearing/domain/scenarios/burn.py`
- Create: `src/pm_clearing/domain/service.py`
- Test: `tests/unit/test_scenario_clearing.py`

Tests use `AsyncMock` for all DB calls. Each scenario test verifies: correct account/position mutations, reserve_balance change, pnl_pool change.

**Key clearing logic (doc 03 §6.2–6.5):**

```
MINT (NB + SS):
  buyer:  frozen→debit trade_price×qty, gain YES volume+cost
  seller: frozen→debit (100-trade_price)×qty, gain NO volume+cost
  market: reserve += 100×qty, yes_shares += qty, no_shares += qty

TRANSFER_YES (NB + NS):
  buyer:  frozen→debit trade_price×qty, gain YES volume+cost
  seller: release pending_sell, lose YES volume (proportional cost), credit trade_price×qty
  market: pnl_pool -= (trade_price×qty - seller_cost_released)

TRANSFER_NO (SB + SS):
  buyer (Sell NO):  release pending_sell, lose NO volume, credit (100-trade_price)×qty
  seller (Buy NO):  frozen→debit (100-trade_price)×qty, gain NO volume+cost
  market: pnl_pool -= ((100-trade_price)×qty - buyer_cost_released)

BURN (SB + NS):
  buyer (Sell NO):  release pending_sell, lose NO volume
  seller (Sell YES): release pending_sell, lose YES volume
  market: reserve -= 100×qty, yes_shares -= qty, no_shares -= qty
          pnl_pool adjustments for both sides
```

Helper for cost release:
```python
def calc_released_cost(cost_sum: int, volume: int, qty: int) -> int:
    if qty == volume:
        return cost_sum  # full close — avoid dust
    return cost_sum * qty // volume
```

**Step 1: Write failing tests (mint + one transfer)**
```python
# tests/unit/test_scenario_clearing.py
from unittest.mock import AsyncMock, MagicMock
from src.pm_clearing.domain.scenarios.mint import clear_mint
from src.pm_clearing.domain.scenarios.transfer_yes import clear_transfer_yes
from src.pm_clearing.domain.service import settle_trade
from src.pm_matching.domain.models import TradeResult
from src.pm_common.enums import TradeScenario

def _trade(buy_type="NATIVE_BUY", sell_type="SYNTHETIC_SELL",
           price=65, qty=100, buy_orig=65) -> TradeResult:
    return TradeResult(
        buy_order_id="bo1", sell_order_id="so1",
        buy_user_id="buyer", sell_user_id="seller",
        market_id="mkt-1", price=price, quantity=qty,
        buy_book_type=buy_type, sell_book_type=sell_type,
        buy_original_price=buy_orig,
        maker_order_id="so1", taker_order_id="bo1",
    )

class TestClearMint:
    async def test_reserve_increases_by_100_per_share(self) -> None:
        trade = _trade("NATIVE_BUY", "SYNTHETIC_SELL", price=65, qty=100)
        market = MagicMock(reserve_balance=0, total_yes_shares=0, total_no_shares=0, pnl_pool=0)
        mock_db = AsyncMock()
        await clear_mint(trade, market, mock_db)
        assert market.reserve_balance == 10000   # 100×100
        assert market.total_yes_shares == 100
        assert market.total_no_shares == 100

class TestClearTransferYes:
    async def test_reserve_unchanged(self) -> None:
        trade = _trade("NATIVE_BUY", "NATIVE_SELL", price=65, qty=100)
        market = MagicMock(reserve_balance=10000, total_yes_shares=100,
                           total_no_shares=100, pnl_pool=0)
        mock_db = AsyncMock()
        # seller position mock
        mock_db.execute.return_value.fetchone.return_value = (100, 6500, 0)  # vol, cost, pending
        await clear_transfer_yes(trade, market, mock_db)
        assert market.reserve_balance == 10000   # unchanged

class TestSettleTrade:
    async def test_dispatches_to_mint(self) -> None:
        trade = _trade("NATIVE_BUY", "SYNTHETIC_SELL")
        market = MagicMock(reserve_balance=0, total_yes_shares=0, total_no_shares=0, pnl_pool=0)
        mock_db = AsyncMock()
        # Should not raise — basic dispatch check
        await settle_trade(trade, market, mock_db, fee_bps=20)
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_scenario_clearing.py -v
```

**Step 3: Implement clearing scenarios**

```python
# src/pm_clearing/domain/scenarios/mint.py
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.pm_matching.domain.models import TradeResult

_UNFREEZE_DEBIT_SQL = text("""
    UPDATE accounts
    SET frozen_balance    = frozen_balance    - :unfreeze,
        available_balance = available_balance + :refund,
        version = version + 1, updated_at = NOW()
    WHERE user_id = :user_id
""")
# refund = unfreeze - actual_cost: moves excess back to available

_ADD_YES_VOLUME_SQL = text("""
    INSERT INTO positions (user_id, market_id, yes_volume, yes_cost_sum)
    VALUES (:user_id, :market_id, :qty, :cost)
    ON CONFLICT (user_id, market_id) DO UPDATE
    SET yes_volume   = positions.yes_volume   + :qty,
        yes_cost_sum = positions.yes_cost_sum + :cost,
        updated_at = NOW()
""")

_ADD_NO_VOLUME_SQL = text("""
    INSERT INTO positions (user_id, market_id, no_volume, no_cost_sum)
    VALUES (:user_id, :market_id, :qty, :cost)
    ON CONFLICT (user_id, market_id) DO UPDATE
    SET no_volume   = positions.no_volume   + :qty,
        no_cost_sum = positions.no_cost_sum + :cost,
        updated_at = NOW()
""")

async def clear_mint(trade: TradeResult, market: object, db: AsyncSession) -> None:
    """MINT: NATIVE_BUY + SYNTHETIC_SELL → create YES/NO contract pair."""
    buyer_cost  = trade.price * trade.quantity
    seller_cost = (100 - trade.price) * trade.quantity

    # buyer: release frozen, debit YES cost, receive YES
    # (fee and refund handled separately in settle_trade)
    await db.execute(_UNFREEZE_DEBIT_SQL, {
        "user_id": trade.buy_user_id,
        "unfreeze": buyer_cost,   # baseline; fee portion handled by collect_fees
        "refund": 0,
    })
    await db.execute(_ADD_YES_VOLUME_SQL, {
        "user_id": trade.buy_user_id, "market_id": trade.market_id,
        "qty": trade.quantity, "cost": buyer_cost,
    })

    # seller (Buy NO): release frozen, debit NO cost, receive NO
    await db.execute(_UNFREEZE_DEBIT_SQL, {
        "user_id": trade.sell_user_id,
        "unfreeze": seller_cost,
        "refund": 0,
    })
    await db.execute(_ADD_NO_VOLUME_SQL, {
        "user_id": trade.sell_user_id, "market_id": trade.market_id,
        "qty": trade.quantity, "cost": seller_cost,
    })

    market.reserve_balance += trade.quantity * 100
    market.total_yes_shares += trade.quantity
    market.total_no_shares += trade.quantity
```

For `transfer_yes.py`, `transfer_no.py`, `burn.py`: follow the same SQL pattern. Key helper:

```python
# Shared helper used by all scenarios
def calc_released_cost(cost_sum: int, volume: int, qty: int) -> int:
    if qty >= volume:
        return cost_sum
    return cost_sum * qty // volume
```

The full implementation of all four scenarios closely follows doc 03 §6.2–6.5. For TRANSFER_YES:
- buyer: same as MINT buyer  
- seller: `REDUCE_YES_VOLUME` (yes_volume -= qty, yes_cost_sum -= released_cost, yes_pending_sell -= qty), then `ADD_BALANCE` (available += trade_price × qty)  
- market.pnl_pool -= (trade_price × qty - released_cost)

For BURN: both sides are "sellers" (Sell NO + Sell YES); reserve -= 100 × qty.

```python
# src/pm_clearing/domain/service.py
from sqlalchemy.ext.asyncio import AsyncSession
from src.pm_matching.domain.models import TradeResult
from src.pm_common.enums import TradeScenario
from src.pm_matching.engine.scenario import determine_scenario
from src.pm_clearing.domain.scenarios.mint import clear_mint
from src.pm_clearing.domain.scenarios.transfer_yes import clear_transfer_yes
from src.pm_clearing.domain.scenarios.transfer_no import clear_transfer_no
from src.pm_clearing.domain.scenarios.burn import clear_burn

async def settle_trade(
    trade: TradeResult, market: object, db: AsyncSession, fee_bps: int
) -> None:
    """Determine scenario and dispatch to the appropriate clearing function."""
    scenario = determine_scenario(trade.buy_book_type, trade.sell_book_type)
    if scenario == TradeScenario.MINT:
        await clear_mint(trade, market, db)
    elif scenario == TradeScenario.TRANSFER_YES:
        await clear_transfer_yes(trade, market, db)
    elif scenario == TradeScenario.TRANSFER_NO:
        await clear_transfer_no(trade, market, db)
    else:
        await clear_burn(trade, market, db)
    # Fee collection and refund handled by caller (MatchingEngine)
```

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_scenario_clearing.py -v
```

**Step 5: Commit**
```bash
git add src/pm_clearing/domain/scenarios/ src/pm_clearing/domain/service.py tests/unit/test_scenario_clearing.py
git commit -m "feat(pm_clearing): add 4 clearing scenarios (MINT/TRANSFER_YES/TRANSFER_NO/BURN) and dispatcher"
```

---

## Task 11: pm_clearing Auto-Netting

**Files:**
- Create: `src/pm_clearing/domain/netting.py`
- Test: `tests/unit/test_auto_netting.py`

**Step 1: Write failing test**
```python
# tests/unit/test_auto_netting.py
from unittest.mock import AsyncMock, MagicMock, patch
from src.pm_clearing.domain.netting import execute_netting_if_needed

class TestNetting:
    async def test_no_netting_when_no_both_sides(self) -> None:
        mock_db = AsyncMock()
        mock_db.execute.return_value.fetchone.return_value = (100, 6500, 0, 0, 0, 0)
        # yes=100 no=0: no netting
        market = MagicMock(reserve_balance=10000, pnl_pool=0)
        result = await execute_netting_if_needed("u1", "mkt-1", market, mock_db)
        assert result == 0

    async def test_netting_qty_excludes_pending_sell(self) -> None:
        # yes_volume=100, yes_pending_sell=80 → available_yes=20
        # no_volume=50, no_pending_sell=0 → available_no=50
        # nettable = min(20, 50) = 20
        mock_db = AsyncMock()
        mock_db.execute.return_value.fetchone.return_value = (100, 6500, 80, 50, 2500, 0)
        market = MagicMock(reserve_balance=20000, pnl_pool=500)
        result = await execute_netting_if_needed("u1", "mkt-1", market, mock_db)
        assert result == 20
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_auto_netting.py -v
```

**Step 3: Implement**
```python
# src/pm_clearing/domain/netting.py
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.pm_clearing.domain.fee import calc_released_cost  # reuse helper

_GET_POSITION_SQL = text("""
    SELECT yes_volume, yes_cost_sum, yes_pending_sell,
           no_volume,  no_cost_sum,  no_pending_sell
    FROM positions
    WHERE user_id = :user_id AND market_id = :market_id
    FOR UPDATE
""")

_UPDATE_POSITION_NET_SQL = text("""
    UPDATE positions
    SET yes_volume       = yes_volume       - :qty,
        yes_cost_sum     = yes_cost_sum     - :yes_cost,
        no_volume        = no_volume        - :qty,
        no_cost_sum      = no_cost_sum      - :no_cost,
        updated_at = NOW()
    WHERE user_id = :user_id AND market_id = :market_id
""")

_CREDIT_AVAILABLE_SQL = text("""
    UPDATE accounts
    SET available_balance = available_balance + :amount,
        version = version + 1, updated_at = NOW()
    WHERE user_id = :user_id
""")

async def execute_netting_if_needed(
    user_id: str, market_id: str, market: object, db: AsyncSession
) -> int:
    """Auto-net YES+NO positions. Returns qty netted (0 if nothing to net)."""
    row = (await db.execute(_GET_POSITION_SQL, {"user_id": user_id, "market_id": market_id})).fetchone()
    if row is None:
        return 0
    yes_vol, yes_cost, yes_pend, no_vol, no_cost, no_pend = row
    available_yes = yes_vol - yes_pend
    available_no  = no_vol  - no_pend
    nettable = min(available_yes, available_no)
    if nettable <= 0:
        return 0

    yes_cost_rel = calc_released_cost(yes_cost, yes_vol, nettable)
    no_cost_rel  = calc_released_cost(no_cost,  no_vol,  nettable)
    total_cost_released = yes_cost_rel + no_cost_rel
    refund = nettable * 100

    await db.execute(_UPDATE_POSITION_NET_SQL, {
        "user_id": user_id, "market_id": market_id,
        "qty": nettable, "yes_cost": yes_cost_rel, "no_cost": no_cost_rel,
    })
    await db.execute(_CREDIT_AVAILABLE_SQL, {"user_id": user_id, "amount": refund})

    market.reserve_balance -= refund
    market.pnl_pool        -= (refund - total_cost_released)

    return nettable
```

Note: `calc_released_cost` lives in `fee.py`. Move it to a shared `utils.py` or keep it in `fee.py` and import from there.

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_auto_netting.py -v
```

**Step 5: Commit**
```bash
git add src/pm_clearing/domain/netting.py tests/unit/test_auto_netting.py
git commit -m "feat(pm_clearing): add auto-netting with pnl_pool update"
```

---

## Task 12: pm_clearing Invariant Verification

**Files:**
- Create: `src/pm_clearing/domain/invariants.py`
- Test: `tests/unit/test_invariants.py`

**Step 1: Write failing test**
```python
# tests/unit/test_invariants.py
from unittest.mock import AsyncMock, MagicMock
from src.pm_clearing.domain.invariants import verify_invariants_after_trade

class TestInvariants:
    async def test_passes_when_all_ok(self) -> None:
        market = MagicMock(total_yes_shares=100, total_no_shares=100,
                           reserve_balance=10000, pnl_pool=500)
        mock_db = AsyncMock()
        # total cost_sum = 10500 = reserve(10000) + pnl_pool(500)
        mock_db.execute.return_value.scalar_one.return_value = 10500
        await verify_invariants_after_trade(market, mock_db)  # no exception

    async def test_inv1_fail_raises(self) -> None:
        market = MagicMock(total_yes_shares=100, total_no_shares=99,
                           reserve_balance=10000, pnl_pool=0)
        mock_db = AsyncMock()
        import pytest
        with pytest.raises(AssertionError, match=r"INV-1"):
            await verify_invariants_after_trade(market, mock_db)
```

**Step 2: Implement**
```python
# src/pm_clearing/domain/invariants.py
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_SUM_COST_SQL = text("""
    SELECT COALESCE(SUM(yes_cost_sum + no_cost_sum), 0)
    FROM positions WHERE market_id = :market_id
""")

async def verify_invariants_after_trade(market: object, db: AsyncSession) -> None:
    """Check INV-1, INV-2, INV-3. Raises AssertionError on violation (triggers tx rollback)."""
    assert market.total_yes_shares == market.total_no_shares, \
        f"[INV-1] YES shares {market.total_yes_shares} != NO shares {market.total_no_shares}"

    expected_reserve = market.total_yes_shares * 100
    assert market.reserve_balance == expected_reserve, \
        f"[INV-2] reserve {market.reserve_balance} != expected {expected_reserve}"

    total_cost = (await db.execute(_SUM_COST_SQL, {"market_id": market.id})).scalar_one()
    assert market.reserve_balance + market.pnl_pool == total_cost, \
        f"[INV-3] reserve+pnl_pool={market.reserve_balance + market.pnl_pool} != Σcost={total_cost}"
```

**Step 3: Run to verify PASS**
```bash
uv run pytest tests/unit/test_invariants.py -v
```

**Step 4: Commit**
```bash
git add src/pm_clearing/domain/invariants.py tests/unit/test_invariants.py
git commit -m "feat(pm_clearing): add post-trade invariant verification (INV-1/2/3)"
```

---

## Task 13: pm_clearing settle_market

**Files:**
- Create: `src/pm_clearing/domain/settlement.py`
- Test: `tests/unit/test_settle_market.py`

```python
# src/pm_clearing/domain/settlement.py
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.pm_common.datetime_utils import utc_now

_GET_POSITIONS_SQL = text("SELECT user_id, yes_volume, no_volume FROM positions WHERE market_id = :market_id")
_CREDIT_SQL = text("UPDATE accounts SET available_balance = available_balance + :amount, version = version + 1, updated_at = NOW() WHERE user_id = :user_id")
_CLEAR_POSITION_SQL = text("UPDATE positions SET yes_volume=0, yes_cost_sum=0, yes_pending_sell=0, no_volume=0, no_cost_sum=0, no_pending_sell=0, updated_at=NOW() WHERE user_id=:user_id AND market_id=:market_id")
_SETTLE_MARKET_SQL = text("UPDATE markets SET status='SETTLED', reserve_balance=0, pnl_pool=0, total_yes_shares=0, total_no_shares=0, resolution_result=:result, settled_at=:settled_at WHERE id=:market_id")

async def settle_market(
    market_id: str, outcome: str, db: AsyncSession
) -> None:
    """Phase 1-5: cancel orders (caller responsibility), pay out winners, zero market."""
    rows = (await db.execute(_GET_POSITIONS_SQL, {"market_id": market_id})).fetchall()
    total_payout = 0
    for user_id, yes_vol, no_vol in rows:
        winning_shares = yes_vol if outcome == "YES" else no_vol
        if winning_shares > 0:
            payout = winning_shares * 100
            await db.execute(_CREDIT_SQL, {"user_id": user_id, "amount": payout})
            total_payout += payout
        await db.execute(_CLEAR_POSITION_SQL, {"user_id": user_id, "market_id": market_id})
    await db.execute(_SETTLE_MARKET_SQL, {
        "market_id": market_id, "result": outcome, "settled_at": utc_now()
    })
```

Tests verify YES/NO outcome payout and reserve == total_payout assertion.

```bash
uv run pytest tests/unit/test_settle_market.py -v
git add src/pm_clearing/domain/settlement.py tests/unit/test_settle_market.py
git commit -m "feat(pm_clearing): add settle_market (YES/NO outcome payout)"
```

---

## Task 14: pm_order DB Models + Repository

**Files:**
- Create: `src/pm_order/infrastructure/db_models.py`
- Create: `src/pm_order/domain/repository.py`
- Create: `src/pm_order/infrastructure/persistence.py`
- Test: `tests/unit/test_order_persistence.py`

**Step 1: DB Models**
```python
# src/pm_order/infrastructure/db_models.py
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, Integer, SmallInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column
from src.pm_common.database import Base

class OrderORM(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    original_side: Mapped[str] = mapped_column(String(10), nullable=False)
    original_direction: Mapped[str] = mapped_column(String(10), nullable=False)
    original_price: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    book_type: Mapped[str] = mapped_column(String(20), nullable=False)
    book_direction: Mapped[str] = mapped_column(String(10), nullable=False)
    book_price: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    price_type: Mapped[str] = mapped_column(String(10), nullable=False, default="LIMIT")
    time_in_force: Mapped[str] = mapped_column(String(3), nullable=False, default="GTC")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remaining_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    frozen_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    frozen_asset_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    cancel_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

**Step 2: Repository Protocol + Persistence**
```python
# src/pm_order/domain/repository.py
from typing import Protocol
from sqlalchemy.ext.asyncio import AsyncSession
from src.pm_order.domain.models import Order

class OrderRepositoryProtocol(Protocol):
    async def save(self, order: Order, db: AsyncSession) -> None: ...
    async def get_by_id(self, order_id: str, db: AsyncSession) -> Order | None: ...
    async def get_by_client_order_id(self, client_order_id: str, user_id: str, db: AsyncSession) -> Order | None: ...
    async def update_status(self, order: Order, db: AsyncSession) -> None: ...
    async def list_by_user(self, user_id: str, market_id: str | None, statuses: list[str] | None, limit: int, cursor_id: str | None, db: AsyncSession) -> list[Order]: ...
```

```python
# src/pm_order/infrastructure/persistence.py — key SQL patterns
_INSERT_ORDER_SQL = text("""
    INSERT INTO orders (id, client_order_id, market_id, user_id,
        original_side, original_direction, original_price,
        book_type, book_direction, book_price, price_type, time_in_force,
        quantity, filled_quantity, remaining_quantity,
        frozen_amount, frozen_asset_type, status)
    VALUES (:id, :client_order_id, :market_id, :user_id,
        :original_side, :original_direction, :original_price,
        :book_type, :book_direction, :book_price, 'LIMIT', :time_in_force,
        :quantity, 0, :quantity, :frozen_amount, :frozen_asset_type, :status)
""")

_UPDATE_ORDER_SQL = text("""
    UPDATE orders
    SET status = :status, filled_quantity = :filled_quantity,
        remaining_quantity = :remaining_quantity,
        frozen_amount = :frozen_amount, updated_at = NOW()
    WHERE id = :id
""")

# List with cursor: id < :cursor_id ORDER BY id DESC LIMIT :limit
_LIST_ORDERS_SQL = text("""
    SELECT id, client_order_id, market_id, user_id,
           original_side, original_direction, original_price,
           book_type, book_direction, book_price, time_in_force,
           quantity, filled_quantity, remaining_quantity,
           frozen_amount, frozen_asset_type, status, created_at, updated_at
    FROM orders
    WHERE user_id = :user_id
      AND (:market_id IS NULL OR market_id = :market_id)
      AND (:statuses IS NULL OR status = ANY(:statuses))
      AND (:cursor_id IS NULL OR id < :cursor_id)
    ORDER BY id DESC
    LIMIT :limit
""")
```

**Step 3: Write unit tests with AsyncMock**
```python
# tests/unit/test_order_persistence.py
from unittest.mock import AsyncMock
from src.pm_order.infrastructure.persistence import OrderRepository
from src.pm_order.domain.models import Order
# ... test save, get_by_id, list_by_user
```

```bash
uv run pytest tests/unit/test_order_persistence.py -v
git add src/pm_order/infrastructure/ src/pm_order/domain/repository.py tests/unit/test_order_persistence.py
git commit -m "feat(pm_order): add OrderORM, repository protocol, and persistence"
```

---
## Task 15: pm_matching MatchingEngine (Full Orchestrator)

**Files:**
- Create: `src/pm_matching/engine/engine.py`
- Create: `src/pm_matching/application/service.py`
- Test: `tests/unit/test_matching_engine.py` (unit, mocked DB)

This is the most complex task. The MatchingEngine holds per-market `asyncio.Lock` and `OrderBook` dict. It orchestrates the full chain within a single DB transaction.

**Step 1: Write failing test (basic place_order)**
```python
# tests/unit/test_matching_engine.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.pm_matching.engine.engine import MatchingEngine

@pytest.fixture
def engine() -> MatchingEngine:
    return MatchingEngine()

class TestMatchingEngineInit:
    def test_engine_starts_empty(self, engine: MatchingEngine) -> None:
        assert len(engine._orderbooks) == 0
        assert len(engine._market_locks) == 0
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_matching_engine.py -v
```

**Step 3: Implement MatchingEngine**
```python
# src/pm_matching/engine/engine.py
"""MatchingEngine — stateful orchestrator for per-market order placement."""
import asyncio
import logging
from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_clearing.domain.invariants import verify_invariants_after_trade
from src.pm_clearing.domain.netting import execute_netting_if_needed
from src.pm_clearing.domain.service import settle_trade
from src.pm_clearing.infrastructure.ledger import write_wal_event
from src.pm_matching.engine.matching_algo import match_order
from src.pm_matching.engine.order_book import OrderBook
from src.pm_order.domain.models import Order
from src.pm_order.domain.transformer import transform_order
from src.pm_risk.rules.balance_check import TAKER_FEE_BPS, _calc_max_fee, check_and_freeze
from src.pm_risk.rules.market_status import check_market_active
from src.pm_risk.rules.order_limit import check_order_limit
from src.pm_risk.rules.price_range import check_price_range
from src.pm_common.errors import AppError
from src.pm_common.id_generator import generate_id
from src.pm_common.datetime_utils import utc_now

logger = logging.getLogger(__name__)

_GET_MARKET_SQL = text("""
    SELECT id, status, reserve_balance, pnl_pool, total_yes_shares, total_no_shares
    FROM markets WHERE id = :market_id FOR UPDATE
""")

_UPDATE_MARKET_SQL = text("""
    UPDATE markets
    SET reserve_balance = :reserve_balance,
        pnl_pool = :pnl_pool,
        total_yes_shares = :total_yes_shares,
        total_no_shares  = :total_no_shares,
        updated_at = NOW()
    WHERE id = :id
""")


class MarketState:
    """In-memory view of market row; mutated during clearing, flushed at end."""
    def __init__(self, row: object) -> None:
        self.id: str = row.id
        self.status: str = row.status
        self.reserve_balance: int = row.reserve_balance
        self.pnl_pool: int = row.pnl_pool
        self.total_yes_shares: int = row.total_yes_shares
        self.total_no_shares: int = row.total_no_shares


class MatchingEngine:
    def __init__(self) -> None:
        self._orderbooks: dict[str, OrderBook] = {}  # noqa: PLW0603
        self._market_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)  # noqa: PLW0603

    def _get_or_create_lock(self, market_id: str) -> asyncio.Lock:
        return self._market_locks[market_id]

    def _get_or_create_orderbook(self, market_id: str) -> OrderBook:
        if market_id not in self._orderbooks:
            self._orderbooks[market_id] = OrderBook(market_id=market_id)
        return self._orderbooks[market_id]

    async def rebuild_orderbook(self, market_id: str, db: AsyncSession) -> None:
        """Lazy rebuild from DB on startup or after error recovery."""
        from sqlalchemy import text as t
        rows = (await db.execute(
            t("SELECT id, user_id, book_type, book_direction, book_price, remaining_quantity, created_at FROM orders WHERE market_id = :mid AND status IN ('OPEN','PARTIALLY_FILLED') ORDER BY created_at ASC"),
            {"mid": market_id}
        )).fetchall()
        ob = OrderBook(market_id=market_id)
        from src.pm_matching.domain.models import BookOrder
        for row in rows:
            bo = BookOrder(order_id=row.id, user_id=row.user_id,
                           book_type=row.book_type, quantity=row.remaining_quantity,
                           created_at=row.created_at)
            ob.add_order(bo, price=row.book_price, side=row.book_direction)
        self._orderbooks[market_id] = ob

    async def place_order(
        self, order: Order, repo: object, db: AsyncSession
    ) -> tuple[Order, list, int]:
        """Main entry point. Returns (order, trades, netting_qty)."""
        lock = self._get_or_create_lock(order.market_id)
        async with lock:
            try:
                async with db.begin():
                    return await self._place_order_inner(order, repo, db)
            except Exception:
                # Evict orderbook — will lazy-rebuild on next request
                self._orderbooks.pop(order.market_id, None)
                raise

    async def _place_order_inner(
        self, order: Order, repo: object, db: AsyncSession
    ) -> tuple[Order, list, int]:
        # Risk checks
        await check_market_active(order.market_id, db)
        check_price_range(order.original_price)
        check_order_limit(order.quantity)

        # Transform
        book_type, book_dir, book_price = transform_order(
            order.original_side, order.original_direction, order.original_price
        )
        order.book_type = book_type
        order.book_direction = book_dir
        order.book_price = book_price

        # Freeze
        await check_and_freeze(order, db)

        # Save order to DB
        await repo.save(order, db)

        # WAL: ORDER_ACCEPTED
        await write_wal_event("ORDER_ACCEPTED", order.id, order.market_id, order.user_id, {}, db)

        # Load market row FOR UPDATE
        market_row = (await db.execute(_GET_MARKET_SQL, {"market_id": order.market_id})).fetchone()
        market = MarketState(market_row)

        # Match
        ob = self._get_or_create_orderbook(order.market_id)
        trade_results = match_order(order, ob)

        # Clear each fill
        trades_db = []
        netting_qty = 0
        self_trade_skipped = 0
        for tr in trade_results:
            await settle_trade(tr, market, db, fee_bps=TAKER_FEE_BPS)
            _sync_frozen_amount(order, order.remaining_quantity)
            await repo.update_status(order, db)
            # Netting for buyer in MINT and TRANSFER_YES
            nq = await execute_netting_if_needed(tr.buy_user_id, order.market_id, market, db)
            netting_qty += nq
            await write_wal_event("ORDER_MATCHED", order.id, order.market_id, order.user_id, {"trade_qty": tr.quantity}, db)
            trades_db.append(tr)

        # Finalize
        await self._finalize_order(order, ob, db, repo, self_trade_skipped)

        # Invariants
        await verify_invariants_after_trade(market, db)

        # Flush market row
        await db.execute(_UPDATE_MARKET_SQL, {
            "id": market.id,
            "reserve_balance": market.reserve_balance,
            "pnl_pool": market.pnl_pool,
            "total_yes_shares": market.total_yes_shares,
            "total_no_shares": market.total_no_shares,
        })

        return order, trades_db, netting_qty

    async def _finalize_order(
        self, order: Order, ob: OrderBook, db: AsyncSession, repo: object, self_trade_skipped: int
    ) -> None:
        if order.remaining_quantity > 0:
            if order.time_in_force == "GTC":
                from src.pm_matching.domain.models import BookOrder
                bo = BookOrder(order_id=order.id, user_id=order.user_id,
                               book_type=order.book_type,
                               quantity=order.remaining_quantity,
                               created_at=order.created_at or utc_now())
                ob.add_order(bo, price=order.book_price, side=order.book_direction)
                if order.filled_quantity > 0:
                    await write_wal_event("ORDER_PARTIALLY_FILLED", order.id,
                                          order.market_id, order.user_id, {}, db)
            else:  # IOC
                if order.filled_quantity == 0 and self_trade_skipped > 0:
                    raise AppError(4003, "Self-trade prevented all fills for IOC order", http_status=400)
                await self._unfreeze_remainder(order, db)
                order.status = "CANCELLED"
                await repo.update_status(order, db)
                await write_wal_event("ORDER_EXPIRED", order.id, order.market_id, order.user_id, {}, db)

    async def _unfreeze_remainder(self, order: Order, db: AsyncSession) -> None:
        from src.pm_risk.rules.balance_check import _FREEZE_FUNDS_SQL
        from sqlalchemy import text as t
        if order.frozen_asset_type == "FUNDS":
            await db.execute(t("""
                UPDATE accounts SET available_balance=available_balance+:amount,
                frozen_balance=frozen_balance-:amount, version=version+1, updated_at=NOW()
                WHERE user_id=:user_id
            """), {"user_id": order.user_id, "amount": order.frozen_amount})
        elif order.frozen_asset_type == "YES_SHARES":
            await db.execute(t("UPDATE positions SET yes_pending_sell=yes_pending_sell-:qty, updated_at=NOW() WHERE user_id=:user_id AND market_id=:market_id"),
                {"user_id": order.user_id, "market_id": order.market_id, "qty": order.remaining_quantity})
        else:
            await db.execute(t("UPDATE positions SET no_pending_sell=no_pending_sell-:qty, updated_at=NOW() WHERE user_id=:user_id AND market_id=:market_id"),
                {"user_id": order.user_id, "market_id": order.market_id, "qty": order.remaining_quantity})

    async def cancel_order(
        self, order_id: str, user_id: str, repo: object, db: AsyncSession
    ) -> Order:
        order = await repo.get_by_id(order_id, db)
        if order is None:
            raise AppError(4004, "Order not found", http_status=404)
        if order.user_id != user_id:
            raise AppError(403, "Forbidden", http_status=403)
        if not order.is_cancellable:
            raise AppError(4006, "Order cannot be cancelled", http_status=422)

        lock = self._get_or_create_lock(order.market_id)
        async with lock:
            try:
                async with db.begin():
                    ob = self._get_or_create_orderbook(order.market_id)
                    ob.cancel_order(order_id)
                    await self._unfreeze_remainder(order, db)
                    order.status = "CANCELLED"
                    await repo.update_status(order, db)
                    await write_wal_event("ORDER_CANCELLED", order.id, order.market_id, order.user_id, {}, db)
                    return order
            except AppError:
                raise
            except Exception:
                self._orderbooks.pop(order.market_id, None)
                raise


def _sync_frozen_amount(order: Order, remaining_qty: int) -> None:
    """Overwrite frozen_amount after each fill (doc §5.2: avoid cumulative rounding errors)."""
    if order.frozen_asset_type == "FUNDS":
        if order.book_type == "NATIVE_BUY":
            remaining_value = order.book_price * remaining_qty
        else:  # SYNTHETIC_SELL
            remaining_value = order.original_price * remaining_qty
        order.frozen_amount = remaining_value + _calc_max_fee(remaining_value)
    else:
        order.frozen_amount = remaining_qty
```

**Step 4: Run engine tests**
```bash
uv run pytest tests/unit/test_matching_engine.py -v
```

**Step 5: Application service singleton**
```python
# src/pm_matching/application/service.py
from src.pm_matching.engine.engine import MatchingEngine

_engine: MatchingEngine | None = None  # noqa: PLW0603

def get_matching_engine() -> MatchingEngine:
    global _engine  # noqa: PLW0603
    if _engine is None:
        _engine = MatchingEngine()
    return _engine
```

**Step 6: Commit**
```bash
git add src/pm_matching/engine/engine.py src/pm_matching/application/service.py tests/unit/test_matching_engine.py
git commit -m "feat(pm_matching): add MatchingEngine orchestrator with per-market lock and full placement chain"
```

---

## Task 16: pm_order Application Schemas + Service + REST API

**Files:**
- Create: `src/pm_order/application/schemas.py`
- Create: `src/pm_order/application/service.py`
- Create: `src/pm_order/api/router.py`
- Modify: `src/main.py`
- Test: `tests/unit/test_order_schemas.py`

**Step 1: Schemas**
```python
# src/pm_order/application/schemas.py
from pydantic import BaseModel, field_validator
from typing import Literal

class PlaceOrderRequest(BaseModel):
    client_order_id: str
    market_id: str
    side: Literal["YES", "NO"]
    direction: Literal["BUY", "SELL"]
    price_cents: int
    quantity: int
    time_in_force: Literal["GTC", "IOC"] = "GTC"

    @field_validator("client_order_id")
    @classmethod
    def no_whitespace(cls, v: str) -> str:
        if not v or v != v.strip() or " " in v:
            raise ValueError("client_order_id must not contain whitespace")
        return v

class TradeResponse(BaseModel):
    buy_order_id: str
    sell_order_id: str
    price: int
    quantity: int
    scenario: str

class OrderResponse(BaseModel):
    id: str
    client_order_id: str
    market_id: str
    side: str
    direction: str
    price_cents: int
    quantity: int
    filled_quantity: int
    remaining_quantity: int
    status: str
    time_in_force: str

class PlaceOrderResponse(BaseModel):
    order: OrderResponse
    trades: list[TradeResponse]
    netting_result: dict | None

class CancelOrderResponse(BaseModel):
    order_id: str
    unfrozen_amount: int
    unfrozen_asset_type: str

class OrderListResponse(BaseModel):
    orders: list[OrderResponse]
    next_cursor: str | None
```

**Step 2: Application service (thin wrapper)**
```python
# src/pm_order/application/service.py
from sqlalchemy.ext.asyncio import AsyncSession
from src.pm_matching.application.service import get_matching_engine
from src.pm_order.application.schemas import PlaceOrderRequest, PlaceOrderResponse, CancelOrderResponse
from src.pm_order.domain.models import Order
from src.pm_order.infrastructure.persistence import OrderRepository
from src.pm_common.id_generator import generate_id
from src.pm_common.datetime_utils import utc_now

_repo = OrderRepository()

async def place_order(req: PlaceOrderRequest, user_id: str, db: AsyncSession) -> PlaceOrderResponse:
    # Idempotency check
    existing = await _repo.get_by_client_order_id(req.client_order_id, user_id, db)
    if existing:
        from src.pm_common.errors import AppError
        # Compare key fields
        if (existing.original_side != req.side or existing.original_direction != req.direction
                or existing.original_price != req.price_cents or existing.quantity != req.quantity):
            raise AppError(4005, "Idempotency conflict: same client_order_id, different payload", http_status=409)
        return _build_response(existing, [], None)

    order = Order(
        id=generate_id(), client_order_id=req.client_order_id,
        market_id=req.market_id, user_id=user_id,
        original_side=req.side, original_direction=req.direction,
        original_price=req.price_cents,
        book_type="", book_direction="", book_price=0,
        quantity=req.quantity, time_in_force=req.time_in_force,
        status="OPEN", created_at=utc_now(), updated_at=utc_now(),
    )
    engine = get_matching_engine()
    order, trades, netting_qty = await engine.place_order(order, _repo, db)
    netting = {"netting_qty": netting_qty, "refund_amount": netting_qty * 100} if netting_qty else None
    return _build_response(order, trades, netting)
```

**Step 3: REST Router**
```python
# src/pm_order/api/router.py
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.pm_common.database import get_db
from src.pm_gateway.auth.dependencies import get_current_user
from src.pm_order.application import service as svc
from src.pm_order.application.schemas import (
    PlaceOrderRequest, PlaceOrderResponse, CancelOrderResponse, OrderListResponse
)

router = APIRouter(prefix="/orders", tags=["orders"])

@router.post("", response_model=PlaceOrderResponse, status_code=201)
async def place_order(
    req: PlaceOrderRequest,
    user_id: str = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PlaceOrderResponse:
    return await svc.place_order(req, user_id, db)

@router.post("/{order_id}/cancel", response_model=CancelOrderResponse)
async def cancel_order(
    order_id: str,
    user_id: str = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CancelOrderResponse:
    return await svc.cancel_order(order_id, user_id, db)

@router.get("", response_model=OrderListResponse)
async def list_orders(
    market_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
    user_id: str = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrderListResponse:
    return await svc.list_orders(user_id, market_id, status, limit, cursor, db)

@router.get("/{order_id}")
async def get_order(
    order_id: str,
    user_id: str = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await svc.get_order(order_id, user_id, db)
```

**Step 4: Wire into main.py**
```python
# Add to src/main.py (after existing imports):
from src.pm_order.api.router import router as order_router
# Add after existing app.include_router calls:
app.include_router(order_router, prefix="/api/v1")
```

**Step 5: Idempotency + schema unit tests**
```python
# tests/unit/test_order_schemas.py
import pytest
from pydantic import ValidationError
from src.pm_order.application.schemas import PlaceOrderRequest

class TestPlaceOrderRequest:
    def test_valid_request(self) -> None:
        req = PlaceOrderRequest(client_order_id="abc", market_id="m1",
                                side="YES", direction="BUY", price_cents=65, quantity=100)
        assert req.time_in_force == "GTC"

    def test_whitespace_in_client_order_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            PlaceOrderRequest(client_order_id="has space", market_id="m1",
                              side="YES", direction="BUY", price_cents=65, quantity=100)
```

```bash
uv run pytest tests/unit/test_order_schemas.py -v
git add src/pm_order/application/ src/pm_order/api/ tests/unit/test_order_schemas.py
git commit -m "feat(pm_order): add Pydantic schemas, thin application service, REST API router"
```

**Step 6: Wire main.py and verify startup**
```bash
git add src/main.py
git commit -m "feat: wire pm_order router into main.py"
uv run uvicorn src.main:app --port 8000 &
curl http://localhost:8000/health
# Expected: {"status": "ok", ...}
```

---

## Task 17: Integration Tests

**Files:**
- Create: `tests/integration/test_order_flow.py`
- Requires: running PostgreSQL, seed data (existing fixtures)

**Step 1: Write integration tests**
```python
# tests/integration/test_order_flow.py
"""Full chain integration: place order → match → clear → verify balance/position/ledger."""
import pytest
pytestmark = pytest.mark.asyncio(loop_scope="session")

class TestPlaceOrderIntegration:
    async def test_gtc_buy_yes_no_match_hangs_in_book(self, client, auth_headers) -> None:
        resp = await client.post("/api/v1/orders", json={
            "client_order_id": "test-gtc-1",
            "market_id": "<active_market_id>",
            "side": "YES", "direction": "BUY",
            "price_cents": 50, "quantity": 10,
        }, headers=auth_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["order"]["status"] == "OPEN"
        assert data["trades"] == []

    async def test_mint_scenario_creates_positions(self, client, auth_headers_a, auth_headers_b) -> None:
        # User A: Buy YES @65 (resting)
        await client.post("/api/v1/orders", json={
            "client_order_id": "mint-a", "market_id": "<active>",
            "side": "YES", "direction": "BUY", "price_cents": 65, "quantity": 10,
        }, headers=auth_headers_a)
        # User B: Buy NO @35 → SYNTHETIC_SELL @65 → MINT
        resp = await client.post("/api/v1/orders", json={
            "client_order_id": "mint-b", "market_id": "<active>",
            "side": "NO", "direction": "BUY", "price_cents": 35, "quantity": 10,
        }, headers=auth_headers_b)
        assert resp.status_code == 201
        data = resp.json()
        assert len(data["trades"]) == 1
        assert data["trades"][0]["scenario"] == "MINT"

    async def test_cancel_order_unfreezes_funds(self, client, auth_headers) -> None:
        # place then cancel
        place_resp = await client.post("/api/v1/orders", json={
            "client_order_id": "cancel-test", "market_id": "<active>",
            "side": "YES", "direction": "BUY", "price_cents": 30, "quantity": 5,
        }, headers=auth_headers)
        order_id = place_resp.json()["order"]["id"]
        cancel_resp = await client.post(f"/api/v1/orders/{order_id}/cancel", headers=auth_headers)
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["unfrozen_asset_type"] == "FUNDS"

    async def test_idempotency_same_payload_returns_200(self, client, auth_headers) -> None:
        payload = {"client_order_id": "idem-1", "market_id": "<active>",
                   "side": "YES", "direction": "BUY", "price_cents": 40, "quantity": 3}
        r1 = await client.post("/api/v1/orders", json=payload, headers=auth_headers)
        r2 = await client.post("/api/v1/orders", json=payload, headers=auth_headers)
        assert r1.status_code == 201
        assert r2.status_code == 200
        assert r1.json()["order"]["id"] == r2.json()["order"]["id"]
```

**Step 2: Run integration tests**
```bash
uv run pytest tests/integration/test_order_flow.py -v
# Expected: 4+ passed
```

**Step 3: Run full test suite + lint checks**
```bash
uv run pytest tests/ -v
uv run ruff check src/ tests/
uv run mypy src/
# Expected: all passed, no errors
```

**Step 4: Final commit**
```bash
git add tests/integration/test_order_flow.py
git commit -m "test(integration): add order placement, cancel, mint scenario, idempotency tests"
```

---

## Execution Options

Plan saved to `Planning/Implementation/2026-02-21-pm-order-plan.md`.

**1. Subagent-Driven (this session)** — dispatch a fresh subagent per task, review between tasks, fast iteration. Requires `superpowers:subagent-driven-development`.

**2. Parallel Session (separate)** — open a new session with `superpowers:executing-plans`, batch execution with checkpoints.

Which approach would you like?
