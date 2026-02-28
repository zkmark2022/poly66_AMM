# pm_market 模块设计文档

> **版本**: v1.0
> **日期**: 2026-02-20
> **状态**: 已审核 ✅
> **对齐文档**: `02_API接口契约.md §4`、`01_全局约定与数据库设计.md §2.3`、`04_WAL预写日志与故障恢复设计.md §2.1`

---

## 1. 模块目标

实现 `pm_market` 模块，提供预测话题的**只读查询**接口：

| 端点 | 说明 |
|------|------|
| `GET /api/v1/markets` | 话题列表（游标分页，按 status/category 过滤） |
| `GET /api/v1/markets/{market_id}` | 话题详情（全量字段） |
| `GET /api/v1/markets/{market_id}/orderbook` | 订单簿快照（DB 聚合，YES/NO 双视角） |

Module 4 范围：**只读**。状态变更（裁决、暂停、作废）属于 Module 9（Admin）。

---

## 2. 架构设计

### 2.1 四层 DDD（与 pm_account 一致）

```
src/pm_market/
  domain/
    models.py         # Market / OrderbookSnapshot / PriceLevel dataclass
    repository.py     # MarketRepositoryProtocol (typing.Protocol)
  infrastructure/
    db_models.py      # MarketORM (SQLAlchemy DeclarativeBase)
    persistence.py    # MarketRepository — 3 个 SQL 方法
  application/
    schemas.py        # Pydantic 响应模型 + cursor 工具
    service.py        # MarketApplicationService（薄胶水层）
  api/
    router.py         # 3 个 GET 端点
```

### 2.2 Domain Models

```python
# domain/models.py

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
    price_cents: int
    total_quantity: int


@dataclass
class OrderbookSnapshot:
    market_id: str
    yes_bids: list[PriceLevel]   # YES 买盘，降序
    yes_asks: list[PriceLevel]   # YES 卖盘，升序
    last_trade_price_cents: int | None
    updated_at: datetime
```

### 2.3 Repository Protocol

```python
# domain/repository.py

class MarketRepositoryProtocol(Protocol):
    async def list_markets(
        self,
        db: AsyncSession,
        status: str | None,       # None = ACTIVE
        category: str | None,
        cursor_ts: str | None,    # ISO 字符串，复合游标的时间部分
        cursor_id: str | None,    # market.id，复合游标的 id 部分
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

---

## 3. 数据访问层

### 3.1 list_markets SQL

```sql
SELECT id, title, description, category, status,
       min_price_cents, max_price_cents,
       maker_fee_bps, taker_fee_bps,
       reserve_balance, total_yes_shares, total_no_shares,
       trading_start_at, resolution_date,
       created_at, updated_at
FROM markets
WHERE
  (:status IS NULL OR status = :status)
  AND (:category IS NULL OR category = :category)
  AND (
    CAST(:cursor_ts AS TIMESTAMPTZ) IS NULL
    OR (created_at, id) < (CAST(:cursor_ts AS TIMESTAMPTZ), :cursor_id)
  )
ORDER BY created_at DESC, id DESC
LIMIT :limit
```

**游标格式**：`Base64({"ts": "<created_at ISO>", "id": "<market_id>"})` — 复合游标保证 VARCHAR PK 场景下翻页稳定。

**status 参数处理**：
- 不传 → 默认 `ACTIVE`（API 契约 §4.1）
- 传 `ALL` → 返回全部状态
- 传具体状态值 → 过滤

### 3.2 get_market_by_id SQL

```sql
SELECT * FROM markets WHERE id = :market_id
```

### 3.3 get_orderbook_snapshot SQL

两条查询，在 Python 层合并：

**Step 1：活跃挂单聚合**
```sql
SELECT book_price, book_direction,
       SUM(remaining_quantity) AS total_qty
FROM orders
WHERE market_id = :market_id
  AND status IN ('OPEN', 'PARTIALLY_FILLED')
GROUP BY book_price, book_direction
ORDER BY book_price
```

**Step 2：最新成交价**
```sql
SELECT trade_price, created_at
FROM trades
WHERE market_id = :market_id
ORDER BY created_at DESC
LIMIT 1
```

**Python 层处理**：
1. 按 `book_direction` 分离 BUY（bids）和 SELL（asks）
2. bids 降序排列，取前 `levels` 档
3. asks 升序排列，取前 `levels` 档
4. 返回 `OrderbookSnapshot`（YES 视角，NO 转换在 schema 层做）

---

## 4. API 层

### 4.1 查询参数

| 端点 | 参数 | 默认 | 约束 |
|------|------|------|------|
| `GET /markets` | `status` | `ACTIVE` | MarketStatus 枚举值或 `ALL` |
| | `category` | `null` | 任意字符串 |
| | `limit` | `20` | 1–100 |
| | `cursor` | `null` | Base64 游标 |
| `GET /markets/{id}/orderbook` | `levels` | `10` | 1–99 |

### 4.2 YES/NO 双视角转换（在 schemas.py 实现）

```python
# yes.bids (降序) → no.asks (升序): price = 100 - yes_bid_price, qty 对应
# yes.asks (升序) → no.bids (降序): price = 100 - yes_ask_price, qty 对应

no_bids = [PriceLevelOut(price_cents=100 - lv.price_cents, total_quantity=lv.total_quantity)
           for lv in reversed(snapshot.yes_asks)]  # 升序 → 降序反转
no_asks = [PriceLevelOut(price_cents=100 - lv.price_cents, total_quantity=lv.total_quantity)
           for lv in reversed(snapshot.yes_bids)]   # 降序 → 升序反转
```

### 4.3 错误处理

| 场景 | HTTP | 错误码 |
|------|------|--------|
| `market_id` 不存在 | 404 | 3001 |
| orderbook 查询非 ACTIVE 话题 | 422 | 3002 |

详情端点（`/markets/{id}`）不限制状态——运营人员需要查看 SETTLED/VOIDED 话题。

### 4.4 响应 Schema 差异

**MarketListItem**（轻量）：省略 `pnl_pool`、`max_order_*` 风控参数、`resolved_at`/`settled_at`。

**MarketDetail**（全量）：API 契约 §4.2 定义的所有字段，包含双字段金额（`_cents` + `_display`）。

---

## 5. 测试策略

### 单元测试

| 文件 | 覆盖内容 |
|------|---------|
| `tests/unit/test_market_schemas.py` | cursor 编解码（往返）、cursor=None 处理、YES→NO 转换逻辑（纯函数） |
| `tests/unit/test_market_service.py` | mock repo，list/detail/orderbook 各场景，市场不存在抛 MarketNotFoundError |

### 集成测试

| 测试用例 | 说明 |
|---------|------|
| 列表返回 3 条 ACTIVE 市场 | 依赖种子数据 |
| category 过滤（crypto → 2 条） | |
| cursor 翻页（limit=2，翻到第 2 页） | |
| 详情返回全字段 | |
| 未知 market_id → 404 (3001) | |
| orderbook 空市场 → bids:[], asks:[] | |
| orderbook levels=2 截断 | |
| 未认证请求 → 401 | |

---

## 6. main.py 接入

```python
from src.pm_market.api.router import router as market_router
app.include_router(market_router, prefix="/api/v1")
```

---

## 7. 假设与取舍记录（Assumptions & Deferred Decisions）

> 将来弥补时对照此表。

| # | 类型 | 内容 | 影响 | 建议弥补时机 |
|---|------|------|------|------------|
| A1 | 取舍 | `orderbook` 从 DB 聚合，非内存 OrderBook | 高并发下比内存慢（毫秒级 vs 微秒级） | Module 6 实现 pm_matching 后，替换 `get_orderbook_snapshot` 实现为内存读取 |
| A2 | 取舍 | `last_trade_price_cents` 从 DB `trades` 表读取最新一条 | Module 5 之前恒为 null，正确但无信息量 | Module 5 完成后自动有值，无需修改 |
| A3 | 假设 | `status=ALL` 作为特殊值返回全部状态 | 若未来需要多状态过滤（如 `status=ACTIVE,SUSPENDED`），需修改 | Module 9 如有运营需求时扩展 |
| A4 | 取舍 | 游标用 `(created_at DESC, id DESC)` 复合排序 | VARCHAR id 无自增序，alphabetical 排序不等于创建序，复合游标更稳定 | 话题数量极小（< 100），性能不是问题；若话题数 > 10K 再优化 |
| A5 | 取舍 | NO 视角转换在 Python schema 层完成，非 DB | 少量 Python 计算，无额外 DB 查询 | 无需弥补 |
| A6 | 假设 | `detail` 端点不限制 status（SETTLED/VOIDED 也可查） | 前端可能展示已结算话题 | 如有安全需求（隐藏已结算话题）在 Module 9 加权限控制 |
| A7 | 取舍 | `list_markets` 只返回轻量字段（无 pnl_pool 等） | 前端列表页不显示风控参数 | 如前端需要，可在 list 参数加 `include_detail=true` 扩展 |
| A8 | 取舍 | `orderbook` 不对 HALTED/SUSPENDED 做特殊处理（与 ACTIVE 一样返回快照） | 用户可能在话题暂停时仍看到订单簿 | Module 9 实现状态变更后，可在此加状态检查或 warning 字段 |

---

## 8. 偏差记录

> 实施完成后填写。

| 偏差 | 说明 |
|------|------|
| （待填写） | |

---

*设计版本: v1.0 | 日期: 2026-02-20 | 状态: 已审核，待实施*
