# AMM 前置改动（撮合引擎侧）— 设计文档

> **版本**: v1.0
> **日期**: 2026-02-28
> **范围**: AMM 机器人上线前，撮合引擎需要完成的所有前置改动
> **对齐来源**: AMM 接口契约 v1.4（Impact Matrix §3）、数据字典 v1.3（§3.1–§3.4）、配置手册 v1.3（§3.8.2）、pm_order 设计文档 v1.0
> **实施计划**: `2026-02-28-amm-prerequisites-plan.md`

---

## 一、实现范围

本阶段（Phase A）覆盖 AMM 接口契约 v1.4 Impact Matrix 中**撮合引擎侧**的全部改动。
这些改动不涉及 AMM 机器人本体代码，仅修改/扩展现有的 pm_clearing、pm_risk、pm_order、pm_gateway、pm_account 模块。

| 改动项 | 涉及模块 | 优先级 | Impact Matrix # |
|--------|---------|--------|-----------------|
| AMM 系统账户预置 | pm_account / Alembic migration | P0 前置 | — |
| `auto_netting_enabled` 清算层判断 | pm_clearing/domain/netting.py + Alembic | P0 | #6 |
| 特权铸造 Mint API | pm_clearing (新增路由 + 多表事务) | P0 | #2 |
| 特权销毁 Burn API | pm_clearing (新增路由 + 多表事务) | P0 | #3 |
| 原子改单 Replace API | pm_order (新增路由 + 原子事务) | P0 | #1 |
| 批量撤单 Batch Cancel API | pm_order (新增路由) | P1 | #4 |
| `is_self_trade` 豁免逻辑 | pm_risk/rules/self_trade.py | P1 | #7 |
| AMM Service Token + 独立限流 | pm_gateway | P1.5 | #8 |

**排除**：
- AMM 机器人本体（Phase B）
- 管理员控制面板 `/api/v1/admin/amm/*`（Impact Matrix #5，后续独立实现）
- Kafka 事件流（Phase 2 目标，MVP 使用 REST 轮询）

---

## 二、架构决策

### 2.1 AMM 专用路由归属

AMM 专用 API（Mint/Burn/Replace/Batch-Cancel）挂载在 `/api/v1/amm/` 前缀下，
但**物理实现**分布在已有模块中，而非创建独立的 `pm_amm` 模块：

```
src/
├── pm_order/
│   └── api/
│       └── amm_router.py       # Replace + Batch-Cancel
│                                # 挂载到 /api/v1/amm/orders/*
├── pm_clearing/
│   └── api/
│       └── amm_router.py       # Mint + Burn
│                                # 挂载到 /api/v1/amm/*
├── pm_account/
│   └── domain/
│       └── constants.py         # AMM_USER_ID, AMM_USERNAME (新增)
└── pm_gateway/
    └── auth/
        └── dependencies.py      # get_current_user 扩展（Service Token 支持）
```

**理由**：Mint/Burn 的核心逻辑是账户余额变更 + 持仓变更 + 流水写入，与 pm_clearing 的职责完全一致。
Replace 是"取消旧单 + 下新单"的原子化，核心依赖 MatchingEngine，归属 pm_order。
避免创建仅有路由转发的 `pm_amm` 壳模块。

### 2.2 DB Schema 变更策略

本阶段需要 2 个 Alembic migration：

1. **accounts 表新增列**：`auto_netting_enabled BOOLEAN NOT NULL DEFAULT TRUE`
2. **预置 AMM 系统账户**：在 `users` + `accounts` 表中插入 AMM 固定行

两个 migration 合并为一个，因为 `auto_netting_enabled` 列和 AMM 账户行是同一功能的组成部分。

### 2.3 认证方案（MVP 阶段）

MVP 阶段 AMM 使用标准 JWT 登录（`amm_market_maker` 用户），不实现 Service Token。
在代码中预留 Service Token 的扩展点（`dependencies.py` 中的 `account_type` 检查），
但当前分支仅检查 `user_id == AMM_USER_ID` 来授予 AMM 专用接口访问权限。

### 2.4 Netting 开关实现选择

**方案 A（选中）**：在 `execute_netting_if_needed` 函数入口读取 `accounts.auto_netting_enabled`，
通过现有 `db` session 查询，若为 `false` 则 `return 0` 跳过。

**方案 B（弃选）**：在内存中缓存 AMM user_id 黑名单。
弃选理由：增加缓存一致性维护负担，且 netting 触发频率不高（每次成交后至多一次）。

### 2.5 Self-Trade 豁免实现选择

**方案 A（选中）**：在 `is_self_trade` 中增加 `SELF_TRADE_EXEMPT_USERS` 集合，
AMM 的 user_id 在集合中则返回 `False`。

**方案 B（弃选）**：移除 self-trade 检查的 AMM 相关假设。
弃选理由：方案 A 更灵活，可扩展支持多个做市商。

---

## 三、目录结构（新增/修改文件）

```
src/
├── pm_account/
│   └── domain/
│       └── constants.py                     # 新增: AMM_USER_ID, AMM_USERNAME
│
├── pm_clearing/
│   ├── domain/
│   │   ├── netting.py                       # 修改: 入口增加 auto_netting_enabled 检查
│   │   ├── mint_service.py                  # 新增: privileged_mint() 业务逻辑
│   │   └── burn_service.py                  # 新增: privileged_burn() 业务逻辑
│   ├── infrastructure/
│   │   └── db_models.py                     # 修改: AccountORM 新增 auto_netting_enabled
│   ├── application/
│   │   └── amm_schemas.py                   # 新增: MintRequest/Response, BurnRequest/Response
│   └── api/
│       └── amm_router.py                    # 新增: POST /mint, POST /burn
│
├── pm_order/
│   ├── application/
│   │   └── amm_schemas.py                   # 新增: ReplaceRequest/Response, BatchCancelRequest/Response
│   └── api/
│       └── amm_router.py                    # 新增: POST /orders/replace, POST /orders/batch-cancel
│
├── pm_risk/
│   └── rules/
│       └── self_trade.py                    # 修改: 增加 SELF_TRADE_EXEMPT_USERS
│
├── pm_gateway/
│   └── auth/
│       └── dependencies.py                  # 修改: 新增 require_amm_user 依赖
│
├── main.py                                  # 修改: 注册 AMM 路由
│
└── alembic/versions/
    └── xxxx_add_amm_prerequisites.py        # 新增: auto_netting_enabled + AMM 预置账户

tests/
├── unit/
│   ├── test_amm_constants.py                # AMM_USER_ID 格式校验
│   ├── test_netting_amm_bypass.py           # auto_netting_enabled=false 跳过
│   ├── test_self_trade_exempt.py            # AMM user_id 豁免
│   ├── test_mint_service.py                 # Mint 业务逻辑（余额、流水、持仓）
│   ├── test_burn_service.py                 # Burn 业务逻辑（余额、流水、持仓）
│   ├── test_replace_logic.py                # Replace 原子性、幂等、部分成交
│   └── test_batch_cancel.py                 # Batch Cancel 逻辑
└── integration/
    ├── test_amm_mint_api.py                 # Mint 端到端
    ├── test_amm_burn_api.py                 # Burn 端到端
    ├── test_amm_replace_api.py              # Replace 端到端
    └── test_amm_netting_bypass.py           # Netting 旁路端到端
```

---

## 四、数据流

### 4.1 Privileged Mint（特权铸造）

```
POST /api/v1/amm/mint
  ↓ require_amm_user (JWT + user_id == AMM_USER_ID)
  ↓ MintRequest 校验 (market_id, quantity > 0, idempotency_key)
  ↓
┌─────────────────────────────────────────────────────────────────┐
│  async with db.begin():                                         │
│                                                                 │
│  Step 1  幂等检查: SELECT ledger_entries                        │
│          WHERE reference_type='AMM_MINT'                        │
│            AND reference_id = idempotency_key                   │
│          → 已存在则直接返回 200 (幂等成功)                       │
│                                                                 │
│  Step 2  校验市场状态: markets.status IN ('ACTIVE')             │
│                                                                 │
│  Step 3  计算成本: cost = quantity × 100                        │
│                                                                 │
│  Step 4  扣款: accounts.available_balance -= cost               │
│          (乐观锁 version check, 余额不足 → 422/2001)           │
│                                                                 │
│  Step 5  增加储备金: markets.reserve_balance += cost            │
│                                                                 │
│  Step 6  增加份额: markets.total_yes_shares += quantity         │
│                   markets.total_no_shares += quantity            │
│                                                                 │
│  Step 7  增加持仓: positions.yes_volume += quantity             │
│                   positions.yes_cost_sum += quantity × 50       │
│                   positions.no_volume += quantity                │
│                   positions.no_cost_sum += quantity × 50        │
│          (若 positions 行不存在则 INSERT)                        │
│                                                                 │
│  Step 8  写入流水 (ledger_entries.id = BIGSERIAL, 省略 id):      │
│   a. ledger_entries: MINT_COST, -(cost), ref=AMM_MINT           │
│   b. ledger_entries: MINT_RESERVE_IN, +(cost)                   │
│                                                                 │
│  Step 9  审计记录: INSERT trades (scenario=MINT,                │
│          buy_user_id=AMM_USER_ID, sell_user_id='SYSTEM',        │
│          price=INITIAL_FAIR_COST_PER_SHARE, quantity=quantity)   │
│                                                                 │
│  ── db.commit() ──                                              │
└─────────────────────────────────────────────────────────────────┘
  ↓
HTTP 201: { minted_quantity, cost_cents, new_yes/no_inventory, remaining_balance }
```

### 4.2 Privileged Burn（特权销毁）

```
POST /api/v1/amm/burn
  ↓ require_amm_user
  ↓ BurnRequest 校验
  ↓
┌─────────────────────────────────────────────────────────────────┐
│  async with db.begin():                                         │
│                                                                 │
│  Step 1  幂等检查 (同 Mint)                                     │
│  Step 2  校验市场状态                                           │
│  Step 3  校验持仓: min(yes_available, no_available) >= quantity │
│          (available = volume - pending_sell)                     │
│                                                                 │
│  Step 4  扣减持仓: positions.yes_volume -= quantity             │
│                   positions.no_volume -= quantity                │
│          cost_sum 按加权平均释放                                 │
│                                                                 │
│  Step 5  扣减储备金: markets.reserve_balance -= quantity × 100  │
│  Step 6  入账: accounts.available_balance += quantity × 100     │
│  Step 7  写入流水 (id=BIGSERIAL): BURN_REVENUE + BURN_RESERVE_OUT│
│  Step 8  审计记录: INSERT trades (scenario=BURN, price=常量)     │
│                                                                 │
│  ── db.commit() ──                                              │
└─────────────────────────────────────────────────────────────────┘
  ↓
HTTP 200: { burned_quantity, recovered_cents, new_yes/no_inventory, remaining_balance }
```

### 4.3 Atomic Replace（原子改单）

```
POST /api/v1/amm/orders/replace
  ↓ require_amm_user
  ↓ ReplaceRequest 校验 (old_order_id, new_order params)
  ↓
┌─────────────────────────────────────────────────────────────────┐
│  MatchingEngine.replace_order(old_order_id, new_params, db):    │
│                                                                 │
│  async with _market_locks[market_id]:                           │
│    async with db.begin():                                       │
│                                                                 │
│  Step 0  幂等: new_params.client_order_id 已存在 → 返回 200    │
│                                                                 │
│  Step 1  加载旧订单: SELECT FOR UPDATE                          │
│    not found         → 6002                                     │
│    owner != AMM      → 6004                                     │
│    status == FILLED  → 6003                                     │
│    filled_qty > 0    → 撤剩余, return 6001 (PARTIALLY_FILLED)  │
│                                                                 │
│  Step 2  撤旧:                                                  │
│    ① orderbook 内存移除                                         │
│    ② 解冻旧单冻结                                               │
│    ③ order.status = CANCELLED                                   │
│                                                                 │
│  Step 3  校验新单:                                              │
│    ① market_id 一致性 (6005)                                    │
│    ② 标准风控: price_range, order_limit                         │
│    ③ check_and_freeze(new_order)                                │
│                                                                 │
│  Step 4  标准下单流程: transform → freeze → match → clear       │
│                                                                 │
│  Step 5  返回: old_order filled_quantity + new_order + trades   │
│                                                                 │
│  ── db.commit() ──                                              │
└─────────────────────────────────────────────────────────────────┘
  ↓
HTTP 200: { old_order_id, old_order_filled_quantity=0, new_order, trades }
```

---

## 五、关键域模型（新增）

### AMM 系统常量

```python
# src/pm_account/domain/constants.py
"""AMM 系统账户唯一标识 — 对齐数据字典 v1.3 §3.1"""

AMM_USER_ID = "00000000-0000-4000-a000-000000000001"
AMM_USERNAME = "amm_market_maker"
AMM_EMAIL = "amm@system.internal"
```

### Mint/Burn Schemas

```python
# src/pm_clearing/application/amm_schemas.py
from pydantic import BaseModel, Field

class MintRequest(BaseModel):
    market_id: str
    quantity: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=128)

class MintResponse(BaseModel):
    market_id: str
    minted_quantity: int
    cost_cents: int
    new_yes_inventory: int
    new_no_inventory: int
    remaining_balance_cents: int

class BurnRequest(BaseModel):
    market_id: str
    quantity: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=128)

class BurnResponse(BaseModel):
    market_id: str
    burned_quantity: int
    recovered_cents: int
    new_yes_inventory: int
    new_no_inventory: int
    remaining_balance_cents: int
```

### Replace Schemas

```python
# src/pm_order/application/amm_schemas.py
from pydantic import BaseModel, Field
from src.pm_order.application.schemas import PlaceOrderRequest

class ReplaceRequest(BaseModel):
    old_order_id: str
    new_order: PlaceOrderRequest

class ReplaceResponse(BaseModel):
    old_order_id: str
    old_order_status: str
    old_order_filled_quantity: int
    old_order_original_quantity: int
    new_order: dict  # OrderResponse
    trades: list[dict]

class BatchCancelRequest(BaseModel):
    market_id: str
    cancel_scope: str = Field(default="ALL", pattern="^(ALL|BUY_ONLY|SELL_ONLY)$")

class BatchCancelResponse(BaseModel):
    market_id: str
    cancelled_count: int
    total_unfrozen_funds_cents: int
    total_unfrozen_yes_shares: int
    total_unfrozen_no_shares: int
```

---

## 六、API 契约摘要

### 新增端点

| # | 方法 | 路径 | HTTP 成功码 | 认证 | 优先级 |
|---|------|------|------------|------|--------|
| 1 | POST | `/api/v1/amm/orders/replace` | 200 | AMM JWT | P0 |
| 2 | POST | `/api/v1/amm/mint` | 201 | AMM JWT | P0 |
| 3 | POST | `/api/v1/amm/burn` | 200 | AMM JWT | P0 |
| 4 | POST | `/api/v1/amm/orders/batch-cancel` | 200 | AMM JWT | P1 |

### 新增错误码（6xxx 段）

| HTTP | 错误码 | 说明 | 端点 |
|------|--------|------|------|
| 422 | 6001 | 旧订单已部分成交（Replace rejected） | Replace |
| 404 | 6002 | 旧订单不存在 | Replace |
| 422 | 6003 | 旧订单已全部成交 | Replace |
| 403 | 6004 | 旧订单不属于 AMM | Replace |
| 422 | 6005 | 新旧订单市场不一致 | Replace |
| 409 | 6006 | 幂等键冲突（Mint/Burn 重复） | Mint, Burn |

### 修改端点（行为变更）

无端点签名变更。内部行为变更：
- `POST /orders`（下单）：成交后 `execute_netting_if_needed` 现在会检查 `auto_netting_enabled`
- `is_self_trade`：对 AMM user_id 返回 `False`（豁免）

---

## 七、DB Migration

### 单次 Migration（合并）

```python
# alembic/versions/xxxx_add_amm_prerequisites.py

"""Add AMM prerequisites: auto_netting_enabled + AMM system account"""

def upgrade():
    # 1. accounts 表新增 auto_netting_enabled 列
    op.add_column('accounts',
        sa.Column('auto_netting_enabled', sa.Boolean(),
                  nullable=False, server_default=sa.text('true')))

    # 2. 插入 AMM 系统用户 (users 表)
    op.execute("""
        INSERT INTO users (id, username, email, password_hash, is_active, created_at, updated_at)
        VALUES (
            '00000000-0000-4000-a000-000000000001',
            'amm_market_maker',
            'amm@system.internal',
            '$2b$12$AMMSystemNoLoginPlac662e4a7b8c9d0e1f2a3b4c5d6e7f8',
            true,
            NOW(),
            NOW()
        )
        ON CONFLICT (id) DO NOTHING;
    """)

    # 3. 插入 AMM 系统账户 (accounts 表), 关闭 auto_netting
    op.execute("""
        INSERT INTO accounts (user_id, available_balance, frozen_balance, version, auto_netting_enabled)
        VALUES (
            '00000000-0000-4000-a000-000000000001',
            0,
            0,
            0,
            false
        )
        ON CONFLICT (user_id) DO UPDATE SET auto_netting_enabled = false;
    """)

def downgrade():
    op.execute("DELETE FROM accounts WHERE user_id = '00000000-0000-4000-a000-000000000001'")
    op.execute("DELETE FROM users WHERE id = '00000000-0000-4000-a000-000000000001'")
    op.drop_column('accounts', 'auto_netting_enabled')
```

---

## 八、测试策略（TDD 执行顺序）

| # | 测试文件 | 覆盖内容 | 关键场景数 |
|---|---------|---------|-----------|
| 1 | `test_amm_constants.py` | UUID 格式校验、常量值 | 3 |
| 2 | `test_netting_amm_bypass.py` | auto_netting_enabled=false 跳过, =true 正常执行 | 4 |
| 3 | `test_self_trade_exempt.py` | AMM 豁免、普通用户正常检测 | 4 |
| 4 | `test_mint_service.py` | 余额扣减、持仓增加、流水写入、幂等、余额不足、市场状态校验 | 8 |
| 5 | `test_burn_service.py` | 持仓扣减、余额入账、流水写入、幂等、持仓不足 | 8 |
| 6 | `test_replace_logic.py` | 原子性、幂等、部分成交拒绝、全成交拒绝、非 AMM 拒绝 | 8 |
| 7 | `test_batch_cancel.py` | ALL/BUY_ONLY/SELL_ONLY、无活跃订单、冻结释放、**ORDER_UNFREEZE 流水写入** | 8 |
| 8 | `test_amm_mint_api.py` (integration) | Mint 端到端: HTTP → DB 验证 | 4 |
| 9 | `test_amm_burn_api.py` (integration) | Burn 端到端 | 4 |
| 10 | `test_amm_replace_api.py` (integration) | Replace 端到端 + 立即成交 | 6 |
| 11 | `test_amm_netting_bypass.py` (integration) | 下单→成交→验证 AMM 不触发 Netting | 3 |

总计约 58 个测试场景。

---

## 九、偏差记录

| # | 偏差点 | 原始设计文档 | 本次实现 | 理由 |
|---|--------|------------|---------|------|
| D-01 | AMM 路由前缀 | 契约文档 §1.3: `/api/v1/amm/` 独立路由 | 物理代码分散在 pm_order + pm_clearing 中 | 按职责归属，避免空壳 pm_amm 模块 |
| D-02 | Service Token | 契约文档 §2.2: 理想为 Service Token | MVP 使用标准 JWT + `require_amm_user` 依赖 | pm_gateway 尚未实现 Service Token，MVP 先用 JWT |
| D-03 | Admin API | Impact Matrix #5 | 本阶段排除 | 管理员控制面板复杂度高，与 AMM 上线非强依赖 |
| D-04 | Kafka 事件流 | 契约文档 §5: Kafka trade_events | 不实现，Phase 2 目标 | MVP 无 Kafka 基础设施，AMM 使用 REST 轮询 |
| D-05 | Migration 拆分 | 通常每个改动独立 migration | 合并为单次 migration | auto_netting_enabled + AMM 账户是同一功能，原子上线 |

---

## 十、v1.0 Review 修正记录

| # | 严重程度 | 修正项 | 原始缺陷 | 修正方案 |
|---|---------|--------|---------|---------|
| R-01 | 🔴 Critical | ledger_entries.id 类型 | Mint/Burn 用 `uuid.uuid4()` 写入 `id` 字段 | `ledger_entries.id` 为 BIGSERIAL 自增，SQL 中省略 `id` 字段 |
| R-02 | 🔴 Critical | Batch Cancel 流水缺失 | 批量解冻只改余额，未写 ORDER_UNFREEZE 流水 | 每个被撤订单写入一条 ORDER_UNFREEZE 流水（对齐契约 v1.1 §3.2） |
| R-03 | 🟠 High | OrderBook 内存安全 | 直接 `_order_index.pop()` 只删索引不删价格层 | 调用 `OrderBook.remove_order()` 同时清理索引和价格层 deque |
| R-04 | 🟠 High | Replace 事务回滚 | `place_order` 异常可能被内部吞掉 | 明确注释：异常必须穿透，触发整个事务回滚保护旧订单 |
| R-05 | 🟡 Minor | 审计 trades 硬编码价格 | `price_cents = 50` 魔数 | 使用常量 `INITIAL_FAIR_COST_PER_SHARE` |
| R-06 | 🟡 Minor | bcrypt 占位符长度 | 51 字符，非标准 bcrypt 60 字符 | 补齐到合规长度 |

---

*设计文档结束*
