# Module 5 全链路代码审查

> **目的**：验证 pm_risk / pm_matching / pm_clearing / pm_order 四个模块的代码逻辑正确性，并与设计文档保持一致。
>
> **断点续传**：每章完成后在对应状态栏打 ✅/❌，重启后从第一个未完成章节继续。
>
> **设计文档**：
> - `Planning/Detail_Design/01_全局约定与数据库设计.md` — DB schema, enum 定义
> - `Planning/Detail_Design/02_API接口契约.md` — REST API 契约
> - `Planning/Detail_Design/03_撮合引擎与清算流程设计.md` — 撮合/清算核心逻辑
> - `Planning/Detail_Design/04_WAL预写日志与故障恢复设计.md` — WAL 结构

---

## 审查章节总览

| 章节 | 模块 | 状态 | 主要问题数 |
|------|------|------|-----------|
| A | pm_risk (5个规则) | ✅ FIXED | 1 medium fixed |
| B | pm_matching (OrderBook + Algo + Scenario) | ✅ PASS | 1 minor |
| C | pm_clearing Part 1 (fee + ledger + WAL + MINT + TRANSFER_YES) | ✅ FIXED | 1 critical fixed |
| D | pm_clearing Part 2 (BURN + netting + invariants + settlement) | ✅ PASS | 0 |
| E | pm_order (domain + transformer + repo + service + API) | ✅ FIXED | 1 critical + 4 schema gaps fixed |
| F | 跨模块一致性 (错误码 + 事务边界 + enum 对齐) | ✅ PASS | 0 |

---

## 已知问题（审查前预修复）

这两个 Bug 已在审查前修复（commit `903c658`）：

| Bug | 文件 | 描述 |
|-----|------|------|
| `_INSERT_ORDER_SQL` 缺少 `price_type` 列名 | `src/pm_order/infrastructure/persistence.py` | VALUES 有 'LIMIT' 但列名没有 price_type |
| 事务从不提交 | `src/pm_order/application/service.py` | `async with db.begin()` 创建 SAVEPOINT 而非真实事务，缺少显式 commit |

---

## Chapter A：pm_risk 审查

**文件范围**：
- `src/pm_risk/rules/price_range.py`
- `src/pm_risk/rules/order_limit.py`
- `src/pm_risk/rules/self_trade.py`
- `src/pm_risk/rules/market_status.py`
- `src/pm_risk/rules/balance_check.py`

**设计参考**：
- `03_撮合引擎与清算流程设计.md` §1（风控规则）
- `02_API接口契约.md` §5（下单入参验证）
- `src/pm_common/errors.py`（错误码对照）

**检查项**：
- [x] 价格范围：1-99 是否正确 — `check_price_range`: `1 <= price <= 99` ✅
- [x] 数量限制：MAX_ORDER_QUANTITY 是否与设计一致 — **已修复**：原为 `100_000`，设计文档要求 `10_000`，已修正 ✅
- [x] 市场状态：错误码 3001/3002 是否正确 — `MarketNotFoundError(3001)`, `MarketNotActiveError(3002)` ✅
- [x] 余额冻结：BUY 冻结公式 `original_price * qty + ceil_fee` 是否正确 — `trade_value + _calc_max_fee(trade_value)` ✅
- [x] 仓位冻结：卖单 YES/NO shares 冻结逻辑是否正确 — NATIVE_SELL 冻结 `yes_pending_sell`；SYNTHETIC_BUY 冻结 `no_pending_sell` ✅
- [x] TAKER_FEE_BPS=20：天花板除法 `(value * 20 + 9999) // 10000` ✅

**状态**：✅ FIXED（1 个 medium 问题已修复）

**审查结果**：

**Medium（已修复 commit `bec3307`）**：`MAX_ORDER_QUANTITY = 100_000`，但设计文档 `02_API接口契约.md` §5 line 505 明确写 `"max_order_quantity": 10000`，差了 10 倍。已修正为 `10_000`。

其余 4 个规则逻辑全部正确，错误码与 errors.py 完全一致。SYNTHETIC_SELL（Buy NO）与 NATIVE_BUY 走同一冻结分支（冻结 FUNDS），逻辑正确。

---

## Chapter B：pm_matching 审查

**文件范围**：
- `src/pm_matching/domain/models.py` (BookOrder, TradeResult)
- `src/pm_matching/engine/order_book.py` (OrderBook)
- `src/pm_matching/engine/scenario.py` (determine_scenario)
- `src/pm_matching/engine/matching_algo.py` (match_order)

**设计参考**：
- `03_撮合引擎与清算流程设计.md` §2（单边订单簿设计）、§3（撮合算法）

**检查项**：
- [x] OrderBook：100 个价格槽，bids/asks 的 add/cancel 逻辑 ✅
- [x] 最优价刷新：best_bid/best_ask 更新逻辑 ✅ (`_refresh_best_bid` 从 99 向下扫，`_refresh_best_ask` 从 1 向上扫)
- [x] 4 种 scenario 判断矩阵是否与设计一致 ✅
- [x] 撮合算法：BUY 匹配 asks（`best_ask <= book_price`），SELL 匹配 bids（`best_bid >= book_price`）✅
- [x] 自我交易跳过：`deque.rotate(-1)` + `checked < total` 计数器 ✅
- [x] TradeResult 字段完整性 ✅（buy_order_id, sell_order_id, price, qty, buy_user_id, sell_user_id, buy_book_type, sell_book_type, buy_original_price, maker_order_id, taker_order_id）

**状态**：✅ PASS（1 个 minor 发现，非阻塞）

**审查结果**：

**Minor（非阻塞）**：`_make_trade_sell_incoming` 中 `buy_original_price=0`。当 sell 是 incoming（taker），resting BUY 的 `original_price` 未存入 `BookOrder`，导致 `buy_original_price=0`。这在 TRANSFER_NO/BURN 场景下会影响未来的费用计算（`fee.py: get_fee_trade_value(SYNTHETIC_SELL)` 使用此字段）。当前 MVP 不执行 per-trade 费用扣除，暂不阻塞。

**注：Chapter B 子 agent 额外报告了 3 个"严重问题"，经人工交叉验证均为误报**：
- "TradeResult 缺少 `scenario` 字段" → 误报：`clearing/service.py:20` 直接调用 `determine_scenario()`，不依赖 TradeResult.scenario
- "`_sync_frozen_amount` 完全缺失" → 误报：该函数在 `engine.py:292` 实现，`engine.py:153` 调用（agent 只读了 matching_algo.py）
- "`BookOrder` 缺少多个字段" → 误报：当前调用方（engine.py rebuild_orderbook）只用到现有 5 个字段，无功能缺陷

---

## Chapter C：pm_clearing Part 1 审查

**文件范围**：
- `src/pm_clearing/domain/fee.py`
- `src/pm_clearing/infrastructure/ledger.py`
- `src/pm_clearing/domain/scenarios/mint.py`
- `src/pm_clearing/domain/scenarios/transfer_yes.py`

**设计参考**：
- `03_撮合引擎与清算流程设计.md` §4（清算流程）、§5（费用计算）
- `04_WAL预写日志与故障恢复设计.md`（WAL 结构）

**检查项**：
- [x] fee.py：get_fee_trade_value 各 book_type 分支逻辑 ✅
- [x] fee.py：calc_fee 天花板除法 ✅
- [x] fee.py：calc_released_cost 比例释放公式 ✅（`qty >= volume` 时返回全部，防止 dust）
- [x] ledger.py：write_ledger SQL 参数名与 ledger_entries 表列名一致 ✅（`description` 可选，nullable）
- [x] ledger.py：write_wal_event SQL 参数名与 wal_events 表列名一致 ✅（**已修复**）
- [x] MINT：reserve += qty*100, yes_shares += qty, no_shares += qty ✅
- [x] MINT：buyer 得 YES（credit yes_volume += qty），seller 得 NO（credit no_volume += qty）✅
- [x] TRANSFER_YES：reserve 不变，卖方 yes_volume 减少，买方 yes_volume 增加 ✅

**状态**：✅ FIXED

**审查结果**：

**CRITICAL（已修复 commit 8ed7584）**：`write_wal_event` SQL 试图插入 `order_id`、`user_id`、`id` 三个不存在于 `wal_events` 表的列。`wal_events` 表实际列为 `(id BIGSERIAL, market_id, event_type, payload, created_at)`，`order_id/user_id` 存储于 payload JSONB（有 GIN 索引）。修复：改为 `INSERT INTO wal_events (market_id, event_type, payload)`，把 `order_id/user_id` 合并进 payload JSON。

**MVP TODO（不阻塞）**：fee.py 函数存在但未被撮合/清算链路调用。冻结额度正确覆盖了最大手续费，但实际手续费从未作为平台收入单独记账。`ledger_entries` 里的 MINT_COST、FEE 等类型目前未被写入。

---

## Chapter D：pm_clearing Part 2 审查

**文件范围**：
- `src/pm_clearing/domain/scenarios/transfer_no.py`
- `src/pm_clearing/domain/scenarios/burn.py`
- `src/pm_clearing/domain/service.py`
- `src/pm_clearing/domain/netting.py`
- `src/pm_clearing/domain/invariants.py`
- `src/pm_clearing/domain/settlement.py`

**设计参考**：
- `03_撮合引擎与清算流程设计.md` §4、§6（净额结算）、§7（不变量）、§9（市场结算）

**检查项**：
- [x] TRANSFER_NO：mirror of TRANSFER_YES，操作 NO 仓位 ✅
- [x] BURN：reserve -= qty*100，yes/no shares 各减少，两侧卖家各拿 proceeds ✅
- [x] service.py：scenario dispatch 与 determine_scenario 一致 ✅
- [x] netting.py：`min(available_yes, available_no) * 100` 退款公式 ✅
- [x] netting.py：position FOR UPDATE 防并发 ✅
- [x] invariants.py：INV-1/2/3 三个断言 SQL 正确 ✅（INV-3 用 DB 聚合 cost_sum）
- [x] settlement.py：settle_market 各阶段逻辑 ✅

**状态**：✅ PASS — 无问题

**审查结果**：

transfer_no.py 是 transfer_yes.py 的 NO 镜像，逻辑正确（`no_trade_price = 100 - trade.price` 转换）。burn.py 正确销毁 YES+NO 对并释放 reserve。netting 使用 `FOR UPDATE` 锁行防并发。三个不变量检查逻辑正确。

---

## Chapter E：pm_order 审查

**文件范围**：
- `src/pm_order/domain/models.py`
- `src/pm_order/domain/transformer.py`
- `src/pm_order/domain/repository.py`
- `src/pm_order/infrastructure/persistence.py`
- `src/pm_order/application/schemas.py`
- `src/pm_order/application/service.py`
- `src/pm_order/api/router.py`

**设计参考**：
- `02_API接口契约.md` §5（POST /orders）、§6（GET /orders）
- `03_撮合引擎与清算流程设计.md` §1（下单入口流程）

**检查项**：
- [x] transformer.py：4 种转换矩阵 ✅
- [x] persistence.py：INSERT/UPDATE/SELECT SQL 列名与 orders 表 schema 一致 ✅
- [x] schemas.py：PlaceOrderRequest 字段与 API 契约一致 ✅
- [x] service.py：幂等性检查逻辑（4005 错误码）✅
- [x] service.py：Order 对象构造完整 ✅
- [x] router.py：HTTP method、status code、路由路径与设计文档一致 ✅

**状态**：✅ FIXED（5 个问题全部已修复）

**审查结果**：

**CRITICAL（已修复 commit `8ed7584`）**：`orders.id` 列是 UUID 类型，但 `generate_id()` 生成的是 snowflake 整数字符串，不是合法 UUID 格式。修复：迁移 `010_alter_orders_id_to_varchar.py` 将 `orders.id` 改为 `VARCHAR(64)`。

**MAJOR × 4（已修复 commit `843d732`）**，均由 Chapter E 子 agent 发现并经 API 契约 §5 交叉验证确认：
1. `OrderResponse` 字段命名错误：`side/direction/price_cents` → `original_side/original_direction/original_price_cents`；同时补充缺失的 `book_type`、`price_type`、`frozen_amount`、`frozen_asset_type`、`created_at`、`cancel_reason` 等字段
2. `CancelOrderResponse` 缺少 `status`（"CANCELLED"）和 `remaining_quantity_cancelled` 字段（契约 §5.2 明确要求）
3. `OrderListResponse` 字段名 `orders` → `items`；补充 `has_more` 字段（契约 §5.3 格式）
4. GET /orders 缺少 `side`、`direction` 过滤参数（契约 §5.3 查询参数表）；已贯通 router → service → persistence SQL → repository protocol

`remaining_quantity` 是 `field(init=False)` 在 `__post_init__` 中由 `quantity - filled_quantity` 计算，row mapper 不传此字段，正确 ✅

**注：Chapter E 子 agent 另报告 `status='NEW' vs 'OPEN'` 为 CRITICAL** — 经分析为误报，两者均是 DB 约束合法值，代码显式设 'OPEN' 直接进入开放状态，语义正确。

---

## Chapter F：跨模块一致性

**检查项**：
- [x] 所有错误码与 `src/pm_common/errors.py` 中的定义一致 ✅
- [x] 所有 enum 值与 `src/pm_common/enums.py` 中的定义一致 ✅（BookType、TradeScenario、OrderStatus 均使用 StrEnum）
- [x] MatchingEngine 链路中的函数签名调用顺序 ✅（risk check → transform → freeze → save → WAL → market lock → match → clear → netting → finalize → invariant → flush market）
- [x] WAL event_type 字符串与设计文档约定一致 ✅（ORDER_ACCEPTED/MATCHED/PARTIALLY_FILLED/CANCELLED/EXPIRED 均在 migration constraint 中）

**状态**：✅ PASS

**审查结果**：

所有错误码与 errors.py 一致。WAL event_type 字符串与 migration 009 的 CHECK constraint 完全匹配。事务边界正确：engine 用 `async with db.begin()`（在 autobegin session 中创建 SAVEPOINT，出错自动回滚），service 层显式 `commit()`（与 pm_account 模式一致）。

**Style Note**：pm_order 分页 cursor 使用原始 snowflake ID 字符串，而 pm_account 使用 base64-encoded JSON。两者功能都正确，但风格不一致，可在 Phase 2 统一。

---

## 最终结论

**审查完成**。共发现并修复 **2 个 Critical + 1 个 Medium + 4 个 Major Bug**：

| Bug | 严重级别 | 文件 | 修复 commit |
|-----|----------|------|------------|
| `write_wal_event` SQL 插入不存在的列（`order_id`, `user_id`, `id`） | Critical | `src/pm_clearing/infrastructure/ledger.py` | `8ed7584` |
| `orders.id` 为 UUID 类型但代码使用 snowflake 字符串 ID | Critical | `alembic/versions/010_alter_orders_id_to_varchar.py` | `8ed7584` |
| `MAX_ORDER_QUANTITY = 100_000` 应为 `10_000`（设计文档 §5） | Medium | `src/pm_risk/rules/order_limit.py` | `bec3307` |
| `OrderResponse` 字段命名错误 + 缺少 7 个字段（§5.1） | Major | `src/pm_order/application/schemas.py` | `843d732` |
| `CancelOrderResponse` 缺少 `status` 和 `remaining_quantity_cancelled`（§5.2） | Major | `src/pm_order/application/schemas.py` | `843d732` |
| `OrderListResponse` 字段名 `orders`→`items`；缺少 `has_more`（§5.3） | Major | `src/pm_order/application/schemas.py` | `843d732` |
| GET /orders 缺少 `side`/`direction` 过滤参数（§5.3） | Major | `router.py`, `service.py`, `persistence.py`, `repository.py` | `843d732` |

**预先修复的 2 个 Bug**（commit `903c658`）：

| Bug | 文件 |
|-----|------|
| `_INSERT_ORDER_SQL` 缺少 `price_type` 列名 | `src/pm_order/infrastructure/persistence.py` |
| 事务从不提交（`async with db.begin()` 创建 SAVEPOINT 而非真实事务） | `src/pm_order/application/service.py` |

**非阻塞 MVP TODO**：

1. **Fee collection 未实现**：`fee.py` 函数存在但未被调用。冻结额度已覆盖最大手续费，但手续费不作为平台收入记账，`ledger_entries` 里 FEE/MINT_COST 等条目从未写入。资金不会丢失（多冻结的手续费留在 frozen_balance），但平台无收入。
2. **`buy_original_price=0` 当 SELL 是 taker**：resting BUY 的 original_price 未存于 BookOrder，影响未来 TRANSFER_NO 场景的 buy 侧手续费计算。
3. **`netting_result` 字段命名不符**：代码返回 `{"netting_qty": N, "refund_amount": N}` 但 API 契约 §5.1 要求 `{"pairs_netted": N, "released_cents": N, "released_display": "$N.NN"}`。netting 不常触发，暂不阻塞核心流程测试。
4. **`TradeResponse` 仍是内部视角**：当前返回 `buy_order_id`/`sell_order_id`（内部 ID）。设计文档要求用户视角（`counterparty_side`、`role`、`fee_cents`、`realized_pnl_cents`、`executed_at`）。需要 TradeResult 扩展 + 交易记录持久化，留 Phase 2。
5. **`OrderStatus.NEW` 是死代码**：DB 默认 `'NEW'`，但代码始终显式设 `status="OPEN"`，`'NEW'` 状态永远不被创建。枚举里保留了 `NEW` 但系统不使用。
6. **分页 cursor 风格不一致**：pm_order 用 raw ID 字符串，pm_account 用 base64 JSON。功能正确但 API 风格不统一。

**结论：代码可以进入 DB 集成测试阶段。** 需先运行 `alembic upgrade head` 应用迁移 010，再跑集成测试。
