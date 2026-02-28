# AMM 接口与事件流契约文档

## AMM 自动做市商机器人 — 系统对接规范

---

### 文档信息

| 项目 | 内容 |
|------|------|
| 文档版本 | v1.4 |
| 状态 | 草稿（待 Review） |
| 适用范围 | AMM 机器人与撮合系统（pm_order/pm_market/pm_account）的对接契约 |
| 对齐文档 | 《AMM 自动做市商机器人具体模块设计 v7.1》、《API 接口契约 v1.2》 |
| 日期 | 2026-02-27 |

---

### 目录

1. 概述与设计边界
2. AMM 系统身份与认证
3. AMM 专用新增 API（撮合引擎需新增实现）
4. AMM 复用的现有 API（撮合引擎已有，AMM 直接调用）
5. 事件流契约：Kafka 消息（撮合 → AMM 方向）
6. 事件流契约：Redis 状态通道（双向）
7. WebSocket 订阅契约（撮合 → AMM 方向）
8. 错误码扩展
9. 接口总览
10. 附录：时序图

---

## 一、概述与设计边界

### 1.1 系统角色

```
┌──────────────────┐          ┌──────────────────┐
│  AMM Bot (消费者)  │  ◄────►  │  撮合系统 (提供者)  │
│                  │          │                  │
│  • Strategy      │  REST    │  • pm_order      │
│  • Risk Control  │  ◄────►  │  • pm_market     │
│  • Connector     │          │  • pm_account    │
│                  │  Kafka   │  • pm_clearing   │
│  • Market        │  ◄─────  │  • pm_matching   │
│    Listener      │          │                  │
└──────────────────┘          └──────────────────┘
```

**AMM 是消费者**：AMM 通过标准 REST API + 少量特权 API 与撮合系统交互，通过 Kafka 接收成交推送。
AMM 不修改撮合引擎内部逻辑，只在外围以"机器人用户"身份参与。

### 1.2 接口分类

| 类别 | 说明 | 数量 |
|------|------|------|
| **AMM 专用新增** | 撮合引擎需新增实现的接口 | 5 个 |
| **现有复用** | 现有 API v1.2 中 AMM 直接调用的接口 | 7 个 |
| **事件流** | Kafka Topic / Redis Channel | 3 个 |
| **WebSocket** | 实时订单簿 + 成交推送 | 2 个 |

### 1.3 全局约定继承

AMM 接口继承《API 接口契约 v1.2》的全部全局约定，包括：

- **Base URL**: `http://localhost:8000/api/v1`（AMM 专用接口在 `/api/v1/amm/` 前缀下）
- **响应格式**: 统一 `{code, message, data, timestamp, request_id}` 封装
- **金额单位**: 全部使用美分（cents），价格范围 [1, 99]
- **错误码体系**: AMM 扩展码在 6xxx 段

---

## 二、AMM 系统身份与认证

### 2.1 AMM 系统账户

AMM 以特殊的"系统机器人用户"身份运行，具有独立的用户账户：

```json
{
    "user_id": "00000000-0000-4000-a000-000000000001",
    "user_alias": "AMM_SYSTEM_001",
    "username": "amm_market_maker",
    "account_type": "SYSTEM_BOT",
    "privileges": [
        "ATOMIC_REPLACE",
        "PRIVILEGED_MINT",
        "BATCH_CANCEL",
        "SELF_TRADE_EXEMPT"
    ]
}
```

> **v1.4 UUID 对齐**（对齐数据字典 v1.3 §3.1 + 全局约定 v2.3）:
> DB 中 `users.id` 为 UUID 类型，AMM 使用固定 UUID `00000000-0000-4000-a000-000000000001`。
> 本文档中的 `AMM_SYSTEM_001` 为可读别名（human-readable alias），在 API 请求和日志中
> 通过 `X-AMM-System-Id` Header 或代码常量 `AMM_USER_ID` 引用实际 UUID。

### 2.2 认证方式

> **⚠️ v1.4 实现状态标注**:
> 当前 pm_gateway 仅实现标准 JWT 用户认证（`POST /api/v1/auth/login`），
> **不支持** Service Token 签发、`account_type` 字段、privilege claims，也不存在 `/api/v1/amm/*` 路由前缀。
>
> | 阶段 | 认证方案 | 限流方案 | 状态 |
> |------|---------|---------|------|
> | **MVP 回退** | AMM 使用标准 JWT 登录（`amm_market_maker` 用户），Token 到期后自动 refresh | 沿用普通用户限流（30次/分钟），AMM 通过自身节流控制报价频率 | 🟢 可用 |
> | **Phase 1.5** | pm_gateway 新增 Service Token 签发端点 + `X-AMM-System-Id` Header 识别 + AMM 专用限流规则 | 600次/分钟下单 + 400次/分钟/市场 Replace | 🔲 待实现 |
>
> **MVP 回退的局限性**：普通用户 30次/分钟限流远低于 AMM 需求（峰值约 240次/分钟），
> 这意味着 MVP 中 AMM 的报价频率将被限制为约 **每 2 秒 1 轮**（而非理想的每 1 秒 1 轮）。
> 若需更高频率，需优先实现 Phase 1.5 的 Service Token + 独立限流。

**理想架构（Phase 1.5+）**：

AMM 使用 **Internal Service Token**（非标准 JWT 用户 Token），区别于普通用户认证：

```
Authorization: Bearer <amm_service_token>
X-AMM-System-Id: AMM_SYSTEM_001
```

| 属性 | 普通用户 JWT | AMM Service Token |
|------|-------------|-------------------|
| 有效期 | 30 分钟 | 24 小时（可自动续期） |
| 签发方 | auth/login | 系统配置/启动时注入 |
| 限流（下单） | 30 次/分钟 | 600 次/分钟 |
| 限流（Replace） | N/A | 400 次/分钟/市场（见下方说明） |
| 自成交检查 | 严格拒绝 | 豁免（STP EXPIRE_TAKER 模式）⚠️ MVP 未实现，见 §2.3 |

**Replace 独立限流说明**:

`POST /api/v1/amm/orders/replace` 独立于普通下单限流，设定为 **400 次/分钟/市场**。
理由：AMM 每个报价周期通常会对同一市场发出 2-4 个 Replace 请求（YES 买/卖 + NO 买/卖），
报价频率约 1-2 秒/轮，因此峰值约 240 次/分钟。400 上限留有约 60% 安全余量。

限流超出时返回 `HTTP 429`，响应头包含：
```
X-RateLimit-Limit: 400
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1740652860
```

### 2.3 权限边界

> **⚠️ v1.3 实现状态对照**:
> 以下权限中，部分为 MVP 代码已有功能的配置化使用，部分需要撮合引擎**新增代码**才能生效。
> 标注 🔲 的项为尚未实现的前置改动。

| 权限 | 说明 | 安全约束 | MVP 实现状态 |
|------|------|----------|-------------|
| ATOMIC_REPLACE | 原子改单 | 仅限修改自身订单 | 🔲 需新增 API 路由 |
| PRIVILEGED_MINT | 特权铸造（初始化 YES/NO 份额） | 需管理员预先审批市场 ID | 🔲 需新增 API 路由 |
| BATCH_CANCEL | 批量撤单 | 仅限撤销自身订单 | 🔲 需新增 API 路由 |
| SELF_TRADE_EXEMPT | 自成交豁免 | AMM 的 YES 买单可与自己的 NO 卖单撮合 | 🔲 需改 `is_self_trade`（P1，见数据字典 §3.4 方案 A/B） |
| AUTO_NETTING_DISABLED | 关闭 Auto-Netting | 见 §2.4 说明 | 🔲 需改 `execute_netting_if_needed`（P0 Blocker） |

### 2.4 AMM 与 Auto-Netting

> **⚠️ 关键规则：AMM 系统账户必须关闭 Auto-Netting。**

v1.2 撮合引擎在普通用户成交后会自动执行 Netting（持有等量 YES + NO → 自动销毁并退款）。
但 AMM **显式持有双边库存**是其做市策略的核心前提——AMM 依赖 YES/NO 双边持仓来提供流动性。

如果撮合引擎对 AMM 账户触发 Auto-Netting，将导致：
1. AMM 的 YES/NO 持仓被意外缩减，破坏库存对称性
2. Redis 缓存的 `amm:inventory` 与 DB 实际持仓不一致（因为 Netting 不经过 AMM 的 Kafka 消费路径）
3. AMM 的 `cost_sum` 计算被外部操作篡改

**撮合引擎实现要求**（🔲 MVP 尚未实现，P0 Blocker）:
- 在 `accounts` 表中为 AMM 系统账户增加 `auto_netting_enabled = false` 标记（DDL 见数据字典 §3.3）
- **关键改动**: 撮合→清算链路中，`execute_netting_if_needed` (pm_clearing/domain/netting.py)
  需在函数入口处读取该标记，若为 `false` 则 `return 0` 跳过 Netting（当前代码对所有用户无差别执行）
- AMM 的库存配对销毁由 AMM 自身通过 `POST /api/v1/amm/burn`（§3.4）主动发起，而非由撮合引擎被动触发

---

## 三、AMM 专用新增 API

> 以下接口需要撮合引擎团队**新增实现**。当前 MVP 代码中**均不存在**这些路由。

**⚠️ v1.3 撮合引擎侧改动清单（Impact Matrix）**:

| # | 改动项 | 涉及模块 | 优先级 | MVP 回退方案 | 说明 |
|---|--------|---------|--------|-------------|------|
| 1 | `POST /api/v1/amm/orders/replace` | pm_order (新增路由 + 原子事务) | P0 | Cancel + Place（非原子，有竞态风险） | AMM 核心报价依赖原子改单 |
| 2 | `POST /api/v1/amm/mint` | pm_clearing (新增路由 + 多表事务) | P0 | ❌ 无回退——必须实现 | 初始化和复投必需 |
| 3 | `POST /api/v1/amm/burn` | pm_clearing (新增路由 + 多表事务) | P0 | ❌ 无回退——必须实现 | Auto-Merge 必需 |
| 4 | `POST /api/v1/amm/cancel-all` | pm_order (新增路由) | P1 | 循环调用 `POST /orders/{id}/cancel` | 紧急停机依赖 |
| 5 | `POST /api/v1/admin/amm/*` | pm_admin (新增路由) | P1 | AMM 进程手动启停（CLI/环境变量） | 管理员生命周期控制 |
| 6 | `auto_netting_enabled` 清算层判断 | pm_clearing/domain/netting.py | P0 | ❌ 无回退——必须实现 | 见 §2.4 |
| 7 | `is_self_trade` 豁免逻辑 | pm_risk/rules/self_trade.py | P1 | 依赖原子改单避免自成交 | 见数据字典 §3.4 方案 A/B |
| 8 | AMM Service Token + 独立限流 | pm_gateway (auth + rate limit) | P1.5 | 标准 JWT 登录 + 自身节流（见 §2.2） | MVP 受限于 30次/分钟 |

> **最小可上线条件（MVP P0 Hard Blockers）**：上表中 #2（Mint）、#3（Burn）、#6（Netting 开关）
> **无任何回退方案**，必须在后端实现后 AMM 才能启动。#1（Replace）虽有回退但严重影响安全性。
> 其余项（#4、#5、#7、#8）可通过 MVP 回退方案临时替代。
>
> **与"AMM 是超级用户"设计哲学的关系**：AMM 在**业务逻辑层面**是超级用户——它不入侵撮合核心算法
> （价格-时间优先、四场景清算）。但 AMM 的上线需要撮合引擎侧提供上述基础设施扩展。
> 这些扩展不改变撮合引擎的核心撮合算法，但需要新增 REST 路由、权限校验和事务流程。
>
> **现有 API 兼容说明**：当前代码库（pm_gateway、pm_order）只暴露标准用户端点
> （`/api/v1/orders`、`/api/v1/auth/*` 等），无 `/api/v1/amm/*` 前缀路由、无 Service Token
> 签发机制、无 `account_type`/`privilege` 字段。上述新增 API 需要在 pm_order / pm_clearing /
> pm_gateway 中分别实现对应的 Router、Handler 和中间件。

### 3.1 原子改单 (Atomic Replace)

```
POST /api/v1/amm/orders/replace
```

**需要 AMM Service Token**

将一个现有订单原子性替换为新订单。撤旧单和挂新单在撮合引擎内部作为不可分割的原子操作执行，
消除传统 Cancel + Place 方式的竞态条件（裸奔风险、重复成交风险）。

**请求体**:
```json
{
    "old_order_id": "660e8400-e29b-41d4-a716-446655440001",
    "new_order": {
        "client_order_id": "amm_replace_20260227_001",
        "market_id": "MKT-BTC-100K-2026",
        "side": "YES",
        "direction": "SELL",
        "price_cents": 54,
        "quantity": 100,
        "time_in_force": "GTC"
    }
}
```

| 字段 | 类型 | 必填 | 校验规则 |
|------|------|------|---------|
| old_order_id | string (UUID) | 是 | 必须为 AMM 自身的活跃订单 |
| new_order | object | 是 | 完整的下单参数，规则同 §5.1 PlaceOrder |
| new_order.client_order_id | string | 是 | 新订单的幂等键 |
| new_order.market_id | string | 是 | 必须与旧订单同一市场 |

**成功响应** (200):

> ⚠️ **v1.1 关键修正**：成功响应**必须**包含旧订单的 `filled_quantity`。
> 原因：旧订单在 Replace 请求传输期间可能刚被部分成交但 Kafka 事件尚未推送到 AMM。
> 如果不返回此字段，AMM 本地库存缓存会永久漏掉这笔成交，导致状态不一致。

```json
{
    "code": 0,
    "message": "Order replaced successfully",
    "data": {
        "old_order_id": "660e8400-...",
        "old_order_status": "CANCELLED",
        "old_order_filled_quantity": 40,
        "old_order_original_quantity": 100,
        "new_order": {
            "id": "770f9500-...",
            "client_order_id": "amm_replace_20260227_001",
            "market_id": "MKT-BTC-100K-2026",
            "side": "YES",
            "direction": "SELL",
            "price_cents": 54,
            "quantity": 100,
            "filled_quantity": 0,
            "remaining_quantity": 100,
            "status": "OPEN",
            "created_at": "2026-02-27T10:00:00Z"
        },
        "trades": []
    }
}
```

**部分成交响应** (422):

> ⚠️ v7.1 关键新增：旧订单在 Replace 请求传输期间被部分成交时，撮合引擎撤销旧订单剩余部分，
> 返回已成交量，但**拒绝创建新订单**。AMM 需根据最新库存状态自行重新决策。

```json
{
    "code": 6001,
    "message": "Old order partially filled, replacement rejected",
    "data": {
        "old_order_id": "660e8400-...",
        "old_order_status": "CANCELLED",
        "filled_quantity": 30,
        "remaining_quantity_cancelled": 70,
        "unfrozen_amount": 4550,
        "unfrozen_asset_type": "FUNDS"
    }
}
```

**错误响应**:

| HTTP 状态码 | 错误码 | 场景 | data 内容 |
|------------|--------|------|-----------|
| 422 | 6001 | 旧订单已部分成交 | `filled_quantity`, `remaining_quantity_cancelled` |
| 404 | 6002 | 旧订单不存在 | `old_order_id` |
| 422 | 6003 | 旧订单已全部成交 | `old_order_id`, `total_filled_quantity` |
| 403 | 6004 | 旧订单不属于 AMM | `old_order_id`, `owner_user_id` |
| 422 | 6005 | 新旧订单市场不一致 | `old_market_id`, `new_market_id` |
| 422 | 4001 | 新订单价格超范围 | 同现有 4001 |
| 422 | 2001 | 新订单所需保证金不足 | 同现有 2001 |

**撮合引擎实现要求**:

```
┌─────────────────────────────────────────────────────────────────┐
│                原子改单内部执行流程                                │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  输入: old_order_id, new_order_params                           │
│                                                                 │
│  Step 0: 幂等前置检查 (v1.2 新增)                                │
│    查询 new_order_params.client_order_id 是否已存在于 orders 表  │
│    if exists → 说明是网络超时后的重试请求，                       │
│                直接返回该已存在新订单的当前状态 (200 OK)           │
│                跳过后续全部逻辑                                   │
│                                                                 │
│  Step 1: 加锁 (对 old_order_id 加行锁)                          │
│                                                                 │
│  Step 2: 查询旧订单状态                                         │
│    if not found        → return 6002 (NOT_FOUND)                │
│    if owner != AMM     → return 6004 (FORBIDDEN)                │
│    if status == FILLED → return 6003 (ALREADY_FILLED)           │
│    if filled_qty > 0   → 撤销剩余部分                           │
│                          return 6001 (PARTIALLY_FILLED)          │
│    if status == OPEN   → 继续 Step 3                            │
│                                                                 │
│  Step 3: 原子替换 (在同一事务内)                                 │
│    ① 从订单簿内存中移除旧订单                                    │
│    ② 更新旧订单状态为 CANCELLED                                  │
│    ③ 解冻旧订单的冻结资金/持仓                                   │
│    ④ 对新订单执行标准下单流程 (风控→冻结→入簿→撮合)              │
│    ⑤ 返回新订单 + 成交列表                                       │
│                                                                 │
│  Step 4: 释放锁                                                 │
│                                                                 │
│  原子性保证: Step 3 中 ①~⑤ 在同一数据库事务内，                  │
│  任一步骤失败则全部回滚，旧订单恢复原状。                        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**副作用**:
1. 旧订单写入 `ORDER_UNFREEZE` 流水
2. 新订单写入 `ORDER_FREEZE` 流水
3. 若新订单立即成交：写入成交记录 + 清算流水

---

### 3.2 批量撤单 (Batch Cancel)

```
POST /api/v1/amm/orders/batch-cancel
```

**需要 AMM Service Token**

AMM 在紧急风控（KILL SWITCH）或阶段切换时需要一次性撤销所有活跃订单。

**请求体**:
```json
{
    "market_id": "MKT-BTC-100K-2026",
    "cancel_scope": "ALL"
}
```

| 字段 | 类型 | 必填 | 校验规则 |
|------|------|------|---------|
| market_id | string | 是 | 目标市场 ID |
| cancel_scope | string | 否 | `ALL`（默认）/ `BUY_ONLY` / `SELL_ONLY`（见下方语义说明） |

> **v1.2 语义澄清**：`cancel_scope` 基于 `original_direction`（用户的原始买卖意图），而非 `book_direction`（订单簿方向）。
> 在单账本架构下，这意味着：
> - `BUY_ONLY` → 撤销 AMM 的所有 Buy YES + Buy NO 订单（对应 book_type: NATIVE_BUY + SYNTHETIC_SELL）
> - `SELL_ONLY` → 撤销 AMM 的所有 Sell YES + Sell NO 订单（对应 book_type: NATIVE_SELL + SYNTHETIC_BUY）

**成功响应** (200):
```json
{
    "code": 0,
    "message": "Batch cancel completed",
    "data": {
        "market_id": "MKT-BTC-100K-2026",
        "cancelled_count": 10,
        "total_unfrozen_funds_cents": 32000,
        "total_unfrozen_yes_shares": 500,
        "total_unfrozen_no_shares": 300
    }
}
```

> **v1.1 精简**: 移除了 `cancelled_orders` 明细数组。理由：KILL SWITCH 场景下可能一次撤销数十个订单，
> 逐单返回明细既增大响应体积也无使用价值——AMM 在批量撤单后会立即清空 Redis `amm:orders` 缓存，
> 不需要逐单核对。如需审计，通过 Kafka `order_events` 中的 `CANCELLED` 事件逐条追溯即可。

**错误**:

| HTTP 状态码 | 错误码 | 场景 |
|------------|--------|------|
| 404 | 3001 | 市场不存在 |
| 200 | 0 | 无活跃订单（cancelled_count = 0，非错误） |

**副作用**: 每个被撤订单各写入一条 `ORDER_UNFREEZE` 流水。

---

### 3.3 特权铸造 (Privileged Mint)

```
POST /api/v1/amm/mint
```

**需要 AMM Service Token**

AMM 初始化或 Auto-Reinvest 时，需要用现金铸造等量的 YES + NO 份额。
普通用户的铸造通过下对手单触发 MINT 场景完成，AMM 可直接调用此接口跳过撮合。

**请求体**:
```json
{
    "market_id": "MKT-BTC-100K-2026",
    "quantity": 1000,
    "idempotency_key": "amm_mint_20260227_001"
}
```

| 字段 | 类型 | 必填 | 校验规则 |
|------|------|------|---------|
| market_id | string | 是 | 已激活的市场 |
| quantity | int | 是 | > 0，每份成本 100 美分，总成本 = quantity × 100 |
| idempotency_key | string | 是 | 幂等键，防止重复铸造 |

**成功响应** (201):
```json
{
    "code": 0,
    "message": "Shares minted successfully",
    "data": {
        "market_id": "MKT-BTC-100K-2026",
        "minted_quantity": 1000,
        "cost_cents": 100000,
        "cost_display": "$1,000.00",
        "new_yes_inventory": 2000,
        "new_no_inventory": 2000,
        "remaining_balance_cents": 400000,
        "remaining_balance_display": "$4,000.00"
    }
}
```

**错误**:

| HTTP 状态码 | 错误码 | 场景 |
|------------|--------|------|
| 422 | 2001 | AMM 账户余额不足 |
| 404 | 3001 | 市场不存在 |
| 422 | 3002 | 市场未激活 |
| 409 | 6006 | 幂等键冲突（已铸造过） |

**副作用**（严格对齐 v1.2 ledger_entry 机制，全部在同一数据库事务内完成）:

1. **AMM 账户扣款**: `accounts` 表 AMM 行 `available_balance -= quantity × 100`
2. **写入 AMM 侧流水**: `ledger_entries` 插入一条 `entry_type = 'MINT_COST'`，
   `amount_cents = -(quantity × 100)`，`reference_type = 'AMM_MINT'`，`reference_id = idempotency_key`
   > ⚠️ v1.2 修正：`entry_type` 复用 DB v2.3 已有的 `'MINT_COST'`（而非新增 `'AMM_MINT'`），
   > 通过 `reference_type = 'AMM_MINT'` 区分 AMM 特权铸造与普通用户成交产生的 Mint。
3. **系统储备金增加**: `markets` 表 `reserve_balance += quantity × 100`
4. **写入系统侧流水**: `ledger_entries` 插入一条 `entry_type = 'MINT_RESERVE_IN'`，
   `amount_cents = +(quantity × 100)`
5. **份额增加**: `markets` 表 `total_yes_shares += quantity`，`total_no_shares += quantity`
6. **AMM 持仓增加**: `positions` 表 AMM 的 YES `volume += quantity`，`cost_sum += quantity × 50`（初始公允价）；
   NO 同理 `volume += quantity`，`cost_sum += quantity × 50`
7. **v1.3 新增 — 可审计事件记录**: `trades` 表插入一条虚拟成交记录，
   `trade_scenario = 'MINT'`，`buy_user_id = 'AMM_SYSTEM_001'`，`sell_user_id = 'SYSTEM'`，
   `price_cents = 50`，`quantity = {quantity}`。
   此记录使走 `trades` 表的对账系统和审计系统也能覆盖 Mint 操作，
   避免 Mint/Burn 仅存在于 `ledger_entries` 而成为 trades 审计盲区。

---

### 3.4 特权销毁 (Privileged Burn / Auto-Merge)

```
POST /api/v1/amm/burn
```

**需要 AMM Service Token**

AMM 的 Auto-Merge 机制：将等量的 YES + NO 份额销毁，回收现金。
这是 v7.1 自动复投重构的核心——优先销毁配对回收现金，而非盲目铸造新份额。

**请求体**:
```json
{
    "market_id": "MKT-BTC-100K-2026",
    "quantity": 200,
    "idempotency_key": "amm_burn_20260227_001"
}
```

| 字段 | 类型 | 必填 | 校验规则 |
|------|------|------|---------|
| market_id | string | 是 | 已激活的市场 |
| quantity | int | 是 | > 0，不超过 min(YES可用持仓, NO可用持仓) |
| idempotency_key | string | 是 | 幂等键 |

**成功响应** (200):
```json
{
    "code": 0,
    "message": "Shares burned (auto-merge) successfully",
    "data": {
        "market_id": "MKT-BTC-100K-2026",
        "burned_quantity": 200,
        "recovered_cents": 20000,
        "recovered_display": "$200.00",
        "new_yes_inventory": 800,
        "new_no_inventory": 800,
        "remaining_balance_cents": 420000,
        "remaining_balance_display": "$4,200.00"
    }
}
```

**错误**:

| HTTP 状态码 | 错误码 | 场景 |
|------------|--------|------|
| 422 | 5001 | YES 或 NO 可用持仓不足 |
| 404 | 3001 | 市场不存在 |
| 409 | 6006 | 幂等键冲突 |

**副作用**（严格对齐 v1.2 ledger_entry 机制，全部在同一数据库事务内完成）:

1. **AMM 持仓扣减**: `positions` 表 AMM 的 YES `volume -= quantity`，NO `volume -= quantity`；
   同步按比例释放 `cost_sum`（FIFO 或加权平均，与 v1.2 §7.1 realized_pnl 计算规则一致）
2. **系统储备金扣减**: `markets` 表 `reserve_balance -= quantity × 100`
3. **AMM 账户入账**: `accounts` 表 AMM 行 `available_balance += quantity × 100`
4. **写入 AMM 侧流水**: `ledger_entries` 插入 `entry_type = 'BURN_REVENUE'`，
   `amount_cents = +(quantity × 100)`，`reference_type = 'AMM_BURN'`，`reference_id = idempotency_key`
   > ⚠️ v1.2 修正：`entry_type` 复用 DB v2.3 已有的 `'BURN_REVENUE'`（而非新增 `'AMM_BURN'`），
   > 通过 `reference_type = 'AMM_BURN'` 区分 AMM 特权销毁与普通用户成交产生的 Burn。
5. **写入系统侧流水**: `ledger_entries` 插入 `entry_type = 'BURN_RESERVE_OUT'`，
   `amount_cents = -(quantity × 100)`
6. **份额减少**: `markets` 表 `total_yes_shares -= quantity`，`total_no_shares -= quantity`
7. **v1.3 新增 — 可审计事件记录**: `trades` 表插入一条虚拟成交记录，
   `trade_scenario = 'BURN'`，`buy_user_id = 'SYSTEM'`，`sell_user_id = 'AMM_SYSTEM_001'`，
   `price_cents = 50`，`quantity = {quantity}`。理由同 §3.3 第 7 步。

---

### 3.5 AMM 生命周期管理

#### 3.5.1 启动 AMM

```
POST /api/v1/admin/amm/start
```

**需要管理员认证**（非 AMM 自身调用，由管理员发起）

**请求体**:
```json
{
    "market_id": "MKT-BTC-100K-2026",
    "initial_budget_cents": 500000,
    "initial_price_cents": 50,
    "config_override": {
        "gamma_mid": 1.5,
        "time_smoothing_kappa": 24.0,
        "base_spread_cents": 2,
        "max_inventory_per_side": 5000
    }
}
```

| 字段 | 类型 | 必填 | 校验规则 |
|------|------|------|---------|
| market_id | string | 是 | 已激活的市场，且该市场尚未有 AMM 运行 |
| initial_budget_cents | int | 是 | > 0，AMM 初始预算 |
| initial_price_cents | int | 是 | [1, 99]，初始公允价格 |
| config_override | object | 否 | 覆盖默认 AMM 配置参数 |

**成功响应** (201):
```json
{
    "code": 0,
    "message": "AMM started for market",
    "data": {
        "market_id": "MKT-BTC-100K-2026",
        "amm_user_id": "AMM_SYSTEM_001",
        "initial_budget_cents": 500000,
        "initial_mint_quantity": 2500,
        "phase": "EXPLORATION",
        "started_at": "2026-02-27T10:00:00Z"
    }
}
```

**副作用**:
1. 向 AMM 系统账户充值 `initial_budget_cents`
2. 自动调用铸造接口，铸造 `initial_budget_cents / 100` 份 YES + NO
3. AMM 进入 EXPLORATION 阶段，开始挂单

#### 3.5.2 停止 AMM

```
POST /api/v1/admin/amm/stop
```

**需要管理员认证**

**请求体**:
```json
{
    "market_id": "MKT-BTC-100K-2026",
    "stop_mode": "GRACEFUL"
}
```

| 字段 | 类型 | 必填 | 校验规则 |
|------|------|------|---------|
| market_id | string | 是 | 当前有 AMM 运行的市场 |
| stop_mode | string | 否 | `GRACEFUL`（默认，撤单后停止）/ `IMMEDIATE`（立即停止） |

**成功响应** (200):
```json
{
    "code": 0,
    "message": "AMM stopped",
    "data": {
        "market_id": "MKT-BTC-100K-2026",
        "cancelled_orders_count": 10,
        "remaining_yes_inventory": 1800,
        "remaining_no_inventory": 1500,
        "remaining_balance_cents": 420000,
        "stopped_at": "2026-02-27T18:00:00Z"
    }
}
```

---

## 四、AMM 复用的现有 API

> AMM 直接调用《API 接口契约 v1.2》中已有的接口，无需修改。
> 以下列出 AMM 使用的接口及其特殊注意事项。

### 4.1 复用接口清单

| # | 现有接口 | AMM 用途 | 调用频率 | 特殊事项 |
|---|---------|---------|---------|---------|
| 1 | `POST /api/v1/orders` | 挂单（非 Replace 场景） | 高频 | AMM 使用自己的 client_order_id 前缀 `amm_` |
| 2 | `POST /api/v1/orders/{id}/cancel` | 单笔撤单 | 中频 | 仅作为 batch-cancel 的降级方案 |
| 3 | `GET /api/v1/account/balance` | 查询 AMM 账户余额 | 每周期 1 次 | 用于 Auto-Reinvest 判断 |
| 4 | `GET /api/v1/positions/{market_id}` | 查询 AMM 当前持仓 | 每周期 1 次 | 用于库存偏斜计算 |
| 5 | `GET /api/v1/markets/{market_id}` | 查询市场详情 | 启动时 1 次 | 获取 fee_bps、resolution_date 等 |
| 6 | `GET /api/v1/markets/{market_id}/orderbook` | 查询订单簿快照 | 每周期 1 次 | 用于 Layer 2 微观价格计算 |
| 7 | `GET /api/v1/orders` | 查询 AMM 活跃订单 | 对账用 | status=OPEN,PARTIALLY_FILLED |

### 4.2 AMM 下单规范

AMM 调用 `POST /api/v1/orders` 时，需遵循以下规范：

**client_order_id 命名规则**:
```
amm_{market_id}_{side}_{direction}_{timestamp_ms}_{seq}

示例: amm_MKT-BTC-100K-2026_YES_SELL_1740652800000_001
```

**AMM 特殊行为**:
- AMM 的 `time_in_force` 始终为 `GTC`
- AMM 豁免自成交检查（`SELF_TRADE_EXEMPT` 权限）
- AMM 的下单限流提升到 600 次/分钟（§2.2 中定义）

### 4.3 持仓查询增强建议

AMM 高频调用 `GET /api/v1/positions/{market_id}` 来获取实时库存。
建议撮合引擎对 AMM 系统账户的持仓查询做以下优化：

1. **Redis 缓存层**: AMM 的持仓变动在 Redis 中维护实时副本（见 §6 Redis 契约）
2. **跳过 DB 查询**: AMM 可直接从 Redis 读取持仓，不走 PostgreSQL
3. **一致性保证**: 每次成交后 Kafka 消费者更新 Redis 缓存

---

## 五、事件流契约：Kafka 消息

### 5.1 成交事件 (Trade Events)

**Topic**: `trade_events`
**分区策略**: 按 `market_id` 分区，保证同一市场的事件有序
**消费者组**: `amm_trade_consumer`

AMM 的 Market Listener 订阅此 Topic，用于：
1. 更新本地库存缓存
2. 触发 Strategy Engine 重新报价
3. 计算 VPIN（逆向选择检测）
4. 喂价给 VolatilityEstimator

**Payload 格式**:

```json
{
    "event_type": "TRADE_EXECUTED",
    "event_id": "evt_20260227_trade_001",
    "timestamp_ms": 1740652800000,
    "market_id": "MKT-BTC-100K-2026",
    "trade": {
        "trade_id": "TRD_20260227_001",
        "scenario": "TRANSFER_YES",
        "price_cents": 65,
        "quantity": 50,
        "buyer": {
            "user_id": "550e8400-...",
            "order_id": "660e8400-...",
            "role": "TAKER",
            "fee_cents": 65,
            "is_amm": false
        },
        "seller": {
            "user_id": "AMM_SYSTEM_001",
            "order_id": "770f9500-...",
            "role": "MAKER",
            "fee_cents": 33,
            "is_amm": true
        },
        "executed_at": "2026-02-27T10:05:00Z"
    }
}
```

**字段说明**:

| 字段 | 类型 | 说明 |
|------|------|------|
| event_type | string | 固定为 `TRADE_EXECUTED` |
| event_id | string | 事件唯一 ID，用于消费幂等 |
| timestamp_ms | long | 事件时间戳（毫秒） |
| market_id | string | 市场 ID（分区键） |
| trade.trade_id | string | 成交 ID |
| trade.scenario | string | `MINT` / `TRANSFER_YES` / `TRANSFER_NO` / `BURN` |
| trade.price_cents | int | 成交价格（美分） |
| trade.quantity | int | 成交数量 |
| trade.buyer.user_id | string | 买方用户 ID |
| trade.buyer.order_id | string | 买方订单 ID |
| trade.buyer.role | string | `MAKER` 或 `TAKER` |
| trade.buyer.fee_cents | int | 买方手续费（美分） |
| trade.buyer.is_amm | bool | 买方是否为 AMM |
| trade.seller.* | — | 同 buyer 结构 |
| trade.executed_at | string | 成交时间 (ISO 8601) |

**⚠️ v1.1→v1.3 关键规则：库存更新的权威数据源**

> **v1.3 MVP vs Phase 2 分层说明**:
>
> 当前 MVP 代码库**不包含 Kafka 基础设施**（无 producer/consumer 实现、无 Kafka 依赖）。
> 因此本节描述的 Kafka 事件驱动架构为 **Phase 2 理想目标**。
>
> | 阶段 | 库存更新权威数据源 | 被动成交感知方式 | 幂等机制 |
> |------|------------------|----------------|---------|
> | **MVP（当前）** | REST 回调 + REST 轮询 + 定期 DB 对账 | `GET /api/v1/trades?user_id=AMM_SYSTEM_001&since={last_trade_id}` 定时轮询（建议间隔 1-2s） | `trade_id` 去重 |
> | **Phase 2** | Kafka `trade_events` 事件驱动（唯一入口） | Kafka consumer 订阅 | `event_id` 幂等去重 |
>
> **MVP 核心原则**：AMM 主动发起的操作（下单、改单、Mint、Burn）通过 REST 响应直接更新 Redis；
> 被动成交（其他用户吃掉 AMM 挂单）通过 REST 轮询发现。两条路径使用 `trade_id` 去重，避免双重扣减。
>
> **Phase 2 迁移路径**：Kafka 上线后，将 REST 轮询替换为 Kafka consumer 订阅，
> REST 响应中的 trades 降级为仅日志/Debug 用途，所有库存变更统一由 Kafka 事件驱动。

> **⚠️ v1.2 例外（MVP/Phase 2 均适用）：特权铸造 (Mint) 和特权销毁 (Burn/Merge)**
>
> 特权铸造/销毁由 AMM 直接调用 REST API（§3.3/§3.4），**不经过订单簿撮合**，因此
> 无论是 MVP 还是 Phase 2，都**不会产生成交事件**。
>
> **规则**：AMM 在调用 Mint/Burn API 收到 `200/201` 成功响应后，**必须同步、直接地**
> 更新本地 Redis 库存：
>
> ```python
> # Mint 成功后立即更新 Redis
> resp = await api.post_mint(market_id, quantity, idempotency_key)
> if resp.code == 0:
>     inventory.increase_yes(quantity)
>     inventory.increase_no(quantity)
>     inventory.decrease_cash(quantity * 100)
>     inventory.update_cost_sum_on_mint(quantity)  # 初始成本 = 50 cents/份
>
> # Burn 成功后立即更新 Redis
> resp = await api.post_burn(market_id, quantity, idempotency_key)
> if resp.code == 0:
>     inventory.decrease_yes(quantity)
>     inventory.decrease_no(quantity)
>     inventory.increase_cash(quantity * 100)
>     inventory.release_cost_sum_on_burn(quantity)
> ```

**AMM 消费逻辑**（Phase 2 Kafka 版本；MVP 版本见下方 `poll_trades` 替代方案）:

```python
# 幂等集合：已处理的 event_id / trade_id
processed_events: set = set()

def on_trade_event(event):
    # ── 幂等去重 ──
    if event['event_id'] in processed_events:
        return  # 已处理，跳过
    processed_events.add(event['event_id'])

    trade = event['trade']

    # 1. 判断 AMM 是否参与
    amm_is_buyer = trade['buyer']['is_amm']
    amm_is_seller = trade['seller']['is_amm']

    if not amm_is_buyer and not amm_is_seller:
        # AMM 未参与，但仍需用于:
        # - 后验价格学习 (Layer 3)
        # - VolatilityEstimator 喂价
        # - VPIN 计算
        strategy.update_market_data(trade)
        return

    # 2. AMM 参与了成交 → 更新 Redis 库存（唯一入口）
    if amm_is_seller and trade['scenario'] in ('TRANSFER_YES', 'BURN'):
        inventory.decrease_yes(trade['quantity'])
    elif amm_is_buyer and trade['scenario'] in ('TRANSFER_YES', 'MINT'):
        inventory.increase_yes(trade['quantity'])
    # ... 对称处理 NO 方向

    # 3. 更新 cost_sum（用于 PnL 计算）
    inventory.update_cost_sum(trade)

    # 4. 扣减手续费（v1.2 新增：确保现金缓存一致）
    if amm_is_buyer:
        inventory.decrease_cash(trade['buyer']['fee_cents'])
    if amm_is_seller:
        inventory.decrease_cash(trade['seller']['fee_cents'])

    # 5. 触发 Post-Fill Delay (v7.1 LVR 防御)
    lvr_defense.on_amm_fill(trade)

    # 6. 触发重新报价（延迟后）
    strategy.schedule_requote()
```

**⚠️ v1.3 MVP 替代方案：REST 轮询被动成交**

```python
# MVP: 无 Kafka，通过 REST 轮询发现被动成交
# 建议轮询间隔: 1-2 秒（或由 WebSocket 成交推送触发即时轮询）
processed_trade_ids: set = set()
last_trade_cursor: str = ""

async def poll_trades():
    """定时轮询 AMM 参与的成交记录"""
    resp = await api.get(f"/api/v1/trades?user_id=AMM_SYSTEM_001&cursor={last_trade_cursor}&limit=50")
    for trade in resp['trades']:
        if trade['trade_id'] in processed_trade_ids:
            continue
        processed_trade_ids.add(trade['trade_id'])

        # 复用 on_trade_event 的库存更新逻辑
        handle_trade(trade)

    if resp['trades']:
        last_trade_cursor = resp['next_cursor']

async def on_ws_trade_push(msg):
    """WebSocket 成交推送 → 立即触发一次 REST 轮询确认"""
    # WS 推送不直接更新库存（不可靠），仅作为加速触发器
    await poll_trades()
```

> **Phase 2 迁移**：Kafka 上线后，`poll_trades` 整体替换为 Kafka consumer `on_trade_event`，
> `processed_trade_ids` 替换为 `processed_events`（基于 event_id），WS 推送保留为延迟优化。

### 5.2 订单状态变更事件

**Topic**: `order_events`
**分区策略**: 按 `market_id` 分区

AMM 需要监听自身订单的状态变更，特别是订单被动成交后的状态更新。

**Payload 格式**:

```json
{
    "event_type": "ORDER_STATUS_CHANGED",
    "event_id": "evt_20260227_order_001",
    "timestamp_ms": 1740652800000,
    "market_id": "MKT-BTC-100K-2026",
    "order": {
        "order_id": "770f9500-...",
        "user_id": "AMM_SYSTEM_001",
        "side": "YES",
        "direction": "SELL",
        "price_cents": 54,
        "original_quantity": 100,
        "filled_quantity": 50,
        "remaining_quantity": 50,
        "status": "PARTIALLY_FILLED",
        "previous_status": "OPEN"
    }
}
```

**AMM 关注的状态转换**:

| previous_status → status | AMM 动作 |
|-------------------------|---------|
| OPEN → PARTIALLY_FILLED | 更新挂单追踪，考虑是否需要补单 |
| PARTIALLY_FILLED → FILLED | 从活跃订单列表移除，触发重新报价 |
| OPEN → CANCELLED | 确认撤单成功（AMM 主动发起时） |
| OPEN → CANCELLED (非 AMM 发起) | 异常！可能是管理员干预，需告警 |

### 5.3 市场状态变更事件

**Topic**: `market_events`
**分区策略**: 按 `market_id` 分区

AMM 需要监听市场生命周期事件，以便在市场暂停、结算时执行相应操作。

**Payload 格式**:

```json
{
    "event_type": "MARKET_STATUS_CHANGED",
    "event_id": "evt_20260227_market_001",
    "timestamp_ms": 1740652800000,
    "market_id": "MKT-BTC-100K-2026",
    "previous_status": "ACTIVE",
    "new_status": "RESOLVED",
    "resolution_result": "YES",
    "resolved_at": "2026-12-31T23:59:59Z"
}
```

**AMM 响应**:

| 新状态 | AMM 动作 |
|-------|---------|
| SUSPENDED | 触发 KILL SWITCH：撤销所有挂单，暂停策略 |
| HALTED | 同 SUSPENDED |
| RESOLVED | 撤销所有挂单，停止 AMM，等待结算 |

---

## 六、事件流契约：Redis 状态通道

### 6.1 AMM 库存实时缓存

**Key 模式**: `amm:inventory:{market_id}`
**数据类型**: Hash
**更新方**: AMM 进程（每次成交后写入）
**读取方**: AMM 进程（每个报价周期读取）

```
HSET amm:inventory:MKT-BTC-100K-2026
    yes_volume          1800
    no_volume           1500
    yes_available       1300    # volume - pending_sell
    no_available        1200
    yes_cost_sum_cents  90000   # 累计买入成本（美分）
    no_cost_sum_cents   75000   # 累计买入成本（美分）
    cash_cents          420000
    updated_at_ms       1740652800000
```

> **v1.1 新增**: `yes_cost_sum_cents` / `no_cost_sum_cents` 用于实时 PnL 计算和 Auto-Reinvest 决策。
> 更新时机与 `volume` 字段同步——均在 Kafka `trade_events` 消费回调中写入。
> 计算方式与 v1.2 `positions.cost_sum` 规则一致（加权平均成本法）。

| 字段 | 类型 | 说明 |
|------|------|------|
| yes_volume | int | YES 总持仓 |
| no_volume | int | NO 总持仓 |
| yes_available | int | YES 可用持仓（排除挂单冻结） |
| no_available | int | NO 可用持仓 |
| yes_cost_sum_cents | int | YES 累计买入成本（美分），用于均价计算和 PnL |
| no_cost_sum_cents | int | NO 累计买入成本（美分） |
| cash_cents | int | 可用现金（美分） |
| updated_at_ms | long | 最后更新时间戳（毫秒） |

**一致性保证**: 每次 Kafka 成交事件消费完成后，原子更新此 Hash。
定期（每 60 秒）与 `GET /api/v1/positions/{market_id}` 的返回值做对账校验。

### 6.2 AMM 活跃订单追踪

**Key 模式**: `amm:orders:{market_id}`
**数据类型**: Hash（字段为 order_id，值为 JSON）

```
HSET amm:orders:MKT-BTC-100K-2026
    "770f9500-..." '{"side":"YES","direction":"SELL","price":54,"qty":100,"remaining":100,"created_at_ms":1740652800000}'
    "880g0600-..." '{"side":"YES","direction":"SELL","price":55,"qty":100,"remaining":100,"created_at_ms":1740652800000}'
```

**生命周期**:
- **新增**: 下单成功后 `HSET`
- **更新**: 部分成交后更新 `remaining`
- **删除**: 全部成交或撤单后 `HDEL`
- **全量重建**: AMM 重启时，调用 `GET /api/v1/orders?status=OPEN,PARTIALLY_FILLED` 重建

### 6.3 AMM 策略状态

**Key 模式**: `amm:state:{market_id}`
**数据类型**: Hash

```
HSET amm:state:MKT-BTC-100K-2026
    phase               "STABILIZATION"
    fair_price_cents    65
    sigma_cents         3.2
    inventory_skew      0.18
    last_requote_ms     1740652800000
    daily_pnl_cents     -1500
    total_fills_today   47
    lvr_cooldown_until  0
```

**读取方**: 监控系统 (Prometheus exporter)、管理后台
**写入方**: AMM Strategy Engine（每个报价周期更新）

---

## 七、WebSocket 订阅契约

### 7.1 订单簿实时推送

AMM 的 Market Listener 需要实时的订单簿变动数据，用于 Layer 2 微观价格计算。

**连接地址**: `ws://localhost:8000/ws/v1/orderbook/{market_id}`
**认证**: 连接时在 query param 传入 AMM Service Token

**消息格式** (服务端 → AMM):

```json
{
    "type": "ORDERBOOK_UPDATE",
    "sequence_id": 1,
    "market_id": "MKT-BTC-100K-2026",
    "timestamp_ms": 1740652800000,
    "yes": {
        "bids": [
            {"price_cents": 65, "total_quantity": 500},
            {"price_cents": 64, "total_quantity": 300}
        ],
        "asks": [
            {"price_cents": 67, "total_quantity": 400},
            {"price_cents": 68, "total_quantity": 250}
        ]
    },
    "update_type": "SNAPSHOT"
}
```

| update_type | 说明 | 频率 |
|-------------|------|------|
| SNAPSHOT | 全量快照 | 连接时发送 1 次，之后每 30 秒发送 1 次 |
| DELTA | 增量变动（某价格档位数量变化） | 每次订单簿变动时 |

**DELTA 消息格式**:
```json
{
    "type": "ORDERBOOK_UPDATE",
    "sequence_id": 42,
    "market_id": "MKT-BTC-100K-2026",
    "timestamp_ms": 1740652800001,
    "yes": {
        "bids": [
            {"price_cents": 65, "total_quantity": 450}
        ],
        "asks": []
    },
    "update_type": "DELTA"
}
```

DELTA 语义：只包含变动的价格档位。`total_quantity = 0` 表示该档位已空。
AMM 本地维护完整订单簿副本，通过 DELTA 增量更新。

> **v1.1 新增 `sequence_id`**:
> 每个 WebSocket 连接上的消息携带单调递增的 `sequence_id`（从 SNAPSHOT 的 1 开始）。
> SNAPSHOT 消息会重置 sequence 计数器。AMM 必须检测 sequence gap：
>
> ```python
> def on_ws_message(msg):
>     if msg['update_type'] == 'SNAPSHOT':
>         self.expected_seq = msg['sequence_id'] + 1
>         self.rebuild_local_book(msg)
>         return
>
>     if msg['sequence_id'] != self.expected_seq:
>         # Gap detected → 本地订单簿可能不一致
>         log.warning(f"sequence gap: expected {self.expected_seq}, got {msg['sequence_id']}")
>         self.request_full_snapshot()  # 主动请求全量快照
>         return
>
>     self.expected_seq = msg['sequence_id'] + 1
>     self.apply_delta(msg)
> ```
>
> 主动请求全量快照的方式：发送 `{"action": "REQUEST_SNAPSHOT"}` 消息给服务端。

### 7.2 实时成交推送

**连接地址**: `ws://localhost:8000/ws/v1/trades/{market_id}`

```json
{
    "type": "TRADE",
    "sequence_id": 15,
    "market_id": "MKT-BTC-100K-2026",
    "timestamp_ms": 1740652800000,
    "trade_id": "TRD_20260227_001",
    "price_cents": 65,
    "quantity": 50,
    "scenario": "TRANSFER_YES",
    "aggressor_side": "BUY"
}
```

> **注意**: WebSocket 成交推送是实时但非可靠的。
> - **MVP**: AMM 以 REST 轮询 `GET /api/v1/trades` 作为权威数据源，WebSocket 作为低延迟触发器
>   加速发现被动成交（收到 WS 推送后立即触发一次 REST 轮询确认）。
> - **Phase 2**: AMM 以 Kafka `trade_events` 作为权威数据源，WebSocket 仅作为延迟优化。

---

## 八、错误码扩展

在《API 接口契约 v1.2》的错误码体系基础上，新增 AMM 专用错误码段：

| 错误码 | 含义 | 触发接口 |
|--------|------|---------|
| 6001 | 旧订单已部分成交，Replace 被拒绝 | POST /amm/orders/replace |
| 6002 | 旧订单不存在 | POST /amm/orders/replace |
| 6003 | 旧订单已全部成交 | POST /amm/orders/replace |
| 6004 | 旧订单不属于 AMM | POST /amm/orders/replace |
| 6005 | 新旧订单市场不一致 | POST /amm/orders/replace |
| 6006 | 幂等键冲突（铸造/销毁已执行过） | POST /amm/mint, /amm/burn |
| 6007 | AMM 已在该市场运行 | POST /admin/amm/start |
| 6008 | AMM 未在该市场运行 | POST /admin/amm/stop |
| 6009 | 市场未被授权启用 AMM | POST /admin/amm/start |

---

## 九、接口总览

### 9.1 AMM 专用新增接口

| # | 方法 | 路径 | 说明 | 认证 |
|---|------|------|------|------|
| 1 | POST | /api/v1/amm/orders/replace | 原子改单 | AMM Token |
| 2 | POST | /api/v1/amm/orders/batch-cancel | 批量撤单 | AMM Token |
| 3 | POST | /api/v1/amm/mint | 特权铸造 | AMM Token |
| 4 | POST | /api/v1/amm/burn | 特权销毁 (Auto-Merge) | AMM Token |
| 5a | POST | /api/v1/admin/amm/start | 启动 AMM | 管理员 |
| 5b | POST | /api/v1/admin/amm/stop | 停止 AMM | 管理员 |

### 9.2 AMM 复用现有接口

| # | 方法 | 路径 | AMM 用途 |
|---|------|------|---------|
| 1 | POST | /api/v1/orders | 挂单 |
| 2 | POST | /api/v1/orders/{id}/cancel | 单笔撤单 |
| 3 | GET | /api/v1/account/balance | 查询余额 |
| 4 | GET | /api/v1/positions/{market_id} | 查询持仓 |
| 5 | GET | /api/v1/markets/{market_id} | 查询市场详情 |
| 6 | GET | /api/v1/markets/{market_id}/orderbook | 查询订单簿 |
| 7 | GET | /api/v1/orders | 查询活跃订单（对账用） |

### 9.3 事件流通道

| # | 类型 | 通道/Topic | 方向 | 说明 | MVP 替代方案 |
|---|------|-----------|------|------|-------------|
| 1 | Kafka | trade_events | 撮合→AMM | 成交事件（Phase 2 权威数据源） | REST 轮询 `GET /trades` + WS 触发 |
| 2 | Kafka | order_events | 撮合→AMM | 订单状态变更 | REST 轮询 `GET /orders` |
| 3 | Kafka | market_events | 撮合→AMM | 市场生命周期 | REST 轮询 `GET /markets/{id}` |
| 4 | Redis | amm:inventory:{market_id} | AMM 读写 | 库存实时缓存 |
| 5 | Redis | amm:orders:{market_id} | AMM 读写 | 活跃订单追踪 |
| 6 | Redis | amm:state:{market_id} | AMM 写，监控读 | 策略状态 |
| 7 | WebSocket | /ws/v1/orderbook/{market_id} | 撮合→AMM | 订单簿实时推送 |
| 8 | WebSocket | /ws/v1/trades/{market_id} | 撮合→AMM | 成交实时推送 |

---

## 十、附录：核心时序图

### 10.1 AMM 正常报价周期

```
┌─────────┐     ┌────────────┐     ┌──────────┐     ┌─────────┐
│ Strategy │     │ Connector  │     │ 撮合引擎  │     │  Kafka  │
│ Engine   │     │            │     │          │     │         │
└────┬─────┘     └─────┬──────┘     └────┬─────┘     └────┬────┘
     │                 │                  │                │
     │  1. 计算新报价   │                  │                │
     │────────────────>│                  │                │
     │                 │                  │                │
     │                 │ 2. POST /amm/    │                │
     │                 │    orders/replace│                │
     │                 │────────────────->│                │
     │                 │                  │                │
     │                 │ 3. 200 OK        │                │
     │                 │    (新订单+成交)  │                │
     │                 │<─────────────────│                │
     │                 │                  │                │
     │  4. 更新本地状态  │                  │ 5. 发布        │
     │<────────────────│                  │    trade_event  │
     │                 │                  │───────────────>│
     │                 │                  │                │
```

### 10.2 部分成交 Fallback 时序

```
┌─────────┐     ┌────────────┐     ┌──────────┐     ┌────────┐
│  Order   │     │ Connector  │     │ 撮合引擎  │     │ Redis  │
│ Manager  │     │            │     │          │     │        │
└────┬─────┘     └─────┬──────┘     └────┬─────┘     └───┬────┘
     │                 │                  │                │
     │ 1. Replace      │                  │                │
     │────────────────>│ 2. POST /amm/    │                │
     │                 │    orders/replace│                │
     │                 │────────────────->│                │
     │                 │                  │                │
     │                 │ 3. 422 (6001)    │                │
     │                 │   PARTIALLY_FILLED│               │
     │                 │   filled_qty=30  │                │
     │                 │<─────────────────│                │
     │                 │                  │                │
     │ 4. 同步库存      │                  │                │
     │─────────────────────────────────────────────────── >│
     │ 5. HGETALL      │                  │                │
     │    amm:inventory │                  │                │
     │< ───────────────────────────────────────────────────│
     │                 │                  │                │
     │ 6. 重新计算报价  │                  │                │
     │────────────────>│ 7. POST /orders  │                │
     │                 │    (独立下单)     │                │
     │                 │────────────────->│                │
     │                 │ 8. 201 Created   │                │
     │                 │<─────────────────│                │
     │                 │                  │                │
```

### 10.3 AMM 启动时序

```
┌────────┐     ┌──────────┐     ┌────────────┐     ┌──────────┐
│ 管理员  │     │ Admin API │     │  AMM 进程  │     │ 撮合引擎  │
└───┬────┘     └────┬─────┘     └─────┬──────┘     └────┬─────┘
    │               │                  │                 │
    │ 1. POST       │                  │                 │
    │ /admin/amm/   │                  │                 │
    │ start         │                  │                 │
    │──────────────>│                  │                 │
    │               │                  │                 │
    │               │ 2. 充值+铸造     │                 │
    │               │────────────────>│                  │
    │               │                  │ 3. POST /amm/   │
    │               │                  │    mint          │
    │               │                  │────────────────>│
    │               │                  │ 4. 201 Created  │
    │               │                  │<────────────────│
    │               │                  │                 │
    │               │                  │ 5. 订阅 Kafka   │
    │               │                  │    + WebSocket   │
    │               │                  │────────────────>│
    │               │                  │                 │
    │               │                  │ 6. 首次挂单     │
    │               │                  │ POST /orders ×N │
    │               │                  │────────────────>│
    │               │                  │                 │
    │ 7. 201 OK     │                  │                 │
    │<──────────────│                  │                 │
    │               │                  │                 │
```

---

---

## 变更记录

| 版本 | 日期 | 变更内容 |
|------|------|---------|
| v1.0 | 2026-02-27 | 初稿：10 章完整契约 |
| v1.1 | 2026-02-27 | 🔴 Replace 响应增加 `old_order_filled_quantity`、`old_order_original_quantity`（§3.1） |
| | | 🔴 明确 Kafka `trade_events` 为库存唯一权威数据源，REST `trades` 仅日志（§5.1） |
| | | 🔴 Mint/Burn 副作用对齐 `ledger_entry` 事务机制（§3.3、§3.4） |
| | | 🟠 Redis `amm:inventory` 增加 `yes_cost_sum_cents` / `no_cost_sum_cents`（§6.1） |
| | | 🟠 AMM 系统账户关闭 Auto-Netting，增加 §2.4 说明 |
| | | 🟡 Replace 独立限流 400 次/分钟/市场（§2.2） |
| | | 🟡 Batch Cancel 精简响应体，移除 `cancelled_orders` 明细数组（§3.2） |
| | | 🟡 WebSocket 全通道增加 `sequence_id` + gap 检测逻辑（§7.1、§7.2） |
| v1.2 | 2026-02-28 | 🔴 Mint/Burn 副作用 entry_type 改回 `MINT_COST`/`BURN_REVENUE`，通过 `reference_type` 区分（§3.3、§3.4） |
| | | 🟠 补充 Mint/Burn 绕过 Kafka 的库存同步例外规则 + 伪代码（§5.1） |
| | | 🟠 Replace 增加 Step 0 幂等前置检查 `client_order_id`（§3.1 执行流程图） |
| | | 🟡 澄清 `cancel_scope` 基于 `original_direction` 语义（§3.2） |
| | | 🟡 Kafka 消费伪代码补充手续费 `fee_cents` 扣减逻辑（§5.1） |
| v1.3 | 2026-02-28 | **文档与代码对齐审计（5 项跨文档反馈修正）**: |
| | | 🔴 §2.3 权限边界：增加 MVP 实现状态列，标注全部 🔲 待实现项 |
| | | 🔴 §2.4 Auto-Netting：明确标注 `execute_netting_if_needed` 需改动（P0 Blocker） |
| | | 🔴 §3 新增撮合引擎侧改动清单（Impact Matrix），列出 8 项所需改动及优先级 |
| | | 🔴 §3.3/§3.4 Mint/Burn：新增第 7 步可审计事件记录（trades 表虚拟成交），消除审计盲区 |
| | | 🟠 §5.1 库存数据源：Kafka 降级为 Phase 2 目标，新增 MVP REST 轮询替代方案 + `poll_trades` 伪代码 |
| | | 🟠 §7.2 WS 成交推送：注释更新为 MVP（REST 轮询权威）/ Phase 2（Kafka 权威）双层说明 |
| | | 🟡 §8 事件总览表：增加 MVP 替代方案列 |
| | | 🟡 重新表述"AMM 是超级用户"——业务逻辑层面是超级用户，但基础设施层面需要撮合引擎扩展 |
| v1.4 | 2026-02-28 | **认证模型 MVP 回退 + UUID 对齐**: |
| | | 🔴 §2.2 认证方式：标注 pm_gateway 不支持 Service Token/privilege claims，新增 MVP 回退方案（标准 JWT + 自身节流） |
| | | 🔴 §3 Impact Matrix：增加 MVP 回退方案列，明确 3 项 Hard Blocker（Mint/Burn/Netting 开关）无回退 |
| | | 🟠 §2.1 AMM user_id 从 `AMM_SYSTEM_001` 改为固定 UUID `00000000-0000-4000-a000-000000000001`（对齐全局约定 `users.id UUID` 类型） |
| | | 🟡 保留 `AMM_SYSTEM_001` 作为文档可读别名，实际 DB/API 使用 UUID |

---

*文档版本: v1.4 | 生成日期: 2026-02-28 | 状态: 草稿（待 Review）*
*对齐: AMM 设计文档 v7.1 + 全局约定与数据库设计 v2.3*
