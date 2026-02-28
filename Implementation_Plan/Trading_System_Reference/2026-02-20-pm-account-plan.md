# pm_account 账户模块实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 实现 pm_account 模块，包含余额查询/充值/提现/流水四个 REST 接口、内部冻结/解冻资金和持仓的接口（供 pm_order 调用），以及 Position 域对象（供撮合模块使用）。

**Architecture:** 四层 DDD 结构（domain → infrastructure → application → api）。Domain 层通过 `AccountRepositoryProtocol`（typing.Protocol）依赖倒置，使业务逻辑可单元测试。Infrastructure 层全部使用原子 PostgreSQL UPDATE（`WHERE available >= :amount RETURNING *`），避免竞争条件——0 行结果即抛出业务异常，无需应用层锁。

**Tech Stack:** SQLAlchemy 2.0 async ORM + raw SQL、Pydantic v2、`pm_common.cents.cents_to_display`、`pm_common.enums.LedgerEntryType`、pytest + AsyncMock（单元测试）、httpx AsyncClient（集成测试）

**设计文档:** `Planning/Implementation/2026-02-20-pm-account-design.md`

**对齐 API 契约:** `Planning/Detail_Design/02_API接口契约.md` §3.1–3.4

---

## 项目约定（必读，零上下文）

- 所有金额单位：美分（`int`），**禁止 float / Decimal**
- `from src.pm_common.cents import cents_to_display` → `cents_to_display(6500)` → `"$65.00"`
- `from src.pm_common.database import Base, get_db_session` — ORM Base + FastAPI DB 依赖
- `from src.pm_common.enums import LedgerEntryType` — 16 种流水类型
- `from src.pm_gateway.auth.dependencies import get_current_user` — 认证依赖
- 所有测试函数必须有 `-> None` 返回类型注解（mypy strict）
- 运行测试：`uv run pytest tests/unit/test_file.py -v`
- 运行全部：`uv run pytest` — 应全绿
- Lint：`uv run ruff check src/ tests/` — 零报错
- 类型检查：`uv run mypy src/` — 零报错

---

## Task 1: Domain 层 — 数据模型 + 枚举

**Files:**
- Create: `src/pm_account/domain/models.py`
- Create: `src/pm_account/domain/enums.py`
- Test: `tests/unit/test_account_domain_models.py`

### Step 1: 写失败测试

```python
# tests/unit/test_account_domain_models.py
from datetime import datetime, UTC

import pytest

from src.pm_account.domain.models import Account, LedgerEntry, Position
from src.pm_account.domain.enums import LedgerEntryType


class TestAccount:
    def test_total_balance(self) -> None:
        account = Account(
            id="uuid-123",
            user_id="user-1",
            available_balance=100000,
            frozen_balance=6500,
            version=0,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        assert account.total_balance == 106500

    def test_total_balance_all_frozen(self) -> None:
        account = Account(
            id="uuid-123",
            user_id="user-1",
            available_balance=0,
            frozen_balance=50000,
            version=0,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        assert account.total_balance == 50000


class TestPosition:
    def test_defaults(self) -> None:
        pos = Position(user_id="user-1", market_id="market-1")
        assert pos.yes_volume == 0
        assert pos.yes_cost_sum == 0
        assert pos.yes_pending_sell == 0
        assert pos.no_volume == 0
        assert pos.no_cost_sum == 0
        assert pos.no_pending_sell == 0

    def test_available_yes(self) -> None:
        pos = Position(
            user_id="user-1",
            market_id="market-1",
            yes_volume=100,
            yes_pending_sell=30,
        )
        assert pos.available_yes == 70

    def test_available_no(self) -> None:
        pos = Position(
            user_id="user-1",
            market_id="market-1",
            no_volume=50,
            no_pending_sell=20,
        )
        assert pos.available_no == 30


class TestLedgerEntryType:
    def test_deposit_value(self) -> None:
        assert LedgerEntryType.DEPOSIT == "DEPOSIT"

    def test_all_16_types_exist(self) -> None:
        expected = {
            "DEPOSIT", "WITHDRAW",
            "ORDER_FREEZE", "ORDER_UNFREEZE",
            "MINT_COST", "MINT_RESERVE_IN",
            "BURN_REVENUE", "BURN_RESERVE_OUT",
            "TRANSFER_PAYMENT", "TRANSFER_RECEIPT",
            "NETTING", "NETTING_RESERVE_OUT",
            "FEE", "FEE_REVENUE",
            "SETTLEMENT_PAYOUT", "SETTLEMENT_VOID",
        }
        actual = {e.value for e in LedgerEntryType}
        assert actual == expected
```

### Step 2: 运行确认测试失败

```bash
uv run pytest tests/unit/test_account_domain_models.py -v
```
Expected: FAIL with `ModuleNotFoundError`

### Step 3: 实现 domain/models.py

```python
# src/pm_account/domain/models.py
"""Domain models for pm_account — pure dataclasses, no SQLAlchemy dependency."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Account:
    id: str
    user_id: str
    available_balance: int   # cents
    frozen_balance: int      # cents
    version: int
    created_at: datetime
    updated_at: datetime

    @property
    def total_balance(self) -> int:
        return self.available_balance + self.frozen_balance


@dataclass
class Position:
    user_id: str
    market_id: str
    yes_volume: int = 0
    yes_cost_sum: int = 0       # cents, 总购入成本，非均价
    yes_pending_sell: int = 0   # 已冻结等待卖出的 YES 份数
    no_volume: int = 0
    no_cost_sum: int = 0        # cents, 总购入成本，非均价
    no_pending_sell: int = 0    # 已冻结等待卖出的 NO 份数
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def available_yes(self) -> int:
        return self.yes_volume - self.yes_pending_sell

    @property
    def available_no(self) -> int:
        return self.no_volume - self.no_pending_sell


@dataclass
class LedgerEntry:
    id: int                          # BIGSERIAL
    user_id: str
    entry_type: str                  # LedgerEntryType value
    amount: int                      # cents, 正=收入 负=支出
    balance_after: int               # cents, 操作后 available_balance 快照
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None
    description: Optional[str] = None
    created_at: Optional[datetime] = None
```

### Step 4: 实现 domain/enums.py

```python
# src/pm_account/domain/enums.py
"""Re-export LedgerEntryType for use within pm_account module."""

from src.pm_common.enums import LedgerEntryType

__all__ = ["LedgerEntryType"]
```

### Step 5: 运行确认测试通过

```bash
uv run pytest tests/unit/test_account_domain_models.py -v
```
Expected: 6 tests PASSED

### Step 6: Lint + commit

```bash
uv run ruff check src/pm_account/domain/ tests/unit/test_account_domain_models.py
git add src/pm_account/domain/models.py src/pm_account/domain/enums.py tests/unit/test_account_domain_models.py
git commit -m "feat(pm_account): add domain models (Account, Position, LedgerEntry)"
```

---

## Task 2: Domain 层 — Repository Protocol + 骨架文件

**Files:**
- Create: `src/pm_account/domain/repository.py`
- Create: `src/pm_account/domain/events.py`
- Create: `src/pm_account/domain/cache.py`

这三个文件无需 TDD（Protocol 纯类型声明，骨架纯 TODO），直接实现后做静态类型检查。

### Step 1: 实现 domain/repository.py

```python
# src/pm_account/domain/repository.py
"""Repository Protocol — dependency inversion for testability.

Unit tests inject a mock that conforms to this Protocol.
Infrastructure layer provides the real implementation.
"""

from typing import Optional, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_account.domain.models import Account, LedgerEntry, Position


class AccountRepositoryProtocol(Protocol):
    async def get_account_by_user_id(
        self, db: AsyncSession, user_id: str
    ) -> Optional[Account]: ...

    async def deposit(
        self, db: AsyncSession, user_id: str, amount: int
    ) -> tuple[Account, LedgerEntry]: ...

    async def withdraw(
        self, db: AsyncSession, user_id: str, amount: int
    ) -> tuple[Account, LedgerEntry]: ...

    async def freeze_funds(
        self,
        db: AsyncSession,
        user_id: str,
        amount: int,
        ref_type: str,
        ref_id: str,
        description: str,
    ) -> tuple[Account, LedgerEntry]: ...

    async def unfreeze_funds(
        self,
        db: AsyncSession,
        user_id: str,
        amount: int,
        ref_type: str,
        ref_id: str,
        description: str,
    ) -> tuple[Account, LedgerEntry]: ...

    async def get_or_create_position(
        self, db: AsyncSession, user_id: str, market_id: str
    ) -> Position: ...

    async def freeze_yes_position(
        self, db: AsyncSession, user_id: str, market_id: str, quantity: int
    ) -> Position: ...

    async def unfreeze_yes_position(
        self, db: AsyncSession, user_id: str, market_id: str, quantity: int
    ) -> Position: ...

    async def freeze_no_position(
        self, db: AsyncSession, user_id: str, market_id: str, quantity: int
    ) -> Position: ...

    async def unfreeze_no_position(
        self, db: AsyncSession, user_id: str, market_id: str, quantity: int
    ) -> Position: ...

    async def list_ledger_entries(
        self,
        db: AsyncSession,
        user_id: str,
        cursor_id: Optional[int],
        limit: int,
        entry_type: Optional[str],
    ) -> list[LedgerEntry]: ...
```

### Step 2: 实现 domain/events.py (骨架)

```python
# src/pm_account/domain/events.py
"""Domain events for pm_account — TODO: implement in Phase 2.

Phase 2 events to add:
  - BalanceChanged(user_id, old_balance, new_balance, entry_id)
  - PositionChanged(user_id, market_id, side, old_volume, new_volume)

Use case: decouple async notifications (WebSocket, email) from core balance logic.
Implementation: Redis Pub/Sub or internal asyncio.Queue.
"""
```

### Step 3: 实现 domain/cache.py (骨架)

```python
# src/pm_account/domain/cache.py
"""Account balance cache — TODO: implement in Phase 2.

Phase 2 cache to add:
  - Hot account balance caching with 5s TTL
  - Cache key: f"account:balance:{user_id}"
  - Write-through: DB first, then cache invalidate
  - Read: cache-aside (check cache → DB on miss → populate cache)

MVP note: All balance reads go directly to PostgreSQL.
"""
```

### Step 4: mypy 类型检查

```bash
uv run mypy src/pm_account/domain/
```
Expected: Success: no issues found

### Step 5: Commit

```bash
git add src/pm_account/domain/repository.py src/pm_account/domain/events.py src/pm_account/domain/cache.py
git commit -m "feat(pm_account): add repository protocol and domain skeletons"
```

---

## Task 3: Infrastructure 层 — ORM Models

**Files:**
- Create: `src/pm_account/infrastructure/db_models.py`

ORM models 是声明式配置，用 mypy 而非 pytest 验证正确性。

### Step 1: 实现 infrastructure/db_models.py

```python
# src/pm_account/infrastructure/db_models.py
"""SQLAlchemy ORM models for pm_account.

These map to existing tables created by Alembic migrations.
DO NOT add/remove columns here without a corresponding migration.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.pm_common.database import Base


class AccountORM(Base):
    __tablename__ = "accounts"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    available_balance: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    frozen_balance: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PositionORM(Base):
    __tablename__ = "positions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market_id: Mapped[str] = mapped_column(String(64), nullable=False)
    yes_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    yes_cost_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    yes_pending_sell: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    no_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    no_cost_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    no_pending_sell: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LedgerEntryORM(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(30), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reference_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    reference_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

### Step 2: mypy 类型检查

```bash
uv run mypy src/pm_account/infrastructure/db_models.py
```
Expected: Success: no issues found

### Step 3: Commit

```bash
git add src/pm_account/infrastructure/db_models.py
git commit -m "feat(pm_account): add ORM models (AccountORM, PositionORM, LedgerEntryORM)"
```

---

## Task 4: Infrastructure 层 — Persistence (AccountRepository)

**Files:**
- Create: `src/pm_account/infrastructure/persistence.py`

这是模块最复杂的部分。全部使用原子 SQL UPDATE，无应用层锁。集成测试会验证正确性（Task 8）。

### Step 1: 实现 infrastructure/persistence.py

```python
# src/pm_account/infrastructure/persistence.py
"""AccountRepository — concrete implementation of AccountRepositoryProtocol.

All balance-mutating operations use atomic PostgreSQL UPDATE ... RETURNING.
A result of 0 rows means a business constraint was violated (insufficient funds/shares).

Transaction ownership: The CALLER (application service or router) is responsible for
starting and committing the transaction via `async with db.begin()`.
"""

from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_account.domain.models import Account, LedgerEntry, Position
from src.pm_common.enums import LedgerEntryType
from src.pm_common.errors import InsufficientBalanceError, InsufficientPositionError

# ---------------------------------------------------------------------------
# SQL: accounts mutations
# ---------------------------------------------------------------------------

_DEPOSIT_SQL = text("""
    UPDATE accounts
    SET available_balance = available_balance + :amount,
        version = version + 1,
        updated_at = NOW()
    WHERE user_id = :user_id
    RETURNING id, user_id, available_balance, frozen_balance, version, created_at, updated_at
""")

_WITHDRAW_SQL = text("""
    UPDATE accounts
    SET available_balance = available_balance - :amount,
        version = version + 1,
        updated_at = NOW()
    WHERE user_id = :user_id AND available_balance >= :amount
    RETURNING id, user_id, available_balance, frozen_balance, version, created_at, updated_at
""")

_FREEZE_FUNDS_SQL = text("""
    UPDATE accounts
    SET available_balance = available_balance - :amount,
        frozen_balance     = frozen_balance   + :amount,
        version = version + 1,
        updated_at = NOW()
    WHERE user_id = :user_id AND available_balance >= :amount
    RETURNING id, user_id, available_balance, frozen_balance, version, created_at, updated_at
""")

_UNFREEZE_FUNDS_SQL = text("""
    UPDATE accounts
    SET available_balance = available_balance + :amount,
        frozen_balance     = frozen_balance   - :amount,
        version = version + 1,
        updated_at = NOW()
    WHERE user_id = :user_id AND frozen_balance >= :amount
    RETURNING id, user_id, available_balance, frozen_balance, version, created_at, updated_at
""")

_INSERT_LEDGER_SQL = text("""
    INSERT INTO ledger_entries
        (user_id, entry_type, amount, balance_after, reference_type, reference_id, description)
    VALUES
        (:user_id, :entry_type, :amount, :balance_after, :reference_type, :reference_id, :description)
    RETURNING id, user_id, entry_type, amount, balance_after,
              reference_type, reference_id, description, created_at
""")

# ---------------------------------------------------------------------------
# SQL: positions mutations
# ---------------------------------------------------------------------------

_GET_OR_CREATE_POSITION_SQL = text("""
    INSERT INTO positions (user_id, market_id)
    VALUES (:user_id, :market_id)
    ON CONFLICT (user_id, market_id) DO UPDATE
        SET updated_at = NOW()
    RETURNING id, user_id, market_id,
              yes_volume, yes_cost_sum, yes_pending_sell,
              no_volume, no_cost_sum, no_pending_sell,
              created_at, updated_at
""")

_FREEZE_YES_SQL = text("""
    UPDATE positions
    SET yes_pending_sell = yes_pending_sell + :quantity,
        updated_at = NOW()
    WHERE user_id = :user_id
      AND market_id = :market_id
      AND (yes_volume - yes_pending_sell) >= :quantity
    RETURNING id, user_id, market_id,
              yes_volume, yes_cost_sum, yes_pending_sell,
              no_volume, no_cost_sum, no_pending_sell,
              created_at, updated_at
""")

_UNFREEZE_YES_SQL = text("""
    UPDATE positions
    SET yes_pending_sell = yes_pending_sell - :quantity,
        updated_at = NOW()
    WHERE user_id = :user_id
      AND market_id = :market_id
      AND yes_pending_sell >= :quantity
    RETURNING id, user_id, market_id,
              yes_volume, yes_cost_sum, yes_pending_sell,
              no_volume, no_cost_sum, no_pending_sell,
              created_at, updated_at
""")

_FREEZE_NO_SQL = text("""
    UPDATE positions
    SET no_pending_sell = no_pending_sell + :quantity,
        updated_at = NOW()
    WHERE user_id = :user_id
      AND market_id = :market_id
      AND (no_volume - no_pending_sell) >= :quantity
    RETURNING id, user_id, market_id,
              yes_volume, yes_cost_sum, yes_pending_sell,
              no_volume, no_cost_sum, no_pending_sell,
              created_at, updated_at
""")

_UNFREEZE_NO_SQL = text("""
    UPDATE positions
    SET no_pending_sell = no_pending_sell - :quantity,
        updated_at = NOW()
    WHERE user_id = :user_id
      AND market_id = :market_id
      AND no_pending_sell >= :quantity
    RETURNING id, user_id, market_id,
              yes_volume, yes_cost_sum, yes_pending_sell,
              no_volume, no_cost_sum, no_pending_sell,
              created_at, updated_at
""")

_LIST_LEDGER_SQL = text("""
    SELECT id, user_id, entry_type, amount, balance_after,
           reference_type, reference_id, description, created_at
    FROM ledger_entries
    WHERE user_id = :user_id
      AND (:cursor_id IS NULL OR id < :cursor_id)
      AND (:entry_type IS NULL OR entry_type = :entry_type)
    ORDER BY id DESC
    LIMIT :limit
""")

_GET_ACCOUNT_SQL = text("""
    SELECT id, user_id, available_balance, frozen_balance, version, created_at, updated_at
    FROM accounts
    WHERE user_id = :user_id
""")


def _row_to_account(row: object) -> Account:  # type: ignore[type-arg]
    return Account(
        id=str(row.id),  # type: ignore[attr-defined]
        user_id=row.user_id,  # type: ignore[attr-defined]
        available_balance=row.available_balance,  # type: ignore[attr-defined]
        frozen_balance=row.frozen_balance,  # type: ignore[attr-defined]
        version=row.version,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
        updated_at=row.updated_at,  # type: ignore[attr-defined]
    )


def _row_to_ledger(row: object) -> LedgerEntry:  # type: ignore[type-arg]
    return LedgerEntry(
        id=row.id,  # type: ignore[attr-defined]
        user_id=row.user_id,  # type: ignore[attr-defined]
        entry_type=row.entry_type,  # type: ignore[attr-defined]
        amount=row.amount,  # type: ignore[attr-defined]
        balance_after=row.balance_after,  # type: ignore[attr-defined]
        reference_type=row.reference_type,  # type: ignore[attr-defined]
        reference_id=row.reference_id,  # type: ignore[attr-defined]
        description=row.description,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
    )


def _row_to_position(row: object) -> Position:  # type: ignore[type-arg]
    return Position(
        user_id=row.user_id,  # type: ignore[attr-defined]
        market_id=row.market_id,  # type: ignore[attr-defined]
        yes_volume=row.yes_volume,  # type: ignore[attr-defined]
        yes_cost_sum=row.yes_cost_sum,  # type: ignore[attr-defined]
        yes_pending_sell=row.yes_pending_sell,  # type: ignore[attr-defined]
        no_volume=row.no_volume,  # type: ignore[attr-defined]
        no_cost_sum=row.no_cost_sum,  # type: ignore[attr-defined]
        no_pending_sell=row.no_pending_sell,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
        updated_at=row.updated_at,  # type: ignore[attr-defined]
    )


class AccountRepository:
    """Concrete repository — all operations atomic at the SQL level."""

    async def get_account_by_user_id(
        self, db: AsyncSession, user_id: str
    ) -> Optional[Account]:
        result = await db.execute(_GET_ACCOUNT_SQL, {"user_id": user_id})
        row = result.fetchone()
        return _row_to_account(row) if row else None

    async def deposit(
        self, db: AsyncSession, user_id: str, amount: int
    ) -> tuple[Account, LedgerEntry]:
        result = await db.execute(_DEPOSIT_SQL, {"user_id": user_id, "amount": amount})
        row = result.fetchone()
        if row is None:
            raise InsufficientBalanceError(amount, 0)
        account = _row_to_account(row)
        ledger_result = await db.execute(
            _INSERT_LEDGER_SQL,
            {
                "user_id": user_id,
                "entry_type": LedgerEntryType.DEPOSIT,
                "amount": amount,
                "balance_after": account.available_balance,
                "reference_type": "DEPOSIT",
                "reference_id": None,
                "description": "Simulated deposit",
            },
        )
        ledger_row = ledger_result.fetchone()
        assert ledger_row is not None
        return account, _row_to_ledger(ledger_row)

    async def withdraw(
        self, db: AsyncSession, user_id: str, amount: int
    ) -> tuple[Account, LedgerEntry]:
        result = await db.execute(_WITHDRAW_SQL, {"user_id": user_id, "amount": amount})
        row = result.fetchone()
        if row is None:
            # Fetch current balance for the error message
            acc_result = await db.execute(_GET_ACCOUNT_SQL, {"user_id": user_id})
            acc_row = acc_result.fetchone()
            available = acc_row.available_balance if acc_row else 0  # type: ignore[union-attr]
            raise InsufficientBalanceError(amount, available)
        account = _row_to_account(row)
        ledger_result = await db.execute(
            _INSERT_LEDGER_SQL,
            {
                "user_id": user_id,
                "entry_type": LedgerEntryType.WITHDRAW,
                "amount": -amount,  # 流水金额为负数（支出）
                "balance_after": account.available_balance,
                "reference_type": "WITHDRAW",
                "reference_id": None,
                "description": "Simulated withdrawal",
            },
        )
        ledger_row = ledger_result.fetchone()
        assert ledger_row is not None
        return account, _row_to_ledger(ledger_row)

    async def freeze_funds(
        self,
        db: AsyncSession,
        user_id: str,
        amount: int,
        ref_type: str,
        ref_id: str,
        description: str,
    ) -> tuple[Account, LedgerEntry]:
        result = await db.execute(_FREEZE_FUNDS_SQL, {"user_id": user_id, "amount": amount})
        row = result.fetchone()
        if row is None:
            acc_result = await db.execute(_GET_ACCOUNT_SQL, {"user_id": user_id})
            acc_row = acc_result.fetchone()
            available = acc_row.available_balance if acc_row else 0  # type: ignore[union-attr]
            raise InsufficientBalanceError(amount, available)
        account = _row_to_account(row)
        ledger_result = await db.execute(
            _INSERT_LEDGER_SQL,
            {
                "user_id": user_id,
                "entry_type": LedgerEntryType.ORDER_FREEZE,
                "amount": -amount,
                "balance_after": account.available_balance,
                "reference_type": ref_type,
                "reference_id": ref_id,
                "description": description,
            },
        )
        ledger_row = ledger_result.fetchone()
        assert ledger_row is not None
        return account, _row_to_ledger(ledger_row)

    async def unfreeze_funds(
        self,
        db: AsyncSession,
        user_id: str,
        amount: int,
        ref_type: str,
        ref_id: str,
        description: str,
    ) -> tuple[Account, LedgerEntry]:
        result = await db.execute(_UNFREEZE_FUNDS_SQL, {"user_id": user_id, "amount": amount})
        row = result.fetchone()
        if row is None:
            raise InsufficientBalanceError(amount, 0)
        account = _row_to_account(row)
        ledger_result = await db.execute(
            _INSERT_LEDGER_SQL,
            {
                "user_id": user_id,
                "entry_type": LedgerEntryType.ORDER_UNFREEZE,
                "amount": amount,
                "balance_after": account.available_balance,
                "reference_type": ref_type,
                "reference_id": ref_id,
                "description": description,
            },
        )
        ledger_row = ledger_result.fetchone()
        assert ledger_row is not None
        return account, _row_to_ledger(ledger_row)

    async def get_or_create_position(
        self, db: AsyncSession, user_id: str, market_id: str
    ) -> Position:
        result = await db.execute(
            _GET_OR_CREATE_POSITION_SQL, {"user_id": user_id, "market_id": market_id}
        )
        row = result.fetchone()
        assert row is not None
        return _row_to_position(row)

    async def freeze_yes_position(
        self, db: AsyncSession, user_id: str, market_id: str, quantity: int
    ) -> Position:
        result = await db.execute(
            _FREEZE_YES_SQL,
            {"user_id": user_id, "market_id": market_id, "quantity": quantity},
        )
        row = result.fetchone()
        if row is None:
            raise InsufficientPositionError(
                f"Insufficient YES shares to freeze: need {quantity}"
            )
        return _row_to_position(row)

    async def unfreeze_yes_position(
        self, db: AsyncSession, user_id: str, market_id: str, quantity: int
    ) -> Position:
        result = await db.execute(
            _UNFREEZE_YES_SQL,
            {"user_id": user_id, "market_id": market_id, "quantity": quantity},
        )
        row = result.fetchone()
        if row is None:
            raise InsufficientPositionError(
                f"Insufficient frozen YES shares to unfreeze: need {quantity}"
            )
        return _row_to_position(row)

    async def freeze_no_position(
        self, db: AsyncSession, user_id: str, market_id: str, quantity: int
    ) -> Position:
        result = await db.execute(
            _FREEZE_NO_SQL,
            {"user_id": user_id, "market_id": market_id, "quantity": quantity},
        )
        row = result.fetchone()
        if row is None:
            raise InsufficientPositionError(
                f"Insufficient NO shares to freeze: need {quantity}"
            )
        return _row_to_position(row)

    async def unfreeze_no_position(
        self, db: AsyncSession, user_id: str, market_id: str, quantity: int
    ) -> Position:
        result = await db.execute(
            _UNFREEZE_NO_SQL,
            {"user_id": user_id, "market_id": market_id, "quantity": quantity},
        )
        row = result.fetchone()
        if row is None:
            raise InsufficientPositionError(
                f"Insufficient frozen NO shares to unfreeze: need {quantity}"
            )
        return _row_to_position(row)

    async def list_ledger_entries(
        self,
        db: AsyncSession,
        user_id: str,
        cursor_id: Optional[int],
        limit: int,
        entry_type: Optional[str],
    ) -> list[LedgerEntry]:
        result = await db.execute(
            _LIST_LEDGER_SQL,
            {
                "user_id": user_id,
                "cursor_id": cursor_id,
                "entry_type": entry_type,
                "limit": limit,
            },
        )
        rows = result.fetchall()
        return [_row_to_ledger(row) for row in rows]
```

### Step 2: mypy 类型检查

```bash
uv run mypy src/pm_account/infrastructure/
```
Expected: Success: no issues found
（如果有 `[attr-defined]` 错误，检查 `# type: ignore[attr-defined]` 注释是否加全）

### Step 3: Commit

```bash
git add src/pm_account/infrastructure/persistence.py
git commit -m "feat(pm_account): add AccountRepository with atomic SQL operations"
```

---

## Task 5: Application 层 — Schemas + Cursor Utility

**Files:**
- Create: `src/pm_account/application/schemas.py`
- Test: `tests/unit/test_account_schemas.py`

### Step 1: 写失败测试

```python
# tests/unit/test_account_schemas.py
"""Tests for pm_account Pydantic schemas and cursor utilities."""

import pytest
from pydantic import ValidationError

from src.pm_account.application.schemas import (
    DepositRequest,
    WithdrawRequest,
    cursor_decode,
    cursor_encode,
)


class TestDepositRequest:
    def test_valid(self) -> None:
        req = DepositRequest(amount_cents=10000)
        assert req.amount_cents == 10000

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DepositRequest(amount_cents=0)

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DepositRequest(amount_cents=-100)


class TestWithdrawRequest:
    def test_valid(self) -> None:
        req = WithdrawRequest(amount_cents=5000)
        assert req.amount_cents == 5000

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WithdrawRequest(amount_cents=0)


class TestCursorUtils:
    def test_encode_decode_roundtrip(self) -> None:
        cursor = cursor_encode(12345)
        assert cursor_decode(cursor) == 12345

    def test_encode_produces_string(self) -> None:
        cursor = cursor_encode(1)
        assert isinstance(cursor, str)
        assert len(cursor) > 0

    def test_decode_invalid_returns_none(self) -> None:
        assert cursor_decode("not-valid-base64!!!") is None

    def test_decode_none_returns_none(self) -> None:
        assert cursor_decode(None) is None
```

### Step 2: 运行确认测试失败

```bash
uv run pytest tests/unit/test_account_schemas.py -v
```
Expected: FAIL with `ModuleNotFoundError`

### Step 3: 实现 application/schemas.py

```python
# src/pm_account/application/schemas.py
"""Pydantic schemas and cursor utilities for pm_account API."""

import base64
import json
from typing import Optional

from pydantic import BaseModel, Field

from src.pm_common.cents import cents_to_display


# ---------------------------------------------------------------------------
# Cursor-based pagination utilities
# ---------------------------------------------------------------------------

def cursor_encode(last_id: int) -> str:
    """Encode a BIGINT primary key into an opaque Base64 cursor string."""
    payload = json.dumps({"id": last_id})
    return base64.b64encode(payload.encode()).decode()


def cursor_decode(cursor: Optional[str]) -> Optional[int]:
    """Decode a cursor string back to the last seen id. Returns None on error."""
    if cursor is None:
        return None
    try:
        payload = json.loads(base64.b64decode(cursor.encode()).decode())
        return int(payload["id"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class DepositRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, description="Amount to deposit in cents")


class WithdrawRequest(BaseModel):
    amount_cents: int = Field(..., gt=0, description="Amount to withdraw in cents")


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class BalanceResponse(BaseModel):
    user_id: str
    available_balance_cents: int
    available_balance_display: str
    frozen_balance_cents: int
    frozen_balance_display: str
    total_balance_cents: int
    total_balance_display: str

    @classmethod
    def from_cents(
        cls,
        user_id: str,
        available: int,
        frozen: int,
    ) -> "BalanceResponse":
        return cls(
            user_id=user_id,
            available_balance_cents=available,
            available_balance_display=cents_to_display(available),
            frozen_balance_cents=frozen,
            frozen_balance_display=cents_to_display(frozen),
            total_balance_cents=available + frozen,
            total_balance_display=cents_to_display(available + frozen),
        )


class DepositResponse(BaseModel):
    available_balance_cents: int
    available_balance_display: str
    deposited_cents: int
    deposited_display: str
    ledger_entry_id: int

    @classmethod
    def from_result(cls, available: int, amount: int, entry_id: int) -> "DepositResponse":
        return cls(
            available_balance_cents=available,
            available_balance_display=cents_to_display(available),
            deposited_cents=amount,
            deposited_display=cents_to_display(amount),
            ledger_entry_id=entry_id,
        )


class WithdrawResponse(BaseModel):
    available_balance_cents: int
    available_balance_display: str
    withdrawn_cents: int
    withdrawn_display: str
    ledger_entry_id: int

    @classmethod
    def from_result(cls, available: int, amount: int, entry_id: int) -> "WithdrawResponse":
        return cls(
            available_balance_cents=available,
            available_balance_display=cents_to_display(available),
            withdrawn_cents=amount,
            withdrawn_display=cents_to_display(amount),
            ledger_entry_id=entry_id,
        )


class LedgerEntryItem(BaseModel):
    id: int
    entry_type: str
    amount_cents: int
    amount_display: str
    balance_after_cents: int
    balance_after_display: str
    reference_type: Optional[str]
    reference_id: Optional[str]
    description: Optional[str]
    created_at: str  # ISO8601 string


class LedgerResponse(BaseModel):
    items: list[LedgerEntryItem]
    next_cursor: Optional[str]
    has_more: bool
```

### Step 4: 运行确认测试通过

```bash
uv run pytest tests/unit/test_account_schemas.py -v
```
Expected: 9 tests PASSED

### Step 5: Lint + commit

```bash
uv run ruff check src/pm_account/application/schemas.py tests/unit/test_account_schemas.py
git add src/pm_account/application/schemas.py tests/unit/test_account_schemas.py
git commit -m "feat(pm_account): add application schemas and cursor utilities"
```

---

## Task 6: Application 层 — AccountApplicationService

**Files:**
- Create: `src/pm_account/application/service.py`
- Test: `tests/unit/test_account_service.py`

### Step 1: 写失败测试

```python
# tests/unit/test_account_service.py
"""Unit tests for AccountApplicationService using a mock repository."""

from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.pm_account.application.schemas import (
    BalanceResponse,
    DepositResponse,
    LedgerResponse,
    WithdrawResponse,
)
from src.pm_account.application.service import AccountApplicationService
from src.pm_account.domain.models import Account, LedgerEntry


def _make_account(available: int = 100000, frozen: int = 0) -> Account:
    return Account(
        id="uuid-1",
        user_id="user-1",
        available_balance=available,
        frozen_balance=frozen,
        version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _make_ledger_entry(entry_id: int = 1, amount: int = 10000, balance_after: int = 110000) -> LedgerEntry:
    return LedgerEntry(
        id=entry_id,
        user_id="user-1",
        entry_type="DEPOSIT",
        amount=amount,
        balance_after=balance_after,
        created_at=datetime.now(UTC),
    )


class TestGetBalance:
    async def test_returns_balance_response(self) -> None:
        mock_repo = AsyncMock()
        mock_repo.get_account_by_user_id.return_value = _make_account(150000, 6500)
        svc = AccountApplicationService(repo=mock_repo)
        db = MagicMock()

        result = await svc.get_balance(db, "user-1")

        assert isinstance(result, BalanceResponse)
        assert result.available_balance_cents == 150000
        assert result.frozen_balance_cents == 6500
        assert result.total_balance_cents == 156500
        assert result.available_balance_display == "$1,500.00"


class TestDeposit:
    async def test_returns_deposit_response(self) -> None:
        mock_repo = AsyncMock()
        mock_repo.deposit.return_value = (
            _make_account(110000),
            _make_ledger_entry(1, 10000, 110000),
        )
        svc = AccountApplicationService(repo=mock_repo)
        db = MagicMock()

        result = await svc.deposit(db, "user-1", 10000)

        assert isinstance(result, DepositResponse)
        assert result.deposited_cents == 10000
        assert result.available_balance_cents == 110000
        assert result.ledger_entry_id == 1


class TestWithdraw:
    async def test_returns_withdraw_response(self) -> None:
        mock_repo = AsyncMock()
        mock_repo.withdraw.return_value = (
            _make_account(90000),
            LedgerEntry(
                id=2, user_id="user-1", entry_type="WITHDRAW",
                amount=-10000, balance_after=90000, created_at=datetime.now(UTC),
            ),
        )
        svc = AccountApplicationService(repo=mock_repo)
        db = MagicMock()

        result = await svc.withdraw(db, "user-1", 10000)

        assert isinstance(result, WithdrawResponse)
        assert result.withdrawn_cents == 10000
        assert result.available_balance_cents == 90000
        assert result.ledger_entry_id == 2


class TestListLedger:
    async def test_returns_ledger_response_no_more(self) -> None:
        mock_repo = AsyncMock()
        mock_repo.list_ledger_entries.return_value = [
            _make_ledger_entry(5, 10000, 110000)
        ]
        svc = AccountApplicationService(repo=mock_repo)
        db = MagicMock()

        result = await svc.list_ledger(db, "user-1", cursor=None, limit=20, entry_type=None)

        assert isinstance(result, LedgerResponse)
        assert len(result.items) == 1
        assert result.items[0].id == 5
        assert result.has_more is False
        assert result.next_cursor is None

    async def test_returns_next_cursor_when_full_page(self) -> None:
        mock_repo = AsyncMock()
        # Return limit+1 items to signal more exist
        entries = [_make_ledger_entry(i, 1000, 100000) for i in range(5, 25)]
        mock_repo.list_ledger_entries.return_value = entries
        svc = AccountApplicationService(repo=mock_repo)
        db = MagicMock()

        result = await svc.list_ledger(db, "user-1", cursor=None, limit=20, entry_type=None)

        assert result.has_more is True
        assert result.next_cursor is not None
        assert len(result.items) == 20
```

### Step 2: 运行确认测试失败

```bash
uv run pytest tests/unit/test_account_service.py -v
```
Expected: FAIL with `ModuleNotFoundError`

### Step 3: 实现 application/service.py

```python
# src/pm_account/application/service.py
"""AccountApplicationService — thin composition layer.

Combines repository calls with schema transformations.
All database mutations use `async with db.begin()` — caller starts the transaction.
"""

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_account.application.schemas import (
    BalanceResponse,
    DepositResponse,
    LedgerEntryItem,
    LedgerResponse,
    WithdrawResponse,
    cursor_decode,
    cursor_encode,
)
from src.pm_account.domain.repository import AccountRepositoryProtocol
from src.pm_account.infrastructure.persistence import AccountRepository
from src.pm_common.cents import cents_to_display
from src.pm_common.errors import InternalError


class AccountApplicationService:
    def __init__(self, repo: Optional[AccountRepositoryProtocol] = None) -> None:
        self._repo: AccountRepositoryProtocol = repo or AccountRepository()

    async def get_balance(self, db: AsyncSession, user_id: str) -> BalanceResponse:
        account = await self._repo.get_account_by_user_id(db, user_id)
        if account is None:
            raise InternalError(f"Account not found for user {user_id}")
        return BalanceResponse.from_cents(
            user_id=user_id,
            available=account.available_balance,
            frozen=account.frozen_balance,
        )

    async def deposit(
        self, db: AsyncSession, user_id: str, amount_cents: int
    ) -> DepositResponse:
        async with db.begin():
            account, entry = await self._repo.deposit(db, user_id, amount_cents)
        return DepositResponse.from_result(
            available=account.available_balance,
            amount=amount_cents,
            entry_id=entry.id,
        )

    async def withdraw(
        self, db: AsyncSession, user_id: str, amount_cents: int
    ) -> WithdrawResponse:
        async with db.begin():
            account, entry = await self._repo.withdraw(db, user_id, amount_cents)
        return WithdrawResponse.from_result(
            available=account.available_balance,
            amount=amount_cents,
            entry_id=entry.id,
        )

    async def list_ledger(
        self,
        db: AsyncSession,
        user_id: str,
        cursor: Optional[str],
        limit: int,
        entry_type: Optional[str],
    ) -> LedgerResponse:
        cursor_id = cursor_decode(cursor)
        # Fetch limit+1 to detect has_more
        entries = await self._repo.list_ledger_entries(
            db, user_id, cursor_id, limit + 1, entry_type
        )
        has_more = len(entries) > limit
        page = entries[:limit]

        items = [
            LedgerEntryItem(
                id=e.id,
                entry_type=e.entry_type,
                amount_cents=e.amount,
                amount_display=cents_to_display(e.amount),
                balance_after_cents=e.balance_after,
                balance_after_display=cents_to_display(e.balance_after),
                reference_type=e.reference_type,
                reference_id=e.reference_id,
                description=e.description,
                created_at=e.created_at.isoformat() if e.created_at else "",
            )
            for e in page
        ]

        next_cursor = cursor_encode(page[-1].id) if has_more and page else None
        return LedgerResponse(items=items, next_cursor=next_cursor, has_more=has_more)
```

### Step 4: 运行确认测试通过

```bash
uv run pytest tests/unit/test_account_service.py -v
```
Expected: 5 tests PASSED

### Step 5: Lint + commit

```bash
uv run ruff check src/pm_account/application/ tests/unit/test_account_service.py
git add src/pm_account/application/service.py tests/unit/test_account_service.py
git commit -m "feat(pm_account): add AccountApplicationService with deposit/withdraw/ledger"
```

---

## Task 7: API 层 — Router + main.py 接入

**Files:**
- Create: `src/pm_account/api/router.py`
- Modify: `src/main.py` (append 2 lines)

### Step 1: 实现 api/router.py

```python
# src/pm_account/api/router.py
"""pm_account REST API — 4 endpoints, all require JWT authentication."""

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_account.application.schemas import DepositRequest, WithdrawRequest
from src.pm_account.application.service import AccountApplicationService
from src.pm_common.database import get_db_session
from src.pm_common.response import ApiResponse, success_response
from src.pm_gateway.auth.dependencies import get_current_user
from src.pm_gateway.user.db_models import UserModel

router = APIRouter(prefix="/account", tags=["account"])

_service = AccountApplicationService()


@router.get("/balance")
async def get_balance(
    current_user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    request: Request,
) -> ApiResponse:
    data = await _service.get_balance(db, str(current_user.id))
    resp = success_response(data.model_dump())
    resp.request_id = getattr(request.state, "request_id", resp.request_id)
    return resp


@router.post("/deposit")
async def deposit(
    body: DepositRequest,
    current_user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    request: Request,
) -> ApiResponse:
    data = await _service.deposit(db, str(current_user.id), body.amount_cents)
    resp = success_response(data.model_dump())
    resp.request_id = getattr(request.state, "request_id", resp.request_id)
    return resp


@router.post("/withdraw")
async def withdraw(
    body: WithdrawRequest,
    current_user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    request: Request,
) -> ApiResponse:
    data = await _service.withdraw(db, str(current_user.id), body.amount_cents)
    resp = success_response(data.model_dump())
    resp.request_id = getattr(request.state, "request_id", resp.request_id)
    return resp


@router.get("/ledger")
async def list_ledger(
    current_user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    request: Request,
    cursor: Optional[str] = Query(None, description="Pagination cursor (opaque Base64)"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
    entry_type: Optional[str] = Query(None, description="Filter by LedgerEntryType"),
) -> ApiResponse:
    data = await _service.list_ledger(
        db, str(current_user.id), cursor, limit, entry_type
    )
    resp = success_response(data.model_dump())
    resp.request_id = getattr(request.state, "request_id", resp.request_id)
    return resp
```

### Step 2: 修改 src/main.py — 接入 router

在 `src/main.py` 的 `app.include_router(auth_router, prefix="/api/v1")` 一行之后，添加：

```python
from src.pm_account.api.router import router as account_router
# ...
app.include_router(account_router, prefix="/api/v1")
```

完整修改后的 `src/main.py` 末尾区域：
```python
app.include_router(auth_router, prefix="/api/v1")
app.include_router(account_router, prefix="/api/v1")  # ← 新增这一行
```

同时在文件顶部 imports 区域添加：
```python
from src.pm_account.api.router import router as account_router
```

### Step 3: mypy 类型检查

```bash
uv run mypy src/pm_account/ src/main.py
```
Expected: Success: no issues found

### Step 4: 运行全部测试（确保无回归）

```bash
uv run pytest tests/unit/ -v
```
Expected: All existing + new unit tests PASSED

### Step 5: Commit

```bash
git add src/pm_account/api/router.py src/main.py
git commit -m "feat(pm_account): add REST router (balance/deposit/withdraw/ledger) and wire into main"
```

---

## Task 8: 集成测试

**Files:**
- Create: `tests/integration/test_account_flow.py`

Pre-condition: `make up && make migrate` — 需要真实的 PG + Redis。

### Step 1: 写集成测试

```python
# tests/integration/test_account_flow.py
"""Integration tests for pm_account endpoints (requires running PG + Redis).

Pre-condition: make up && make migrate

Uses the session-scoped client fixture from tests/integration/conftest.py.
All tests share one event loop — avoids asyncpg pool cross-loop error.
"""

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unique_user() -> dict[str, str]:
    uid = uuid.uuid4().hex[:8]
    return {
        "username": f"acct_{uid}",
        "email": f"acct_{uid}@example.com",
        "password": "TestPass1",
    }


async def _register_and_login(client: AsyncClient) -> str:
    """Register a fresh user and return the access token."""
    user = _unique_user()
    await client.post("/api/v1/auth/register", json=user)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": user["username"], "password": user["password"]},
    )
    return resp.json()["data"]["access_token"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGetBalance:
    async def test_unauthenticated_returns_401(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/account/balance")
        assert resp.status_code == 401

    async def test_new_user_has_zero_balance(self, client: AsyncClient) -> None:
        token = await _register_and_login(client)
        resp = await client.get(
            "/api/v1/account/balance",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["available_balance_cents"] == 0
        assert data["frozen_balance_cents"] == 0
        assert data["available_balance_display"] == "$0.00"


class TestDeposit:
    async def test_deposit_increases_balance(self, client: AsyncClient) -> None:
        token = await _register_and_login(client)
        headers = {"Authorization": f"Bearer {token}"}

        resp = await client.post(
            "/api/v1/account/deposit",
            json={"amount_cents": 100000},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["available_balance_cents"] == 100000
        assert data["deposited_cents"] == 100000
        assert data["deposited_display"] == "$1,000.00"
        assert data["ledger_entry_id"] > 0

    async def test_deposit_zero_rejected(self, client: AsyncClient) -> None:
        token = await _register_and_login(client)
        resp = await client.post(
            "/api/v1/account/deposit",
            json={"amount_cents": 0},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422

    async def test_unauthenticated_deposit_returns_401(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/account/deposit", json={"amount_cents": 1000})
        assert resp.status_code == 401


class TestWithdraw:
    async def test_withdraw_decreases_balance(self, client: AsyncClient) -> None:
        token = await _register_and_login(client)
        headers = {"Authorization": f"Bearer {token}"}

        await client.post("/api/v1/account/deposit", json={"amount_cents": 50000}, headers=headers)
        resp = await client.post(
            "/api/v1/account/withdraw",
            json={"amount_cents": 20000},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["available_balance_cents"] == 30000
        assert data["withdrawn_cents"] == 20000
        assert data["ledger_entry_id"] > 0

    async def test_withdraw_insufficient_balance_returns_422(self, client: AsyncClient) -> None:
        token = await _register_and_login(client)
        resp = await client.post(
            "/api/v1/account/withdraw",
            json={"amount_cents": 99999999},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == 2001  # InsufficientBalanceError


class TestListLedger:
    async def test_empty_ledger(self, client: AsyncClient) -> None:
        token = await _register_and_login(client)
        resp = await client.get(
            "/api/v1/account/ledger",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["items"] == []
        assert data["has_more"] is False
        assert data["next_cursor"] is None

    async def test_deposit_creates_ledger_entry(self, client: AsyncClient) -> None:
        token = await _register_and_login(client)
        headers = {"Authorization": f"Bearer {token}"}

        await client.post("/api/v1/account/deposit", json={"amount_cents": 5000}, headers=headers)
        resp = await client.get("/api/v1/account/ledger", headers=headers)
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["entry_type"] == "DEPOSIT"
        assert items[0]["amount_cents"] == 5000

    async def test_ledger_ordered_descending(self, client: AsyncClient) -> None:
        token = await _register_and_login(client)
        headers = {"Authorization": f"Bearer {token}"}

        # Create two deposits
        await client.post("/api/v1/account/deposit", json={"amount_cents": 1000}, headers=headers)
        await client.post("/api/v1/account/deposit", json={"amount_cents": 2000}, headers=headers)

        resp = await client.get("/api/v1/account/ledger", headers=headers)
        items = resp.json()["data"]["items"]
        assert len(items) == 2
        # Most recent first (descending by id)
        assert items[0]["id"] > items[1]["id"]

    async def test_unauthenticated_ledger_returns_401(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/account/ledger")
        assert resp.status_code == 401
```

### Step 2: 确认 Docker 服务运行

```bash
make up       # 启动 PG + Redis (如未运行)
uv run alembic current  # 应显示 011 (head)
```

### Step 3: 运行集成测试

```bash
uv run pytest tests/integration/test_account_flow.py -v
```
Expected: 10 tests PASSED

### Step 4: 运行全量测试确认无回归

```bash
uv run pytest
```
Expected: All tests PASSED (unit + integration)

```bash
uv run ruff check src/ tests/
uv run mypy src/
```
Expected: 零报错

### Step 5: Commit

```bash
git add tests/integration/test_account_flow.py
git commit -m "test(pm_account): add integration tests for balance/deposit/withdraw/ledger"
```

---

## 完成验收标准

```
✅ GET  /api/v1/account/balance  → 200，返回 available/frozen/total + display
✅ POST /api/v1/account/deposit  → 200，余额增加，写 DEPOSIT 流水
✅ POST /api/v1/account/withdraw → 200，余额减少；余额不足 → 422 (2001)
✅ GET  /api/v1/account/ledger   → 200，降序分页，游标翻页可用
✅ 未认证所有接口 → 401
✅ make test 全绿（单元 + 集成）
✅ make lint 零报错
✅ make typecheck 零报错
```

---

## 偏差记录

（实现时如有偏差，在此补充）

| 偏差 | 原设计 | 实际决定 | 理由 |
|------|--------|---------|------|
| | | | |

---

*计划版本: v1.0 | 日期: 2026-02-20 | 状态: ✅ 全部完成*
