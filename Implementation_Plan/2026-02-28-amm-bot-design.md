# AMM 机器人本体 — 设计文档

> **版本**: v1.0
> **日期**: 2026-02-28
> **范围**: AMM 机器人作为独立 Sidecar Service 的完整实现
> **前置依赖**: Phase A（`2026-02-28-amm-prerequisites-plan.md`）全部完成
> **对齐来源**: AMM 模块设计 v7.1（主算法）、接口契约 v1.4、数据字典 v1.3、配置手册 v1.3
> **实施计划**: `2026-02-28-amm-bot-plan.md`

---

## 一、实现范围

AMM 机器人是一个独立进程，通过 REST API 与撮合引擎交互。
本阶段实现 v7.1 设计文档中的三层模块架构：Connector Layer、Strategy Layer、Risk Middleware Layer。

| 模块 | 包含 | 排除 |
|------|------|------|
| Connector Layer | REST API 客户端、订单管理器、库存同步（REST 轮询）、Token 管理 | Kafka 消费者（Phase 2）、WebSocket 订阅（Phase 2） |
| Strategy Layer | 三层定价引擎、A-S 报价引擎、梯度计算、阶段管理器 | CPMM 参考模块（降级为辅助，暂不实现） |
| Risk Middleware | 三道防线、预算管理器、Kill Switch | LVR 防御高级模式（Phase 2） |
| Infrastructure | 配置管理（YAML + Redis 热更新）、Redis 缓存、日志、健康检查 | Prometheus 指标导出（Phase 2） |
| Lifecycle | 启动初始化、优雅停机、定期对账 | 管理员 Web 控制台 |

**排除项（后续阶段）**：
- Kafka 事件驱动库存同步（Phase 2，MVP 用 REST 轮询）
- WebSocket 订单簿实时推送（Phase 2）
- 回测与模拟框架（v7.1 §13，独立实施）
- 管理员控制面板前端

---

## 二、架构决策

### 2.1 独立服务 vs 嵌入撮合引擎

**选择**：独立 Python 进程（Sidecar Service），与撮合引擎进程分离。

**理由**：
- AMM 设计哲学的核心：AMM 是"超级机器人用户"，不是撮合引擎组件
- 独立部署、独立升级、独立回滚
- AMM 崩溃不影响撮合引擎正常运行
- 可按需水平扩展（每个市场一个 AMM 实例或单实例多市场）

### 2.2 单进程多市场 vs 多进程单市场

**选择**：单进程 + asyncio 多市场并发。每个市场一个独立的 `MarketContext`。

**理由**：MVP 阶段市场数量有限（< 50），asyncio 足够处理。
市场间通过 `asyncio.Lock` 隔离，无需多进程复杂度。

### 2.3 库存数据源策略（MVP）

**选择**：REST 轮询 + 定期全量对账。

```
┌──────────┐     REST 轮询 (2s)    ┌──────────────┐
│  AMM Bot │ ◄──────────────────── │ 撮合引擎 API  │
│          │                       │              │
│ Redis    │     全量对账 (5min)    │ PostgreSQL   │
│ (cache)  │ ◄──────────────────── │ (truth)      │
└──────────┘                       └──────────────┘
```

**轮询策略**：
- `GET /api/v1/trades?user_id=AMM&cursor={last}&limit=50` — 每 2 秒
- `GET /api/v1/account/balance` — 每 30 秒
- `GET /api/v1/positions/{market_id}` — 每 5 分钟（全量对账）

### 2.4 配置管理

**三层配置**（对齐配置手册 v1.3）：
1. **YAML 文件**：启动时加载，包含所有 87+ 参数默认值
2. **Redis 热更新**：`amm:config:{market_id}` Hash，管理员实时调参
3. **内存运行时**：`MarketConfig` dataclass，策略引擎直接读取

优先级：Redis > YAML > 代码默认值

### 2.5 报价循环架构

```
┌─────────────────── Quote Cycle (每 1-2 秒) ───────────────────┐
│                                                                │
│  Step 1: Sync (Connector)                                     │
│    └─ poll_trades() → 更新 Redis 库存                          │
│                                                                │
│  Step 2: Strategy (纯计算，无 I/O)                             │
│    ├─ three_layer_pricing() → mid_price                        │
│    ├─ as_engine.reservation_price() → r (保留价格)             │
│    ├─ as_engine.optimal_spread() → δ (最优点差)                │
│    ├─ gradient_engine.build_ladder()                           │
│    │   → ask_ladder (SELL YES), bid_ladder (SELL NO, mapped)  │
│    └─ phase_manager.check_transition() → 阶段状态              │
│                                                                │
│  Step 3: Risk (中间件拦截)                                     │
│    ├─ budget_check() → 预算是否超限                             │
│    ├─ inventory_check() → 库存是否偏斜                          │
│    ├─ defense_stack.evaluate() → NORMAL/WIDEN/ONE_SIDE/KILL    │
│    └─ sanitize_orders() → 修正/拒绝非法订单                    │
│                                                                │
│  Step 4: Execute (Connector)                                   │
│    ├─ diff_orders() → 计算当前挂单 vs 目标挂单的差异            │
│    ├─ replace_order() / place_order() / cancel_order()         │
│    └─ update Redis amm:orders 缓存                             │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

---

## 三、目录结构

```
src/amm/
├── __init__.py
├── main.py                          # AMM 服务入口: asyncio.run(amm_main())
├── config/
│   ├── __init__.py
│   ├── loader.py                    # YAML + Redis 配置加载器
│   ├── models.py                    # GlobalConfig, MarketConfig dataclass
│   └── default.yaml                 # 87+ 参数默认值
│
├── connector/
│   ├── __init__.py
│   ├── api_client.py                # REST API 封装 (httpx.AsyncClient)
│   ├── auth.py                      # JWT Token 管理 (auto-refresh)
│   ├── order_manager.py             # 订单生命周期: place/cancel/replace + 本地缓存
│   ├── inventory_sync.py            # REST 轮询 → Redis 库存更新
│   └── trade_poller.py              # poll_trades() 增量同步
│
├── strategy/
│   ├── __init__.py
│   ├── pricing/
│   │   ├── __init__.py
│   │   ├── three_layer.py           # ThreeLayerPricing: 外部锚定 + 簿内微观 + 后验学习
│   │   ├── anchor.py                # 外部锚定价 (初始概率, admin 注入)
│   │   ├── micro.py                 # 簿内微观价 (mid-price, VWAP, 反 Spoofing)
│   │   └── posterior.py             # 后验学习价 (贝叶斯更新)
│   ├── as_engine.py                 # Avellaneda-Stoikov: reservation_price + optimal_spread
│   ├── gradient.py                  # 梯度引擎: build_ask_ladder / build_bid_ladder
│   ├── phase_manager.py             # 阶段管理: EXPLORATION ↔ STABILIZATION
│   └── models.py                    # MarketState, OrderIntent, QuoteResult
│
├── risk/
│   ├── __init__.py
│   ├── defense_stack.py             # 三道防线: NORMAL → WIDEN → ONE_SIDE → KILL_SWITCH
│   ├── budget_manager.py            # 预算管理: daily_pnl, per_market_pnl
│   ├── inventory_guard.py           # 库存阈值检查
│   └── sanitizer.py                 # 订单合规性修正 (价格边界, 数量上限)
│
├── lifecycle/
│   ├── __init__.py
│   ├── initializer.py               # 启动初始化: DB 对账 → Redis 重建 → 首次报价
│   ├── reconciler.py                # 定期对账: Redis vs DB (每 5 分钟)
│   ├── shutdown.py                  # 优雅停机: batch_cancel → 等待清算 → 退出
│   └── health.py                    # 健康检查端点 (FastAPI mini app)
│
├── cache/
│   ├── __init__.py
│   ├── redis_client.py              # Redis 连接管理 (redis.asyncio)
│   ├── inventory_cache.py           # amm:inventory:{market_id} Hash CRUD
│   └── order_cache.py               # amm:orders:{market_id} Hash CRUD
│
├── models/
│   ├── __init__.py
│   ├── inventory.py                 # Inventory dataclass (yes_volume, no_volume, cash, costs)
│   ├── market_context.py            # MarketContext: 单个市场的全部运行时状态
│   └── enums.py                     # AMM 专用枚举 (DefenseLevel, Phase, QuoteAction)
│
└── utils/
    ├── __init__.py
    ├── integer_math.py              # 整数安全计算 (ceiling_div, fee_calc)
    └── logging.py                   # 结构化日志

tests/
├── unit/amm/
│   ├── test_three_layer_pricing.py
│   ├── test_as_engine.py
│   ├── test_gradient.py
│   ├── test_phase_manager.py
│   ├── test_defense_stack.py
│   ├── test_budget_manager.py
│   ├── test_inventory_sync.py
│   ├── test_order_manager.py
│   ├── test_config_loader.py
│   ├── test_reconciler.py
│   ├── test_integer_math.py
│   └── test_market_context.py
└── integration/amm/
    ├── test_amm_startup.py
    ├── test_amm_quote_cycle.py
    ├── test_amm_defense_escalation.py
    └── test_amm_graceful_shutdown.py
```

---

## 四、核心数据模型

### 4.1 MarketContext（单市场运行时状态）

```python
@dataclass
class MarketContext:
    """Everything the AMM needs to make decisions for one market."""
    market_id: str
    config: MarketConfig

    # Inventory (from Redis cache, updated by trade_poller)
    inventory: Inventory

    # Strategy state
    phase: Phase                     # EXPLORATION or STABILIZATION
    mid_price: int                   # current mid-price in cents [1, 99]
    reservation_price: float         # A-S reservation price
    optimal_spread: float            # A-S optimal spread

    # Active orders (local cache, synced with Redis)
    active_orders: dict[str, ActiveOrder]  # order_id → ActiveOrder

    # Risk state
    defense_level: DefenseLevel      # NORMAL / WIDEN / ONE_SIDE / KILL_SWITCH
    daily_pnl_cents: int             # accumulated P&L today
    session_start_inventory: Inventory  # snapshot at AMM start

    # Timing
    last_quote_at: float             # monotonic time of last quote cycle
    last_reconcile_at: float         # monotonic time of last full reconciliation
```

### 4.2 Inventory（库存状态）

```python
@dataclass
class Inventory:
    """AMM inventory for a single market. All values in integer cents/shares."""
    cash_cents: int                  # available_balance (not frozen)
    yes_volume: int                  # YES shares held
    no_volume: int                   # NO shares held
    yes_cost_sum_cents: int          # YES cumulative cost basis
    no_cost_sum_cents: int           # NO cumulative cost basis
    yes_pending_sell: int            # YES shares locked in sell orders
    no_pending_sell: int             # NO shares locked in sell orders
    frozen_balance_cents: int        # cash frozen in buy orders

    @property
    def yes_available(self) -> int:
        return self.yes_volume - self.yes_pending_sell

    @property
    def no_available(self) -> int:
        return self.no_volume - self.no_pending_sell

    @property
    def inventory_skew(self) -> float:
        """q = (yes - no) / (yes + no). Range [-1, 1]. 0 = balanced."""
        total = self.yes_volume + self.no_volume
        if total == 0:
            return 0.0
        return (self.yes_volume - self.no_volume) / total
```

### 4.3 OrderIntent（策略输出）

```python
@dataclass
class OrderIntent:
    """Strategy layer output — pure intent, no I/O.

    v1.0 Review Fix #3 — CRITICAL MAPPING RULE:
    AMM NEVER issues BUY orders. All intents must be direction="SELL".
      - Ask ladder: side="YES", direction="SELL", price=ask_price
      - Bid ladder: side="NO",  direction="SELL", price=100-bid_price
    Issuing BUY YES would freeze cash instead of utilizing existing NO shares
    from Mint, violating the single-orderbook dual-inventory design.
    """
    action: QuoteAction              # PLACE / REPLACE / CANCEL / HOLD
    side: str                        # YES / NO (bid maps to NO)
    direction: str                   # SELL only (AMM never BUYs)
    price_cents: int                 # [1, 99]
    quantity: int                    # > 0
    replace_order_id: str | None = None  # for REPLACE action
    reason: str = ""                 # audit trail
```

---

## 五、A-S 报价模型核心公式

对齐 v7.1 §5（Avellaneda-Stoikov 模型）：

```
保留价格 (v1.0 Review Fix #1 — 量纲修正):
  r = s - q · γ · σ² · τ(h) × 100

其中:
  s = mid_price (三层定价输出, 整数 cents, 即概率×100)
  q = inventory_skew ∈ [-1, 1]
  γ = risk_aversion (生命周期分档: EARLY=0.1, MID=0.3, LATE=0.8)
  σ = bernoulli_sigma = sqrt(p(1-p)) / 100 (p = mid_price/100)
  τ(h) = 绝对小时时间因子 = remaining_hours (到交易结束)

  关键: σ² ≈ 0.000025（概率空间），而 s 在美分空间 [1,99]。
  调整项 q·γ·σ²·τ 是概率空间值，必须 ×100 转换为美分空间。
  否则调整项 ≈ 0.00009，取整后恒等于 0，库存调控完全失效。

最优点差 (v1.0 Review Fix #1 — 同步量纲修正):
  δ = (γ · σ² · τ(h) + (2/γ) · ln(1 + γ/κ)) × 100

其中:
  κ = market_depth (订单到达强度, 从配置读取)
  同理: 点差公式的两个分量均在概率空间，×100 转为美分。

报价 (v1.0 Review Fix #6 — ceil/floor 替代 round):
  ask = ceil(r + δ/2)    # 向上取整 (math.ceil, 非 round)
  bid = floor(r - δ/2)   # 向下取整 (math.floor, 非 round)
  ask = clamp(ask, 1, 99)
  bid = clamp(bid, 1, 99)
  if ask <= bid: ask = bid + 1  # 保证正点差

  注意: Python round() 使用银行家舍入 (round-half-to-even)，
  round(2.5) = 2 而非 3。做市报价必须向外取整，否则点差在 .5
  边界被意外压缩，甚至出现 ask <= bid 异常。
```

### γ 生命周期分档

```python
GAMMA_TIERS = {
    "EARLY":  0.1,   # market_lifecycle_days <= 3
    "MID":    0.3,   # 3 < days <= 14
    "LATE":   0.8,   # 14 < days <= 30
    "MATURE": 1.5,   # days > 30
}
```

---

## 六、三道防线风控

```
┌──────────────────────────────────────────────────────────────┐
│                  Defense Level Escalation                      │
│                                                              │
│  NORMAL ──(inventory_skew > 0.3)──► WIDEN                    │
│                                                              │
│  WIDEN  ──(skew > 0.6 OR pnl < -budget/2)──► ONE_SIDE       │
│                                                              │
│  ONE_SIDE ──(pnl < -budget OR skew > 0.8)──► KILL_SWITCH    │
│                                                              │
│  任何级别 ──(market.status != ACTIVE)──► KILL_SWITCH          │
│  任何级别 ──(admin 手动触发)──► KILL_SWITCH                   │
│                                                              │
│  降级: 需连续 N 个周期满足降级条件 (防抖)                      │
└──────────────────────────────────────────────────────────────┘

各级别行为:
  NORMAL:     正常双边报价
  WIDEN:      扩大点差 (spread × widen_factor)
  ONE_SIDE:   仅在减仓方向报价 + 折价抛售
  KILL_SWITCH: batch_cancel → 停止报价 → 等待人工干预
```

---

## 七、测试策略

| # | 测试文件 | 覆盖内容 | 场景数 |
|---|---------|---------|--------|
| 1 | `test_integer_math.py` | ceiling_div, fee_calc, clamp | 8 |
| 2 | `test_three_layer_pricing.py` | 锚定价、微观价、后验学习、权重混合 | 12 |
| 3 | `test_as_engine.py` | reservation_price, optimal_spread, γ 分档, σ 计算, ceil/floor 取整 | 18 |
| 4 | `test_gradient.py` | ask/bid ladder 构建, bid→SELL NO 映射, 梯度递减, 边界裁剪 | 14 |
| 5 | `test_phase_manager.py` | EXPLORATION→STABILIZATION 转换条件, 回退条件, 防抖 | 8 |
| 6 | `test_defense_stack.py` | 4 级升降级, market 状态触发, 防抖计数 | 12 |
| 7 | `test_budget_manager.py` | daily 预算, per-market 预算, 熔断触发 | 8 |
| 8 | `test_inventory_sync.py` | poll_trades 增量同步, cursor 管理, 去重, 手续费扣除, cost_sum 更新 | 12 |
| 9 | `test_order_manager.py` | place/cancel/replace 封装, 错误重试, 状态追踪, pending_sell 同步 | 12 |
| 10 | `test_config_loader.py` | YAML 加载, Redis 热更新, 优先级合并, 校验 | 8 |
| 11 | `test_reconciler.py` | Redis vs DB 差异检测, 自动修复, 告警 | 6 |
| 12 | `test_market_context.py` | MarketContext 生命周期, inventory_skew 计算 | 6 |
| 13 | `test_amm_startup.py` (integration) | 完整启动流程: 认证→配置→库存→首次报价 | 4 |
| 14 | `test_amm_quote_cycle.py` (integration) | 端到端报价循环: sync→strategy→risk→execute | 6 |
| 15 | `test_amm_defense_escalation.py` (integration) | 防线升级全流程: NORMAL→WIDEN→ONE_SIDE→KILL | 4 |
| 16 | `test_amm_graceful_shutdown.py` (integration) | 优雅停机: SIGTERM→batch_cancel→等待→退出 | 3 |

总计约 141 个测试场景（v1.0 Review 后从 129 增至 141）。

---

## 八、偏差记录

| # | 偏差点 | 原始设计文档 | 本次实现 | 理由 |
|---|--------|------------|---------|------|
| D-01 | Kafka 消费 | v7.1 §5.1: Kafka trade_events 驱动库存 | REST 轮询 (2s) | MVP 无 Kafka 基础设施 |
| D-02 | WebSocket | v7.1 §7: WS 订单簿推送 | 不实现 | Phase 2 目标，MVP 不依赖实时推送 |
| D-03 | CPMM 参考 | v7.1 §7: CPMM 作为辅助参考 | 不实现 | 降级为辅助且非必需，MVP 仅用 A-S + 三层定价 |
| D-04 | LVR 防御 | v7.1 §10: LVR 高级模式 | 简化为基于 inventory_skew 的朴素检测 | 高级 LVR 需要外部 Oracle feed |
| D-05 | 回测框架 | v7.1 §13: SimulatedConnector | 不实现 | 独立实施计划 |
| D-06 | Prometheus | v7.1 暗示 metrics | 不实现 | Phase 2，MVP 用结构化日志 |
| D-07 | Service Token | 契约 §2.2: Service Token | 标准 JWT (对齐 Phase A 决策) | pm_gateway 尚未实现 |

---

## 九、v1.0 Review 修正记录

| # | 严重度 | 问题 | 影响范围 | 修正内容 |
|---|--------|------|---------|---------|
| 1 | 🔴 致命 | A-S 模型量纲错位：σ² 在概率空间，mid_price 在美分空间，调整项 ≈ 0 | §5 公式, Plan Task 9 | `reservation_price` 和 `optimal_spread` 结果均 ×100 转换为美分空间 |
| 2 | 🔴 致命 | Trade Poller `_apply_trade` 漏算手续费和 cost_sum | Plan Task 7 | 补全 fee 扣除 + yes/no_cost_sum_cents 增减更新 |
| 3 | 🔴 致命 | Bid Ladder 映射错误：直接发 BUY YES 会冻结现金，违背双边库存设计 | §4.3, §2.5, Plan Task 10/19 | `bid_ladder` 全部映射为 SELL NO @ (100-P)，AMM 永远只 SELL |
| 4 | 🟠 高危 | `pending_sell` 未维护：yes_available 恒等于 yes_volume，导致超卖 | Plan Task 15/19 | OrderManager 添加 `get_pending_sells()` + 每周期同步到 Redis |
| 5 | 🟠 高危 | API Client 缺 HTTP 429 退避：触发限流后 quote_cycle 崩溃 | Plan Task 6 | `_request` 添加 429 指数退避重试（最多 3 次，cap 30s） |
| 6 | 🟡 中等 | `round()` 银行家舍入：round(2.5)=2，可能压缩点差导致 ask≤bid | §5 公式, Plan Task 9 | ask 改用 `math.ceil()`，bid 改用 `math.floor()`，向外取整 |

测试场景总数从 129 增至 141（+12 个覆盖修正点的新测试）。

---

*设计文档结束*
