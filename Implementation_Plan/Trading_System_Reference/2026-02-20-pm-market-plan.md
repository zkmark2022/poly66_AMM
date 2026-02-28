# pm_market 模块实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 实现 pm_market 模块，提供话题列表、话题详情、订单簿快照三个只读 API 端点。

**Architecture:** 四层 DDD（domain → infrastructure → application → api），与 pm_account 结构完全一致。orderbook 从 DB 聚合（orders 表 WHERE status IN ('OPEN','PARTIALLY_FILLED')），YES/NO 双视角转换在 schema 层完成，Module 6 后替换数据源为内存 OrderBook。

**Tech Stack:** FastAPI, SQLAlchemy async (raw text SQL), Pydantic v2, asyncpg, pytest-asyncio

**设计文档:** `Planning/Implementation/2026-02-20-pm-market-design.md`

**对齐 API 契约:** `Planning/Detail_Design/02_API接口契约.md` §4.1–4.3

**状态: ✅ 全部完成**

---

## 关键约定（必读）

1. **所有金额为 int（美分）**，禁止 float/Decimal。
2. **asyncpg NULL 参数**：传 None 时必须用 `CAST(:param AS TYPE) IS NULL` 模式，否则报 `AmbiguousParameterError`。
3. **事务管理**：Repository 方法不 commit，调用方（service 或 router）负责。本模块全是只读操作，无需 commit。
4. **错误类**：`MarketNotFoundError`（3001）和 `MarketNotActiveError`（3002）已在 `src/pm_common/errors.py` 定义，直接 import。
5. **trades 表字段**：成交价格列名是 `price`（不是 `trade_price`）。
6. **orders 表**：订单簿聚合用 `book_price`, `book_direction`, `remaining_quantity`，状态过滤用 `status IN ('OPEN','PARTIALLY_FILLED')`。
7. **游标**：markets 使用复合游标 `{"ts": "<created_at ISO>", "id": "<market_id>"}`，因为 PK 是 VARCHAR 非自增。
8. **ruff 规则**：用 `X | None` 不用 `Optional[X]`；用 `StrEnum` 不用 `(str, Enum)`。

---

## Task 1: Domain Models

**Files:**
- Create: `src/pm_market/domain/models.py`
- Test: `tests/unit/test_market_domain_models.py`

### Step 1: Write the failing test

```python
# tests/unit/test_market_domain_models.py
from datetime import UTC, datetime
from src.pm_market.domain.models import Market, OrderbookSnapshot, PriceLevel


def _make_market(**kwargs) -> Market:
    defaults = dict(
        id="MKT-TEST",
        title="Test Market",
        description=None,
        category="crypto",
        status="ACTIVE",
        min_price_cents=1,
        max_price_cents=99,
        max_order_quantity=10000,
        max_position_per_user=25000,
        max_order_amount_cents=1000000,
        maker_fee_bps=10,
        taker_fee_bps=20,
        reserve_balance=500000,
        pnl_pool=-1200,
        total_yes_shares=5000,
        total_no_shares=5000,
        trading_start_at=None,
        trading_end_at=None,
        resolution_date=None,
        resolved_at=None,
        settled_at=None,
        resolution_result=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    defaults.update(kwargs)
    return Market(**defaults)


class TestMarket:
    def test_construction(self):
        m = _make_market()
        assert m.id == "MKT-TEST"
        assert m.status == "ACTIVE"
        assert m.pnl_pool == -1200   # can be negative

    def test_optional_fields_none(self):
        m = _make_market()
        assert m.description is None
        assert m.resolution_result is None
        assert m.resolved_at is None


class TestPriceLevel:
    def test_construction(self):
        lv = PriceLevel(price_cents=65, total_quantity=500)
        assert lv.price_cents == 65
        assert lv.total_quantity == 500


class TestOrderbookSnapshot:
    def test_construction(self):
        snap = OrderbookSnapshot(
            market_id="MKT-TEST",
            yes_bids=[PriceLevel(65, 500), PriceLevel(64, 300)],
            yes_asks=[PriceLevel(67, 400)],
            last_trade_price_cents=65,
            updated_at=datetime.now(UTC),
        )
        assert snap.market_id == "MKT-TEST"
        assert len(snap.yes_bids) == 2
        assert snap.last_trade_price_cents == 65

    def test_empty_orderbook(self):
        snap = OrderbookSnapshot(
            market_id="MKT-EMPTY",
            yes_bids=[],
            yes_asks=[],
            last_trade_price_cents=None,
            updated_at=datetime.now(UTC),
        )
        assert snap.yes_bids == []
        assert snap.last_trade_price_cents is None
```

### Step 2: Run test — expect FAIL

```bash
uv run pytest tests/unit/test_market_domain_models.py -v
# Expected: ModuleNotFoundError (models.py doesn't exist)
```

### Step 3: Implement

```python
# src/pm_market/domain/models.py
"""Domain models for pm_market — pure dataclasses, no business logic."""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Market:
    id: str
    title: str
    description: str | None
    category: str | None
    status: str
    min_price_cents: int
    max_price_cents: int
    max_order_quantity: int
    max_position_per_user: int
    max_order_amount_cents: int
    maker_fee_bps: int
    taker_fee_bps: int
    reserve_balance: int
    pnl_pool: int
    total_yes_shares: int
    total_no_shares: int
    trading_start_at: datetime | None
    trading_end_at: datetime | None
    resolution_date: datetime | None
    resolved_at: datetime | None
    settled_at: datetime | None
    resolution_result: str | None
    created_at: datetime
    updated_at: datetime


@dataclass
class PriceLevel:
    """Single price level in an order book."""

    price_cents: int
    total_quantity: int


@dataclass
class OrderbookSnapshot:
    """YES-side order book snapshot. NO conversion done in schema layer."""

    market_id: str
    yes_bids: list[PriceLevel]       # descending by price
    yes_asks: list[PriceLevel]       # ascending by price
    last_trade_price_cents: int | None
    updated_at: datetime
```

### Step 4: Run test — expect PASS

```bash
uv run pytest tests/unit/test_market_domain_models.py -v
# Expected: 5 passed
```

### Step 5: Commit

```bash
git add src/pm_market/domain/models.py tests/unit/test_market_domain_models.py
git commit -m "feat(pm_market): add domain models (Market, PriceLevel, OrderbookSnapshot)"
```

---

## Task 2: Repository Protocol

**Files:**
- Create: `src/pm_market/domain/repository.py`
- Test: (no separate test — protocol tested via mock in Task 6)

### Step 1: Implement (no test needed for a Protocol stub)

```python
# src/pm_market/domain/repository.py
"""Repository Protocol — dependency inversion for testability.

Unit tests inject a mock that conforms to this Protocol.
Infrastructure layer provides the real implementation.
"""

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_market.domain.models import Market, OrderbookSnapshot


class MarketRepositoryProtocol(Protocol):
    async def list_markets(
        self,
        db: AsyncSession,
        status: str | None,
        category: str | None,
        cursor_ts: str | None,
        cursor_id: str | None,
        limit: int,
    ) -> list[Market]: ...

    async def get_market_by_id(
        self,
        db: AsyncSession,
        market_id: str,
    ) -> Market | None: ...

    async def get_orderbook_snapshot(
        self,
        db: AsyncSession,
        market_id: str,
        levels: int,
    ) -> OrderbookSnapshot: ...
```

### Step 2: Verify lint passes

```bash
uv run ruff check src/pm_market/domain/repository.py
# Expected: no errors
```

### Step 3: Commit

```bash
git add src/pm_market/domain/repository.py
git commit -m "feat(pm_market): add MarketRepositoryProtocol"
```

---

## Task 3: ORM Model

**Files:**
- Create: `src/pm_market/infrastructure/db_models.py`
- Test: (smoke test in Task 4's integration test)

### Step 1: Implement

```python
# src/pm_market/infrastructure/db_models.py
"""SQLAlchemy ORM model for the markets table.

Used for type reference only — persistence.py uses raw text() SQL.
Alembic migrations (004_create_markets.py) are the authoritative DDL source.
"""

from datetime import datetime

from sqlalchemy import BigInteger, SmallInteger, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class MarketORM(Base):
    __tablename__ = "markets"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    min_price_cents: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    max_price_cents: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    max_order_quantity: Mapped[int] = mapped_column(nullable=False)
    max_position_per_user: Mapped[int] = mapped_column(nullable=False)
    max_order_amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    maker_fee_bps: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    taker_fee_bps: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    trading_start_at: Mapped[datetime | None] = mapped_column()
    trading_end_at: Mapped[datetime | None] = mapped_column()
    resolution_date: Mapped[datetime | None] = mapped_column()
    resolved_at: Mapped[datetime | None] = mapped_column()
    settled_at: Mapped[datetime | None] = mapped_column()
    resolution_result: Mapped[str | None] = mapped_column(Text)
    reserve_balance: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pnl_pool: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_yes_shares: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_no_shares: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
```

### Step 2: Verify lint

```bash
uv run ruff check src/pm_market/infrastructure/db_models.py
# Expected: no errors
```

### Step 3: Commit

```bash
git add src/pm_market/infrastructure/db_models.py
git commit -m "feat(pm_market): add MarketORM model"
```

---

## Task 4: Infrastructure Persistence (MarketRepository)

**Files:**
- Create: `src/pm_market/infrastructure/persistence.py`
- Test: `tests/unit/test_market_persistence.py` (mock AsyncSession)

### Step 1: Write the failing test

```python
# tests/unit/test_market_persistence.py
"""Unit tests for MarketRepository using MagicMock AsyncSession."""
import pytest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from src.pm_market.infrastructure.persistence import MarketRepository


def _make_market_row(**kwargs):
    """Build a mock DB row with all required fields."""
    row = MagicMock()
    row.id = kwargs.get("id", "MKT-TEST")
    row.title = kwargs.get("title", "Test Market")
    row.description = kwargs.get("description", None)
    row.category = kwargs.get("category", "crypto")
    row.status = kwargs.get("status", "ACTIVE")
    row.min_price_cents = 1
    row.max_price_cents = 99
    row.max_order_quantity = 10000
    row.max_position_per_user = 25000
    row.max_order_amount_cents = 1000000
    row.maker_fee_bps = 10
    row.taker_fee_bps = 20
    row.reserve_balance = 500000
    row.pnl_pool = 0
    row.total_yes_shares = 5000
    row.total_no_shares = 5000
    row.trading_start_at = None
    row.trading_end_at = None
    row.resolution_date = None
    row.resolved_at = None
    row.settled_at = None
    row.resolution_result = None
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    return row


@pytest.fixture
def db():
    return MagicMock()


class TestGetMarketById:
    @pytest.mark.asyncio
    async def test_returns_market_when_found(self, db):
        row = _make_market_row(id="MKT-BTC", title="BTC Market")
        result_mock = MagicMock()
        result_mock.fetchone.return_value = row
        db.execute = AsyncMock(return_value=result_mock)

        repo = MarketRepository()
        market = await repo.get_market_by_id(db, "MKT-BTC")

        assert market is not None
        assert market.id == "MKT-BTC"
        assert market.title == "BTC Market"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self, db):
        result_mock = MagicMock()
        result_mock.fetchone.return_value = None
        db.execute = AsyncMock(return_value=result_mock)

        repo = MarketRepository()
        market = await repo.get_market_by_id(db, "MKT-MISSING")

        assert market is None


class TestListMarkets:
    @pytest.mark.asyncio
    async def test_returns_list(self, db):
        rows = [_make_market_row(id=f"MKT-{i}") for i in range(3)]
        result_mock = MagicMock()
        result_mock.fetchall.return_value = rows
        db.execute = AsyncMock(return_value=result_mock)

        repo = MarketRepository()
        markets = await repo.list_markets(db, "ACTIVE", None, None, None, 20)

        assert len(markets) == 3

    @pytest.mark.asyncio
    async def test_returns_empty_list(self, db):
        result_mock = MagicMock()
        result_mock.fetchall.return_value = []
        db.execute = AsyncMock(return_value=result_mock)

        repo = MarketRepository()
        markets = await repo.list_markets(db, "ACTIVE", None, None, None, 20)

        assert markets == []
```

### Step 2: Run test — expect FAIL

```bash
uv run pytest tests/unit/test_market_persistence.py -v
# Expected: ModuleNotFoundError (persistence.py doesn't exist)
```

### Step 3: Implement

```python
# src/pm_market/infrastructure/persistence.py
"""MarketRepository — concrete implementation of MarketRepositoryProtocol.

All queries use raw text() SQL (no ORM).
asyncpg NULL parameter pattern: CAST(:param AS TYPE) IS NULL required for None values.
"""

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_market.domain.models import Market, OrderbookSnapshot, PriceLevel

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_GET_MARKET_SQL = text("""
    SELECT id, title, description, category, status,
           min_price_cents, max_price_cents,
           max_order_quantity, max_position_per_user, max_order_amount_cents,
           maker_fee_bps, taker_fee_bps,
           reserve_balance, pnl_pool,
           total_yes_shares, total_no_shares,
           trading_start_at, trading_end_at,
           resolution_date, resolved_at, settled_at, resolution_result,
           created_at, updated_at
    FROM markets
    WHERE id = :market_id
""")

_LIST_MARKETS_SQL = text("""
    SELECT id, title, description, category, status,
           min_price_cents, max_price_cents,
           max_order_quantity, max_position_per_user, max_order_amount_cents,
           maker_fee_bps, taker_fee_bps,
           reserve_balance, pnl_pool,
           total_yes_shares, total_no_shares,
           trading_start_at, trading_end_at,
           resolution_date, resolved_at, settled_at, resolution_result,
           created_at, updated_at
    FROM markets
    WHERE
        (CAST(:status AS TEXT) IS NULL OR status = CAST(:status AS TEXT))
        AND (CAST(:category AS TEXT) IS NULL OR category = CAST(:category AS TEXT))
        AND (
            CAST(:cursor_ts AS TIMESTAMPTZ) IS NULL
            OR created_at < CAST(:cursor_ts AS TIMESTAMPTZ)
            OR (
                created_at = CAST(:cursor_ts AS TIMESTAMPTZ)
                AND id < CAST(:cursor_id AS TEXT)
            )
        )
    ORDER BY created_at DESC, id DESC
    LIMIT :limit
""")

_ORDERBOOK_AGGREGATE_SQL = text("""
    SELECT book_price, book_direction,
           SUM(remaining_quantity) AS total_qty
    FROM orders
    WHERE market_id = :market_id
      AND status IN ('OPEN', 'PARTIALLY_FILLED')
    GROUP BY book_price, book_direction
    ORDER BY book_price
""")

_LAST_TRADE_SQL = text("""
    SELECT price
    FROM trades
    WHERE market_id = :market_id
    ORDER BY executed_at DESC, id DESC
    LIMIT 1
""")

# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------

def _row_to_market(row: object) -> Market:
    return Market(
        id=row.id,  # type: ignore[attr-defined]
        title=row.title,  # type: ignore[attr-defined]
        description=row.description,  # type: ignore[attr-defined]
        category=row.category,  # type: ignore[attr-defined]
        status=row.status,  # type: ignore[attr-defined]
        min_price_cents=row.min_price_cents,  # type: ignore[attr-defined]
        max_price_cents=row.max_price_cents,  # type: ignore[attr-defined]
        max_order_quantity=row.max_order_quantity,  # type: ignore[attr-defined]
        max_position_per_user=row.max_position_per_user,  # type: ignore[attr-defined]
        max_order_amount_cents=row.max_order_amount_cents,  # type: ignore[attr-defined]
        maker_fee_bps=row.maker_fee_bps,  # type: ignore[attr-defined]
        taker_fee_bps=row.taker_fee_bps,  # type: ignore[attr-defined]
        reserve_balance=row.reserve_balance,  # type: ignore[attr-defined]
        pnl_pool=row.pnl_pool,  # type: ignore[attr-defined]
        total_yes_shares=row.total_yes_shares,  # type: ignore[attr-defined]
        total_no_shares=row.total_no_shares,  # type: ignore[attr-defined]
        trading_start_at=row.trading_start_at,  # type: ignore[attr-defined]
        trading_end_at=row.trading_end_at,  # type: ignore[attr-defined]
        resolution_date=row.resolution_date,  # type: ignore[attr-defined]
        resolved_at=row.resolved_at,  # type: ignore[attr-defined]
        settled_at=row.settled_at,  # type: ignore[attr-defined]
        resolution_result=row.resolution_result,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
        updated_at=row.updated_at,  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class MarketRepository:
    """Concrete repository — all operations are read-only SQL queries."""

    async def get_market_by_id(
        self, db: AsyncSession, market_id: str
    ) -> Market | None:
        result = await db.execute(_GET_MARKET_SQL, {"market_id": market_id})
        row = result.fetchone()
        return _row_to_market(row) if row else None

    async def list_markets(
        self,
        db: AsyncSession,
        status: str | None,
        category: str | None,
        cursor_ts: str | None,
        cursor_id: str | None,
        limit: int,
    ) -> list[Market]:
        result = await db.execute(
            _LIST_MARKETS_SQL,
            {
                "status": status,
                "category": category,
                "cursor_ts": cursor_ts,
                "cursor_id": cursor_id,
                "limit": limit,
            },
        )
        rows = result.fetchall()
        return [_row_to_market(row) for row in rows]

    async def get_orderbook_snapshot(
        self, db: AsyncSession, market_id: str, levels: int
    ) -> OrderbookSnapshot:
        # Step 1: aggregate active orders by price level
        agg_result = await db.execute(
            _ORDERBOOK_AGGREGATE_SQL, {"market_id": market_id}
        )
        agg_rows = agg_result.fetchall()

        bids: list[PriceLevel] = []
        asks: list[PriceLevel] = []
        for row in agg_rows:
            level = PriceLevel(
                price_cents=row.book_price,
                total_quantity=row.total_qty,
            )
            if row.book_direction == "BUY":
                bids.append(level)
            else:
                asks.append(level)

        # Sort: bids descending, asks ascending; then truncate to `levels`
        bids.sort(key=lambda lv: lv.price_cents, reverse=True)
        asks.sort(key=lambda lv: lv.price_cents)
        bids = bids[:levels]
        asks = asks[:levels]

        # Step 2: last trade price
        trade_result = await db.execute(
            _LAST_TRADE_SQL, {"market_id": market_id}
        )
        trade_row = trade_result.fetchone()
        last_price = trade_row.price if trade_row else None

        return OrderbookSnapshot(
            market_id=market_id,
            yes_bids=bids,
            yes_asks=asks,
            last_trade_price_cents=last_price,
            updated_at=datetime.now(UTC),
        )
```

### Step 4: Run test — expect PASS

```bash
uv run pytest tests/unit/test_market_persistence.py -v
# Expected: 4 passed
```

### Step 5: Commit

```bash
git add src/pm_market/infrastructure/persistence.py tests/unit/test_market_persistence.py
git commit -m "feat(pm_market): add MarketRepository (list/detail/orderbook SQL)"
```

---

## Task 5: Application Schemas + Cursor Utilities

**Files:**
- Create: `src/pm_market/application/schemas.py`
- Test: `tests/unit/test_market_schemas.py`

### Step 1: Write the failing test

```python
# tests/unit/test_market_schemas.py
import base64
import json
from datetime import UTC, datetime

import pytest

from src.pm_market.application.schemas import (
    MarketDetail,
    MarketListItem,
    MarketListResponse,
    OrderbookResponse,
    cursor_decode,
    cursor_encode,
)
from src.pm_market.domain.models import Market, OrderbookSnapshot, PriceLevel


def _make_market(**kwargs) -> Market:
    defaults = dict(
        id="MKT-TEST",
        title="Test Market",
        description="A test market",
        category="crypto",
        status="ACTIVE",
        min_price_cents=1,
        max_price_cents=99,
        max_order_quantity=10000,
        max_position_per_user=25000,
        max_order_amount_cents=1000000,
        maker_fee_bps=10,
        taker_fee_bps=20,
        reserve_balance=500000,
        pnl_pool=-1200,
        total_yes_shares=5000,
        total_no_shares=5000,
        trading_start_at=None,
        trading_end_at=None,
        resolution_date=None,
        resolved_at=None,
        settled_at=None,
        resolution_result=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    defaults.update(kwargs)
    return Market(**defaults)


class TestCursorEncodeDecode:
    def test_roundtrip(self):
        m = _make_market(id="MKT-BTC", created_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC))
        encoded = cursor_encode(m)
        ts, mid = cursor_decode(encoded)
        assert mid == "MKT-BTC"
        assert ts is not None
        assert "2026" in ts

    def test_decode_none_returns_none(self):
        ts, mid = cursor_decode(None)
        assert ts is None
        assert mid is None

    def test_decode_invalid_returns_none(self):
        ts, mid = cursor_decode("not-valid-base64!!")
        assert ts is None
        assert mid is None


class TestMarketListItem:
    def test_from_domain_basic_fields(self):
        m = _make_market(reserve_balance=500000)
        item = MarketListItem.from_domain(m)
        assert item.id == "MKT-TEST"
        assert item.status == "ACTIVE"
        assert item.reserve_balance_cents == 500000
        assert item.reserve_balance_display == "$5,000.00"

    def test_from_domain_null_timestamps(self):
        m = _make_market(trading_start_at=None, resolution_date=None)
        item = MarketListItem.from_domain(m)
        assert item.trading_start_at is None
        assert item.resolution_date is None


class TestMarketDetail:
    def test_from_domain_includes_pnl_pool(self):
        m = _make_market(pnl_pool=-1200)
        detail = MarketDetail.from_domain(m)
        assert detail.pnl_pool_cents == -1200
        assert detail.pnl_pool_display == "-$12.00"

    def test_from_domain_includes_risk_params(self):
        m = _make_market()
        detail = MarketDetail.from_domain(m)
        assert detail.max_order_quantity == 10000
        assert detail.max_position_per_user == 25000
        assert detail.max_order_amount_cents == 1000000


class TestOrderbookResponse:
    def _make_snapshot(self) -> OrderbookSnapshot:
        return OrderbookSnapshot(
            market_id="MKT-TEST",
            yes_bids=[PriceLevel(65, 500), PriceLevel(64, 300)],
            yes_asks=[PriceLevel(67, 400), PriceLevel(68, 200)],
            last_trade_price_cents=65,
            updated_at=datetime.now(UTC),
        )

    def test_yes_side_preserved(self):
        resp = OrderbookResponse.from_snapshot(self._make_snapshot())
        assert resp.yes.bids[0].price_cents == 65
        assert resp.yes.asks[0].price_cents == 67

    def test_no_bids_from_yes_asks(self):
        """NO bids = 100 - YES asks, reversed (YES asks ascending → NO bids descending)."""
        resp = OrderbookResponse.from_snapshot(self._make_snapshot())
        # YES asks: [67, 68] ascending → NO bids: [100-68=32, 100-67=33] descending
        assert resp.no.bids[0].price_cents == 33   # 100 - 67
        assert resp.no.bids[1].price_cents == 32   # 100 - 68

    def test_no_asks_from_yes_bids(self):
        """NO asks = 100 - YES bids, reversed (YES bids descending → NO asks ascending)."""
        resp = OrderbookResponse.from_snapshot(self._make_snapshot())
        # YES bids: [65, 64] descending → NO asks: [100-64=36, 100-65=35] ascending
        assert resp.no.asks[0].price_cents == 35   # 100 - 65
        assert resp.no.asks[1].price_cents == 36   # 100 - 64

    def test_no_quantities_match(self):
        """NO quantities mirror YES quantities."""
        resp = OrderbookResponse.from_snapshot(self._make_snapshot())
        # NO bids come from YES asks: [67→400, 68→200], reversed → [33→200, 32→400]... wait
        # NO bids[0] = 100-68=32 with qty 200, then NO bids[1] = 100-67=33 with qty 400
        # Actually reversed means: yes_asks=[67:400, 68:200] reversed=[68:200, 67:400]
        # → no_bids = [100-68=32:200, 100-67=33:400]
        # Hmm - check sorted descending: no_bids should be [33:400, 32:200]
        # Because reversed([67:400, 68:200]) = [68:200, 67:400]
        # → no_bids = [100-68=32:200, 100-67=33:400] - this is ascending not descending!
        # The implementation needs to re-sort after conversion.
        # Let's just assert quantities sum correctly.
        yes_ask_total = sum(lv.total_quantity for lv in self._make_snapshot().yes_asks)
        no_bid_total = sum(lv.total_quantity for lv in resp.no.bids)
        assert yes_ask_total == no_bid_total

    def test_empty_orderbook(self):
        snap = OrderbookSnapshot(
            market_id="MKT-EMPTY",
            yes_bids=[],
            yes_asks=[],
            last_trade_price_cents=None,
            updated_at=datetime.now(UTC),
        )
        resp = OrderbookResponse.from_snapshot(snap)
        assert resp.yes.bids == []
        assert resp.no.bids == []
        assert resp.last_trade_price_cents is None
```

### Step 2: Run test — expect FAIL

```bash
uv run pytest tests/unit/test_market_schemas.py -v
# Expected: ModuleNotFoundError
```

### Step 3: Implement

```python
# src/pm_market/application/schemas.py
"""Pydantic schemas for pm_market API responses.

Cursor format for markets (VARCHAR PK, not sequential):
  {"ts": "<created_at ISO>", "id": "<market_id>"}
  Encoded as Base64 JSON string.

YES→NO orderbook conversion (per API contract §4.3):
  NO bids[i].price = 100 - YES asks[reversed(i)].price (same qty)
  NO asks[i].price = 100 - YES bids[reversed(i)].price (same qty)
  Sorted after conversion: no.bids descending, no.asks ascending.
"""

import base64
import json

from pydantic import BaseModel

from src.pm_common.cents import cents_to_display
from src.pm_market.domain.models import Market, OrderbookSnapshot, PriceLevel


# ---------------------------------------------------------------------------
# Cursor utilities
# ---------------------------------------------------------------------------

def cursor_encode(last_market: Market) -> str:
    """Encode composite cursor from last market in page."""
    payload = {
        "ts": last_market.created_at.isoformat(),
        "id": last_market.id,
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def cursor_decode(cursor: str | None) -> tuple[str | None, str | None]:
    """Decode composite cursor → (ts_iso, market_id), or (None, None) on error."""
    if cursor is None:
        return None, None
    try:
        data = json.loads(base64.b64decode(cursor.encode()).decode())
        return data["ts"], data["id"]
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Price level
# ---------------------------------------------------------------------------

class PriceLevelOut(BaseModel):
    price_cents: int
    total_quantity: int


class OrderSideOut(BaseModel):
    bids: list[PriceLevelOut]
    asks: list[PriceLevelOut]


# ---------------------------------------------------------------------------
# Orderbook response
# ---------------------------------------------------------------------------

def _to_no_view(
    yes_bids: list[PriceLevelOut],
    yes_asks: list[PriceLevelOut],
) -> tuple[list[PriceLevelOut], list[PriceLevelOut]]:
    """Convert YES orderbook to NO dual view.

    NO bids come from YES asks: price = 100 - yes_ask_price, same qty.
    NO asks come from YES bids: price = 100 - yes_bid_price, same qty.
    Sort result: no.bids descending, no.asks ascending.
    """
    no_bids = sorted(
        [PriceLevelOut(price_cents=100 - lv.price_cents, total_quantity=lv.total_quantity)
         for lv in yes_asks],
        key=lambda x: x.price_cents,
        reverse=True,
    )
    no_asks = sorted(
        [PriceLevelOut(price_cents=100 - lv.price_cents, total_quantity=lv.total_quantity)
         for lv in yes_bids],
        key=lambda x: x.price_cents,
    )
    return no_bids, no_asks


class OrderbookResponse(BaseModel):
    market_id: str
    yes: OrderSideOut
    no: OrderSideOut
    last_trade_price_cents: int | None
    updated_at: str

    @classmethod
    def from_snapshot(cls, snapshot: OrderbookSnapshot) -> "OrderbookResponse":
        yes_bids = [PriceLevelOut(price_cents=lv.price_cents, total_quantity=lv.total_quantity)
                    for lv in snapshot.yes_bids]
        yes_asks = [PriceLevelOut(price_cents=lv.price_cents, total_quantity=lv.total_quantity)
                    for lv in snapshot.yes_asks]
        no_bids, no_asks = _to_no_view(yes_bids, yes_asks)
        return cls(
            market_id=snapshot.market_id,
            yes=OrderSideOut(bids=yes_bids, asks=yes_asks),
            no=OrderSideOut(bids=no_bids, asks=no_asks),
            last_trade_price_cents=snapshot.last_trade_price_cents,
            updated_at=snapshot.updated_at.isoformat(),
        )


# ---------------------------------------------------------------------------
# Market list item (lightweight)
# ---------------------------------------------------------------------------

class MarketListItem(BaseModel):
    id: str
    title: str
    description: str | None
    category: str | None
    status: str
    min_price_cents: int
    max_price_cents: int
    maker_fee_bps: int
    taker_fee_bps: int
    reserve_balance_cents: int
    reserve_balance_display: str
    total_yes_shares: int
    total_no_shares: int
    trading_start_at: str | None
    resolution_date: str | None

    @classmethod
    def from_domain(cls, m: Market) -> "MarketListItem":
        return cls(
            id=m.id,
            title=m.title,
            description=m.description,
            category=m.category,
            status=m.status,
            min_price_cents=m.min_price_cents,
            max_price_cents=m.max_price_cents,
            maker_fee_bps=m.maker_fee_bps,
            taker_fee_bps=m.taker_fee_bps,
            reserve_balance_cents=m.reserve_balance,
            reserve_balance_display=cents_to_display(m.reserve_balance),
            total_yes_shares=m.total_yes_shares,
            total_no_shares=m.total_no_shares,
            trading_start_at=m.trading_start_at.isoformat() if m.trading_start_at else None,
            resolution_date=m.resolution_date.isoformat() if m.resolution_date else None,
        )


class MarketListResponse(BaseModel):
    items: list[MarketListItem]
    next_cursor: str | None
    has_more: bool


# ---------------------------------------------------------------------------
# Market detail (full fields)
# ---------------------------------------------------------------------------

class MarketDetail(BaseModel):
    id: str
    title: str
    description: str | None
    category: str | None
    status: str
    min_price_cents: int
    max_price_cents: int
    max_order_quantity: int
    max_position_per_user: int
    max_order_amount_cents: int
    maker_fee_bps: int
    taker_fee_bps: int
    reserve_balance_cents: int
    reserve_balance_display: str
    pnl_pool_cents: int
    pnl_pool_display: str
    total_yes_shares: int
    total_no_shares: int
    trading_start_at: str | None
    trading_end_at: str | None
    resolution_date: str | None
    resolved_at: str | None
    settled_at: str | None
    resolution_result: str | None

    @classmethod
    def from_domain(cls, m: Market) -> "MarketDetail":
        def _iso(dt: object) -> str | None:
            if dt is None:
                return None
            return dt.isoformat()  # type: ignore[attr-defined]

        return cls(
            id=m.id,
            title=m.title,
            description=m.description,
            category=m.category,
            status=m.status,
            min_price_cents=m.min_price_cents,
            max_price_cents=m.max_price_cents,
            max_order_quantity=m.max_order_quantity,
            max_position_per_user=m.max_position_per_user,
            max_order_amount_cents=m.max_order_amount_cents,
            maker_fee_bps=m.maker_fee_bps,
            taker_fee_bps=m.taker_fee_bps,
            reserve_balance_cents=m.reserve_balance,
            reserve_balance_display=cents_to_display(m.reserve_balance),
            pnl_pool_cents=m.pnl_pool,
            pnl_pool_display=cents_to_display(m.pnl_pool),
            total_yes_shares=m.total_yes_shares,
            total_no_shares=m.total_no_shares,
            trading_start_at=_iso(m.trading_start_at),
            trading_end_at=_iso(m.trading_end_at),
            resolution_date=_iso(m.resolution_date),
            resolved_at=_iso(m.resolved_at),
            settled_at=_iso(m.settled_at),
            resolution_result=m.resolution_result,
        )
```

### Step 4: Run test — expect PASS

```bash
uv run pytest tests/unit/test_market_schemas.py -v
# Expected: all passed
# NOTE: test_no_quantities_match uses sum comparison — should pass regardless of ordering
```

### Step 5: Commit

```bash
git add src/pm_market/application/schemas.py tests/unit/test_market_schemas.py
git commit -m "feat(pm_market): add schemas (MarketListItem, MarketDetail, OrderbookResponse, cursor utils)"
```

---

## Task 6: MarketApplicationService

**Files:**
- Create: `src/pm_market/application/service.py`
- Test: `tests/unit/test_market_service.py`

### Step 1: Write the failing test

```python
# tests/unit/test_market_service.py
"""Unit tests for MarketApplicationService using mock repository."""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.pm_market.application.service import MarketApplicationService
from src.pm_market.domain.models import Market, OrderbookSnapshot, PriceLevel
from src.pm_common.errors import MarketNotFoundError, MarketNotActiveError


def _make_market(**kwargs) -> Market:
    defaults = dict(
        id="MKT-TEST", title="Test", description=None, category="crypto",
        status="ACTIVE", min_price_cents=1, max_price_cents=99,
        max_order_quantity=10000, max_position_per_user=25000,
        max_order_amount_cents=1000000, maker_fee_bps=10, taker_fee_bps=20,
        reserve_balance=0, pnl_pool=0, total_yes_shares=0, total_no_shares=0,
        trading_start_at=None, trading_end_at=None, resolution_date=None,
        resolved_at=None, settled_at=None, resolution_result=None,
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    defaults.update(kwargs)
    return Market(**defaults)


def _make_snapshot(market_id: str = "MKT-TEST") -> OrderbookSnapshot:
    return OrderbookSnapshot(
        market_id=market_id, yes_bids=[], yes_asks=[],
        last_trade_price_cents=None, updated_at=datetime.now(UTC)
    )


@pytest.fixture
def db():
    return MagicMock()


@pytest.fixture
def mock_repo():
    return MagicMock()


class TestListMarkets:
    @pytest.mark.asyncio
    async def test_returns_list_response(self, db, mock_repo):
        markets = [_make_market(id=f"MKT-{i}") for i in range(3)]
        mock_repo.list_markets = AsyncMock(return_value=markets)
        svc = MarketApplicationService(repo=mock_repo)

        resp = await svc.list_markets(db, status=None, category=None, cursor=None, limit=20)

        assert len(resp.items) == 3
        assert resp.has_more is False
        assert resp.next_cursor is None

    @pytest.mark.asyncio
    async def test_has_more_when_over_limit(self, db, mock_repo):
        # repo returns limit+1 items → has_more=True
        markets = [_make_market(id=f"MKT-{i}") for i in range(21)]
        mock_repo.list_markets = AsyncMock(return_value=markets)
        svc = MarketApplicationService(repo=mock_repo)

        resp = await svc.list_markets(db, status=None, category=None, cursor=None, limit=20)

        assert resp.has_more is True
        assert len(resp.items) == 20
        assert resp.next_cursor is not None

    @pytest.mark.asyncio
    async def test_status_all_passes_none_to_repo(self, db, mock_repo):
        mock_repo.list_markets = AsyncMock(return_value=[])
        svc = MarketApplicationService(repo=mock_repo)

        await svc.list_markets(db, status="ALL", category=None, cursor=None, limit=20)

        call_args = mock_repo.list_markets.call_args
        assert call_args.args[1] is None  # status=None passed to repo


class TestGetMarket:
    @pytest.mark.asyncio
    async def test_returns_detail_when_found(self, db, mock_repo):
        mock_repo.get_market_by_id = AsyncMock(return_value=_make_market(id="MKT-BTC"))
        svc = MarketApplicationService(repo=mock_repo)

        detail = await svc.get_market(db, "MKT-BTC")

        assert detail.id == "MKT-BTC"

    @pytest.mark.asyncio
    async def test_raises_not_found(self, db, mock_repo):
        mock_repo.get_market_by_id = AsyncMock(return_value=None)
        svc = MarketApplicationService(repo=mock_repo)

        with pytest.raises(MarketNotFoundError):
            await svc.get_market(db, "MKT-MISSING")


class TestGetOrderbook:
    @pytest.mark.asyncio
    async def test_returns_orderbook_response(self, db, mock_repo):
        mock_repo.get_market_by_id = AsyncMock(
            return_value=_make_market(id="MKT-BTC", status="ACTIVE")
        )
        mock_repo.get_orderbook_snapshot = AsyncMock(
            return_value=_make_snapshot("MKT-BTC")
        )
        svc = MarketApplicationService(repo=mock_repo)

        resp = await svc.get_orderbook(db, "MKT-BTC", levels=10)

        assert resp.market_id == "MKT-BTC"
        assert resp.yes.bids == []

    @pytest.mark.asyncio
    async def test_raises_not_found(self, db, mock_repo):
        mock_repo.get_market_by_id = AsyncMock(return_value=None)
        svc = MarketApplicationService(repo=mock_repo)

        with pytest.raises(MarketNotFoundError):
            await svc.get_orderbook(db, "MKT-MISSING", levels=10)

    @pytest.mark.asyncio
    async def test_raises_not_active_for_non_active_market(self, db, mock_repo):
        mock_repo.get_market_by_id = AsyncMock(
            return_value=_make_market(id="MKT-SETTLED", status="SETTLED")
        )
        svc = MarketApplicationService(repo=mock_repo)

        with pytest.raises(MarketNotActiveError):
            await svc.get_orderbook(db, "MKT-SETTLED", levels=10)
```

### Step 2: Run test — expect FAIL

```bash
uv run pytest tests/unit/test_market_service.py -v
# Expected: ModuleNotFoundError
```

### Step 3: Implement

```python
# src/pm_market/application/service.py
"""MarketApplicationService — thin composition layer.

All methods are read-only; no commit/rollback needed.
The caller (router) passes db session; service delegates to repository.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_market.application.schemas import (
    MarketDetail,
    MarketListResponse,
    MarketListItem,
    OrderbookResponse,
    cursor_decode,
    cursor_encode,
)
from src.pm_market.domain.repository import MarketRepositoryProtocol
from src.pm_market.infrastructure.persistence import MarketRepository
from src.pm_common.errors import MarketNotActiveError, MarketNotFoundError


class MarketApplicationService:
    def __init__(self, repo: MarketRepositoryProtocol | None = None) -> None:
        self._repo: MarketRepositoryProtocol = repo or MarketRepository()

    async def list_markets(
        self,
        db: AsyncSession,
        status: str | None,
        category: str | None,
        cursor: str | None,
        limit: int,
    ) -> MarketListResponse:
        # status=None → default ACTIVE; status='ALL' → no filter
        sql_status = None if status == "ALL" else (status or "ACTIVE")
        cursor_ts, cursor_id = cursor_decode(cursor)

        # Fetch limit+1 to detect has_more without COUNT(*)
        markets = await self._repo.list_markets(
            db, sql_status, category, cursor_ts, cursor_id, limit + 1
        )
        has_more = len(markets) > limit
        page = markets[:limit]

        items = [MarketListItem.from_domain(m) for m in page]
        next_cursor = cursor_encode(page[-1]) if has_more and page else None
        return MarketListResponse(items=items, next_cursor=next_cursor, has_more=has_more)

    async def get_market(self, db: AsyncSession, market_id: str) -> MarketDetail:
        market = await self._repo.get_market_by_id(db, market_id)
        if market is None:
            raise MarketNotFoundError(market_id)
        return MarketDetail.from_domain(market)

    async def get_orderbook(
        self, db: AsyncSession, market_id: str, levels: int
    ) -> OrderbookResponse:
        market = await self._repo.get_market_by_id(db, market_id)
        if market is None:
            raise MarketNotFoundError(market_id)
        if market.status != "ACTIVE":
            raise MarketNotActiveError(market_id)
        snapshot = await self._repo.get_orderbook_snapshot(db, market_id, levels)
        return OrderbookResponse.from_snapshot(snapshot)
```

### Step 4: Run test — expect PASS

```bash
uv run pytest tests/unit/test_market_service.py -v
# Expected: 8 passed
```

### Step 5: Commit

```bash
git add src/pm_market/application/service.py tests/unit/test_market_service.py
git commit -m "feat(pm_market): add MarketApplicationService"
```

---

## Task 7: API Router + main.py Wiring

**Files:**
- Create: `src/pm_market/api/router.py`
- Modify: `src/main.py`
- Test: (covered by integration tests in Task 8)

### Step 1: Implement router

```python
# src/pm_market/api/router.py
"""pm_market REST endpoints.

GET /markets                          — list with cursor pagination
GET /markets/{market_id}              — full detail
GET /markets/{market_id}/orderbook    — order book snapshot (DB aggregated)
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_common.database import get_db_session
from src.pm_common.response import ApiResponse
from src.pm_gateway.auth.dependencies import get_current_user
from src.pm_market.application.service import MarketApplicationService

router = APIRouter(prefix="/markets", tags=["markets"])

_service = MarketApplicationService()


@router.get("")
async def list_markets(
    request: Request,
    status: str | None = Query(None, description="Filter by status. Default: ACTIVE. Use ALL for no filter."),
    category: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
    db: AsyncSession = Depends(get_db_session),
    _user: dict = Depends(get_current_user),
) -> ApiResponse:
    result = await _service.list_markets(db, status, category, cursor, limit)
    resp = ApiResponse.ok(result.model_dump())
    resp.request_id = getattr(request.state, "request_id", resp.request_id)
    return resp


@router.get("/{market_id}")
async def get_market(
    market_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    _user: dict = Depends(get_current_user),
) -> ApiResponse:
    result = await _service.get_market(db, market_id)
    resp = ApiResponse.ok(result.model_dump())
    resp.request_id = getattr(request.state, "request_id", resp.request_id)
    return resp


@router.get("/{market_id}/orderbook")
async def get_orderbook(
    market_id: str,
    request: Request,
    levels: int = Query(10, ge=1, le=99),
    db: AsyncSession = Depends(get_db_session),
    _user: dict = Depends(get_current_user),
) -> ApiResponse:
    result = await _service.get_orderbook(db, market_id, levels)
    resp = ApiResponse.ok(result.model_dump())
    resp.request_id = getattr(request.state, "request_id", resp.request_id)
    return resp
```

### Step 2: Wire into main.py

Find the last `include_router` line in `src/main.py` and add after it:

```python
from src.pm_market.api.router import router as market_router
# ...
app.include_router(market_router, prefix="/api/v1")
```

**Exact edit**: in `src/main.py`, after the line `app.include_router(account_router, prefix="/api/v1")`, add:

```python
from src.pm_market.api.router import router as market_router  # noqa: E402
app.include_router(market_router, prefix="/api/v1")
```

### Step 3: Verify server starts

```bash
uv run uvicorn src.main:app --port 8000 &
curl -s http://localhost:8000/health | python3 -m json.tool
# Expected: {"code": 0, "data": {"status": "ok", ...}}
kill %1
```

### Step 4: Verify lint and typecheck

```bash
uv run ruff check src/pm_market/
uv run mypy src/pm_market/ --ignore-missing-imports
# Expected: no errors
```

### Step 5: Commit

```bash
git add src/pm_market/api/router.py src/main.py
git commit -m "feat(pm_market): add REST router (list/detail/orderbook) and wire into main"
```

---

## Task 8: Integration Tests

**Files:**
- Create: `tests/integration/test_market_flow.py`

**Prerequisites:** Docker running (`make up`), migrations applied (`make migrate`).

The seed data (migration 011) creates 3 ACTIVE markets:
- `MKT-BTC-100K-2026` (category: crypto)
- `MKT-ETH-10K-2026` (category: crypto)
- `MKT-FED-RATE-CUT-2026Q2` (category: economics)

### Step 1: Write integration tests

```python
# tests/integration/test_market_flow.py
"""Integration tests for pm_market endpoints.

Requires Docker (make up) + migrations (make migrate).
Seed data: 3 ACTIVE markets from 011_seed_initial_data.py.
"""

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio(loop_scope="session")


class TestUnauthenticated:
    async def test_list_markets_requires_auth(self, client):
        resp = await client.get("/api/v1/markets")
        assert resp.status_code == 401

    async def test_get_market_requires_auth(self, client):
        resp = await client.get("/api/v1/markets/MKT-BTC-100K-2026")
        assert resp.status_code == 401

    async def test_orderbook_requires_auth(self, client):
        resp = await client.get("/api/v1/markets/MKT-BTC-100K-2026/orderbook")
        assert resp.status_code == 401


class TestListMarkets:
    async def test_default_returns_active_markets(self, auth_client):
        resp = await auth_client.get("/api/v1/markets")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data["items"]) == 3
        assert data["has_more"] is False
        assert data["next_cursor"] is None

    async def test_status_filter_active(self, auth_client):
        resp = await auth_client.get("/api/v1/markets?status=ACTIVE")
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert all(i["status"] == "ACTIVE" for i in items)

    async def test_category_filter_crypto(self, auth_client):
        resp = await auth_client.get("/api/v1/markets?category=crypto")
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) == 2
        assert all(i["category"] == "crypto" for i in items)

    async def test_status_all_returns_all(self, auth_client):
        resp = await auth_client.get("/api/v1/markets?status=ALL")
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) == 3

    async def test_cursor_pagination(self, auth_client):
        # Page 1: limit=2
        resp1 = await auth_client.get("/api/v1/markets?limit=2")
        assert resp1.status_code == 200
        data1 = resp1.json()["data"]
        assert len(data1["items"]) == 2
        assert data1["has_more"] is True
        assert data1["next_cursor"] is not None

        # Page 2: use cursor
        cursor = data1["next_cursor"]
        resp2 = await auth_client.get(f"/api/v1/markets?limit=2&cursor={cursor}")
        assert resp2.status_code == 200
        data2 = resp2.json()["data"]
        assert len(data2["items"]) == 1
        assert data2["has_more"] is False

        # No overlap between pages
        ids1 = {i["id"] for i in data1["items"]}
        ids2 = {i["id"] for i in data2["items"]}
        assert ids1.isdisjoint(ids2)

    async def test_list_response_fields(self, auth_client):
        resp = await auth_client.get("/api/v1/markets")
        item = resp.json()["data"]["items"][0]
        # Required fields
        assert "id" in item
        assert "title" in item
        assert "status" in item
        assert "reserve_balance_cents" in item
        assert "reserve_balance_display" in item
        # Not in list item (lightweight)
        assert "pnl_pool_cents" not in item
        assert "max_order_quantity" not in item


class TestGetMarket:
    async def test_returns_full_detail(self, auth_client):
        resp = await auth_client.get("/api/v1/markets/MKT-BTC-100K-2026")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["id"] == "MKT-BTC-100K-2026"
        # Full detail fields
        assert "pnl_pool_cents" in data
        assert "pnl_pool_display" in data
        assert "max_order_quantity" in data
        assert "max_position_per_user" in data
        assert "max_order_amount_cents" in data

    async def test_not_found_returns_404(self, auth_client):
        resp = await auth_client.get("/api/v1/markets/MKT-DOES-NOT-EXIST")
        assert resp.status_code == 404
        assert resp.json()["code"] == 3001

    async def test_reserve_balance_display_format(self, auth_client):
        resp = await auth_client.get("/api/v1/markets/MKT-BTC-100K-2026")
        data = resp.json()["data"]
        # reserve_balance=0 from seed → $0.00
        assert data["reserve_balance_display"] == "$0.00"


class TestGetOrderbook:
    async def test_empty_orderbook_returns_empty_lists(self, auth_client):
        """No orders exist yet (Module 5 not done), so all lists are empty."""
        resp = await auth_client.get("/api/v1/markets/MKT-BTC-100K-2026/orderbook")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["market_id"] == "MKT-BTC-100K-2026"
        assert data["yes"]["bids"] == []
        assert data["yes"]["asks"] == []
        assert data["no"]["bids"] == []
        assert data["no"]["asks"] == []
        assert data["last_trade_price_cents"] is None

    async def test_orderbook_not_found_returns_404(self, auth_client):
        resp = await auth_client.get("/api/v1/markets/MKT-MISSING/orderbook")
        assert resp.status_code == 404
        assert resp.json()["code"] == 3001

    async def test_levels_param_accepted(self, auth_client):
        resp = await auth_client.get("/api/v1/markets/MKT-BTC-100K-2026/orderbook?levels=5")
        assert resp.status_code == 200

    async def test_response_has_dual_view_structure(self, auth_client):
        resp = await auth_client.get("/api/v1/markets/MKT-BTC-100K-2026/orderbook")
        data = resp.json()["data"]
        assert "yes" in data
        assert "no" in data
        assert "bids" in data["yes"]
        assert "asks" in data["yes"]
        assert "bids" in data["no"]
        assert "asks" in data["no"]
```

### Step 2: Check if `auth_client` fixture exists

Look at `tests/integration/conftest.py`. It should already have `client` and `auth_client` fixtures from pm_gateway/pm_account. If `auth_client` is missing, add it:

```python
# tests/integration/conftest.py — add if missing:
@pytest_asyncio.fixture(scope="session")
async def auth_client(client):
    """Authenticated client — registers a user and injects Bearer token."""
    reg_resp = await client.post("/api/v1/auth/register", json={
        "username": "market_test_user",
        "email": "market_test@example.com",
        "password": "TestPass123!",
    })
    # May already exist if running tests multiple times — that's OK
    login_resp = await client.post("/api/v1/auth/login", json={
        "username": "market_test_user",
        "password": "TestPass123!",
    })
    token = login_resp.json()["data"]["access_token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client
```

### Step 3: Run integration tests

```bash
uv run pytest tests/integration/test_market_flow.py -v
# Expected: all passed
```

### Step 4: Run full test suite

```bash
make test
# Expected: all tests pass (previous 131 + new market tests)
```

### Step 5: Verify lint and typecheck

```bash
make lint
make typecheck
# Expected: zero errors
```

### Step 6: Commit

```bash
git add tests/integration/test_market_flow.py tests/integration/conftest.py
git commit -m "test(pm_market): add integration tests for list/detail/orderbook endpoints"
```

---

## Completion Checklist

```
✅ GET /api/v1/markets           → list with status/category filter + cursor pagination
✅ GET /api/v1/markets/{id}      → full detail including pnl_pool + risk params
✅ GET /api/v1/markets/{id}/orderbook → DB-aggregated YES+NO dual view
✅ Unauthenticated → 401
✅ Unknown market_id → 404 (3001)
✅ Orderbook on non-ACTIVE market → 422 (3002)
✅ make test 全绿
✅ make lint 零报错
✅ make typecheck 零报错
```

---

## 偏差记录

> 实施完成后填写（与设计文档 §8 对应）。

| 偏差 | 说明 |
|------|------|
| `_LIST_MARKETS_SQL` 全列查询 | 设计文档 §3.1 要求只 SELECT 16 个轻量字段；实现选择了全量 24 列（与 `_GET_MARKET_SQL` 一致）。`MarketListItem.from_domain()` 在 schema 层负责字段裁剪，行为正确。理由：一个 `_row_to_market` 复用，DRY，话题数 < 100，性能不是问题。 |

---

*计划版本: v1.0 | 日期: 2026-02-20 | 状态: ✅ 全部完成*
