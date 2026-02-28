# 文档 2：API 接口契约

> **版本**: v1.2
> **状态**: 草稿（待 Review）
> **适用范围**: Phase 1 MVP，前后端对接与集成测试的唯一依据
> **对齐文档**: 《完整实施计划 v4.1》、《全局约定与数据库设计 v2.3》
> **技术栈**: FastAPI + Pydantic v2 + JWT
> **日期**: 2026-02-20

---

## 第一部分：全局约定

### 1.1 Base URL

```
开发环境: http://localhost:8000/api/v1
```

所有接口路径以 `/api/v1` 为前缀。版本号在 URL 中体现，便于中期共存升级。

### 1.2 认证方式

JWT Bearer Token，通过 `Authorization` 请求头传递：

```
Authorization: Bearer <access_token>
```

- 登录接口返回 `access_token`（有效期 30 分钟）和 `refresh_token`（有效期 7 天）
- 除 `POST /auth/register` 和 `POST /auth/login` 外，所有接口需要认证
- Token 过期返回 `401`，需用 `refresh_token` 换取新 Token

### 1.3 统一响应格式

所有接口（含错误响应）使用统一 JSON 封装：

```json
{
    "code": 0,
    "message": "success",
    "data": { ... },
    "timestamp": "2026-02-20T12:00:00Z",
    "request_id": "req_abc123"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| code | int | 0=成功, 非0=错误码 |
| message | string | 人类可读描述 |
| data | object/null | 业务数据，错误时为 null |
| timestamp | string | 服务器响应时间 (ISO 8601 UTC) |
| request_id | string | 请求追踪 ID，排查问题用 |

### 1.4 金额展示规则

API 响应中的金额字段采用**双字段策略**，同时返回美分原值和美元展示值：

```json
{
    "available_balance_cents": 150000,
    "available_balance_display": "$1,500.00"
}
```

- 所有写入接口（下单等）只接受 `_cents` 整数字段
- 所有读取接口同时返回 `_cents`（int，用于计算）和 `_display`（string，用于展示）
- 价格字段范围 [1, 99]，直接用整数，不提供 display 字段

### 1.5 分页约定

列表接口统一使用游标分页（Cursor-based Pagination），**不返回 `total_count`**（避免 `COUNT(*)` 全表扫描，尤其在千万级 `ledger_entries` 表上），前端使用 `has_more` 驱动无限滚动：

```json
{
    "items": [ ... ],
    "next_cursor": "eyJ0IjoiMjAyNi0wMi0yMFQxMjowMDowMFoifQ==",
    "has_more": true
}
```

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| limit | int | 20 | 每页条数，最大 100 |
| cursor | string | null | 翻页游标，首页不传 |

**游标实现规范**:
- 游标内部使用**主键 `id`**（UUID 或 BIGSERIAL），**不使用 `created_at`**
- 理由：`created_at` 在高并发下可能重复（同一毫秒多条记录），导致翻页漏行或重复；主键天然唯一且有序
- 游标编码为 Base64（`eyJ...`），内部格式: `{"id": "<last_item_id>"}`
- 后端 SQL: `WHERE id < :cursor_id ORDER BY id DESC LIMIT :limit`（降序场景）
- `ledger_entries` 使用 `BIGSERIAL` 主键，天然递增，游标排序高效
- `orders`、`trades` 使用 `UUID` 主键，配合 `created_at DESC, id DESC` 复合排序

### 1.6 错误码体系

| 错误码范围 | 模块 | 示例 |
|-----------|------|------|
| 1xxx | 认证/用户 | 1001=用户名已存在, 1002=邮箱已存在, 1003=用户名或密码错误, 1004=账户已禁用, 1005=Refresh Token无效或过期 |
| 2xxx | 账户 | 2001=余额不足, 2002=账户冻结 |
| 3xxx | 话题 | 3001=话题不存在, 3002=话题未激活, 3003=话题状态不允许裁决, 3004=裁决结果无效 |
| 4xxx | 订单 | 4001=价格超范围, 4002=超限额, 4003=自成交, 4004=订单不存在, 4005=幂等冲突, 4006=订单状态不可取消 |
| 5xxx | 持仓 | 5001=持仓不足 |
| 9xxx | 系统 | 9001=限流, 9002=系统内部错误 |

**错误响应示例**:
```json
{
    "code": 2001,
    "message": "Insufficient balance: required 6500 cents, available 3000 cents",
    "data": null,
    "timestamp": "2026-02-20T12:00:00Z",
    "request_id": "req_abc123"
}
```

### 1.7 HTTP 状态码映射

| HTTP 状态码 | 含义 | 对应场景 |
|------------|------|---------|
| 200 | 成功 | 查询、取消订单 |
| 201 | 已创建 | 注册用户、下单 |
| 400 | 请求参数错误 | Pydantic 校验失败 |
| 401 | 未认证 | Token 缺失或过期 |
| 403 | 无权限 | 操作他人资源 |
| 404 | 资源不存在 | 话题/订单不存在 |
| 409 | 冲突 | 幂等键(不同Payload)重复、用户名已存在 |
| 422 | 业务规则拒绝 | 余额不足、持仓不足、自成交、超限额 |
| 429 | 限流 | 请求过于频繁 |
| 500 | 内部错误 | 未预期异常 |

### 1.8 限流规则

| 接口类别 | 限流 | 说明 |
|---------|------|------|
| 认证接口 | 5 次/分钟/IP | 防暴力破解 |
| 下单接口 | 30 次/分钟/用户 | 防刷单 |
| 查询接口 | 120 次/分钟/用户 | 宽松 |
| 管理接口 | 10 次/分钟/用户 | 运维操作 |

---

## 第二部分：认证模块 (pm_gateway)

### 2.1 用户注册

```
POST /api/v1/auth/register
```

**无需认证**

**请求体**:
```json
{
    "username": "alice",
    "email": "alice@example.com",
    "password": "SecureP@ss123"
}
```

| 字段 | 类型 | 必填 | 校验规则 |
|------|------|------|---------|
| username | string | 是 | 3-64 字符, 字母/数字/下划线, 唯一 |
| email | string | 是 | 合法邮箱格式, 唯一 |
| password | string | 是 | 8-128 字符, 至少含大写/小写/数字 |

**成功响应** (201):
```json
{
    "code": 0,
    "message": "User registered successfully",
    "data": {
        "user_id": "550e8400-e29b-41d4-a716-446655440000",
        "username": "alice",
        "email": "alice@example.com",
        "created_at": "2026-02-20T12:00:00Z"
    }
}
```

**错误**:
| 状态码 | 错误码 | 场景 |
|--------|--------|------|
| 409 | 1001 | 用户名已存在 |
| 409 | 1002 | 邮箱已存在 |
| 400 | — | 请求体校验失败 |

**副作用**: 自动创建对应的 `accounts` 行（available=0, frozen=0）。

---

### 2.2 用户登录

```
POST /api/v1/auth/login
```

**无需认证**

**请求体**:
```json
{
    "username": "alice",
    "password": "SecureP@ss123"
}
```

**成功响应** (200):
```json
{
    "code": 0,
    "message": "Login successful",
    "data": {
        "access_token": "eyJhbGciOi...",
        "refresh_token": "eyJhbGciOi...",
        "token_type": "Bearer",
        "expires_in": 1800,
        "user": {
            "user_id": "550e8400-e29b-41d4-a716-446655440000",
            "username": "alice",
            "email": "alice@example.com"
        }
    }
}
```

**错误**:
| 状态码 | 错误码 | 场景 |
|--------|--------|------|
| 401 | 1003 | 用户名或密码错误 |
| 422 | 1004 | 账户已禁用 |

---

### 2.3 刷新 Token

```
POST /api/v1/auth/refresh
```

**无需 Access Token**（用 Refresh Token）

**请求体**:
```json
{
    "refresh_token": "eyJhbGciOi..."
}
```

**成功响应** (200):
```json
{
    "code": 0,
    "message": "Token refreshed",
    "data": {
        "access_token": "eyJhbGciOi...(new)",
        "expires_in": 1800
    }
}
```

**错误**:
| 状态码 | 错误码 | 场景 |
|--------|--------|------|
| 401 | 1005 | Refresh Token 无效或过期 |

---

## 第三部分：账户模块 (pm_account)

### 3.1 查询余额

```
GET /api/v1/account/balance
```

**需要认证**

**成功响应** (200):
```json
{
    "code": 0,
    "data": {
        "user_id": "550e8400-...",
        "available_balance_cents": 150000,
        "available_balance_display": "$1,500.00",
        "frozen_balance_cents": 6500,
        "frozen_balance_display": "$65.00",
        "total_balance_cents": 156500,
        "total_balance_display": "$1,565.00"
    }
}
```

---

### 3.2 模拟充值

```
POST /api/v1/account/deposit
```

**需要认证**。MVP 阶段为模拟充值，无需真实支付。

**请求体**:
```json
{
    "amount_cents": 100000
}
```

| 字段 | 类型 | 必填 | 校验规则 |
|------|------|------|---------|
| amount_cents | int | 是 | > 0, 最大 10,000,000 (10 万美元) |

**成功响应** (200):
```json
{
    "code": 0,
    "data": {
        "available_balance_cents": 250000,
        "available_balance_display": "$2,500.00",
        "deposited_cents": 100000,
        "deposited_display": "$1,000.00",
        "ledger_entry_id": 1001
    }
}
```

**副作用**: 写入一条 `DEPOSIT` 流水。

---

### 3.3 模拟提现

```
POST /api/v1/account/withdraw
```

**需要认证**

**请求体**:
```json
{
    "amount_cents": 50000
}
```

| 字段 | 类型 | 必填 | 校验规则 |
|------|------|------|---------|
| amount_cents | int | 是 | > 0, <= available_balance |

**成功响应** (200):
```json
{
    "code": 0,
    "data": {
        "available_balance_cents": 200000,
        "available_balance_display": "$2,000.00",
        "withdrawn_cents": 50000,
        "withdrawn_display": "$500.00",
        "ledger_entry_id": 1002
    }
}
```

**错误**:
| 状态码 | 错误码 | 场景 |
|--------|--------|------|
| 422 | 2001 | 可用余额不足 |

**副作用**: 写入一条 `WITHDRAW` 流水（金额为负数）。

---

### 3.4 查询资金流水

```
GET /api/v1/account/ledger
```

**需要认证**

**查询参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| limit | int | 否 | 每页条数，默认 20，最大 100 |
| cursor | string | 否 | 翻页游标 |
| entry_type | string | 否 | 按流水类型过滤，逗号分隔 |
| market_id | string | 否 | 按话题过滤 |

**成功响应** (200):
```json
{
    "code": 0,
    "data": {
        "items": [
            {
                "id": 1005,
                "entry_type": "ORDER_FREEZE",
                "amount_cents": -6500,
                "amount_display": "-$65.00",
                "balance_after_cents": 93500,
                "balance_after_display": "$935.00",
                "reference_type": "ORDER",
                "reference_id": "550e8400-...",
                "description": "Freeze for Buy YES @65 x100",
                "created_at": "2026-02-20T12:01:00Z"
            },
            {
                "id": 1004,
                "entry_type": "DEPOSIT",
                "amount_cents": 100000,
                "amount_display": "$1,000.00",
                "balance_after_cents": 100000,
                "balance_after_display": "$1,000.00",
                "reference_type": "DEPOSIT",
                "reference_id": null,
                "description": "Simulated deposit",
                "created_at": "2026-02-20T12:00:00Z"
            }
        ],
        "next_cursor": "eyJ0IjoiMjAyNi0wMi0yMFQxMjowMDowMFoifQ==",
        "has_more": true
    }
}
```

---

## 第四部分：预测话题模块 (pm_market)

### 4.1 查询话题列表

```
GET /api/v1/markets
```

**需要认证**

**查询参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| status | string | 否 | 按状态过滤，默认 ACTIVE |
| category | string | 否 | 按分类过滤 |
| limit | int | 否 | 每页条数，默认 20 |
| cursor | string | 否 | 翻页游标 |

**成功响应** (200):
```json
{
    "code": 0,
    "data": {
        "items": [
            {
                "id": "MKT-BTC-100K-2026",
                "title": "Will BTC exceed $100,000 by end of 2026?",
                "description": "Resolves YES if Bitcoin price exceeds $100,000...",
                "category": "crypto",
                "status": "ACTIVE",
                "min_price_cents": 1,
                "max_price_cents": 99,
                "maker_fee_bps": 10,
                "taker_fee_bps": 20,
                "reserve_balance_cents": 5000000,
                "reserve_balance_display": "$50,000.00",
                "total_yes_shares": 50000,
                "total_no_shares": 50000,
                "trading_start_at": "2026-01-01T00:00:00Z",
                "resolution_date": "2026-12-31T23:59:59Z"
            }
        ],
        "next_cursor": null,
        "has_more": false
    }
}
```

---

### 4.2 查询话题详情

```
GET /api/v1/markets/{market_id}
```

**需要认证**

**成功响应** (200):
```json
{
    "code": 0,
    "data": {
        "id": "MKT-BTC-100K-2026",
        "title": "Will BTC exceed $100,000 by end of 2026?",
        "description": "Resolves YES if Bitcoin price exceeds $100,000...",
        "category": "crypto",
        "status": "ACTIVE",
        "min_price_cents": 1,
        "max_price_cents": 99,
        "max_order_quantity": 10000,
        "max_position_per_user": 25000,
        "max_order_amount_cents": 1000000,
        "maker_fee_bps": 10,
        "taker_fee_bps": 20,
        "reserve_balance_cents": 5000000,
        "reserve_balance_display": "$50,000.00",
        "pnl_pool_cents": -12500,
        "pnl_pool_display": "-$125.00",
        "total_yes_shares": 50000,
        "total_no_shares": 50000,
        "trading_start_at": "2026-01-01T00:00:00Z",
        "trading_end_at": null,
        "resolution_date": "2026-12-31T23:59:59Z",
        "resolution_result": null
    }
}
```

**错误**:
| 状态码 | 错误码 | 场景 |
|--------|--------|------|
| 404 | 3001 | 话题不存在 |

---

### 4.3 查询订单簿深度

```
GET /api/v1/markets/{market_id}/orderbook
```

**需要认证**

**查询参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| levels | int | 否 | 返回的价格档位数，默认 10，最大 99 |

**成功响应** (200):

注意：返回的是**用户友好视角**（YES 买/卖 + NO 买/卖），而非内部单一 YES 订单簿的原始数据。服务端将内部 YES 簿的 bids/asks 双向转换展示。

```json
{
    "code": 0,
    "data": {
        "market_id": "MKT-BTC-100K-2026",
        "yes": {
            "bids": [
                { "price_cents": 65, "total_quantity": 500 },
                { "price_cents": 64, "total_quantity": 300 },
                { "price_cents": 63, "total_quantity": 200 }
            ],
            "asks": [
                { "price_cents": 67, "total_quantity": 400 },
                { "price_cents": 68, "total_quantity": 250 },
                { "price_cents": 70, "total_quantity": 100 }
            ]
        },
        "no": {
            "bids": [
                { "price_cents": 33, "total_quantity": 400 },
                { "price_cents": 32, "total_quantity": 250 },
                { "price_cents": 30, "total_quantity": 100 }
            ],
            "asks": [
                { "price_cents": 35, "total_quantity": 500 },
                { "price_cents": 36, "total_quantity": 300 },
                { "price_cents": 37, "total_quantity": 200 }
            ]
        },
        "last_trade_price_cents": 65,
        "updated_at": "2026-02-20T12:05:00Z"
    }
}
```

**视角转换规则**:

| YES 簿原始数据 | 排序 | 转换公式 | NO 视角 | 排序 |
|---------------|------|---------|---------|------|
| `yes.bids` (降序: 65, 64, 63) | 价格降序 | 100 - yes.bids | `no.asks` (升序: 35, 36, 37) | 价格升序 |
| `yes.asks` (升序: 67, 68, 70) | 价格升序 | 100 - yes.asks | `no.bids` (降序: 33, 32, 30) | 价格降序 |

- `no.bids[i].price_cents` = 100 - `yes.asks[i].price_cents`（NO 买价 = 100 - YES 卖价）
- `no.asks[i].price_cents` = 100 - `yes.bids[i].price_cents`（NO 卖价 = 100 - YES 买价）
- 数量直接对应：`no.bids[i].total_quantity` = `yes.asks[i].total_quantity`（同一个挂单池）
- **排序反转**：YES bids 降序 → NO asks 升序；YES asks 升序 → NO bids 降序

---

## 第五部分：订单模块 (pm_order)

### 5.1 下单

```
POST /api/v1/orders
```

**需要认证**

这是整个平台最核心的接口，触发完整的"转换→风控→撮合→清算→Netting"链路。

**请求体**:
```json
{
    "client_order_id": "coid_20260220_001",
    "market_id": "MKT-BTC-100K-2026",
    "side": "YES",
    "direction": "BUY",
    "price_cents": 65,
    "quantity": 100,
    "time_in_force": "GTC"
}
```

| 字段 | 类型 | 必填 | 校验规则 |
|------|------|------|---------|
| client_order_id | string | 是 | 1-64 字符，唯一幂等键 |
| market_id | string | 是 | 已存在的活跃话题 |
| side | string | 是 | `YES` 或 `NO` |
| direction | string | 是 | `BUY` 或 `SELL` |
| price_cents | int | 是 | [1, 99]，话题允许范围内 |
| quantity | int | 是 | > 0，不超过话题 max_order_quantity |
| time_in_force | string | 否 | `GTC`（默认）或 `IOC` |

**成功响应** (201):
```json
{
    "code": 0,
    "message": "Order placed successfully",
    "data": {
        "order": {
            "id": "660e8400-e29b-41d4-a716-446655440001",
            "client_order_id": "coid_20260220_001",
            "market_id": "MKT-BTC-100K-2026",
            "original_side": "YES",
            "original_direction": "BUY",
            "original_price_cents": 65,
            "book_type": "NATIVE_BUY",
            "book_direction": "BUY",
            "book_price_cents": 65,
            "price_type": "LIMIT",
            "time_in_force": "GTC",
            "quantity": 100,
            "filled_quantity": 80,
            "remaining_quantity": 20,
            "frozen_amount": 1300,
            "frozen_asset_type": "FUNDS",
            "status": "PARTIALLY_FILLED",
            "created_at": "2026-02-20T12:10:00Z"
        },
        "trades": [
            {
                "trade_id": "TRD_20260220_001",
                "scenario": "MINT",
                "counterparty_side": "SELL",
                "price_cents": 65,
                "quantity": 50,
                "role": "TAKER",
                "fee_cents": 65,
                "realized_pnl_cents": null,
                "executed_at": "2026-02-20T12:10:00Z"
            },
            {
                "trade_id": "TRD_20260220_002",
                "scenario": "TRANSFER_YES",
                "counterparty_side": "SELL",
                "price_cents": 63,
                "quantity": 30,
                "role": "TAKER",
                "fee_cents": 38,
                "realized_pnl_cents": null,
                "executed_at": "2026-02-20T12:10:00Z"
            }
        ],
        "netting_result": null
    }
}
```

**响应字段说明**:
- `order`: 订单当前状态（下单后可能已部分/全部成交）
- `trades`: 本次下单产生的成交列表（可能 0~N 笔）
- `netting_result`: 若触发 Auto-Netting，返回抵消信息；否则 null
- `frozen_amount`: 当前剩余冻结额。**⚠️ 前端展示建议**: 当 `frozen_asset_type = "FUNDS"` 时，`frozen_amount` 包含**交易本金 + 最大手续费缓冲**，实际冻结额会略大于 `price × remaining_quantity`。建议前端展示时附加说明文案（如 "含手续费预留"），避免用户误以为被多扣

**Netting 结果示例** (当触发时):
```json
{
    "netting_result": {
        "pairs_netted": 50,
        "released_cents": 5000,
        "released_display": "$50.00"
    }
}
```

**错误**:
| 状态码 | 错误码 | 场景 |
|--------|--------|------|
| 404 | 3001 | 话题不存在 |
| 422 | 3002 | 话题未激活（非 ACTIVE 状态） |
| 422 | 4001 | 价格超出 [min_price, max_price] 范围 |
| 422 | 4002 | 超过单笔限额（数量或金额） |
| 422 | 4003 | 自成交拒绝 |
| 422 | 2001 | 余额不足 |
| 422 | 5001 | 持仓不足（Sell 操作时） |
| 409 | 4005 | client_order_id 重复且 Payload 不同（幂等冲突） |

**幂等性规则** (对标 Stripe 幂等键设计):
- **相同 `client_order_id` + 相同 Payload**: 返回 `200 OK`，直接返回已有订单数据（含 trades），视为幂等重试成功。前端请求库不会抛异常。
- **相同 `client_order_id` + 不同 Payload**: 返回 `409 Conflict`（错误码 4005），表示幂等键已被占用且参数冲突。不会产生新订单。

**副作用**:
1. 写入 `ORDER_FREEZE` 流水
2. 若成交：写入成交记录 + 按场景写入清算流水（用户侧 + 系统侧成对）
3. 若触发 Netting：写入 `NETTING` + `NETTING_RESERVE_OUT` 流水
4. 若扣手续费：写入 `FEE` + `FEE_REVENUE` 流水

---

### 5.2 取消订单

```
POST /api/v1/orders/{order_id}/cancel
```

**需要认证**。只能取消自己的订单，且订单状态必须为 `OPEN` 或 `PARTIALLY_FILLED`。

**成功响应** (200):
```json
{
    "code": 0,
    "message": "Order cancelled",
    "data": {
        "order_id": "660e8400-...",
        "status": "CANCELLED",
        "unfrozen_amount": 1300,
        "unfrozen_asset_type": "FUNDS",
        "remaining_quantity_cancelled": 20
    }
}
```

**错误**:
| 状态码 | 错误码 | 场景 |
|--------|--------|------|
| 404 | 4004 | 订单不存在 |
| 403 | — | 非本人订单 |
| 422 | 4006 | 订单状态不可取消（FILLED/CANCELLED/REJECTED） |

**副作用**:
1. 根据 `frozen_asset_type` 解冻对应资产（资金或持仓）
2. 写入 `ORDER_UNFREEZE` 流水
3. 从内存订单簿中移除挂单

---

### 5.3 查询订单列表

```
GET /api/v1/orders
```

**需要认证**

**查询参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| market_id | string | 否 | 按话题过滤 |
| status | string | 否 | 按状态过滤，逗号分隔多个 |
| side | string | 否 | YES 或 NO |
| direction | string | 否 | BUY 或 SELL |
| limit | int | 否 | 每页条数，默认 20 |
| cursor | string | 否 | 翻页游标 |

**成功响应** (200):
```json
{
    "code": 0,
    "data": {
        "items": [
            {
                "id": "660e8400-...",
                "client_order_id": "coid_20260220_001",
                "market_id": "MKT-BTC-100K-2026",
                "original_side": "YES",
                "original_direction": "BUY",
                "original_price_cents": 65,
                "book_type": "NATIVE_BUY",
                "price_type": "LIMIT",
                "time_in_force": "GTC",
                "quantity": 100,
                "filled_quantity": 80,
                "remaining_quantity": 20,
                "frozen_amount": 1300,
                "frozen_asset_type": "FUNDS",
                "status": "PARTIALLY_FILLED",
                "created_at": "2026-02-20T12:10:00Z",
                "updated_at": "2026-02-20T12:10:05Z"
            }
        ],
        "next_cursor": null,
        "has_more": false
    }
}
```

---

### 5.4 查询订单详情

```
GET /api/v1/orders/{order_id}
```

**需要认证**

**成功响应** (200):

返回完整订单信息 + 该订单的所有成交记录。

```json
{
    "code": 0,
    "data": {
        "order": {
            "id": "660e8400-...",
            "client_order_id": "coid_20260220_001",
            "market_id": "MKT-BTC-100K-2026",
            "original_side": "YES",
            "original_direction": "BUY",
            "original_price_cents": 65,
            "book_type": "NATIVE_BUY",
            "book_direction": "BUY",
            "book_price_cents": 65,
            "price_type": "LIMIT",
            "time_in_force": "GTC",
            "quantity": 100,
            "filled_quantity": 100,
            "remaining_quantity": 0,
            "frozen_amount": 0,
            "frozen_asset_type": "FUNDS",
            "status": "FILLED",
            "cancel_reason": null,
            "created_at": "2026-02-20T12:10:00Z",
            "updated_at": "2026-02-20T12:15:00Z"
        },
        "trades": [
            {
                "trade_id": "TRD_20260220_001",
                "scenario": "MINT",
                "price_cents": 65,
                "quantity": 50,
                "role": "TAKER",
                "fee_cents": 65,
                "realized_pnl_cents": null,
                "executed_at": "2026-02-20T12:10:00Z"
            },
            {
                "trade_id": "TRD_20260220_003",
                "scenario": "TRANSFER_YES",
                "price_cents": 64,
                "quantity": 50,
                "role": "MAKER",
                "fee_cents": 32,
                "realized_pnl_cents": null,
                "executed_at": "2026-02-20T12:15:00Z"
            }
        ]
    }
}
```

**`frozen_amount` 前端展示规范** (适用于 §5.1、§5.3、§5.4 所有涉及订单的响应):

> 当 `frozen_asset_type = "FUNDS"` 时，`frozen_amount` = 交易本金 + taker_fee_bps 级别的最大手续费缓冲。
> 前端建议: 在订单卡片中展示为 "冻结 $X.XX（含手续费预留）"，或拆分展示为 "本金 + 手续费预留"。
> 这样用户不会因为看到冻结额大于 `price × quantity` 而困惑。
> 当 `frozen_asset_type = "YES_SHARES"` 或 `"NO_SHARES"` 时，`frozen_amount` 就是冻结的合约份数，无需额外说明。

---

## 第六部分：持仓模块

### 6.1 查询我的持仓列表

```
GET /api/v1/positions
```

**需要认证**

**查询参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| market_id | string | 否 | 按话题过滤 |
| has_volume | bool | 否 | 仅返回有持仓的记录，默认 true |

**成功响应** (200):
```json
{
    "code": 0,
    "data": {
        "items": [
            {
                "market_id": "MKT-BTC-100K-2026",
                "market_title": "Will BTC exceed $100,000 by end of 2026?",
                "yes": {
                    "volume": 200,
                    "cost_sum_cents": 13000,
                    "cost_sum_display": "$130.00",
                    "avg_cost_display": "$0.65",
                    "pending_sell": 0,
                    "available": 200
                },
                "no": {
                    "volume": 50,
                    "cost_sum_cents": 1750,
                    "cost_sum_display": "$17.50",
                    "avg_cost_display": "$0.35",
                    "pending_sell": 0,
                    "available": 50
                },
                "updated_at": "2026-02-20T12:30:00Z"
            }
        ]
    }
}
```

**字段说明**:
- `avg_cost_display`: 仅供展示，由 `cost_sum / volume` 在序列化层计算（volume=0 时显示 "$0.00"）
- `available`: `volume - pending_sell`，可卖出的份数

---

### 6.2 查询单个话题持仓

```
GET /api/v1/positions/{market_id}
```

**需要认证**

返回格式同 6.1 中的单条记录。若用户在该话题无持仓，返回 YES/NO volume 均为 0 的默认记录。

---

## 第七部分：成交记录

### 7.1 查询我的成交记录

```
GET /api/v1/trades
```

**需要认证**

**查询参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| market_id | string | 否 | 按话题过滤 |
| scenario | string | 否 | 按场景过滤: MINT/TRANSFER_YES/TRANSFER_NO/BURN |
| limit | int | 否 | 每页条数，默认 20 |
| cursor | string | 否 | 翻页游标 |

**成功响应** (200):
```json
{
    "code": 0,
    "data": {
        "items": [
            {
                "trade_id": "TRD_20260220_001",
                "market_id": "MKT-BTC-100K-2026",
                "scenario": "TRANSFER_YES",
                "my_side": "SELL",
                "my_original_side": "YES",
                "my_original_direction": "SELL",
                "counterparty_original_side": "YES",
                "counterparty_original_direction": "BUY",
                "price_cents": 72,
                "quantity": 50,
                "role": "MAKER",
                "fee_cents": 36,
                "fee_display": "$0.36",
                "realized_pnl_cents": 314,
                "realized_pnl_display": "+$3.14",
                "executed_at": "2026-02-20T12:10:00Z"
            }
        ],
        "next_cursor": null,
        "has_more": false
    }
}
```

**字段说明**:
- `my_side`: 当前用户在此笔成交中是 BUY 侧还是 SELL 侧（订单簿视角）
- `my_original_side/direction`: 当前用户的原始意图
- `counterparty_original_side/direction`: 对手方的原始意图（帮助用户理解场景）
- `role`: `MAKER` 或 `TAKER`
- `realized_pnl_cents`: 本笔成交中当前用户的已实现盈亏（美分）。开仓操作（MINT 的双方、TRANSFER 的买方）为 `null`；平仓操作（TRANSFER 的卖方、BURN 的双方）在清算时计算并持久化。计算公式: `卖出收入 - 按比例分摊的历史成本`
- `realized_pnl_display`: 盈亏展示值，盈利前缀 "+"，亏损前缀 "-"

**四种场景下的 realized_pnl 适用性**:

| 场景 | Buy 侧行为 | buy_realized_pnl | Sell 侧行为 | sell_realized_pnl |
|------|-----------|-----------------|------------|------------------|
| MINT | Buy YES (开仓) | `null` | Buy NO (开仓) | `null` |
| TRANSFER_YES | Buy YES (开仓) | `null` | Sell YES (平仓) | `(YES成交价 × qty) - 释放的YES历史成本` |
| TRANSFER_NO | Sell NO (平仓) | `(NO成交价 × qty) - 释放的NO历史成本` | Buy NO (开仓) | `null` |
| BURN | Sell NO (平仓) | `(NO成交价 × qty) - 释放的NO历史成本` | Sell YES (平仓) | `(YES成交价 × qty) - 释放的YES历史成本` |

---

## 第八部分：管理/运维接口

### 8.1 不变量校验

```
POST /api/v1/admin/verify-invariants
```

**需要认证**（MVP 阶段暂不做角色区分，后续增加 admin 角色）

**成功响应** (200):
```json
{
    "code": 0,
    "data": {
        "verified_at": "2026-02-20T13:00:00Z",
        "all_passed": true,
        "checks": [
            {
                "name": "shares_balanced",
                "description": "total_yes_shares == total_no_shares for all active markets",
                "passed": true,
                "details": { "markets_checked": 3 }
            },
            {
                "name": "reserve_consistency",
                "description": "reserve_balance == total_yes_shares * 100 for all active markets",
                "passed": true,
                "details": { "markets_checked": 3 }
            },
            {
                "name": "cost_conservation",
                "description": "reserve_balance + pnl_pool == Σ(cost_sum) for all active markets",
                "passed": true,
                "details": { "markets_checked": 3 }
            },
            {
                "name": "global_zero_sum",
                "description": "Σ(user balances) + reserve + fees == Σ(deposits) - Σ(withdrawals)",
                "passed": true,
                "details": {
                    "total_user_funds": 5000000,
                    "reserve_balance": 3000000,
                    "fee_balance": 15000,
                    "net_deposits": 8015000
                }
            },
            {
                "name": "reserve_account_consistency",
                "description": "SYSTEM_RESERVE.available == Σ(active market reserve_balance)",
                "passed": true,
                "details": {
                    "global_reserve": 3000000,
                    "sum_market_reserves": 3000000
                }
            }
        ]
    }
}
```

---

### 8.2 话题统计

```
GET /api/v1/admin/markets/{market_id}/stats
```

**需要认证**

**成功响应** (200):
```json
{
    "code": 0,
    "data": {
        "market_id": "MKT-BTC-100K-2026",
        "reserve_balance_cents": 5000000,
        "pnl_pool_cents": -12500,
        "total_yes_shares": 50000,
        "total_no_shares": 50000,
        "active_orders_count": 127,
        "total_trades_count": 4532,
        "trades_by_scenario": {
            "MINT": 2100,
            "TRANSFER_YES": 1500,
            "TRANSFER_NO": 800,
            "BURN": 132
        },
        "total_fees_collected_cents": 15000,
        "unique_traders": 89
    }
}
```

---

### 8.3 市场裁决与结算

```
POST /api/v1/admin/markets/{market_id}/resolve
```

**需要认证**（MVP 阶段暂不做角色区分，后续增加 admin 角色）

管理员对话题做出裁决（YES 胜、NO 胜、或作废），并触发自动结算流程。

**请求体**:
```json
{
    "resolution_result": "YES"
}
```

| 字段 | 类型 | 必填 | 校验规则 |
|------|------|------|---------|
| resolution_result | string | 是 | `YES`、`NO`、`VOID` 三选一 |

**前置条件**:
- 话题状态必须为 `ACTIVE` 或 `SUSPENDED` 或 `HALTED`
- 话题不能已处于 `RESOLVED` / `SETTLED` / `VOIDED` 状态

**成功响应** (200):
```json
{
    "code": 0,
    "message": "Market resolved and settlement initiated",
    "data": {
        "market_id": "MKT-BTC-100K-2026",
        "resolution_result": "YES",
        "resolved_at": "2026-12-31T23:59:59Z",
        "settlement": {
            "status": "COMPLETED",
            "settled_at": "2027-01-01T00:00:05Z",
            "total_payout_cents": 5000000,
            "total_payout_display": "$50,000.00",
            "winners_count": 89,
            "cancelled_orders_count": 127,
            "cancelled_orders_unfrozen_cents": 350000
        }
    }
}
```

**结算流程** (原子事务内):
1. `market.status` → `RESOLVED`，记录 `resolution_result` 和 `resolved_at`
2. 取消该话题所有活跃订单（OPEN/PARTIALLY_FILLED → CANCELLED），解冻冻结资金/持仓
3. 根据裁决结果派奖:
   - `YES`: 每份 YES 合约持有者获得 100 美分，Reserve 清零
   - `NO`: 每份 NO 合约持有者获得 100 美分，Reserve 清零
   - `VOID`: 全额退还每个用户的 `cost_sum`（YES + NO），Reserve 清零
4. 写入 `SETTLEMENT_PAYOUT`（或 `SETTLEMENT_VOID`）流水
5. `market.status` → `SETTLED`（或 `VOIDED`），记录 `settled_at`

**错误**:
| 状态码 | 错误码 | 场景 |
|--------|--------|------|
| 404 | 3001 | 话题不存在 |
| 422 | 3003 | 话题状态不允许裁决（已 RESOLVED/SETTLED/VOIDED） |
| 422 | 3004 | 裁决结果无效 |

---

## 第九部分：Pydantic Schema 示例

### 9.1 请求 Schema

```python
from pydantic import BaseModel, Field, field_validator

class PlaceOrderRequest(BaseModel):
    """下单请求"""
    client_order_id: str = Field(..., min_length=1, max_length=64)
    market_id: str = Field(..., min_length=1, max_length=64)
    side: str = Field(..., pattern="^(YES|NO)$")
    direction: str = Field(..., pattern="^(BUY|SELL)$")
    price_cents: int = Field(..., ge=1, le=99)
    quantity: int = Field(..., gt=0)
    time_in_force: str = Field(default="GTC", pattern="^(GTC|IOC)$")

    @field_validator('client_order_id')
    @classmethod
    def no_whitespace(cls, v: str) -> str:
        if ' ' in v:
            raise ValueError('client_order_id must not contain spaces')
        return v
```

### 9.2 响应 Schema

```python
from pydantic import BaseModel, computed_field
from typing import Generic, TypeVar

T = TypeVar("T")

class ApiResponse(BaseModel, Generic[T]):
    """统一响应封装"""
    code: int = 0
    message: str = "success"
    data: T | None = None
    timestamp: str
    request_id: str

class BalanceResponse(BaseModel):
    """余额响应"""
    user_id: str
    available_balance_cents: int
    frozen_balance_cents: int

    @computed_field
    @property
    def available_balance_display(self) -> str:
        return f"${self.available_balance_cents / 100:,.2f}"

    @computed_field
    @property
    def frozen_balance_display(self) -> str:
        return f"${self.frozen_balance_cents / 100:,.2f}"

    @computed_field
    @property
    def total_balance_cents(self) -> int:
        return self.available_balance_cents + self.frozen_balance_cents

    @computed_field
    @property
    def total_balance_display(self) -> str:
        return f"${self.total_balance_cents / 100:,.2f}"
```

### 9.3 美分 ↔ 展示 转换工具

```python
def cents_to_display(cents: int) -> str:
    """美分整数 → 美元展示字符串"""
    return f"${cents / 100:,.2f}"

# 6500 → "$65.00"
# 150000 → "$1,500.00"
# -1200 → "-$12.00"
```

---

## 第十部分：接口总览

| # | 方法 | 路径 | 模块 | 认证 | 说明 |
|---|------|------|------|------|------|
| 1 | POST | /auth/register | pm_gateway | 否 | 用户注册 |
| 2 | POST | /auth/login | pm_gateway | 否 | 用户登录 |
| 3 | POST | /auth/refresh | pm_gateway | 否 | 刷新 Token |
| 4 | GET | /account/balance | pm_account | 是 | 查询余额 |
| 5 | POST | /account/deposit | pm_account | 是 | 模拟充值 |
| 6 | POST | /account/withdraw | pm_account | 是 | 模拟提现 |
| 7 | GET | /account/ledger | pm_account | 是 | 查询流水 |
| 8 | GET | /markets | pm_market | 是 | 话题列表 |
| 9 | GET | /markets/{id} | pm_market | 是 | 话题详情 |
| 10 | GET | /markets/{id}/orderbook | pm_market | 是 | 订单簿深度 |
| 11 | POST | /orders | pm_order | 是 | **下单（核心）** |
| 12 | POST | /orders/{id}/cancel | pm_order | 是 | 取消订单 |
| 13 | GET | /orders | pm_order | 是 | 订单列表 |
| 14 | GET | /orders/{id} | pm_order | 是 | 订单详情 |
| 15 | GET | /positions | pm_account | 是 | 持仓列表 |
| 16 | GET | /positions/{market_id} | pm_account | 是 | 单话题持仓 |
| 17 | GET | /trades | pm_clearing | 是 | 成交记录 |
| 18 | POST | /admin/verify-invariants | pm_common | 是 | 不变量校验 |
| 19 | GET | /admin/markets/{id}/stats | pm_market | 是 | 话题统计 |
| 20 | POST | /admin/markets/{id}/resolve | pm_market | 是 | **市场裁决与结算** |

**MVP 总计: 20 个接口**

---

*文档版本: v1.2 | 生成日期: 2026-02-20 | 状态: 待 Review*
*对齐: 完整实施计划 v4.1 + 数据库设计 v2.3*
*v1.1 变更: 订单簿NO视角数学修正 + 取消订单POST/cancel + 幂等性200/409分流 + 游标分页删除total_count + 成交记录双向realized_pnl*
*v1.2 变更: 移除tick_size_cents + §1.5游标分页规范(用id不用created_at) + frozen_amount前端展示规范 + §8.3市场裁决与结算接口(20个接口) + 错误码3003/3004*
