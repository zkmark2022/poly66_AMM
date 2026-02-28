# Module 3：pm_account 账户模块 — 设计文档

> **版本**: v1.0
> **日期**: 2026-02-20
> **状态**: 已确认，待实施
> **对齐文档**:
> - `Planning/Detail_Design/01_全局约定与数据库设计.md` v2.3
> - `Planning/Detail_Design/02_API接口契约.md` v1.2
> - `Planning/预测市场平台_完整实施计划_v4_Python.md`
> **实施计划**: `2026-02-20-pm-account-plan.md`

---

## 一、模块职责

`pm_account` 负责用户资金账户管理和持仓数据维护。是撮合、清算模块的数据基础。

**包含**：
- 余额查询（available + frozen）
- 模拟充值 / 提现（MVP 无真实支付）
- 流水记录查询（游标分页）
- 内部接口：冻结/解冻资金、冻结/解冻持仓（供 pm_order、pm_matching 调用）
- `Position` 域对象 + ORM（REST 查询在 Module 8 实现）
- `events.py` 骨架（TODO: 领域事件发布）
- `cache.py` 骨架（TODO: 热点账户缓存）

**不包含**：
- 持仓 REST 查询接口（Module 8 实现：`GET /api/v1/positions`）
- 真实支付集成
- Redis 余额缓存（MVP 全走 PostgreSQL）

---

## 二、文件结构

```
src/pm_account/
├── __init__.py
├── domain/
│   ├── __init__.py
│   ├── models.py          # Account, Position, LedgerEntry dataclasses
│   ├── enums.py           # EntryType StrEnum (重新从 pm_common 导出或本地定义)
│   ├── repository.py      # AccountRepositoryProtocol (typing.Protocol)
│   ├── service.py         # AccountDomainService (纯业务逻辑，无 DB 依赖)
│   ├── events.py          # TODO 骨架：领域事件
│   └── cache.py           # TODO 骨架：热点缓存接口
├── infrastructure/
│   ├── __init__.py
│   ├── db_models.py       # AccountORM, PositionORM, LedgerEntryORM (SQLAlchemy)
│   └── persistence.py     # AccountRepository 实现 (AsyncSession → 原子 SQL)
├── application/
│   ├── __init__.py
│   ├── schemas.py         # Pydantic 请求/响应 Schema
│   └── service.py         # AccountApplicationService (薄层，组合 domain + infra)
└── api/
    ├── __init__.py
    └── router.py          # 4 个 REST 端点
```

---

## 三、Domain 层

### 3.1 Domain Models

```python
# domain/models.py
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

@dataclass
class Account:
    id: str                   # UUID str
    user_id: str              # VARCHAR(64)
    available_balance: int    # cents (BIGINT)
    frozen_balance: int       # cents (BIGINT)
    version: int              # optimistic lock
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
    yes_cost_sum: int = 0        # cents，总购入成本，非均价
    yes_pending_sell: int = 0    # 已冻结等待卖出的 YES 份数
    no_volume: int = 0
    no_cost_sum: int = 0         # cents，总购入成本，非均价
    no_pending_sell: int = 0     # 已冻结等待卖出的 NO 份数
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class LedgerEntry:
    id: int                       # BIGSERIAL
    user_id: str
    entry_type: str               # LedgerEntryType value
    amount: int                   # cents, 正=收入 负=支出
    balance_after: int            # cents, 操作后余额快照
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None
    description: Optional[str] = None
    created_at: Optional[datetime] = None
```

### 3.2 EntryType

直接使用 `pm_common.enums.LedgerEntryType`（16 个值，与 DB CHECK 约束完全一致）：

```python
# domain/enums.py
from src.pm_common.enums import LedgerEntryType  # re-export
__all__ = ["LedgerEntryType"]
```

### 3.3 AccountRepositoryProtocol

```python
# domain/repository.py
from typing import Protocol, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from .models import Account, Position, LedgerEntry

class AccountRepositoryProtocol(Protocol):
    async def get_account_by_user_id(self, db: AsyncSession, user_id: str) -> Optional[Account]: ...
    async def deposit(self, db: AsyncSession, user_id: str, amount: int) -> tuple[Account, LedgerEntry]: ...
    async def withdraw(self, db: AsyncSession, user_id: str, amount: int) -> tuple[Account, LedgerEntry]: ...
    async def freeze_funds(self, db: AsyncSession, user_id: str, amount: int, ref_type: str, ref_id: str, desc: str) -> tuple[Account, LedgerEntry]: ...
    async def unfreeze_funds(self, db: AsyncSession, user_id: str, amount: int, ref_type: str, ref_id: str, desc: str) -> tuple[Account, LedgerEntry]: ...
    async def get_or_create_position(self, db: AsyncSession, user_id: str, market_id: str) -> Position: ...
    async def freeze_yes_position(self, db: AsyncSession, user_id: str, market_id: str, quantity: int) -> Position: ...
    async def unfreeze_yes_position(self, db: AsyncSession, user_id: str, market_id: str, quantity: int) -> Position: ...
    async def freeze_no_position(self, db: AsyncSession, user_id: str, market_id: str, quantity: int) -> Position: ...
    async def unfreeze_no_position(self, db: AsyncSession, user_id: str, market_id: str, quantity: int) -> Position: ...
    async def list_ledger_entries(self, db: AsyncSession, user_id: str, cursor_id: Optional[int], limit: int, entry_type: Optional[str]) -> list[LedgerEntry]: ...
```

### 3.4 AccountDomainService

Domain Service 只做**纯业务逻辑校验**（无 DB 依赖），实际 DB 操作委托给 Repository：

```python
# domain/service.py
class AccountDomainService:
    """
    纯业务规则校验层。不直接执行 SQL，由 Repository 原子完成。
    主要作用：前置校验、错误映射、业务组合。
    """

    # deposit: amount 必须 > 0
    # withdraw: amount 必须 > 0，account.available_balance 必须 >= amount (DB 原子保证)
    # freeze_funds: amount 必须 > 0（余额检查由 DB 原子 UPDATE 保证）
    # unfreeze_funds: amount 必须 > 0
    # freeze_yes_position: quantity 必须 > 0，pos.yes_volume - pos.yes_pending_sell 必须 >= qty
    # freeze_no_position: quantity 必须 > 0，pos.no_volume - pos.no_pending_sell 必须 >= qty
```

### 3.5 events.py (骨架)

```python
# domain/events.py
# TODO: 领域事件发布（Phase 2）
# BalanceChanged, PositionChanged 等事件
# 用于解耦撮合后的异步通知
```

### 3.6 cache.py (骨架)

```python
# domain/cache.py
# TODO: 热点账户 Redis 缓存（Phase 2）
# 高频余额查询缓存，5秒 TTL
# 写操作必须先 DB，成功后 invalidate cache
```

---

## 四、Infrastructure 层

### 4.1 ORM Models

```python
# infrastructure/db_models.py
from sqlalchemy import BigInteger, String, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass

class AccountORM(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    available_balance: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    frozen_balance: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class PositionORM(Base):
    __tablename__ = "positions"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    yes_volume: Mapped[int] = mapped_column(BigInteger, default=0)
    yes_cost_sum: Mapped[int] = mapped_column(BigInteger, default=0)
    yes_pending_sell: Mapped[int] = mapped_column(BigInteger, default=0)
    no_volume: Mapped[int] = mapped_column(BigInteger, default=0)
    no_cost_sum: Mapped[int] = mapped_column(BigInteger, default=0)
    no_pending_sell: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class LedgerEntryORM(Base):
    __tablename__ = "ledger_entries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(30), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reference_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    reference_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

### 4.2 原子余额操作（核心设计）

```python
# infrastructure/persistence.py
# 充值 — 直接增加余额
UPDATE_DEPOSIT_SQL = text("""
    UPDATE accounts
    SET available_balance = available_balance + :amount,
        version = version + 1,
        updated_at = NOW()
    WHERE user_id = :user_id
    RETURNING id, user_id, available_balance, frozen_balance, version, created_at, updated_at
""")

# 提现 — 原子检查并扣减（0 行 = 余额不足）
UPDATE_WITHDRAW_SQL = text("""
    UPDATE accounts
    SET available_balance = available_balance - :amount,
        version = version + 1,
        updated_at = NOW()
    WHERE user_id = :user_id AND available_balance >= :amount
    RETURNING id, user_id, available_balance, frozen_balance, version, created_at, updated_at
""")

# 冻结资金 — 可用转冻结（0 行 = 余额不足）
UPDATE_FREEZE_SQL = text("""
    UPDATE accounts
    SET available_balance = available_balance - :amount,
        frozen_balance = frozen_balance + :amount,
        version = version + 1,
        updated_at = NOW()
    WHERE user_id = :user_id AND available_balance >= :amount
    RETURNING id, user_id, available_balance, frozen_balance, version, created_at, updated_at
""")

# 解冻资金 — 冻结转可用
UPDATE_UNFREEZE_SQL = text("""
    UPDATE accounts
    SET available_balance = available_balance + :amount,
        frozen_balance = frozen_balance - :amount,
        version = version + 1,
        updated_at = NOW()
    WHERE user_id = :user_id AND frozen_balance >= :amount
    RETURNING id, user_id, available_balance, frozen_balance, version, created_at, updated_at
""")
```

所有操作均在同一事务内写入 `ledger_entries`：
```python
INSERT INTO ledger_entries (user_id, entry_type, amount, balance_after, ...)
VALUES (:user_id, :entry_type, :amount, :balance_after, ...)
RETURNING id
```

若 `UPDATE RETURNING` 返回 0 行，抛出 `InsufficientBalanceError(required, available)`。

---

## 五、Application 层

### 5.1 Pydantic Schemas

```python
# application/schemas.py

class BalanceResponse(BaseModel):
    user_id: str
    available_balance_cents: int
    available_balance_display: str   # "$1,500.00"
    frozen_balance_cents: int
    frozen_balance_display: str
    total_balance_cents: int
    total_balance_display: str

class DepositRequest(BaseModel):
    amount_cents: int   # > 0

class DepositResponse(BaseModel):
    available_balance_cents: int
    available_balance_display: str
    deposited_cents: int
    deposited_display: str
    ledger_entry_id: int

class WithdrawRequest(BaseModel):
    amount_cents: int   # > 0, <= available_balance

class WithdrawResponse(BaseModel):
    available_balance_cents: int
    available_balance_display: str
    withdrawn_cents: int
    withdrawn_display: str
    ledger_entry_id: int

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
    created_at: str   # ISO8601

class LedgerResponse(BaseModel):
    items: list[LedgerEntryItem]
    next_cursor: Optional[str]   # base64({"id": last_id})
    has_more: bool
```

金额 display 格式化由 `pm_common.cents.display(amount: int) -> str` 提供（已实现）。

### 5.2 AccountApplicationService

```python
# application/service.py
class AccountApplicationService:
    """薄层，负责组合 Repository 调用 + Schema 转换"""

    def __init__(self, repo: AccountRepositoryProtocol) -> None:
        self._repo = repo

    async def get_balance(self, db: AsyncSession, user_id: str) -> BalanceResponse: ...
    async def deposit(self, db: AsyncSession, user_id: str, amount_cents: int) -> DepositResponse: ...
    async def withdraw(self, db: AsyncSession, user_id: str, amount_cents: int) -> WithdrawResponse: ...
    async def list_ledger(self, db: AsyncSession, user_id: str, cursor: Optional[str], limit: int, entry_type: Optional[str]) -> LedgerResponse: ...
```

---

## 六、API 层

**Base prefix**: `/api/v1`，所有接口需要认证（`Depends(get_current_user)`）

| # | 方法 | 路径 | HTTP 成功码 | 说明 |
|---|------|------|------------|------|
| 1 | GET | `/account/balance` | 200 | 查询当前用户余额 |
| 2 | POST | `/account/deposit` | 200 | 模拟充值 |
| 3 | POST | `/account/withdraw` | 200 | 模拟提现 |
| 4 | GET | `/account/ledger` | 200 | 资金流水（游标分页） |

**`GET /account/ledger` 查询参数**：
- `cursor`: 游标（Base64 编码，可选）
- `limit`: 返回条数，默认 20，最大 100
- `entry_type`: 按类型过滤（可选，LedgerEntryType 值）

**游标规则**（按 API 契约 §1.5）：
- 降序：`WHERE id < :cursor_id ORDER BY id DESC LIMIT :limit`
- `next_cursor = base64(json.dumps({"id": last_entry.id}))`
- 无更多数据时 `next_cursor = null`，`has_more = false`

---

## 七、内部接口（供 pm_order、pm_matching 调用）

下列方法通过 `AccountApplicationService` 或直接 `Repository` 暴露，不走 HTTP：

| 方法 | 调用方 | 说明 |
|------|--------|------|
| `freeze_funds(user_id, amount, ref_type, ref_id, desc)` | pm_order | 下单时冻结保证金 |
| `unfreeze_funds(user_id, amount, ref_type, ref_id, desc)` | pm_order | 撤单时解冻 |
| `freeze_yes_position(user_id, market_id, quantity)` | pm_order | Sell YES 时冻结持仓 |
| `unfreeze_yes_position(user_id, market_id, quantity)` | pm_order | 撤 Sell YES 单时解冻 |
| `freeze_no_position(user_id, market_id, quantity)` | pm_order | Sell NO 时冻结持仓 |
| `unfreeze_no_position(user_id, market_id, quantity)` | pm_order | 撤 Sell NO 单时解冻 |

这些方法在 Module 5 实现时会被调用，Module 3 仅实现其签名和 DB 操作。

---

## 八、并发安全

- 充值/提现/冻结/解冻均使用 **原子 `UPDATE ... RETURNING`** + 同事务写 ledger，无竞争条件
- `accounts.version` 字段用于 debug / future optimistic locking（MVP 不依赖）
- Position 操作（freeze_yes/no）使用 `UPDATE ... WHERE yes_volume - yes_pending_sell >= qty`，0 行 = `InsufficientPositionError`

---

## 九、测试范围

### 单元测试（mock repo，无 DB 依赖）

| 文件 | 覆盖点 |
|------|--------|
| `tests/unit/test_account_domain.py` | domain service 校验逻辑（amount <= 0、余额不足异常路径）|
| `tests/unit/test_account_schemas.py` | Pydantic schema 校验（amount > 0、cursor 解码、display 格式）|
| `tests/unit/test_account_cursor.py` | 游标 encode/decode 工具函数 |

预计 ~15 个单元测试。

### 集成测试（真实 PG，asyncpg pool）

| 场景 | 预期 |
|------|------|
| 充值后余额增加 | 200，available_balance_cents 正确 |
| 提现成功 | 200，余额减少 |
| 余额不足提现 | 422，错误码 2001 |
| 流水降序返回 | 200，items 按 id 降序 |
| 游标翻页 | 200，next_cursor 可用，has_more 正确 |
| 充值后流水有 DEPOSIT 条目 | 200，entry_type == "DEPOSIT" |
| 未认证访问 | 401 |

预计 ~10 个集成测试。

---

## 十、main.py 改动

```python
# 新增:
from src.pm_account.api.router import router as account_router
app.include_router(account_router, prefix="/api/v1")
```

---

## 十一、偏差记录

| 偏差 | 原设计 | 实际决定 | 理由 |
|------|--------|---------|------|
| 无持仓 REST 接口 | Module 3 含持仓查询 | 推迟到 Module 8 | 撮合完成后才有真实持仓数据，避免空接口；Position ORM 在此实现供内部使用 |
| AccountDomainService 极薄 | 完整 DDD domain service | 前置校验 + 委托 Repository | DB 原子操作已处理大多数业务规则，过度封装无收益 |
| 无 Redis 余额缓存 | cache.py 骨架 | cache.py 仅 TODO 注释 | MVP 阶段 PG 性能足够，避免缓存一致性复杂度 |

---

*设计版本: v1.0 | 日期: 2026-02-20 | 状态: 已确认*
