# pm_order / pm_risk / pm_matching / pm_clearing — 设计文档

> **版本**: v1.0
> **日期**: 2026-02-21
> **范围**: 下单全链路（订单转换 → 风控 → 撮合 → 清算 → Netting → 市场结算）
> **对齐来源**: 01_全局约定与数据库设计.md (v2.3)、02_API接口契约.md (v1.2)、03_撮合引擎与清算流程设计.md (v1.2)、04_WAL预写日志与故障恢复设计.md (v1.1)

---

## 一、实现范围

| 模块 | 包含 | 排除 |
|------|------|------|
| pm_order | REST API 4 端点、订单 DB 持久化、转换层 | — |
| pm_risk | 风控规则链、冻结/解冻 | — |
| pm_matching | MatchingEngine（per-market lock）、内存 OrderBook、撮合算法 | — |
| pm_clearing | 4 种清算场景、Auto-Netting、WAL 写入、settle_market | void_market、SYSTEM_RESERVE/PLATFORM_FEE 后台聚合任务 |

---

## 二、架构决策：MatchingEngine 作为核心协调者

```
pm_order/api  →  pm_order/application (薄)
                       ↓
            pm_matching/MatchingEngine
            （持有 per-market asyncio.Lock + _orderbooks）
                ↓           ↓             ↓
           pm_risk       pm_order      pm_clearing
         (规则+冻结)    (OrderRepo)    (4场景+Netting
                                       +settle+WAL)
```

**理由**：与 doc 03 §2 伪代码完全一致；per-market lock 与 OrderBook 字典是内聚的有状态资产，天然属于 pm_matching；pm_risk / pm_clearing 是无状态纯函数模块。

---

## 三、目录结构

```
src/
├── pm_order/
│   ├── domain/
│   │   ├── models.py           # Order dataclass（original_* + book_* 双记录）
│   │   ├── transformer.py      # transform_order()
│   │   └── repository.py       # OrderRepositoryProtocol
│   ├── infrastructure/
│   │   ├── db_models.py        # SQLAlchemy ORM（orders 表）
│   │   └── persistence.py      # OrderRepository 实现
│   ├── application/
│   │   ├── schemas.py          # PlaceOrderRequest / CancelOrderResponse / OrderResponse 等
│   │   └── service.py          # 薄包装 → 调用 MatchingEngine
│   └── api/
│       └── router.py           # POST /orders, POST /orders/{id}/cancel
│                               # GET /orders, GET /orders/{id}
│
├── pm_risk/
│   ├── domain/
│   │   ├── rules.py            # RiskRule Protocol
│   │   └── service.py          # RiskDomainService（规则链）
│   └── rules/
│       ├── price_range.py      # check_price_range()：[1, 99]
│       ├── market_status.py    # check_market_active()
│       ├── order_limit.py      # check_order_limit()：单笔数量上限
│       ├── balance_check.py    # check_and_freeze()（含 max_taker_fee buffer）
│       └── self_trade.py       # is_self_trade() 谓词（由 matching_algo 调用）
│
├── pm_matching/
│   ├── domain/
│   │   └── models.py           # BookOrder dataclass, TradeResult dataclass
│   ├── engine/
│   │   ├── order_book.py       # OrderBook（array[100] deque + best_bid/ask 游标）
│   │   ├── scenario.py         # TradeScenario enum + determine_scenario()
│   │   ├── matching_algo.py    # _match_buy_order / _match_sell_order
│   │   └── engine.py           # MatchingEngine（协调全链路）
│   └── application/
│       └── service.py          # 单例暴露，供 main.py 注入
│
└── pm_clearing/
    ├── domain/
    │   ├── fee.py              # calculate_fees(), get_fee_trade_value()
    │   ├── service.py          # settle_trade() 分发器
    │   ├── netting.py          # execute_netting_if_needed()
    │   ├── settlement.py       # settle_market()
    │   ├── invariants.py       # verify_invariants_after_trade()
    │   └── scenarios/
    │       ├── mint.py         # clear_mint()
    │       ├── transfer_yes.py # clear_transfer_yes()
    │       ├── transfer_no.py  # clear_transfer_no()
    │       └── burn.py         # clear_burn()
    ├── infrastructure/
    │   └── ledger.py           # write_ledger(), write_wal_event()
    └── application/
        └── service.py          # ClearingApplicationService（供注入）
```

---

## 四、数据流

### 4.1 POST /orders（下单主链路）

```
HTTP Request → pm_order/api/router.py（验证 PlaceOrderRequest）
    ↓
pm_order/application/service.py → MatchingEngine.place_order(cmd, db)

┌──────────────────────────────────────────────────────────────────┐
│  async with _market_locks[market_id]:                            │
│    async with db.begin():                                        │
│                                                                  │
│  Step 1  pm_risk: check_market_active(market_id, db)            │
│  Step 2  pm_risk: check_price_range(price_cents)   [1,99]       │
│  Step 3  pm_risk: check_order_limit(quantity)                    │
│  Step 4  pm_order/domain/transformer.py                          │
│           transform_order(side, direction, price)                │
│           → book_type, book_direction, book_price                │
│  Step 5  pm_risk/rules/balance_check.py                          │
│           check_and_freeze(order, db)  [原子 SQL]                │
│           FUNDS:      freeze = original_price × qty              │
│                               + max_taker_fee(TAKER_FEE_BPS=20) │
│           YES/NO_SHARES: freeze = qty（pending_sell）            │
│  Step 6  OrderRepository.save(order, db)                         │
│  Step 7  write_wal_event(ORDER_ACCEPTED, order, db)              │
│                                                                  │
│  Step 8  matching_algo.match(order, orderbook)                   │
│           for each fill:                                         │
│    ├─ if resting.user_id == incoming.user_id: continue  [skip]  │
│    ├─ determine_scenario(buy_book_type, sell_book_type)          │
│    ├─ settle_trade(trade, market, db)                            │
│    │     → clear_mint / transfer_yes / transfer_no / burn        │
│    │     → collect_fees（from frozen or available）              │
│    │     → refund_price_improvement_and_fee_surplus              │
│    │     → write_clearing_ledger_entries                         │
│    │     → INSERT INTO trades                                    │
│    ├─ _sync_frozen_amount(order, remaining_qty)  [覆盖赋值]      │
│    ├─ execute_netting_if_needed(user_id, market_id, db)          │
│    └─ write_wal_event(ORDER_MATCHED, trade, db)                  │
│                                                                  │
│  Step 9  finalize_order(order, orderbook, db)                    │
│    GTC & remainder > 0 → 插入 orderbook                         │
│                        → write_wal_event(ORDER_PARTIALLY_FILLED) │
│    IOC & remainder > 0 → 解冻剩余, status=CANCELLED              │
│                        → write_wal_event(ORDER_EXPIRED)          │
│    IOC & fills=0 & self_trade_skipped > 0 → raise Error(4003)   │
│                                                                  │
│  Step 10 verify_invariants_after_trade(market_id, db)            │
│  Step 11 UPDATE markets SET ...                                   │
│  ── db.commit() ──                                               │
└──────────────────────────────────────────────────────────────────┘
    ↓
HTTP 201：{ order, trades, netting_result }
```

### 4.2 POST /orders/{id}/cancel（取消订单）

```
MatchingEngine.cancel_order(order_id, user_id, db)
  async with _market_locks[market_id]:
    async with db.begin():
      1. 获取订单，校验 user_id 归属（403）
      2. 校验 status in {OPEN, PARTIALLY_FILLED}（422 / 4006）
      3. orderbook._order_index.pop(order_id)（从内存移除）
      4. 更新 best_bid / best_ask 游标（如需）
      5. 解冻：
           FUNDS      → accounts.available += frozen_amount
                        accounts.frozen    -= frozen_amount
           YES_SHARES → positions.yes_pending_sell -= remaining_qty
           NO_SHARES  → positions.no_pending_sell  -= remaining_qty
      6. order.status = CANCELLED，DB UPDATE
      7. write_wal_event(ORDER_CANCELLED, order, db)
    commit
  → HTTP 200：{ order_id, unfrozen_amount, unfrozen_asset_type }
```

---

## 五、关键域模型

### pm_order/domain/models.py

```python
@dataclass
class Order:
    id: str
    client_order_id: str
    market_id: str
    user_id: str
    # 用户原始意图
    original_side: str           # YES / NO
    original_direction: str      # BUY / SELL
    original_price: int          # 1–99
    # 订单簿视角（转换后）
    book_type: str               # NATIVE_BUY / NATIVE_SELL / SYNTHETIC_BUY / SYNTHETIC_SELL
    book_direction: str          # BUY / SELL
    book_price: int              # 1–99
    # 数量状态
    quantity: int                # 原始数量（DB: quantity）
    filled_quantity: int = 0
    remaining_quantity: int = 0
    # 冻结
    frozen_amount: int = 0
    frozen_asset_type: str = ""  # FUNDS / YES_SHARES / NO_SHARES
    # 控制
    time_in_force: str = "GTC"   # GTC / IOC
    status: str = "OPEN"         # OPEN / PARTIALLY_FILLED / FILLED / CANCELLED
    created_at: datetime | None = None
    updated_at: datetime | None = None
```

### pm_matching/domain/models.py

```python
@dataclass
class BookOrder:
    """内存订单簿条目（精简，仅匹配所需）"""
    order_id: str
    user_id: str
    book_type: str      # 用于 determine_scenario + self-trade 检测
    quantity: int       # 剩余可撮合量
    created_at: datetime

@dataclass
class TradeResult:
    """单次撮合填单结果，传递给清算层"""
    buy_order_id: str
    sell_order_id: str
    buy_user_id: str
    sell_user_id: str
    market_id: str
    scenario: TradeScenario
    price: int              # YES 成交价（book_price，maker 价）
    quantity: int
    buy_book_type: str
    sell_book_type: str
    buy_original_price: int # Synthetic 手续费计算用（NO 原价）
    maker_order_id: str     # 挂单方
    taker_order_id: str     # incoming 方
```

### pm_matching/engine/order_book.py

```python
@dataclass
class OrderBook:
    market_id: str
    # index 0 废弃；index 1–99 对应 YES 价格
    bids: list[deque] = field(default_factory=lambda: [deque() for _ in range(100)])
    asks: list[deque] = field(default_factory=lambda: [deque() for _ in range(100)])
    best_bid: int = 0    # 0 = 无买单
    best_ask: int = 100  # 100 = 无卖单
    _order_index: dict[str, BookOrder] = field(default_factory=dict)  # O(1) cancel
```

### 冻结量规则（balance_check.py）

```
TAKER_FEE_BPS = 20  # 0.2%，可配置
max_taker_fee(v) = (v × TAKER_FEE_BPS + 9999) // 10000  # 向上取整

NATIVE_BUY    → FUNDS,      freeze = original_price × qty + max_taker_fee(...)
SYNTHETIC_SELL→ FUNDS,      freeze = original_price × qty + max_taker_fee(...)
               （original_price = NO 价格，用户实际出资）
NATIVE_SELL   → YES_SHARES, freeze = qty（yes_pending_sell += qty）
SYNTHETIC_BUY → NO_SHARES,  freeze = qty（no_pending_sell  += qty）
```

---

## 六、API 契约

### 错误码

| HTTP | 错误码 | 说明 |
|------|--------|------|
| 400 | 4001 | price_cents 超出 [1, 99] |
| 400 | 4002 | 单笔数量超上限 |
| 400 | 4003 | 自成交检测（IOC 且 filled_qty=0 且有自成交跳过）|
| 404 | 4004 | market 不存在或非 ACTIVE / 订单不存在 |
| 409 | 4005 | 幂等冲突（相同 client_order_id + 不同 payload）|
| 422 | 4006 | 订单不可取消（FILLED/CANCELLED）|
| 402 | 5001 | 资金或持仓不足（freeze 失败）|
| 403 | —    | 非归属者（cancel / GET /{id}）|

### 幂等逻辑

```
查询 orders WHERE client_order_id = ? AND user_id = ?
  找到 → 比较 (side, direction, price_cents, quantity) 是否完全一致
    一致   → HTTP 200，返回已存订单（不重新撮合）
    不一致 → HTTP 409 / 4005
  未找到 → 正常执行下单流程
```

### GET /orders 游标

```
cursor = base64({"id": "<last_order_id>"})
查询：id < :cursor_id  ORDER BY id DESC  LIMIT :limit
（order.id 为 ULID，时序有序，单字段游标足够）
```

---

## 七、测试策略（TDD 执行顺序）

| # | 测试文件 | 覆盖内容 | 关键场景数 |
|---|---------|---------|-----------|
| 1 | `test_order_transformer.py` | 4 种转换 + 边界值 price=1/99 | 8 |
| 2 | `test_risk_rules.py` | 每条规则 pass/reject + 冻结正确性 | 12+ |
| 3 | `test_order_book.py` | 挂单/取消/best_bid/ask 游标 | 10 |
| 4 | `test_scenario.py` | determine_scenario 4 种组合 | 4 |
| 5 | `test_matching_engine.py` | 主计划 16 个撮合场景 | 16+ |
| 6 | `test_fee_calculation.py` | NATIVE vs SYNTHETIC + ceiling 取整 | 8 |
| 7 | `test_scenario_clearing.py` | 4 种清算：余额/持仓/reserve/pnl_pool | 12+ |
| 8 | `test_auto_netting.py` | Netting + pnl_pool + pending_sell 排除 | 6 |
| 9 | `test_invariants.py` | INV-1/2/3/7（正常 + 违反触发 HALT）| 8 |
| 10 | `test_settle_market.py` | YES/NO 结算 + reserve == payout | 4 |
| 11 | `test_order_api.py` (integration) | 完整链路：下单→撮合→清算→一致性 | 8+ |

---

## 八、偏差记录

| # | 偏差点 | 原始设计文档 | 本次实现 | 理由 |
|---|--------|------------|---------|------|
| D-01 | cancel_order HTTP 方法 | 主计划 §4.7：`DELETE /orders/{id}` | `POST /orders/{id}/cancel` | API 契约文档 02 §5.2 明确为 POST，契约文档优先级更高 |
| D-02 | `engine/market_router.py` | 主计划 §6.4：单独文件 | 嵌入 `engine/engine.py` 作为 `_orderbooks` 属性 | 详细设计文档 §2 伪代码将 `_locks` 和 `_orderbooks` 作为 MatchingEngine 类属性；单独文件增加无必要的间接层 |
| D-03 | 4003 自成交错误码 | API 契约文档暗示 pre-match 拒绝；匹配文档 §5.4 为 skip 策略 | skip 策略 + 4003 仅用于 IOC 且 filled_qty=0 且有自成交跳过 | skip 策略性能更优，且符合主流预测市场实践；保留 4003 为后续更严格规则的扩展入口 |
| D-04 | WAL write_wal_event 归属 | doc 03 未明确；doc 04 在 MatchingEngine 上下文中描述 WAL | `write_wal_event()` 函数放在 `pm_clearing/infrastructure/ledger.py`，由 MatchingEngine 显式调用 | pm_clearing 已有 DB infrastructure 层；pm_matching 无 infrastructure 目录（stub 结构无此层）；函数放 ledger.py 复用现有基础设施 |

---

*设计文档结束*
