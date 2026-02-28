# AMM 数据字典与状态机规范

## AMM 自动做市商机器人 — 数据模型与状态转换定义

---

### 文档信息

| 项目 | 内容 |
|------|------|
| 文档版本 | v1.3 |
| 状态 | 草稿（待 Review） |
| 适用范围 | AMM 机器人运行时涉及的全部数据结构：DB 依赖、DB 扩展、Redis 缓存、内存状态、状态机 |
| 对齐文档 | 《全局约定与数据库设计 v2.3》、《AMM 模块设计 v7.1》、《AMM 接口与事件流契约 v1.4》 |
| 日期 | 2026-02-27 |

---

### 目录

1. 概述：AMM 数据分层架构
2. AMM 对现有 DB 表的依赖映射
3. AMM 需要的 DB Schema 扩展
4. AMM 专用 Redis 数据字典
5. AMM 内存运行时数据结构
6. 状态机定义
7. 数据流与同步规则
8. AMM 专属不变量与对账规则
9. 枚举值完整定义
10. 附录

---

## 一、概述：AMM 数据分层架构

AMM 的数据分为四层，从持久到易失排列：

```
┌──────────────────────────────────────────────────────────┐
│  Layer 4: 内存运行时 (Process Memory)                      │
│  • 策略引擎状态、定价计算中间值、活跃订单簿本地副本          │
│  • 进程重启后丢失，从 Layer 2 + Layer 3 重建               │
├──────────────────────────────────────────────────────────┤
│  Layer 3: Redis 缓存 (Semi-Persistent)                     │
│  • 库存实时缓存、活跃订单追踪、策略状态                     │
│  • AMM 独占读写，进程重启时从 Layer 1 全量重建              │
├──────────────────────────────────────────────────────────┤
│  Layer 2: Kafka 事件流 (Transient, Ordered)                │
│  • trade_events / order_events / market_events             │
│  • AMM 消费后更新 Layer 3，自身不持久化                     │
├──────────────────────────────────────────────────────────┤
│  Layer 1: PostgreSQL (Persistent, Source of Truth)          │
│  • accounts / positions / orders / trades / ledger_entries │
│  • AMM 不直接写入（除 Mint/Burn 通过 API 间接触发写入）     │
│  • AMM 读取用于启动初始化和定期对账                         │
└──────────────────────────────────────────────────────────┘
```

**关键原则**：
- AMM 的库存变更 **主入口** 是 Kafka `trade_events` 消费回调 → 写 Redis（见契约文档 §5.1）
- **唯一例外**：特权铸造 (Mint) / 特权销毁 (Burn) 不经过撮合，无 Kafka 事件，AMM 在 REST 成功响应后直接更新 Redis
- AMM **不直接写 PostgreSQL**。DB 写入由撮合引擎在处理 AMM 的 REST 请求时完成
- Redis 是 AMM 的"工作内存"，PostgreSQL 是"真相源"。两者定期对账

---

## 二、AMM 对现有 DB 表的依赖映射

AMM 作为一个特殊的"系统机器人用户"，其数据存在于现有 DB 表中。以下列出 AMM 关心的每张表、每个字段的用途。

### 2.1 accounts（AMM 账户余额）

AMM 在 `accounts` 表中有一行记录，`user_id = 'AMM_SYSTEM_001'`。

| 字段 | 类型 | AMM 用途 | 读/写 | 说明 |
|------|------|---------|-------|------|
| user_id | VARCHAR(64) | 身份标识 | 只读 | 固定值 `AMM_SYSTEM_001` |
| available_balance | BIGINT | 可用现金 | 间接写（通过 Mint/Burn/Trade） | AMM 预算的实时剩余，Auto-Reinvest 决策依据 |
| frozen_balance | BIGINT | 挂单冻结额 | 间接写（通过下单/撤单） | 所有活跃挂单冻结的资金/持仓折算总额 |
| version | BIGINT | 乐观锁 | 间接写 | AMM 不直接使用，由撮合引擎在原子操作中递增 |

**AMM 读取时机**：
- 启动初始化：`GET /api/v1/account/balance`
- 定期对账（每 5 分钟）：与 Redis `amm:inventory:{market_id}.cash_cents` 比对

### 2.2 positions（AMM 持仓）

AMM 在每个做市的 `market_id` 下有一行 `positions` 记录。

| 字段 | 类型 | AMM 用途 | 说明 |
|------|------|---------|------|
| user_id | VARCHAR(64) | 固定 `AMM_SYSTEM_001` | — |
| market_id | VARCHAR(64) | AMM 做市的话题 ID | — |
| yes_volume | INT | YES 总持仓 | 对应 Redis `yes_volume` |
| yes_cost_sum | BIGINT | YES 累计成本（美分） | 对应 Redis `yes_cost_sum_cents` |
| yes_pending_sell | INT | YES 卖单冻结份数 | AMM 的 NATIVE_SELL 挂单冻结量 |
| no_volume | INT | NO 总持仓 | 对应 Redis `no_volume` |
| no_cost_sum | BIGINT | NO 累计成本（美分） | 对应 Redis `no_cost_sum_cents` |
| no_pending_sell | INT | NO 卖单冻结份数 | AMM 的 SYNTHETIC_BUY 挂单冻结量 |

**AMM 读取时机**：
- 启动初始化：`GET /api/v1/positions/{market_id}`
- 定期对账（每 5 分钟）
- Kafka 消费异常时的 fallback 同步

**Redis ↔ DB 字段对照**：

| Redis 字段 | DB 字段 | 同步方向 | 说明 |
|-----------|---------|---------|------|
| yes_volume | positions.yes_volume | DB → Redis（启动）; Redis 自维护（运行时） | Kafka 回调更新 Redis，DB 由撮合引擎更新 |
| no_volume | positions.no_volume | 同上 | — |
| yes_cost_sum_cents | positions.yes_cost_sum | 同上 | 完全相同含义，命名差异仅为 Redis 显式标注单位 |
| no_cost_sum_cents | positions.no_cost_sum | 同上 | — |
| yes_available | yes_volume - yes_pending_sell | 派生 | Redis 中独立维护，DB 中需计算 |
| no_available | no_volume - no_pending_sell | 派生 | — |

### 2.3 orders（AMM 订单）

AMM 的挂单与普通用户共用 `orders` 表。AMM 特有的字段使用约定：

| 字段 | AMM 约定 | 说明 |
|------|---------|------|
| user_id | `AMM_SYSTEM_001` | 固定值 |
| client_order_id | `amm_{market_id}_{side}_{direction}_{timestamp_ms}_{seq}` | 命名规则见契约文档 §4.2 |
| price_type | `LIMIT` | AMM 只使用限价单 |
| time_in_force | `GTC` | AMM 挂单始终为 GTC |
| original_side | `YES` 或 `NO` | AMM 同时在 YES/NO 双边挂单 |
| original_direction | `BUY` 或 `SELL` | 四种组合：Buy YES, Sell YES, Buy NO, Sell NO |

**AMM 关心的订单状态子集**：

| 状态 | AMM 关注 | 触发动作 |
|------|---------|---------|
| OPEN | 是 | 活跃订单，存入 Redis `amm:orders` |
| PARTIALLY_FILLED | 是 | 更新 Redis `remaining`，触发重新报价 |
| FILLED | 是 | 从 Redis `amm:orders` 删除，更新库存 |
| CANCELLED | 是 | 从 Redis `amm:orders` 删除，解冻资产 |
| NEW | 否 | 瞬态，AMM 不感知 |
| REJECTED | 是 | 记录日志，触发告警 |

### 2.4 trades（AMM 相关成交）

AMM 不直接读取 `trades` 表。成交信息通过 Kafka `trade_events` 接收。

但 `trades` 表中的以下字段对 AMM 审计有价值：

| 字段 | AMM 审计用途 |
|------|------------|
| scenario | 验证 AMM 参与的成交场景分布（MINT/TRANSFER_YES/TRANSFER_NO/BURN） |
| buy_realized_pnl / sell_realized_pnl | AMM 的已实现盈亏，用于日终对账 |
| maker_fee / taker_fee | AMM 的手续费支出统计 |

**审计查询**（非实时，运维脚本调用）：

```sql
-- AMM 日成交统计
SELECT
    scenario,
    COUNT(*) AS trade_count,
    SUM(quantity) AS total_quantity,
    SUM(CASE WHEN buy_user_id = 'AMM_SYSTEM_001' THEN maker_fee + taker_fee ELSE 0 END
      + CASE WHEN sell_user_id = 'AMM_SYSTEM_001' THEN maker_fee + taker_fee ELSE 0 END
    ) AS total_fees_paid,
    SUM(COALESCE(
        CASE WHEN buy_user_id = 'AMM_SYSTEM_001' THEN buy_realized_pnl ELSE NULL END,
        CASE WHEN sell_user_id = 'AMM_SYSTEM_001' THEN sell_realized_pnl ELSE NULL END
    )) AS total_realized_pnl
FROM trades
WHERE (buy_user_id = 'AMM_SYSTEM_001' OR sell_user_id = 'AMM_SYSTEM_001')
  AND market_id = :market_id
  AND executed_at >= :start_of_day
GROUP BY scenario;
```

### 2.5 markets（话题元数据）

AMM 从 `markets` 表读取做市参数和生命周期状态。

| 字段 | AMM 用途 | 说明 |
|------|---------|------|
| status | 生命周期监听 | ACTIVE → 做市；SUSPENDED/HALTED → KILL SWITCH |
| min_price_cents / max_price_cents | 报价边界 | AMM 的挂单价格必须在 [1, 99] |
| maker_fee_bps / taker_fee_bps | 成本计算 | 纳入报价 spread 保护 |
| trading_end_at | 临近到期检测 | 触发 γ 时间因子调整 |
| resolution_date | 生命周期分档 | 计算 `market_lifecycle_days`，决定 γ tier |
| reserve_balance | 对账校验 | 与 AMM 的 Mint/Burn 操作一致性验证 |
| total_yes_shares / total_no_shares | 全市场份额 | AMM 占比计算（AMM 持仓 / 全市场份额） |

**AMM 读取时机**：启动时一次性读取，通过 Kafka `market_events` 监听后续变更。

### 2.6 ledger_entries（流水审计）

AMM 不实时读取 `ledger_entries`。流水用于事后审计和对账。

**AMM 相关的 entry_type**：

| entry_type | AMM 场景 | amount 方向 |
|------------|---------|------------|
| ORDER_FREEZE | AMM 挂单冻结 | 负（available → frozen） |
| ORDER_UNFREEZE | AMM 撤单解冻 | 正（frozen → available） |
| MINT_COST | AMM Mint 扣款 | 负 |
| MINT_RESERVE_IN | 系统侧 Mint 入账 | 正 |
| BURN_REVENUE | AMM Burn 收款 | 正 |
| BURN_RESERVE_OUT | 系统侧 Burn 出账 | 负 |
| TRANSFER_PAYMENT | AMM 作为买方付款 | 负 |
| TRANSFER_RECEIPT | AMM 作为卖方收款 | 正 |
| FEE | AMM 手续费扣除 | 负 |
> **v1.1 修正**：AMM 特权铸造/销毁的 `entry_type` 复用 DB v2.3 已有的 `'MINT_COST'` / `'BURN_REVENUE'`，
> **不新增** `'AMM_MINT'` / `'AMM_BURN'` 枚举值（否则违反 `ck_ledger_entry_type` CHECK 约束，导致 500 错误）。
> AMM 特权操作与普通用户成交产生的同名 entry_type 通过 `reference_type` 字段区分：
> 特权操作 `reference_type = 'AMM_MINT'` 或 `'AMM_BURN'`，普通成交 `reference_type = 'TRADE'`。

---

## 三、AMM 需要的 DB Schema 扩展

AMM 的引入需要对现有数据库做以下最小化扩展。

### 3.1 AMM 系统账户标识

> **⚠️ v1.3 UUID 兼容性修正**（对齐全局约定 v2.3 §2.1）:
>
> 原设计使用 `'AMM_SYSTEM_001'` 作为 AMM 用户 ID，但全局约定中 `users.id` 列类型为
> `UUID PRIMARY KEY DEFAULT gen_random_uuid()`，插入非 UUID 字符串会导致类型错误。
>
> **修正方案**: 使用固定 UUID 常量，并在全部文档和代码中统一引用。

```python
# amm/constants.py — AMM 系统账户唯一标识
AMM_USER_ID = "00000000-0000-4000-a000-000000000001"  # 固定 UUID v4 格式
AMM_USERNAME = "amm_market_maker"
```

> **为什么不用随机 UUID**: AMM 账户在所有环境（dev/staging/prod）中需要一致的 ID，
> 以便配置文件、监控告警、审计日志、风控白名单中硬编码引用。随机 UUID 会导致跨环境不一致。
>
> 以下 SQL 和文档中所有 `'AMM_SYSTEM_001'` 的位置，在实际代码中应替换为上述 UUID 常量。
> 文档中保留 `AMM_SYSTEM_001` 作为可读别名（human-readable alias），但 DB 层面
> 存储的是 UUID `00000000-0000-4000-a000-000000000001`。

### 3.2 users 表扩展

```sql
-- AMM 系统用户种子数据
-- 注意: users.id 类型为 UUID，必须使用合法 UUID 格式
INSERT INTO users (id, username, email, password_hash, is_active)
VALUES (
    '00000000-0000-4000-a000-000000000001',  -- 固定 UUID（AMM_SYSTEM_001 别名）
    'amm_market_maker',
    'amm@system.internal',
    '$2b$12$SYSTEM_NO_LOGIN',   -- 不可登录的占位 hash
    TRUE
);
```

### 3.2b accounts 表扩展

```sql
-- AMM 系统账户种子数据
-- accounts.user_id 为 VARCHAR(64)，存储 UUID 字符串
INSERT INTO accounts (user_id, available_balance, frozen_balance, version)
VALUES ('00000000-0000-4000-a000-000000000001', 0, 0, 0);
```

### 3.3 accounts 表新增字段

> **⚠️ v1.2 实现状态标注**:
> 以下 Schema 变更为 **AMM 上线前置必要条件（P0 Blocker）**，当前 MVP 代码尚未实现。
> 除 DB Schema 变更外，还需要修改撮合引擎清算层代码——当前 `execute_netting_if_needed`
> (pm_clearing/domain/netting.py) 对所有用户无差别执行 Netting，**不读取此标志**。
> 必须在该函数入口处增加 `auto_netting_enabled` 的读取与短路判断，否则此字段形同虚设。

```sql
-- 新增: Auto-Netting 开关 (AMM 必须关闭)
-- 🔴 [撮合引擎改动] 需同步修改 pm_clearing/domain/netting.py:
--    在 execute_netting_if_needed() 入口读取此字段，
--    若 auto_netting_enabled = FALSE 则 RETURN 0 跳过 Netting。
ALTER TABLE accounts
ADD COLUMN auto_netting_enabled BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN accounts.auto_netting_enabled IS
    '是否启用 Auto-Netting。普通用户默认 TRUE，AMM_SYSTEM_001 设置为 FALSE';

-- AMM 账户关闭 Auto-Netting
UPDATE accounts SET auto_netting_enabled = FALSE
WHERE user_id = '00000000-0000-4000-a000-000000000001';  -- AMM_SYSTEM_001
```

**撮合引擎侧所需改动（伪代码）**:
```python
# pm_clearing/domain/netting.py — 需新增的逻辑
async def execute_netting_if_needed(user_id, market_id, market, db):
    # ── P0: 读取 auto_netting_enabled 标志 ──
    account = await db.execute(
        "SELECT auto_netting_enabled FROM accounts WHERE user_id = :uid",
        {"uid": user_id}
    )
    if not account.scalar():  # auto_netting_enabled = FALSE
        return 0              # 跳过 Netting

    # ... 原有 Netting 逻辑不变 ...
```

### 3.4 trades 表约束放宽

> **⚠️ v1.2 实现状态标注**:
> 以下 Schema 变更为 **AMM 上线的 P1 改动**，当前 MVP 代码尚未实现。
> 除 DB 约束放宽外，还需要修改撮合引擎风控层——当前 `is_self_trade`
> (pm_risk/rules/self_trade.py) 只做 `incoming_user_id == resting_user_id` 的纯布尔判断，
> **不识别任何豁免权限**。即使放宽 DB 约束，撮合引擎仍会在匹配阶段跳过 AMM 的自交叉订单。
>
> **两种可选方案**:
> - **方案 A（推荐）**: 修改 `is_self_trade` 使其接受 `exempt_user_ids: set` 参数，AMM 账户跳过检查。
>   这是 defense-in-depth 最优解，允许原子改单的旧单被自身新单撮合。
> - **方案 B**: 评估 AMM 报价逻辑后认定自成交在实际运行中不会发生（原子改单保证旧单先撤后挂），
>   则移除 SELF_TRADE_EXEMPT 权限，保留现有风控不变。DB 约束也无需放宽。

现有 `ck_trades_diff_users` 约束要求 `buy_user_id != sell_user_id`，
AMM 若需自成交豁免（方案 A），需同步修改撮合引擎风控层和 DB 约束。

```sql
-- 方案 A (推荐): 修改约束为条件约束
-- 🔴 [撮合引擎改动] 需同步修改 pm_risk/rules/self_trade.py:
--    is_self_trade() 增加 exempt_user_ids 参数或查询 accounts 表权限列
ALTER TABLE trades DROP CONSTRAINT ck_trades_diff_users;
ALTER TABLE trades ADD CONSTRAINT ck_trades_diff_users CHECK (
    buy_user_id != sell_user_id
    OR buy_user_id = '00000000-0000-4000-a000-000000000001'  -- AMM 自成交豁免
);
```

**撮合引擎侧所需改动（方案 A 伪代码）**:
```python
# pm_risk/rules/self_trade.py — 需新增的逻辑
SELF_TRADE_EXEMPT_USERS: set[str] = {"AMM_SYSTEM_001"}  # 配置化

def is_self_trade(incoming_user_id: str, resting_user_id: str) -> bool:
    if incoming_user_id in SELF_TRADE_EXEMPT_USERS:
        return False  # 豁免账户跳过自成交检查
    return incoming_user_id == resting_user_id
```

### 3.5 ledger_entries — 无需 Schema 变更

> **v1.1 修正**：AMM 特权铸造/销毁复用现有的 `'MINT_COST'` / `'BURN_REVENUE'` entry_type，
> 通过 `reference_type = 'AMM_MINT'` / `'AMM_BURN'` 区分来源。
> 因此 `ck_ledger_entry_type` CHECK 约束 **无需修改**。

### 3.6 Alembic 迁移文件

```
alembic/versions/
├── ...（原有迁移）
├── 012_add_auto_netting_enabled.py      # accounts 新增字段
├── 013_amm_self_trade_constraint.py     # trades 约束放宽（方案 A 时才需要）
└── 014_seed_amm_system_account.py       # AMM 种子数据（使用固定 UUID）
```

---

## 四、AMM 专用 Redis 数据字典

### 4.1 amm:inventory:{market_id} — 库存实时缓存

**数据类型**: Hash
**生命周期**: AMM 启动时创建，AMM 停止时保留（重启时重建）
**写入方**: AMM 进程（成交事件回调，见下方数据源说明）
**读取方**: AMM 进程（每个报价周期）

> **⚠️ v1.2 数据源实现状态**:
>
> | 阶段 | 库存更新数据源 | 状态 |
> |------|--------------|------|
> | **MVP（当前）** | ① REST 回调：下单/改单/Mint/Burn 的 HTTP 响应中直接更新 Redis；② REST 轮询：定时调用 `GET /api/v1/trades?user_id=AMM_SYSTEM_001&since={last_id}` 发现被动成交；③ 定期对账：每 `reconciliation_interval_ms` 拉取 DB `positions` 全量校验 | 🟢 可实现 |
> | **Phase 2（理想）** | Kafka `trade_events` / `order_events` 事件驱动 + Redis Pub/Sub 实时推送 | 🔲 待实现 |
>
> MVP 阶段不存在 Kafka 基础设施（代码库无 Kafka 依赖），下方字段"来源"列中的 Kafka 引用
> 应理解为 **Phase 2 理想架构目标**，MVP 中由 REST 轮询 + 本地成交回调替代。

```
HSET amm:inventory:MKT-BTC-100K-2026
    yes_volume          1800        # YES 总持仓量
    no_volume           1500        # NO 总持仓量
    yes_available       1300        # YES 可用 = volume - pending_sell
    no_available        1200        # NO 可用
    yes_cost_sum_cents  90000       # YES 累计成本（美分）
    no_cost_sum_cents   75000       # NO 累计成本（美分）
    cash_cents          420000      # 可用现金（美分）
    updated_at_ms       1740652800000
```

**字段详细定义**（类型列为 Python/应用层类型；Redis Hash 存储层所有字段均为 binary string）：

| 字段 | 类型 | 范围 | 来源 (MVP → Phase 2) | 更新时机 | 说明 |
|------|------|------|------|---------|------|
| yes_volume | int | ≥ 0 | REST 轮询/回调 → Kafka trade_events | 每次 AMM 参与的 YES 成交 | 与 DB `positions.yes_volume` 对应 |
| no_volume | int | ≥ 0 | REST 轮询/回调 → Kafka trade_events | 每次 AMM 参与的 NO 成交 | 与 DB `positions.no_volume` 对应 |
| yes_available | int | ≥ 0 | 派生计算 | 每次成交 + 每次挂单/撤单 | `= yes_volume - yes_pending_sell` |
| no_available | int | ≥ 0 | 派生计算 | 同上 | `= no_volume - no_pending_sell` |
| yes_cost_sum_cents | long (BIGINT) | ≥ 0 | REST 轮询/回调 → Kafka trade_events | 每次 AMM 买入 YES 或卖出 YES | 加权平均成本法计算，对齐 DB BIGINT |
| no_cost_sum_cents | long (BIGINT) | ≥ 0 | REST 轮询/回调 → Kafka trade_events | 每次 AMM 买入 NO 或卖出 NO | 同上 |
| cash_cents | long (BIGINT) | ≥ 0 | REST 轮询/回调 + Mint/Burn API → Kafka + Mint/Burn API | 每次成交或铸造/销毁 | 与 DB `accounts.available_balance` (BIGINT) 对应 |
| updated_at_ms | long | > 0 | 系统时钟 | 每次写入 | 用于陈旧度检测 |

**cost_sum 更新规则**（与 DB v2.3 §2.6 positions 一致）：

```python
# 买入（开仓）: 增加成本
def on_buy(volume, price_cents, cost_sum):
    cost_sum += price_cents * volume
    return cost_sum

# 卖出（平仓）: 按比例释放成本
def on_sell(sell_volume, total_volume, cost_sum):
    released_cost = (cost_sum * sell_volume) // total_volume  # 整数除法
    cost_sum -= released_cost
    return cost_sum, released_cost
```

### 4.2 amm:orders:{market_id} — 活跃订单追踪

**数据类型**: Hash（以 order_id 为 field）
**生命周期**: 随订单创建/终结动态增删
**写入方**: AMM Connector（下单/撤单后）
**读取方**: AMM Strategy Engine（Replace 前查旧订单 ID）

```
HSET amm:orders:MKT-BTC-100K-2026
    "660e8400-..."  '{"side":"YES","direction":"SELL","price":54,"qty":100,"remaining":100,"book_type":"NATIVE_SELL","created_ms":1740652800000}'
    "770f9500-..."  '{"side":"YES","direction":"BUY","price":46,"qty":100,"remaining":100,"book_type":"NATIVE_BUY","created_ms":1740652800000}'
    "880a6600-..."  '{"side":"NO","direction":"SELL","price":42,"qty":80,"remaining":80,"book_type":"SYNTHETIC_BUY","created_ms":1740652800100}'
    "990b7700-..."  '{"side":"NO","direction":"BUY","price":58,"qty":80,"remaining":80,"book_type":"SYNTHETIC_SELL","created_ms":1740652800100}'
```

**Value JSON 字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| side | string | YES / NO（用户视角） |
| direction | string | BUY / SELL（用户视角） |
| price | int | 原始挂单价格（美分） |
| qty | int | 原始数量 |
| remaining | int | 剩余待成交数量 |
| book_type | string | NATIVE_BUY / NATIVE_SELL / SYNTHETIC_BUY / SYNTHETIC_SELL |
| created_ms | long | 下单时间戳 |

**生命周期事件**：

| 事件 | Redis 操作 | MVP 数据源 | Phase 2 数据源 |
|------|-----------|-----------|--------------|
| 下单成功（REST 201） | `HSET amm:orders:{market_id} {order_id} {json}` | REST 响应 | 同左 |
| 部分成交 | `HSET`，更新 `remaining` | REST 轮询 `GET /trades` | Kafka order_events |
| 全部成交 | `HDEL amm:orders:{market_id} {order_id}` | REST 轮询 `GET /trades` | Kafka order_events |
| 撤单成功 | `HDEL amm:orders:{market_id} {order_id}` | REST 响应 | Kafka order_events / REST |
| 批量撤单成功 | `DEL amm:orders:{market_id}` （整个 key 删除） | REST 响应 | 同左 |
| AMM 重启 | 调用 `GET /api/v1/orders?status=OPEN,PARTIALLY_FILLED` 全量重建 | REST | 同左 |

### 4.3 amm:state:{market_id} — 策略状态

**数据类型**: Hash
**写入方**: AMM Strategy Engine（每个报价周期更新）
**读取方**: 监控系统 (Prometheus exporter)、管理后台

```
HSET amm:state:MKT-BTC-100K-2026
    phase               "STABILIZATION"     # 当前策略阶段
    fair_price_cents    65                  # 当前公允价（美分）
    sigma_cents         3.2                 # 当前波动率（美分）
    inventory_skew      0.18                # 库存偏斜度
    last_requote_ms     1740652800000       # 上次报价时间
    daily_pnl_cents     -1500               # 日内 PnL（美分）
    total_fills_today   47                  # 日内成交笔数
    lvr_cooldown_until  0                   # LVR 冷却期结束时间
    kill_switch         "OFF"               # KILL SWITCH 状态
    defense_level       "NORMAL"            # 风控级别
```

**字段详细定义**：

| 字段 | 类型 | 取值 | 更新频率 | 说明 |
|------|------|------|---------|------|
| phase | string | EXPLORATION / STABILIZATION | 阶段转换时 | 探索—收敛两阶段 |
| fair_price_cents | int | [1, 99] | 每次报价 | 三层定价输出 |
| sigma_cents | float | [1.0, 15.0] | 每次报价 | APV-BVD 波动率估计值 |
| inventory_skew | float | [-1.0, 1.0] | 每次报价 | (YES - NO) / (YES + NO) |
| last_requote_ms | long | > 0 | 每次报价 | 用于心跳监控 |
| daily_pnl_cents | int | 任意 | 每次成交 | 零点重置 |
| total_fills_today | int | ≥ 0 | 每次成交 | 零点重置 |
| lvr_cooldown_until | long | ≥ 0 | LVR 触发时 | 0 表示无冷却 |
| kill_switch | string | OFF / ON | 风控触发/恢复时 | ON 时停止所有报价 |
| defense_level | string | NORMAL / WIDEN / ONE_SIDE / KILL_SWITCH | 风控评估时 | DefenseStack 输出 |

### 4.4 amm:config:{market_id} — 运行时配置

**数据类型**: Hash
**写入方**: 管理后台 / 启动脚本
**读取方**: AMM 进程（启动时加载 + 配置热更新）

```
HSET amm:config:MKT-BTC-100K-2026
    gamma_short         2.5
    gamma_mid           1.5
    gamma_long          0.8
    time_smoothing_kappa 24.0
    base_spread_cents   2
    max_spread_cents    15
    budget_limit_cents  500000
    position_limit      5000
    max_loss_daily_cents 50000
    lvr_velocity_threshold 0.10
    lvr_cooldown_ms     5000
    oracle_stale_ms     30000
    oracle_deviation_cents 5
    oracle_gap_cents    5
```

> 配置热更新：AMM 进程每 30 秒检查一次 Redis 中的配置版本号。
> 如果版本号变化，重新加载配置并在下一个报价周期生效。

---

## 五、AMM 内存运行时数据结构

以下数据结构在 AMM 进程内存中维护，进程重启后从 Redis + DB 重建。

### 5.1 StrategyState — 策略引擎状态

```python
@dataclass
class StrategyState:
    """策略引擎主状态（内存，每个 market_id 一个实例）"""

    market_id: str                  # 做市话题 ID
    phase: str                      # "EXPLORATION" | "STABILIZATION"

    # 三层定价输出
    fair_price_cents: int           # 综合公允价 [1, 99]
    layer1_oracle_price: int        # Layer 1: 外部锚定价
    layer2_micro_price: int         # Layer 2: 微观订单簿价格
    layer3_post_fill_price: int     # Layer 3: 后验成交价

    # A-S 模型参数
    sigma_cents: float              # 波动率（美分）
    gamma: float                    # 风险厌恶系数（根据生命周期分档）
    tau: float                      # 时间因子 τ(h) = h/(h+κ)
    reservation_price_cents: float  # 保留价格 r = s - q·γ·σ²·τ

    # 库存信息（从 Redis 读取的本地副本）
    yes_volume: int
    no_volume: int
    yes_available: int
    no_available: int
    cash_cents: int
    inventory_skew: float           # (yes - no) / (yes + no)

    # 报价输出
    ask_price_cents: int            # 卖出价
    bid_price_cents: int            # 买入价
    ask_quantity: int               # 卖出数量
    bid_quantity: int               # 买入数量

    # 风控状态
    defense_level: str              # NORMAL / WIDEN / ONE_SIDE / KILL_SWITCH
    daily_pnl_cents: int            # 日内累计 PnL
    total_budget_used_cents: int    # 累计使用的预算

    # 时间
    last_requote_ms: int            # 上次报价时间
    market_lifecycle_days: int      # 话题生命周期天数
    hours_remaining: float          # 距结束剩余小时数
```

### 5.2 LocalOrderBook — 本地订单簿副本

```python
@dataclass
class LocalOrderBook:
    """WebSocket 推送维护的本地订单簿副本"""

    market_id: str
    last_sequence_id: int           # 最新 sequence_id，用于 gap 检测

    # YES 视角订单簿（价格 → 总量）
    bids: dict[int, int]            # {price_cents: total_quantity}
    asks: dict[int, int]            # {price_cents: total_quantity}

    # 派生指标（每次更新后重算）
    best_bid: int | None            # 最高买价
    best_ask: int | None            # 最低卖价
    mid_price: float | None         # (best_bid + best_ask) / 2
    spread: int | None              # best_ask - best_bid
    bid_depth_3: int                # 最优 3 档买盘总量
    ask_depth_3: int                # 最优 3 档卖盘总量

    def apply_snapshot(self, msg: dict): ...
    def apply_delta(self, msg: dict): ...
    def get_vwap(self, side: str, depth: int) -> float: ...
```

### 5.3 ProcessedEventSet — 幂等去重集

```python
class ProcessedEventSet:
    """成交事件幂等去重（内存 LRU）

    MVP: 防护 REST 轮询可能返回已处理的成交记录
    Phase 2: 防护 Kafka at-least-once 语义下的重复投递
    """

    def __init__(self, max_size: int = 100_000):
        self._set: OrderedDict[str, None] = OrderedDict()
        self._max_size = max_size

    def contains(self, event_id: str) -> bool: ...
    def add(self, event_id: str): ...
    def _evict_oldest(self): ...
```

> **注意**：去重集仅在单进程运行时有效。
> - **MVP**: AMM 通过 REST 轮询发现被动成交，使用 `trade_id` 去重，防止同一笔成交被多次轮询处理。
> - **Phase 2**: Kafka consumer group 的 offset 管理确保不重复消费已 commit 事件，
>   去重集作为 at-least-once 语义的额外防护层。

---

## 六、状态机定义

### 6.1 AMM 生命周期状态机

```
                          ┌───────────────────────────┐
                          │      管理员发起启动         │
                          └──────────┬────────────────┘
                                     ▼
                            ┌──────────────┐
                            │   STARTING   │
                            │  (初始化中)   │
                            └──────┬───────┘
                                   │ 初始化完成
                                   ▼
                            ┌──────────────┐     管理员手动停止
              ┌─────────────│   RUNNING    │─────────────────┐
              │             │  (运行中)     │                 │
              │             └──────┬───────┘                 │
              │                    │                          │
    KILL SWITCH 触发               │ 市场 RESOLVED            │
              │                    │                          │
              ▼                    ▼                          ▼
     ┌────────────────┐  ┌────────────────┐        ┌──────────────┐
     │   EMERGENCY    │  │  WINDING_DOWN  │        │   STOPPED    │
     │  (紧急停止)     │  │  (清仓收尾)    │        │  (已停止)     │
     └───────┬────────┘  └───────┬────────┘        └──────────────┘
             │                   │                         ▲
             │ 人工恢复           │ 清仓完成                 │
             ├───────────────────┴─────────────────────────┘
             │
             ▼
     ┌──────────────┐
     │   RUNNING    │  (恢复运行)
     └──────────────┘
```

**状态定义**：

| 状态 | 说明 | 进入条件 | 退出条件 |
|------|------|---------|---------|
| STARTING | 初始化阶段：加载配置、从 DB 同步库存到 Redis、订阅 WS（MVP）/ Kafka+WS（Phase 2） | 管理员 POST /admin/amm/start | 初始化完成或失败 |
| RUNNING | 正常做市：报价、成交、风控循环 | 初始化成功 或 人工恢复 | 见下方三个退出路径 |
| EMERGENCY | 紧急停止：已撤销所有挂单，暂停报价，保留库存 | DefenseStack 触发 KILL_SWITCH | 人工恢复或手动停止 |
| WINDING_DOWN | 清仓收尾：停止新建挂单，等待现有挂单成交/撤销，执行 Burn | 市场 RESOLVED | 清仓完成 |
| STOPPED | 终态：AMM 完全停止，释放所有资源 | 管理员停止 / 清仓完成 / 初始化失败 | — |

### 6.2 策略阶段状态机（Phase）

```
          ┌──────────────────┐
          │   EXPLORATION    │
          │     (探索期)      │
          └────────┬─────────┘
                   │
          ┌────────┴──────────────────┐
          │ 触发条件 (满足任一):        │
          │  • 时间到期                │
          │  • 成交量达标              │
          │  • 价格稳定               │
          └────────┬──────────────────┘
                   ▼
         ┌───────────────────┐
         │  STABILIZATION    │
         │    (收敛期)        │
         └────────┬──────────┘
                  │
         ┌────────┴──────────────────┐
         │ 紧急回退条件 (满足任一):     │
         │  • 5 分钟波动率 > 10%      │
         │  • 日内亏损 > 预算 50%     │
         │  （有 10 分钟冷却期）       │
         └────────┬──────────────────┘
                  │
                  ▼
          ┌──────────────────┐
          │   EXPLORATION    │ (回退，重新探索)
          └──────────────────┘
```

**阶段转换记录字段**：

| 字段 | 说明 |
|------|------|
| from_phase | 原阶段 |
| to_phase | 目标阶段 |
| reason | TIME_EXPIRED / VOLUME_REACHED / PRICE_STABLE / EMERGENCY_ROLLBACK |
| timestamp_ms | 转换时间 |

### 6.3 风控级别状态机（DefenseStack）

```
    ┌──────────┐
    │  NORMAL  │ ─── 日亏损 > 50% 预算 ──────────────────┐
    │          │ ─── 库存偏斜 > 80% ──┐                   │
    └────┬─────┘                      │                   │
         │                            ▼                   │
         │                    ┌──────────────┐            │
         │                    │    WIDEN     │            │
         │                    │  (加宽价差)   │            │
         │                    └──────┬───────┘            │
         │                           │                    │
         │              库存偏斜 > 90% (单边)               │
         │                           │                    │
         │                           ▼                    │
         │                    ┌──────────────┐            │
         │                    │  ONE_SIDE    │            │
         │                    │ (单边报价     │            │
         │                    │ + 减价出货)   │            │
         │                    └──────┬───────┘            │
         │                           │                    │
         │        库存偏斜 > 95% 或达到 Kill 条件            │
         │                           │                    │
         │                           ▼                    ▼
         │                    ┌─────────────────┐
         │                    │  KILL_SWITCH    │
         │                    │  (全部撤单,停止)  │
         │                    └─────────────────┘
         │                           │
         │          人工确认恢复        │
         │◄──────────────────────────┘
```

**级别行为对照**：

| 级别 | 价差调整 | 报价方向 | 特殊行为 |
|------|---------|---------|---------|
| NORMAL | 基础价差 | 双边 | 无 |
| WIDEN | 基础价差 × 1.5~3 | 双边 | spread 随偏斜度线性放大 |
| ONE_SIDE | 基础价差 × 2~4 | 仅轻仓方向 | distress_discount 线性增加 0~5 cents |
| KILL_SWITCH | — | 停止报价 | 撤销所有挂单，触发告警 |

### 6.4 订单状态机（AMM 视角）

AMM 管理的订单遵循以下状态转换：

```
         REST 下单成功
              │
              ▼
         ┌─────────┐
         │  ACTIVE  │ ──── Kafka: PARTIALLY_FILLED ────┐
         │ (活跃)   │                                   │
         └────┬─────┘                                   │
              │                                         ▼
              │                                ┌──────────────────┐
              │                                │ PARTIALLY_ACTIVE │
              │                                │  (部分成交，      │
              │                                │   剩余仍在簿)    │
              │                                └───────┬──────────┘
              │                                        │
              ├──── AMM Replace 成功 ─────► REPLACED   │
              │     (旧订单被原子替换)       (已替换)    │
              │                                        │
              ├──── AMM Cancel 成功 ────► CANCELLED    │
              │     (主动撤单)             (已撤销)      │
              │                                        │
              ├──── Kafka: FILLED ──────► FILLED       │
              │     (全部成交)             (已成交)      ├──── 同左
              │                                        │
              └──── Kafka: CANCELLED ──► CANCELLED     │
                    (被动撤销，如市场暂停)  (已撤销)      │
                                                       │
```

> **注意**: AMM 的 Redis `amm:orders` 中只保留 ACTIVE 和 PARTIALLY_ACTIVE 状态的订单。
> 终态（FILLED / CANCELLED / REPLACED）触发 HDEL 删除。

### 6.5 市场生命周期与 AMM 反应

基于 `markets.status` 的状态机（继承自 DB v2.3 §2.3）：

| 市场状态 | AMM 反应 | 说明 |
|----------|---------|------|
| ACTIVE | RUNNING（正常做市） | — |
| SUSPENDED | EMERGENCY（撤单 + 暂停） | 等待市场恢复 ACTIVE |
| HALTED | EMERGENCY（撤单 + 暂停） | 需人工介入 |
| RESOLVED | WINDING_DOWN（清仓收尾） | 停止做市，执行 Burn 回收现金 |
| SETTLED | STOPPED | 结算完成，AMM 退出 |
| VOIDED | STOPPED（退款） | 话题作废，AMM 退出 |

---

## 七、数据流与同步规则

### 7.1 正常运行时数据流

```
                   REST API                    Kafka
    AMM ──────────────────► 撮合引擎 ──────────────────► AMM
    │                                                     │
    │ 1. POST /amm/orders/replace                         │
    │    (挂单/改单)                                       │
    │                                                     │
    │ 2. REST Response                                    │
    │    ← new_order + trades[]                           │
    │    → 更新 Redis amm:orders                          │
    │    → trades[] 仅日志，不更新库存                      │
    │                                                     │
    │                                    3. trade_events   │
    │                                    ← 权威成交数据     │
    │                                    → 更新 Redis      │
    │                                      amm:inventory   │
    │                                                     │
    │ 4. 下一轮报价周期                                     │
    │    ← 读取 Redis amm:inventory                        │
    │    ← 读取 Redis amm:orders                           │
    │    → 计算新报价                                       │
    │    → 回到步骤 1                                       │
```

### 7.2 启动时数据同步流程

```
Step 1: 读取 DB（真相源）
    GET /api/v1/account/balance          → cash_cents
    GET /api/v1/positions/{market_id}    → yes/no_volume, cost_sum
    GET /api/v1/orders?status=OPEN,PARTIALLY_FILLED → 活跃订单列表
    GET /api/v1/markets/{market_id}      → 市场元数据

Step 2: 写入 Redis（工作内存）
    HSET amm:inventory:{market_id} ...   → 库存缓存初始化
    HSET amm:orders:{market_id} ...      → 活跃订单重建
    HSET amm:state:{market_id} ...       → 策略状态初始化

Step 3: 订阅事件流
    Kafka consumer.subscribe(['trade_events', 'order_events', 'market_events'])
    WebSocket connect /ws/v1/orderbook/{market_id}
    WebSocket connect /ws/v1/trades/{market_id}

Step 4: 开始报价
    等待首个 WebSocket SNAPSHOT → 构建本地订单簿
    → 进入正常报价循环
```

### 7.3 定期对账规则

AMM 每 **5 分钟** 执行一次 Redis ↔ DB 全量对账：

```python
async def periodic_reconciliation(market_id: str):
    """
    Redis ↔ DB 对账
    - 如果差异在容忍范围内（≤ 2 份 或 ≤ 200 cents），记录 WARNING 但不修正
    - 如果差异超过容忍范围，强制从 DB 重建 Redis，触发 ALERT
    """
    # 1. 从 DB 读取真相
    db_position = await api.get_positions(market_id)
    db_balance = await api.get_balance()

    # 2. 从 Redis 读取当前缓存
    redis_inv = await redis.hgetall(f"amm:inventory:{market_id}")

    # 3. 比对
    diffs = {
        'yes_volume': abs(db_position.yes_volume - int(redis_inv['yes_volume'])),
        'no_volume': abs(db_position.no_volume - int(redis_inv['no_volume'])),
        'yes_cost_sum': abs(db_position.yes_cost_sum - int(redis_inv['yes_cost_sum_cents'])),
        'no_cost_sum': abs(db_position.no_cost_sum - int(redis_inv['no_cost_sum_cents'])),
        'cash': abs(db_balance.available - int(redis_inv['cash_cents'])),
    }

    # 4. 判定
    volume_tolerance = 2        # 份
    cash_tolerance = 200        # 美分
    cost_tolerance = 500        # 美分

    has_critical_diff = (
        diffs['yes_volume'] > volume_tolerance
        or diffs['no_volume'] > volume_tolerance
        or diffs['cash'] > cash_tolerance
        or diffs['yes_cost_sum'] > cost_tolerance
        or diffs['no_cost_sum'] > cost_tolerance
    )

    if has_critical_diff:
        alert.fire("INVENTORY_DRIFT_CRITICAL", diffs=diffs)
        await force_rebuild_redis_from_db(market_id)
    elif any(v > 0 for v in diffs.values()):
        log.warning(f"Minor inventory drift detected: {diffs}")
```

**对账容忍度设计理由**：
- 由于 Kafka 消费有延迟，Redis 可能暂时落后 DB 1~2 笔成交
- 2 份 / 200 美分的容忍度覆盖了 1~2 笔最大单量（99 cents × 2 = 198 cents）
- 超出该范围说明数据通道可能出现异常（如 Kafka 消息丢失）

---

## 八、AMM 专属不变量与对账规则

### 8.1 AMM 库存不变量

**不变量 A1**: AMM 的 YES 和 NO 持仓之和应趋向平衡（允许偏斜）

```
abs(yes_volume - no_volume) / max(yes_volume + no_volume, 1) <= 0.95
```

当偏斜度超过 0.95 时，DefenseStack 应已触发 KILL_SWITCH。

**不变量 A2**: AMM 可用持仓不得为负

```
yes_available >= 0 AND no_available >= 0
```

如果违反，说明挂单冻结量超过了总持仓，存在超卖风险。

**不变量 A3**: AMM 的总资产（现金 + 持仓价值）应不低于预设底线

```
cash_cents + yes_volume × fair_price + no_volume × (100 - fair_price) >= min_asset_floor
```

跌破底线时触发 KILL_SWITCH。

### 8.2 AMM 与平台不变量的关系

AMM 作为 `AMM_SYSTEM_001` 用户，参与平台的全部 5 条不变量（DB v2.3 §5.1~§5.5）：

| 平台不变量 | AMM 的角色 | AMM 特殊影响 |
|-----------|-----------|-------------|
| 份数平衡（YES = NO） | AMM 的 Mint/Burn 成对操作维护平衡 | AMM 自成交可能同时在 YES/NO 开仓，仍保持平衡 |
| 托管平衡（reserve = shares × 100） | AMM 的 Mint 增加 reserve，Burn 减少 | AMM 特权 Mint/Burn 必须通过标准 ledger 路径 |
| 成本守恒 | AMM 的 cost_sum 参与左侧求和 | AMM 关闭 Netting 后不会被意外 Netting 打破 |
| 全局零和 | AMM 的 available + frozen 参与求和 | AMM 的资金完全在平台体系内闭环 |
| Reserve 一致性 | AMM 的 Mint/Burn 影响 SYSTEM_RESERVE | AMM 特权操作必须对齐系统侧流水 |

### 8.3 AMM 日终对账 SQL

```sql
-- AMM 日终 PnL 对账
WITH amm_trades AS (
    SELECT
        t.*,
        CASE
            WHEN t.buy_user_id = 'AMM_SYSTEM_001' THEN 'BUYER'
            WHEN t.sell_user_id = 'AMM_SYSTEM_001' THEN 'SELLER'
        END AS amm_role
    FROM trades t
    WHERE (t.buy_user_id = 'AMM_SYSTEM_001' OR t.sell_user_id = 'AMM_SYSTEM_001')
      AND t.market_id = :market_id
      AND t.executed_at >= :start_of_day
),
amm_flows AS (
    SELECT
        entry_type,
        SUM(amount) AS total_amount
    FROM ledger_entries
    WHERE user_id = 'AMM_SYSTEM_001'
      AND created_at >= :start_of_day
    GROUP BY entry_type
)
SELECT
    -- 成交统计
    (SELECT COUNT(*) FROM amm_trades) AS total_trades,
    (SELECT SUM(quantity) FROM amm_trades) AS total_volume,

    -- 场景分布
    (SELECT COUNT(*) FROM amm_trades WHERE scenario = 'MINT') AS mint_count,
    (SELECT COUNT(*) FROM amm_trades WHERE scenario = 'TRANSFER_YES') AS transfer_yes_count,
    (SELECT COUNT(*) FROM amm_trades WHERE scenario = 'TRANSFER_NO') AS transfer_no_count,
    (SELECT COUNT(*) FROM amm_trades WHERE scenario = 'BURN') AS burn_count,

    -- 已实现 PnL
    (SELECT SUM(COALESCE(
        CASE WHEN amm_role = 'BUYER' THEN buy_realized_pnl ELSE NULL END,
        CASE WHEN amm_role = 'SELLER' THEN sell_realized_pnl ELSE NULL END
    )) FROM amm_trades) AS realized_pnl_cents,

    -- 手续费支出
    (SELECT SUM(
        CASE WHEN amm_role = 'BUYER' THEN
            CASE WHEN buy_order_id = maker_order_id THEN maker_fee ELSE taker_fee END
        ELSE
            CASE WHEN sell_order_id = maker_order_id THEN maker_fee ELSE taker_fee END
        END
    ) FROM amm_trades) AS total_fees_cents,

    -- 流水交叉验证
    (SELECT COALESCE(SUM(total_amount), 0) FROM amm_flows) AS net_ledger_flow;
```

### 8.4 AMM Mint/Burn 一致性校验

AMM 的每次 Mint/Burn 操作后，可立即验证：

```sql
-- Mint 后校验: AMM 持仓增量 = 铸造数量
SELECT
    p.yes_volume - :prev_yes_volume = :mint_quantity AS yes_delta_ok,
    p.no_volume - :prev_no_volume = :mint_quantity AS no_delta_ok,
    m.reserve_balance - :prev_reserve = :mint_quantity * 100 AS reserve_delta_ok,
    m.total_yes_shares - :prev_yes_shares = :mint_quantity AS shares_delta_ok
FROM positions p, markets m
WHERE p.user_id = 'AMM_SYSTEM_001'
  AND p.market_id = :market_id
  AND m.id = :market_id;
```

---

## 九、枚举值完整定义

### 9.1 AMM 专用枚举

```python
from enum import Enum

class AMMLifecycleState(str, Enum):
    """AMM 生命周期状态"""
    STARTING = "STARTING"           # 初始化中
    RUNNING = "RUNNING"             # 正常做市
    EMERGENCY = "EMERGENCY"         # 紧急停止
    WINDING_DOWN = "WINDING_DOWN"   # 清仓收尾
    STOPPED = "STOPPED"             # 已停止

class AMMPhase(str, Enum):
    """策略阶段（探索—收敛）"""
    EXPLORATION = "EXPLORATION"       # 探索期: 宽价差，小单量
    STABILIZATION = "STABILIZATION"   # 收敛期: 窄价差，大深度

class PhaseTransitionReason(str, Enum):
    """阶段转换原因"""
    TIME_EXPIRED = "TIME_EXPIRED"         # 探索期时间到
    VOLUME_REACHED = "VOLUME_REACHED"     # 成交量达标
    PRICE_STABLE = "PRICE_STABLE"         # 价格稳定
    EMERGENCY_ROLLBACK = "EMERGENCY_ROLLBACK"  # 紧急回退

class DefenseLevel(str, Enum):
    """风控级别"""
    NORMAL = "NORMAL"                 # 正常: 基础价差
    WIDEN = "WIDEN"                   # 加宽: 价差放大
    ONE_SIDE = "ONE_SIDE"             # 单边: 仅轻仓方向报价 + 减价出货
    KILL_SWITCH = "KILL_SWITCH"       # 终止: 全部撤单停止

class KillSwitchState(str, Enum):
    """KILL SWITCH 状态"""
    OFF = "OFF"
    ON = "ON"

class AMMOrderState(str, Enum):
    """AMM 视角的订单状态（与 DB OrderStatus 的映射）"""
    ACTIVE = "ACTIVE"                       # → DB: OPEN
    PARTIALLY_ACTIVE = "PARTIALLY_ACTIVE"   # → DB: PARTIALLY_FILLED
    FILLED = "FILLED"                       # → DB: FILLED
    CANCELLED = "CANCELLED"                 # → DB: CANCELLED
    REPLACED = "REPLACED"                   # → DB: CANCELLED (通过 Replace)
```

### 9.2 AMM 复用的平台枚举

AMM 直接复用 DB v2.3 §4.1 中的以下枚举，不重新定义：

| 枚举类 | AMM 使用的值 | 说明 |
|--------|------------|------|
| MarketStatus | ACTIVE, SUSPENDED, HALTED, RESOLVED, SETTLED, VOIDED | 监听市场状态变更 |
| BookType | 全部 4 种 | AMM 同时作为 NATIVE_BUY/SELL 和 SYNTHETIC_BUY/SELL |
| TradeScenario | 全部 4 种 | AMM 参与所有场景 |
| FrozenAssetType | 全部 3 种 | AMM 冻结资金和 YES/NO 持仓 |
| OrderStatus | OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED | AMM 关心的状态子集 |
| OriginalSide | YES, NO | AMM 在两边同时挂单 |
| OrderDirection | BUY, SELL | — |
| LedgerEntryType | 全部（AMM 特权操作复用 MINT_COST/BURN_REVENUE，通过 reference_type 区分） | AMM 触发的流水类型 |

### 9.3 AMM Kafka 事件枚举

| 枚举 | 值 | 来源 Topic |
|------|---|-----------|
| TradeEventType | TRADE_EXECUTED | trade_events |
| OrderEventType | ORDER_STATUS_CHANGED | order_events |
| MarketEventType | MARKET_STATUS_CHANGED | market_events |

---

## 十、附录

### 附录 A: Redis Key 命名汇总

| Key 模式 | 类型 | 说明 | TTL |
|----------|------|------|-----|
| `amm:inventory:{market_id}` | Hash | 库存实时缓存 | 无（持久） |
| `amm:orders:{market_id}` | Hash | 活跃订单追踪 | 无（持久） |
| `amm:state:{market_id}` | Hash | 策略状态 | 无（持久） |
| `amm:config:{market_id}` | Hash | 运行时配置 | 无（持久） |
| `amm:processed:{market_id}` | Set | 幂等去重（备用，主要用内存） | 24h |
| `ratelimit:AMM_SYSTEM_001:replace` | String | Replace 限流计数器 | 60s |

### 附录 B: DB 扩展迁移检查清单

| # | 迁移内容 | 影响表 | 向后兼容 | 说明 |
|---|---------|-------|---------|------|
| 1 | 新增 `auto_netting_enabled` 列 | accounts | 是（DEFAULT TRUE） | 新列有默认值，不影响现有数据 |
| 2 | 放宽自成交约束 | trades | 是（约束放宽） | 不影响已有数据 |
| 3 | 插入 AMM 种子数据 | users, accounts | 是（新增行） | 需确保 user_id 不冲突 |

### 附录 C: 数据结构大小估算

| 数据结构 | 预估大小 | 说明 |
|----------|---------|------|
| Redis amm:inventory | ~200 bytes/market | 8 个字段 |
| Redis amm:orders | ~200 bytes/order × 4~20 orders | AMM 通常维护 4~20 个活跃订单 |
| Redis amm:state | ~300 bytes/market | 10 个字段 |
| Redis amm:config | ~400 bytes/market | 14 个配置参数 |
| 内存 StrategyState | ~500 bytes/market | Python dataclass |
| 内存 LocalOrderBook | ~10 KB/market | 99 价格档位 × 2 方向 |
| 内存 ProcessedEventSet | ~10 MB max | 100K event_id × ~100 bytes |

---

## 变更记录

| 版本 | 日期 | 变更内容 |
|------|------|---------|
| v1.0 | 2026-02-27 | 初稿：10 章完整数据字典与状态机规范 |
| v1.1 | 2026-02-28 | 🔴 entry_type 对齐 DB v2.3：AMM 特权 Mint/Burn 复用 `MINT_COST`/`BURN_REVENUE`，通过 `reference_type` 区分（§2.6、§3.5） |
| | | 🟠 补充 Mint/Burn 绕过 Kafka 的库存同步例外规则（§1 关键原则） |
| | | 移除不再需要的 §3.5 ledger CHECK 扩展迁移（改为无需变更说明） |
| | | 修正 Redis cost_sum_cents 类型为 long (BIGINT)（§4.1） |
| v1.2 | 2026-02-28 | **文档与代码对齐审计（5 项跨文档反馈修正）**: |
| | | 🔴 §3.3 Auto-Netting：标注 MVP 未实现 + 撮合引擎 `execute_netting_if_needed` 需增加标志读取（P0 Blocker） |
| | | 🔴 §3.4 Self-Trade：标注 MVP 未实现 + `is_self_trade` 无豁免机制，给出方案 A/B 选择 + 伪代码（P1） |
| | | 🟠 §4.1 库存数据源：Kafka 引用全部标注为 Phase 2 目标，MVP 使用 REST 轮询/回调 + 定期对账 |
| | | 🟠 §4.2 订单生命周期事件：增加 MVP / Phase 2 双列数据源对照 |
| | | 🟡 §5.3 ProcessedEventSet：注释和说明从 Kafka 幂等改为通用事件幂等（MVP 用 trade_id 去重） |
| v1.3 | 2026-02-28 | **UUID 兼容性 + 迁移检查清单修正**: |
| | | 🔴 §3.1-3.2 AMM user_id 从 `'AMM_SYSTEM_001'` 改为固定 UUID `00000000-0000-4000-a000-000000000001`——对齐全局约定 `users.id UUID` 类型约束 |
| | | 🟠 §3.3/§3.4 SQL 中所有 `AMM_SYSTEM_001` 替换为 UUID 常量 |
| | | 🟡 新增 §3.1 AMM 标识常量定义（`amm/constants.py`），文档保留 `AMM_SYSTEM_001` 作为可读别名 |

---

*文档版本: v1.3 | 生成日期: 2026-02-28 | 状态: 草稿（待 Review）*
*对齐: 全局约定与数据库设计 v2.3 + AMM 模块设计 v7.1 + AMM 接口与事件流契约 v1.4*
