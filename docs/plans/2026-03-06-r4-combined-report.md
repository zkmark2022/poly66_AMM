# R4 审查报告：数据流类型一致性 & 防线有效性

---

## Step 1：数据流图

```
Oracle (polymarket.py)                    Oracle (polymarket_oracle.py)
  get_price() → float cents 0-100          get_yes_price() → float cents 0-100
  check_deviation() → bool (re-fetches!)   evaluate() → OracleState
       │                                          │
       └──────────┬───────────────────────────────┘
                  ▼
        _evaluate_oracle_state() → OracleState
                  │
                  ▼  internal_price_cents = float(mid)   ← mid is int from ThreeLayerPricing
┌─────────────────┴──────────────────────────────────────────┐
│ ThreeLayerPricing.compute()                                │
│   anchor: int cents → float cents                          │
│   micro: (int,int,int,int) → float cents | None            │
│   posterior: list[dict] → float cents [1.0, 99.0]          │
│   OUTPUT: int cents [1, 99] via clamp(round(raw))          │
└──────────────────┬─────────────────────────────────────────┘
                   ▼  mid: int cents
┌──────────────────┴─────────────────────────────────────────┐
│ ASEngine                                                    │
│   bernoulli_sigma(mid) → float = sqrt(p(1-p)) / 100       │
│   reservation_price(mid, skew, γ, σ, τ) → float cents     │
│   optimal_spread(γ, σ, τ, κ) → float cents                 │
│   compute_quotes(mid, skew, γ, σ, τ, κ) → (int, int)      │
│   OUTPUT: (ask_cents, bid_cents) clamped [1, 99]           │
└──────────────────┬─────────────────────────────────────────┘
                   ▼  ask: int, bid: int
┌──────────────────┴─────────────────────────────────────────┐
│ GradientEngine                                              │
│   build_ask_ladder(ask, config, qty) → list[OrderIntent]   │
│   build_bid_ladder(bid, config, qty) → list[OrderIntent]   │
│   price_cents: int [1, 99], side: "YES"/"NO", dir: "SELL" │
└──────────────────┬─────────────────────────────────────────┘
                   ▼
┌──────────────────┴─────────────────────────────────────────┐
│ OrderSanitizer.sanitize(intents, defense, ctx)             │
│   clamp price [1,99], filter by defense, cap to available  │
│   OUTPUT: list[OrderIntent]                                │
└──────────────────┬─────────────────────────────────────────┘
                   ▼
┌──────────────────┴─────────────────────────────────────────┐
│ OrderManager.execute_intents()                              │
│   sends price_cents (int) to API as-is                     │
│   API expects: int cents                                   │
│   ✅ CONSISTENT                                            │
└────────────────────────────────────────────────────────────┘

Inventory flow:
  API → Initializer._build_inventory() → int fields → InventoryCache.set() → Redis (int strings)
  Redis → InventoryCache.get() → int() parse → Inventory dataclass
  TradePoller._apply_trade() → InventoryCache.adjust() → hincrby (int deltas) ✅
  Reconciler → InventoryCache.set() (full overwrite) ⚠️ race with adjust()
```

---

## Step 2 & 3：防线有效性 + 单位一致性 — 问题报告

---

#### [A-001] σ 双重缩放导致 A-S 模型价差恒定最大化

- **严重程度**: P0
- **位置**: `strategy/as_engine.py:bernoulli_sigma()` → `strategy/as_engine.py:optimal_spread()`
- **现象**: `σ = sqrt(p(1-p)) / 100` 产生极小值（p=0.5 时 σ=0.005），使得 `optimal_spread` 中的库存时间分量 `γ·σ²·τ` 趋近于零，价差完全由深度分量 `(2/γ)·ln(1+γ/κ)` 主导。

  **数值验证**（默认参数 γ=0.3, κ=1.5, τ=24, mid=50）：
  ```
  σ = 0.005, σ² = 0.000025
  inv_component = 0.3 × 0.000025 × 24 = 0.00018  (≈0)
  depth_component = (2/0.3) × ln(1 + 0.3/1.5) = 6.667 × 0.1823 = 1.215
  δ = (0.00018 + 1.215) × 100 = 121.5 cents
  
  → ask = ceil(50 + 60.75) = 111 → clamped to 99
  → bid = floor(50 - 60.75) = -11 → clamped to 1
  ```

  **所有 gamma tier 均受影响**：EARLY(0.1)→129c, MID(0.3)→122c, LATE(0.8)→95c, MATURE(1.5)→93c — 均超出 [1,99] 可用区间。

- **影响**: A-S 引擎在所有默认配置下恒定输出 ask=99, bid=1。Gradient ladder 在极端价格处只能放置 1 级（ask@99, bid mapped to SELL NO@99），实质上不做市。库存风险调整 (reservation price) 几乎为零，γ tier 和 τ 的调整无实际效果。
- **修复建议**: 公式维度需要统一。两种方案：
  - **方案A**：σ = sqrt(p(1-p))（不除100），去掉公式中 `× 100`，因为 mid_price 已经是 cents
  - **方案B**：将所有计算统一到 [0,1] 空间（mid_price/100, σ不除100），最终结果 ×100 转回 cents
  
  方案A 验证（γ=0.3, κ=1.5, τ=24, mid=50）：
  ```
  σ = 0.5, δ = 0.3×0.25×24 + 1.215 = 3.015 cents
  ask=52, bid=47 — 合理的 5c 价差
  ```

---

#### [A-002] `spread_min_cents` / `spread_max_cents` 配置已定义但从未使用

- **严重程度**: P1
- **位置**: `config/models.py:MarketConfig` (定义) → 全代码库无引用
- **现象**: `spread_min_cents=2` 和 `spread_max_cents=20` 在配置中定义但从未在 ASEngine、GradientEngine、Sanitizer 或 main.py 中引用。即使 A-001 修复后产生合理价差，也没有下限/上限保护。
- **影响**: 无法通过配置控制最小/最大价差。极端市场条件下（σ→0 或 τ→0）价差可能收窄至 1 cent 或扩大到不合理范围，没有安全网。
- **修复建议**: 在 `ASEngine.compute_quotes()` 返回前添加：
  ```python
  spread = ask - bid
  if spread < config.spread_min_cents:
      ask = mid + ceil(config.spread_min_cents / 2)
      bid = mid - floor(config.spread_min_cents / 2)
  ```

---

#### [A-003] ONE_SIDE 防线在 skew=0 时完全无效（PnL 触发场景）

- **严重程度**: P1
- **位置**: `risk/defense_stack.py:_determine_target()` (PnL 触发) → `risk/sanitizer.py:_sanitize_one()` (过滤逻辑)
- **现象**: PnL 亏损超过阈值时 `DefenseStack` 返回 `ONE_SIDE`，但 `OrderSanitizer` 的 ONE_SIDE 过滤只根据 `inventory_skew` 方向决定压制哪一侧：
  ```python
  if skew > 0 and intent.side == "NO": return None
  if skew < 0 and intent.side == "YES": return None
  # skew == 0 → 两条都不命中 → 双侧照常报价
  ```
  当库存平衡（skew=0）但 PnL 大亏时，ONE_SIDE 防线退化为 NORMAL，无法减少风险敞口。
- **影响**: PnL 亏损驱动的防御升级失效。亏损继续累积，直到达到 KILL_SWITCH 阈值才停止。
- **修复建议**: PnL 触发的 ONE_SIDE 应独立于 skew 方向。可以在 `DefenseStack` 中传递触发原因（SKEW vs PNL），PnL 触发时压制利润较低的一侧（或简单地两侧都缩量 50%）：
  ```python
  if defense == DefenseLevel.ONE_SIDE and trigger == "PNL" and abs(skew) < 0.05:
      intent.quantity = max(1, intent.quantity // 2)
  ```

---

#### [A-004] Reconciler 与 TradePoller 并发竞态 — 库存写覆盖

- **严重程度**: P1
- **位置**: `lifecycle/reconciler.py:reconcile()` (`cache.set()`) ↔ `connector/trade_poller.py:_apply_trade()` (`cache.adjust()`)
- **现象**: 两者共享 `InventoryCache`，无协调机制：
  1. TradePoller 处理成交 → `cache.adjust(yes_delta=+10)`
  2. Reconciler 同一时间从 API 获取快照（尚未反映该成交）
  3. Reconciler 调用 `cache.set()` 全量覆盖 → 丢失 TradePoller 的增量更新
  
  虽然 asyncio 是单线程的，但两个协程在各自的 `await` 点交替执行。Reconciler 的 API fetch（await）和 cache write（await）之间，TradePoller 可能插入执行。
- **影响**: 每 5 分钟的 reconciliation 可能回退最近的成交记录，导致库存账面短暂不准。下一次 reconciliation 会修正，但期间可能基于错误库存报价。
- **修复建议**: 
  - 方案A：Reconciler 使用 Redis WATCH/MULTI 事务，检测到 key 被修改时放弃本次 reconciliation
  - 方案B：Reconciler 只对有 drift 的字段做 `hincrby(delta)`，不做全量 `set`
  - 方案C：在 reconciliation 期间暂停该 market 的 quote_cycle

---

#### [A-005] `last_known_market_active` 默认 False — 首次报价前 API 故障即触发 KILL

- **严重程度**: P2
- **位置**: `models/market_context.py` (默认值 `False`) → `main.py:quote_cycle()` (使用 fallback)
- **现象**: 
  ```python
  # market_context.py
  last_known_market_active: bool = False  # 默认值
  
  # quote_cycle 中：
  if now - ctx.market_status_checked_at >= _MARKET_STATUS_TTL:
      try:
          status = await api.get_market_status(...)
      except Exception:
          # API 调用失败 → 使用 last_known 默认值 False
          pass
  market_is_active = ctx.last_known_market_active  # False
  # → DefenseStack._determine_target: not active → KILL_SWITCH
  ```
  Initializer 已验证市场为 active，但未将结果写入 `ctx.last_known_market_active`。首次 quote_cycle 的 market status API 调用如果因网络抖动失败，直接 KILL。
- **影响**: 瞬态网络错误可导致刚初始化的市场立即停止报价。
- **修复建议**: 在 `initializer.py` 初始化 MarketContext 时设置 `last_known_market_active=True`：
  ```python
  ctx = MarketContext(
      ...,
      last_known_market_active=True,
      market_status_checked_at=time.monotonic(),
  )
  ```

---

#### [A-006] 异步 Oracle `check_deviation` 每周期重复调用 CLI

- **严重程度**: P2
- **位置**: `oracle/polymarket.py:check_deviation()` → 内部调用 `get_price()`
- **现象**: `_oracle_refresh_loop` 每 30 秒更新 `last_price`，但 `check_deviation()` 无视缓存，每次调用都重新 `await self.get_price()`（触发子进程 CLI 调用）。quote_cycle 默认每 2 秒执行一次 → 每 2 秒一次 CLI 子进程。
- **影响**: 不必要的子进程开销。如果 CLI 偶尔慢于 2 秒，deviation check 可能阻塞整个 quote_cycle（虽然 10s timeout 兜底）。
- **修复建议**: `check_deviation` 应使用 `self.last_price` 而非重新获取：
  ```python
  async def check_deviation(self, internal_price: float, threshold: float = 20.0) -> bool:
      if self.last_price is None:
          return False
      return abs(internal_price - self.last_price) > threshold
  ```

---

#### [A-007] 首次 Gradient Ladder 构建为死代码

- **严重程度**: P3
- **位置**: `main.py:quote_cycle()` — 第一次 `gradient.build_*_ladder()` 调用
- **现象**: ladders 在风险评估之前构建一次，然后在 WIDEN 检查之后**无条件**重建：
  ```python
  # 第一次构建（约第180行）
  ask_ladder = gradient.build_ask_ladder(ask, ...)
  bid_ladder = gradient.build_bid_ladder(bid, ...)
  
  # ... 风险评估 ...
  # WIDEN 可能修改 ask/bid
  
  # 第二次构建（约第210行）— 总是执行，覆盖第一次
  ask_ladder = gradient.build_ask_ladder(ask, ...)
  bid_ladder = gradient.build_bid_ladder(bid, ...)
  ```
- **影响**: 浪费计算。不影响正确性。
- **修复建议**: 删除第一次构建。

---

#### [A-008] 两个同名 `PolymarketOracle` 类共存

- **严重程度**: P3
- **位置**: `oracle/polymarket.py` (async, 接受 `market_slug: str`) vs `oracle/polymarket_oracle.py` (sync, 接受 `MarketConfig`)
- **现象**: 两个文件定义了同名类 `PolymarketOracle`，接口完全不同。`main.py` 从 `polymarket.py` 导入类、从 `polymarket_oracle.py` 导入 `OracleState`。误导入会导致运行时错误。
- **影响**: 维护陷阱。新开发者或 IDE 自动导入可能选错模块。
- **修复建议**: 统一为一个 Oracle 实现，或重命名其中一个（如 `AsyncPolymarketOracle`）。

---

#### [A-009] 异步 Oracle 无 LVR 检测能力

- **严重程度**: P2
- **位置**: `oracle/polymarket.py` (无 `check_lvr` 方法) → `main.py:_evaluate_oracle_state()`
- **现象**: `main.py` 实际使用的是 `oracle/polymarket.py` 的异步 Oracle，该类没有 `check_lvr` 方法也没有价格历史。`_evaluate_oracle_state` 的 fallback 路径只检查 lag 和 deviation，跳过 LVR：
  ```python
  # _evaluate_oracle_state 的 else 分支（async oracle 走这里）
  if oracle.check_lag(...): return STALE
  deviation = oracle.check_deviation(...)  # 只有 lag + deviation
  # LVR 从不检查
  ```
  而 sync Oracle (`polymarket_oracle.py`) 维护价格历史并实现了 LVR 检测，但从未被 `main.py` 使用。
- **影响**: LVR 防护（设计中的快速价格变动检测）在运行时完全不生效。Flash crash 或大幅价格变动不会触发 KILL_SWITCH。
- **修复建议**: 在异步 Oracle 中添加价格历史和 LVR 检测，或改用 sync Oracle 的 `evaluate()` 方法。

---

#### [A-010] `protocols.py` 重复定义 `set` 方法

- **严重程度**: P3
- **位置**: `cache/protocols.py:AsyncRedisLike`
- **现象**: 
  ```python
  def set(self, name, value, ex=None, px=None, nx=False, xx=False) -> Awaitable[Any]: ...
  async def set(self, name, value, ex=None, nx=False) -> Any: ...
  ```
  第二个定义覆盖第一个。类型检查器看到的是 `async def` 签名（缺少 `px`/`xx` 参数）。
- **影响**: 不影响运行时（真正的 Redis client 两个签名都支持），但 mypy/pyright 可能误报。
- **修复建议**: 只保留一个签名。

---

## Step 4：并发安全总结

| 共享资源 | 访问方 | 保护机制 | 评估 |
|----------|--------|----------|------|
| `InventoryCache` (Redis) | quote_cycle + reconciler + trade_poller | 无锁 | **⚠️ [A-004]** reconciler 全量覆盖可丢失增量 |
| `ctx.inventory` (内存) | quote_cycle 独占读写 | asyncio 单线程 | ✅ 安全 — reconciler 不修改 ctx |
| `order_mgr.active_orders` (内存) | quote_cycle 独占 | 每 market 独立实例 | ✅ 安全 |
| `httpx.AsyncClient` | 所有任务共享 | httpx 内部连接池 | ✅ 安全 |
| Redis pipeline (`adjust`) | trade_poller | `transaction=True` 默认原子 | ✅ 单个 pipeline 原子 |
| `HealthState` | health_server + market tasks | 无锁，但只有简单 int/bool 赋值 | ✅ 实际安全（GIL + asyncio） |

Redis pipeline 操作在 pipeline 内部是原子的，但 `set`（全量写）和 `adjust`（增量写）之间无事务隔离 — 这是 [A-004] 的根因。

---

## 未发现问题的检查路径

| 检查项 | 验证结果 |
|--------|----------|
| OrderIntent → API 的 price_cents 单位 | ✅ 一致：int cents 全程传递，无转换 |
| KILL_SWITCH 防线有效性 | ✅ `cancel_all()` 调用 `batch_cancel` API + 清空 `active_orders` + 清空 OrderCache |
| Sanitizer BUY 拦截 | ✅ `direction != "SELL"` 检查在第一行，GradientEngine 只生成 SELL，无绕过路径 |
| τ=0 边界 | ✅ 不崩溃，spread 极大化（depth 分量兜底），ask/bid clamp 到 [1,99] |
| inventory_skew 计算 | ✅ `(yes-no)/(yes+no)`，total=0 时返回 0.0，无除零 |
| TradePoller 去重 | ✅ `deque(maxlen=1000)` per market，先检查再追加 |
| Inventory.total_value_cents | ✅ mid_price_cents 是 int，计算 `yes*mid + no*(100-mid) + cash + frozen` 正确 |
# R4 调用链完整性审查报告

## Step 1：组件清单

| 组件 | 来自模块 | 在 `main.py` 中的角色 |
| --- | --- | --- |
| `create_redis_client()` | `src.amm.cache.redis_client` | 创建 Redis 客户端 |
| `httpx.AsyncClient` | `httpx` | 共享 HTTP 连接池 |
| `TokenManager` | `src.amm.connector.auth` | 登录与 token 刷新 |
| `AMMApiClient` | `src.amm.connector.api_client` | 统一 REST API 访问层 |
| `InventoryCache` | `src.amm.cache.inventory_cache` | Redis 库存快照读写 |
| `ConfigLoader` | `src.amm.config.loader` | 加载全局/市场配置 |
| `HealthState` | `src.amm.lifecycle.health` | 健康检查共享状态 |
| `AMMInitializer` | `src.amm.lifecycle.initializer` | 启动时登录、拉取状态、构造 `MarketContext` |
| `GracefulShutdown` | `src.amm.lifecycle.shutdown` | 退出时批量撤单与关闭 API |
| `PolymarketOracle` | `src.amm.oracle.polymarket` | 每市场 oracle 实例 |
| `TradePoller` | `src.amm.connector.trade_poller` | 每轮同步成交并回写 Redis |
| `AnchorPricing` | `src.amm.strategy.pricing.anchor` | 三层定价的 anchor 层 |
| `MicroPricing` | `src.amm.strategy.pricing.micro` | 三层定价的 micro 层 |
| `PosteriorPricing` | `src.amm.strategy.pricing.posterior` | 三层定价的 posterior 层 |
| `ThreeLayerPricing` | `src.amm.strategy.pricing.three_layer` | 中间价计算器 |
| `ASEngine` | `src.amm.strategy.as_engine` | A-S 报价引擎 |
| `GradientEngine` | `src.amm.strategy.gradient` | 生成 YES/NO 卖单梯度 |
| `DefenseStack` | `src.amm.risk.defense_stack` | 风险防御级别判定 |
| `OrderSanitizer` | `src.amm.risk.sanitizer` | 下单前净化/裁剪意图 |
| `OrderManager` | `src.amm.connector.order_manager` | 执行订单 diff、下单/撤单 |
| `PhaseManager` | `src.amm.strategy.phase_manager` | EXPLORATION/STABILIZATION 状态机 |
| `run_market_with_health()` task | `src.amm.main` | 每市场主循环任务 |
| `AMMReconciler` | `src.amm.lifecycle.reconciler` | 5 分钟对账器 |
| `reconcile_loop()` task | `src.amm.main` | 周期调度对账器 |
| `_oracle_refresh_loop()` task | `src.amm.main` | 周期刷新 oracle |
| `run_health_server()` task | `src.amm.lifecycle.health` | 健康检查 HTTP 服务 |

## Step 2：逐个追踪生命周期

| 组件 | 初始化 | 运行 | 清理 | 结论 |
| --- | --- | --- | --- | --- |
| `create_redis_client()` / `redis_client` | `amm_main()` 正确创建 | 被 `InventoryCache`/`ConfigLoader` 使用 | `redis_client.aclose()` 调用 | ✅ 完整 |
| `httpx.AsyncClient` | 正确创建并共享给认证/API 客户端 | 被 `TokenManager` 与 `AMMApiClient` 复用 | `http_client.aclose()` 调用 | ✅ 完整 |
| `TokenManager` | 参数完整 | 启动时 `login()`，运行期可能 `refresh()` | 依赖共享 `http_client` 关闭 | ✅ 完整 |
| `AMMApiClient` | 参数完整 | 全流程调用 | `GracefulShutdown.execute()` 调 `api.close()` | ✅ 完整 |
| `InventoryCache` | 参数完整 | 启动、报价、对账均使用 | 随 Redis 客户端关闭 | ✅ 完整 |
| `ConfigLoader` | 参数完整 | `load_global()`/`load_market()` 调用 | 无显式清理需求 | ✅ 完整 |
| `HealthState` | 正确构造 | 市场任务与健康服务共享 | 无显式清理需求 | ✅ 完整 |
| `AMMInitializer` | 参数完整 | `initialize()` 调用一次 | 无显式清理需求 | ✅ 完整 |
| `GracefulShutdown` | 参数完整 | `finally` 中执行 | 其自身无额外资源 | ✅ 完整 |
| `PolymarketOracle` | 仅在 `oracle_slug` 非空时构造 | 预热 `get_price()`，并启动刷新 task | 依赖 task cancel，无显式释放 | ⚠️ 部分遗漏 |
| `TradePoller` | 每市场正确构造 | 每个 `quote_cycle()` 第一步调用 | 无显式清理需求 | ✅ 完整 |
| `AnchorPricing` | 正确构造 | 被 `ThreeLayerPricing.compute()` 调用 | 无显式清理需求 | ✅ 完整 |
| `MicroPricing` | 正确构造 | 被 `ThreeLayerPricing.compute()` 调用 | 无显式清理需求 | ✅ 完整 |
| `PosteriorPricing` | 正确构造 | 被 `ThreeLayerPricing.compute()` 调用 | 无显式清理需求 | ✅ 完整 |
| `ThreeLayerPricing` | 参数完整 | 每轮计算 mid | 无显式清理需求 | ✅ 完整 |
| `ASEngine` | 正确构造 | 每轮计算报价 | 无显式清理需求 | ✅ 完整 |
| `GradientEngine` | 正确构造 | 每轮生成梯度意图 | 无显式清理需求 | ✅ 完整 |
| `DefenseStack` | 正确构造 | 每轮 `evaluate()` | 无显式清理需求 | ✅ 完整 |
| `OrderSanitizer` | 正确构造 | 每轮下单前调用 `sanitize()` | 无显式清理需求 | ✅ 完整 |
| `OrderManager` | 只传入 `api` + `inventory_cache`，缺失 `order_cache` 恢复链 | 每轮执行 `execute_intents()` | shutdown 时仅被 `api.batch_cancel()` 间接替代 | ⚠️ 部分遗漏 |
| `PhaseManager` | 正确构造 | 每个成功进入 Step 1.5 的周期调用 `update()` | 无显式清理需求 | ✅ 完整 |
| `run_market_with_health()` | task 正确创建 | 持续运行市场循环 | `finally` 递减 `health_state.markets_active` | ✅ 完整 |
| `AMMReconciler` | 构造参数错误：`cache=` 不匹配构造签名 | 因构造失败，`reconcile_loop()` 根本不会启动 | 无法进入清理 | ❌ 完全缺失 |
| `reconcile_loop()` | 依赖上一步失败 | 未被真正调度 | 无法进入清理 | ❌ 完全缺失 |
| `_oracle_refresh_loop()` | 仅有 oracle 的市场会创建 | 被 `asyncio.create_task()` 调度 | shutdown 时 task cancel | ✅ 完整 |
| `run_health_server()` | task 正确创建 | 被 `asyncio.create_task()` 调度 | shutdown 时 task cancel，未见额外显式停止 | ⚠️ 部分遗漏 |

## Step 3：`quote_cycle()` 执行路径追踪

### 3.1 正常路径

1. `poller.poll(ctx.market_id)` 拉取成交并更新 Redis。
2. `inventory_cache.get(ctx.market_id)` 刷新 `ctx.inventory`。
3. 若 `ctx.phase == STABILIZATION`，调用 `maybe_auto_reinvest()`。
4. 若 `phase_mgr is not None`，调用 `phase_mgr.update()`。
5. `api.get_orderbook()` 获取盘口；失败则回退 anchor。
6. `pricing.compute()` 产出 `mid`。
7. `ASEngine` 计算 `ask/bid`。
8. `GradientEngine` 先生成一次梯度。
9. 计算 `ctx.session_pnl_cents`。
10. 若配置了 oracle，执行 `_evaluate_oracle_state()`。
11. 依据 TTL 可能调用 `api.get_market_status()`。
12. `risk.evaluate()` 生成内部防御级别，与 oracle 防御级别取更高值。
13. `KILL_SWITCH` 时直接 `order_mgr.cancel_all()` 并返回。
14. `WIDEN` 时扩张 spread，并重新生成梯度。
15. `sanitizer.sanitize()` 过滤意图。
16. `drop_buy_side_intents_when_cash_depleted()` 在无现金时移除 `SELL NO`。
17. `order_mgr.execute_intents()` 执行最终意图。

### 3.2 防御状态触发点

| 防御状态 | 触发点 | 实际效果 | 是否在路径上 |
| --- | --- | --- | --- |
| `NORMAL` | `risk.evaluate()` 与 oracle 都不升级 | 正常双边卖单 | 是 |
| `WIDEN` | `DefenseStack._determine_target()` 命中 widen 条件 | `ask/bid` 扩宽后重新生成梯度 | 是 |
| `ONE_SIDE` | `DefenseStack` 命中 one-side 条件，或 oracle `STALE -> ONE_SIDE` | 仅在 `sanitizer._sanitize_one()` 里基于 `inventory_skew` 删单 | 是，但效果并不总是生效 |
| `KILL_SWITCH` | `market_active=False`、库存/PnL 触发 kill、或 oracle `DEVIATION/LVR` | `order_mgr.cancel_all()` 并返回 | 是 |

补充判断：

- `PhaseManager.update()`：在 `amm_main()` 调用链中每轮都会传入 `phase_mgr`，所以每个“成功执行到 Step 1.5”的周期都会调用；若 `poller.poll()`、`inventory_cache.get()`、`maybe_auto_reinvest()` 之前抛异常，则该周期会跳过。
- `Sanitizer`：每次真正进入下单步骤前都会调用一次；仅当 `KILL_SWITCH` 提前返回，或此前步骤异常时跳过。
- `AMMReconciler`：不会被真正 `asyncio` 调度，因为 `AMMReconciler(api=api, cache=inventory_cache)` 在构造阶段就会抛 `TypeError`。
- 跳过条件：
  - `maybe_auto_reinvest()`：仅 `ctx.phase == STABILIZATION` 时执行。
  - oracle 检查：仅 `oracle is not None and ctx.config.oracle_slug` 时执行。
  - `api.get_market_status()`：仅 TTL 过期时执行。
  - `WIDEN` 分支：仅防御级别等于 `WIDEN` 时执行。
  - `execute_intents()`：仅非 `KILL_SWITCH` 且前置步骤未抛异常时执行。

### 3.3 额外路径结论

- `LVR` 触发点在当前主调用链上不可达。`amm_main()` 实例化的是 `src/amm/oracle/polymarket.py` 的 `PolymarketOracle`，它没有 `evaluate()` / `check_lvr()`；`_evaluate_oracle_state()` 因此只会走 `check_lag()` + `check_deviation()` 分支。
- `ONE_SIDE` 在 `inventory_skew == 0` 时会退化成“什么都不删”。因此由 oracle stale 或 PnL 触发的 `ONE_SIDE`，并不保证只挂单边。

## Step 4：识别“永远不会执行”的代码

### 永远不会执行或当前调用链不可达

- `src/amm/lifecycle/winding_down.py:13-49` 的 `handle_winding_down()`：
  - `main.py` 未 import，`quote_cycle()` 路径中也没有任何调用点。
  - 市场结束时只会走 `KILL_SWITCH + cancel_all()`，不会执行 burn/清盘。

- `src/amm/connector/order_manager.py:226-238` 的 `load_from_cache()`：
  - `amm_main()` 没有给 `OrderManager` 注入 `order_cache`，也没有调用 `load_from_cache()`。
  - 这条“重启后恢复活动订单”链路在当前入口中是死的。

- `src/amm/oracle/polymarket_oracle.py:88-115` 的 `check_lvr()` / `evaluate()`：
  - 定义存在，但 `amm_main()` 未实例化这个模块里的 `PolymarketOracle`。
  - `OracleState.LVR` 因此在实际运行路径上不可达。

### 条件恒真/恒假

- 未发现明显的语法级“恒真/恒假”分支。
- 但在 `amm_main()` 当前构造方式下，`quote_cycle()` 中的 oracle 行为等价于：
  - “只会检查 lag/deviation，不会检查 LVR”。
  - 这是由对象实现不匹配导致的“事实上的恒假路径”。

### `main.py` 中 import 但未使用

- 未发现 `main.py` 中明确未使用的 import。

## Step 5：错误处理路径检查

### `quote_cycle()` 单步抛异常时

- `quote_cycle()` 内部几乎没有分段降级，只有：
  - `get_orderbook()` 失败时回退 anchor。
  - `get_market_status()` 失败时回退 `last_known_market_active`。
- 其他步骤如 `poller.poll()`、`inventory_cache.get()`、`pricing.compute()`、`sanitizer.sanitize()`、`order_mgr.execute_intents()` 一旦抛异常，会直接把整个周期抛给 `run_market()`。
- 因此整体策略更接近“整轮失败并重试下一轮”，不是“局部降级后继续报价”。

### `run_market()` 的 `except`

- `src/amm/main.py:309-314` 会捕获所有 `Exception`，仅记录日志，然后 sleep 并继续下一轮。
- 结果：
  - 关键编程错误会被吞掉，市场 task 不会失败退出。
  - 上层 `asyncio.gather(*tasks, return_exceptions=True)` 通常看不到市场 task 的失败。
  - 健康状态也不会因为单市场持续失败而自动转为 not ready。

### `_bg_task_guard`

- 覆盖范围不完整：
  - 只覆盖 `background_tasks`，不覆盖 market tasks。
  - 只覆盖“task 以异常结束”的场景，不覆盖 task 内部已经捕获并吞掉的异常。
  - 不覆盖 background task 创建前的异常。
- 例如 `_oracle_refresh_loop()` 自己 `except Exception` 之后只记 warning，不会让 `_bg_task_guard` 触发。

## Step 6：问题报告

#### [B-001] 对账器构造参数错误，5 分钟对账链路完全不起作用
- **严重程度**: P0
- **位置**: `src/amm/main.py:459`
- **现象**: `AMMReconciler` 的构造签名是 `AMMReconciler(api, inventory_cache)`，但入口传入的是 `AMMReconciler(api=api, cache=inventory_cache)`。`cache` 不是有效关键字参数。
- **影响**: 运行到这里会直接抛 `TypeError`。`reconcile_loop()`、health server、oracle refresh task 的后续创建都不会完成；更严重的是，异常发生在 `try/finally` 之前，已经创建出的 market tasks 会被 `asyncio.run()` 粗暴取消，不能走正常 shutdown。
- **修复建议**: 改为 `AMMReconciler(api=api, inventory_cache=inventory_cache)`，并把 market/background task 创建整体放进一个更早的 `try/finally`，避免入口后半段异常时跳过清理。

#### [B-002] 市场结束清盘逻辑未接入主调用链，`winding_down.py` 实际是死代码
- **严重程度**: P1
- **位置**: `src/amm/main.py:255-280`, `src/amm/lifecycle/winding_down.py:13-49`
- **现象**: `quote_cycle()` 只会通过 `api.get_market_status()` 把 `market_active=False` 送入 `DefenseStack`，随后触发 `KILL_SWITCH -> order_mgr.cancel_all()`。`handle_winding_down()` 从未被 import 或调用。
- **影响**: 市场结束时不会执行 burn，不会把可烧毁的 YES/NO 对冲仓位回收为现金，也不会设置预期的 winding-down 退出路径。机器人会继续以“已 kill 的市场”身份空转。
- **修复建议**: 在 `quote_cycle()` 获取到市场状态后，优先判断是否进入终局状态；若是，则调用 `handle_winding_down(ctx, api, market_status, order_mgr)`，并在成功后持久化库存/退出市场循环。

#### [B-003] Oracle 类型接错，LVR 防御状态在实际运行路径上永远不会触发
- **严重程度**: P1
- **位置**: `src/amm/main.py:39-40`, `src/amm/main.py:128-160`, `src/amm/main.py:393-401`, `src/amm/oracle/polymarket.py:13-54`, `src/amm/oracle/polymarket_oracle.py:88-115`
- **现象**: 入口实例化的是 `src/amm/oracle/polymarket.py` 的 `PolymarketOracle`，它只有 `get_price()`、`check_deviation()`、`check_lag()`；而 `OracleState.LVR` 及 `evaluate()/check_lvr()` 在另一个模块 `polymarket_oracle.py` 中。`_evaluate_oracle_state()` 因对象接口不匹配，实际只会执行 lag/deviation 检查。
- **影响**: 设计要求中的 `LVR -> KILL` 永远不会发生；当前 oracle 防御只剩 `STALE` 和 `DEVIATION` 两条路径，防御栈不完整。
- **修复建议**: 统一 oracle 实现，只保留一个 `PolymarketOracle` 类型，并让 `amm_main()` 与 `_evaluate_oracle_state()` 使用同一接口。若保留 `evaluate()` 版本，则入口必须实例化 `src.amm.oracle.polymarket_oracle.PolymarketOracle(MarketConfig)`。

#### [B-004] 重启恢复链不完整：`OrderManager` 没有接入 `OrderCache`，`load_from_cache()` 永远不会执行
- **严重程度**: P1
- **位置**: `src/amm/main.py:436`, `src/amm/connector/order_manager.py:27-37`, `src/amm/connector/order_manager.py:226-238`
- **现象**: `amm_main()` 构造 `OrderManager` 时只传了 `api` 与 `inventory_cache`，没有传 `order_cache`。同时入口也没有调用 `load_from_cache()`。
- **影响**: 进程重启后，`active_orders` 一定是空的，订单 diff 失去历史状态，可能对已经挂着的卖单再次下单；“Redis 恢复活动订单/挂单状态”的链路当前不存在。
- **修复建议**: 在 `amm_main()` 中创建并注入 `OrderCache`，然后在市场 task 启动前执行 `await order_mgr.load_from_cache(ctx.market_id)`。如果不打算做 Redis 级订单恢复，就应删除相关死代码并明确改为启动时强制 batch-cancel + 全量重建。

#### [B-005] `run_market()` 吞掉所有 `quote_cycle()` 异常，关键失败不会上浮到监督层
- **严重程度**: P1
- **位置**: `src/amm/main.py:309-314`
- **现象**: `run_market()` 对整个 `quote_cycle()` 使用 `except Exception`，记录日志后继续 sleep 并重试。
- **影响**: 一旦出现持续性的编程错误、数据契约错误或本地状态损坏，市场 task 不会失败退出，`asyncio.gather()` 看起来仍然“成功”，健康检查也不会立刻反映真实故障。系统会在静默失败状态下长时间不报价或重复失败。
- **修复建议**: 只吞掉明确可恢复的异常类型；对不可恢复异常应设置 `ctx.shutdown_requested = True` 并重新抛出，或上报到统一 supervisor，让 `amm_main()` 进入失败关闭流程。

#### [B-006] `ONE_SIDE` 防御在零库存偏斜时会退化为双边继续报价
- **严重程度**: P1
- **位置**: `src/amm/main.py:269-295`, `src/amm/risk/sanitizer.py:50-58`
- **现象**: `ONE_SIDE` 的实际执行只在 `OrderSanitizer._sanitize_one()` 中依据 `ctx.inventory.inventory_skew` 删除一侧订单。若 `ONE_SIDE` 是由 PnL 或 oracle stale 触发、但当前 `inventory_skew == 0`，那么 YES/NO 两侧订单都不会被删除。
- **影响**: 设计上应更保守的 `ONE_SIDE` 模式，在某些关键风险场景下会继续双边挂卖单，防御效果与预期不一致。
- **修复建议**: 把 `ONE_SIDE` 的“保留哪一边”策略显式化，不要仅依赖当前 skew 的正负号。可在 `DefenseStack` 或 `MarketContext` 中产出 side preference，再由 `sanitizer` 无条件删除另一侧。
