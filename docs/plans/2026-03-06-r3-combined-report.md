# AMM R3 联合审查报告

> 日期: 2026-03-06  
> Agent A: Claude Opus 4.6（调用链完整性）  
> Agent B: Codex GPT-5.4（数据流类型一致性 + 防线有效性）

# AMM Bot R3 代码审查报告

## Step 1: 组件清单

| 组件 | 来自模块 | 在 main.py 中的角色 |
|------|----------|---------------------|
| `create_redis_client` | `cache.redis_client` | 创建 Redis 连接 |
| `InventoryCache` | `cache.inventory_cache` | Redis 库存 CRUD |
| `ConfigLoader` | `config.loader` | YAML + Redis 配置加载 |
| `TokenManager` | `connector.auth` | JWT 认证管理 |
| `AMMApiClient` | `connector.api_client` | REST API 客户端 |
| `OrderManager` | `connector.order_manager` | 订单差异执行 |
| `TradePoller` | `connector.trade_poller` | 成交轮询/库存同步 |
| `HealthState` / `run_health_server` | `lifecycle.health` | 健康检查端点 |
| `AMMInitializer` | `lifecycle.initializer` | 启动序列 |
| `AMMReconciler` | `lifecycle.reconciler` | 定期对账 |
| `GracefulShutdown` | `lifecycle.shutdown` | 优雅停机 |
| `maybe_auto_reinvest` | `lifecycle.reinvest` | 自动再投资 |
| `drop_buy_side_intents_when_cash_depleted` | `lifecycle.reinvest` | 现金耗尽时过滤 |
| `DefenseStack` | `risk.defense_stack` | 三线防御 |
| `OrderSanitizer` | `risk.sanitizer` | 下单前校验 |
| `ASEngine` | `strategy.as_engine` | A-S 定价模型 |
| `GradientEngine` | `strategy.gradient` | 梯度挂单 |
| `PhaseManager` | `strategy.phase_manager` | 阶段状态机 |
| `AnchorPricing` | `strategy.pricing.anchor` | 锚定价格层 |
| `MicroPricing` | `strategy.pricing.micro` | 微观结构层 |
| `PosteriorPricing` | `strategy.pricing.posterior` | 后验价格层 |
| `ThreeLayerPricing` | `strategy.pricing.three_layer` | 三层定价组合 |
| `PolymarketOracle` | `oracle.polymarket` | 外部价格源 |
| `OracleState` | `oracle.polymarket_oracle` | Oracle 状态枚举 |

---

## Step 2: 生命周期追踪

| 组件 | 初始化 | 运行 | 清理 | 结论 |
|------|--------|------|------|------|
| `redis_client` | ✅ `create_redis_client()` | ✅ 各处使用 | ✅ `redis_client.aclose()` | ✅ |
| `http_client` | ✅ `httpx.AsyncClient()` | ✅ 通过 API | ✅ `http_client.aclose()` | ✅ |
| `TokenManager` | ✅ 构造+login via initializer | ✅ refresh in _request | ✅ 随 http_client 关闭 | ✅ |
| `AMMApiClient` | ✅ 正确构造 | ✅ 全程使用 | ✅ `shutdown.execute()` → `api.close()` | ⚠️ 见 A-001 |
| `InventoryCache` | ✅ | ✅ | ✅ 随 redis 关闭 | ✅ |
| `ConfigLoader` | ✅ | ✅ `load_global()` 被调用 | N/A | ⚠️ 见 A-002 |
| `HealthState` | ✅ | ✅ `run_health_server` 任务 | ✅ `health_state.ready = False` | ✅ |
| `AMMInitializer` | ✅ | ✅ `initialize()` | N/A | ✅ |
| `AMMReconciler` | ✅ | ✅ `reconcile_loop` 任务 | ✅ 任务被取消 | ✅ |
| `GracefulShutdown` | ✅ | ✅ `shutdown.execute()` | N/A | ✅ |
| `TradePoller` | ✅ 每市场构造 | ✅ `quote_cycle` | N/A 无状态清理 | ✅ |
| `ThreeLayerPricing` | ✅ | ✅ `quote_cycle` | N/A | ✅ |
| `ASEngine` | ✅ | ✅ | N/A | ✅ |
| `GradientEngine` | ✅ | ✅ | N/A | ✅ |
| `DefenseStack` | ✅ | ✅ | N/A | ✅ |
| `OrderSanitizer` | ✅ | ✅ | N/A | ✅ |
| `OrderManager` | ✅ | ✅ `execute_intents` | ⚠️ 见 A-003 | ⚠️ |
| `PhaseManager` | ✅ | ✅ | N/A | ✅ |
| `PolymarketOracle` | ✅ 每市场构造 | ✅ `_oracle_refresh_loop` | ✅ 任务被取消 | ⚠️ 见 A-004 |
| `OrderCache` | ❌ 从未构造 | ❌ | ❌ | ❌ 见 A-005 |

---

## Step 3: quote_cycle() 执行路径追踪

### 完整路径

```
poll trades → update inventory from Redis → maybe_auto_reinvest (STABILIZATION only)
→ phase_mgr.update() → get_orderbook → ThreeLayerPricing.compute()
→ ASEngine.compute_quotes() → GradientEngine.build_ask/bid_ladder()
→ update session P&L → oracle evaluation → market status check (TTL cached)
→ DefenseStack.evaluate() → max(risk_defense, oracle_defense)
→ KILL_SWITCH? → cancel_all + return
→ WIDEN? → widen spread → rebuild ladders
→ Sanitizer.sanitize() → drop_buy_side_intents_when_cash_depleted()
→ OrderManager.execute_intents()
```

### 防御状态触发点

| 防御级别 | 触发源 | 路径上是否存在 |
|----------|--------|----------------|
| NORMAL | skew < 0.3, pnl ok, market active | ✅ DefenseStack._determine_target |
| WIDEN | skew ≥ 0.3 | ✅ DefenseStack + quote_cycle 行内宽价处理 |
| ONE_SIDE | skew ≥ 0.6 或 pnl ≤ -50% 或 oracle STALE | ✅ DefenseStack + Sanitizer 过滤 |
| KILL_SWITCH | skew ≥ 0.8 或 pnl ≤ -max 或 market inactive 或 oracle DEVIATION/LVR | ✅ cancel_all + return |

### 关键调用检查

- **PhaseManager.update()**: ✅ 每周期调用（phase_mgr 非 None 时）
- **Sanitizer.sanitize()**: ✅ 每次下单前调用
- **跳过条件**: `KILL_SWITCH` 时跳过 sanitizer 和 execute（正确行为，先 cancel_all 再 return）

### ONE_SIDE 路径遗漏

⚠️ `quote_cycle` 中 `defense == DefenseLevel.WIDEN` 时重建梯度并宽价，但 `ONE_SIDE` 时 **没有** 特殊的价格/梯度处理——仅在 Sanitizer 中过滤掉一侧。梯度本身未做调整。这可能是 by design（Sanitizer 负责单边过滤），但值得注意。

---

## Step 4: "永远不会执行"的代码

### 4.1 梯度被计算两次（第一次结果被丢弃）

`quote_cycle` 行 `base_qty = ...` 和 `ask_ladder = gradient.build_ask_ladder(...)` 在 Step 2 末尾计算了一次，然后在 Step 3.5（WIDEN 处理之后）**完全重新计算**。第一次的 `ask_ladder` / `bid_ladder` 赋值永远被覆盖。见 A-006。

### 4.2 `import yaml` 在 `amm_main` 函数体内

`amm_main` 中 `import yaml` 在函数体内（非顶层），这本身可以工作，但如果 `oracle.yaml` 不存在则该 import 不会被使用。不过这不是"永远不执行"的问题。

### 4.3 `winding_down` 模块从未在 main.py 中使用

`lifecycle/winding_down.py` 定义了 `handle_winding_down()` 但 `main.py` 和 `quote_cycle` 中 **从未调用**。市场结束时的清盘逻辑完全缺失。见 A-007。

### 4.4 `OrderCache` 从未实例化

`OrderManager` 接受 `order_cache` 参数，但 `amm_main` 中构造 `OrderManager` 时未传入 `OrderCache`，所以 `order_cache` 始终为 `None`。所有 `if self._order_cache is not None:` 分支永远为 False。见 A-005。

### 4.5 `models/orders.py` 中的 `ActiveOrder` 从未使用

`src/amm/models/orders.py` 定义了 `ActiveOrder`，但 `order_manager.py` 有自己的 `ActiveOrder` dataclass。`models/orders.py` 的版本完全是死代码。

### 4.6 `last_known_market_active` 初始值 = `False`

`MarketContext.last_known_market_active` 默认 `False`，`market_status_checked_at` 默认 `0.0`。第一个 quote_cycle 进入时 `now - 0.0 >= 30.0` 为 True 所以会触发 API 查询。但如果 API 查询抛异常，则 `market_is_active = False` → `DefenseStack._determine_target` 返回 `KILL_SWITCH` → 首次 cycle 就 cancel_all。见 A-008。

---

## Step 5: 问题报告

#### [A-001] shutdown 时 `api.close()` 和 `http_client.aclose()` 双重关闭
- **严重程度**: P2
- **位置**: `main.py:319-320`（`shutdown.execute()` 调用 `api.close()`，然后 `http_client.aclose()`）
- **现象**: `GracefulShutdown.execute()` 内部调用 `self._api.close()`，而 `AMMApiClient.close()` 在 `_owns_client=False` 时不关闭 client。但紧接着 `main.py` 又调用 `http_client.aclose()`。由于 `AMMApiClient` 构造时传入了 `http_client`（`_owns_client=False`），`api.close()` 实际上是空操作。最终 `http_client.aclose()` 正常关闭。
- **影响**: 当前无实际 bug，但 `shutdown.execute()` 中的 `api.close()` 是误导性空操作。
- **修复建议**: 从 `GracefulShutdown.execute()` 中移除 `await self._api.close()`，或在 `amm_main` 中不再手动调用 `http_client.aclose()`，让 `api` 拥有 client 的生命周期。

---

#### [A-002] `config_loader.load_global()` 返回值被丢弃
- **严重程度**: P2
- **位置**: `main.py:274`
- **现象**: `await config_loader.load_global()` 的返回值 `GlobalConfig` 未被保存。`reconcile_interval_seconds`、`trade_poll_interval_seconds` 等全局配置被硬编码（如 `reconcile_loop` 传入 `300.0`）而非从 `GlobalConfig` 读取。
- **影响**: 修改 YAML 或 Redis 中的全局配置不会生效。`quote_interval_seconds` 来自 `MarketConfig`，但 `reconcile_interval_seconds` 被硬编码为 300。
- **修复建议**: `global_cfg = await config_loader.load_global()` 并用 `global_cfg.reconcile_interval_seconds` 替代硬编码。

---

#### [A-003] shutdown 时 OrderManager 未 cancel_all
- **严重程度**: P1
- **位置**: `main.py:311-316` + `lifecycle/shutdown.py:21`
- **现象**: `GracefulShutdown.execute()` 使用 `api.batch_cancel()` 直接调用 API 取消所有订单，但 **不更新** `OrderManager.active_orders` 字典。如果 shutdown 后有任何代码路径试图读取 `active_orders`，状态将不一致。更重要的是，`OrderCache`（如果启用）不会被清除。
- **影响**: 当前因为 `OrderCache` 未启用（见 A-005），影响有限。但如果修复 A-005 后，shutdown 路径会遗留脏缓存。
- **修复建议**: `GracefulShutdown` 应接受 `OrderManager` 列表，调用 `order_mgr.cancel_all()` 而非直接 `api.batch_cancel()`。

---

#### [A-004] Oracle 类型混淆：`oracle.polymarket` vs `oracle.polymarket_oracle`
- **严重程度**: P0
- **位置**: `main.py:34-35`（import）、`main.py:290-298`（构造）
- **现象**: `main.py` import 的是 `from src.amm.oracle.polymarket import PolymarketOracle`（异步版本，构造器接受 `market_slug: str`），但 `_evaluate_oracle_state()` 中调用的 `oracle.evaluate()` / `oracle.check_stale()` / `oracle.check_lvr()` 等方法来自 `oracle.polymarket_oracle.PolymarketOracle`（同步版本，构造器接受 `config: MarketConfig`）。
- **两个类的关键差异**:

| 特性 | `oracle.polymarket` | `oracle.polymarket_oracle` |
|------|---------------------|---------------------------|
| 构造参数 | `market_slug: str` | `config: MarketConfig` |
| `refresh()` | ❌ 不存在 | ✅ 同步 |
| `evaluate()` | ❌ 不存在 | ✅ 同步 |
| `check_stale()` | ❌ 不存在 | ✅ |
| `check_lvr()` | ❌ 不存在 | ✅ |
| `check_deviation()` | `async (internal, threshold)` | `sync (internal)` |
| `get_price()` | ✅ async | ❌ 不存在 |

- **`_evaluate_oracle_state` 执行路径分析**:
  1. 调用 `getattr(oracle, "evaluate", None)` → `None`（`polymarket.PolymarketOracle` 无此方法）→ 跳过
  2. 调用 `getattr(oracle, "check_stale", None)` → `None` → 跳过
  3. 进入 `oracle.check_lag()` → ✅ 存在
  4. 调用 `oracle.check_deviation(internal_price_cents, threshold=...)` → 这是 **async** 方法
  5. `deviation = oracle.check_deviation(...)` 返回 **coroutine**，不是 bool
  6. `if inspect.isawaitable(deviation): deviation = await deviation` → ✅ 正确 await
  
  所以 **当前路径实际上能工作**，但是走的是降级路径（check_lag + check_deviation）。`check_stale()` 和 `check_lvr()` 永远不会被调用。

- **`_oracle_refresh_loop` 分析**:
  1. `getattr(oracle, "refresh", None)` → `None`（polymarket 版本无此方法）
  2. 回退到 `await oracle.get_price()` → ✅ 正常工作

- **影响**: LVR 检测 **完全失效**。Oracle 只能检测 lag（stale）和 deviation，无法检测短窗口快速价格变化。这是设计意图的重大偏离。
- **修复建议**: `main.py` 应 import 并使用 `oracle.polymarket_oracle.PolymarketOracle`（传入 `config`），或者统一两个 Oracle 类为一个。

---

#### [A-005] OrderCache 从未实例化 — 重启后订单状态丢失
- **严重程度**: P1
- **位置**: `main.py:303`（`OrderManager(api=api, cache=inventory_cache)` — 未传 `order_cache`）
- **现象**: `OrderManager.__init__` 的 `order_cache` 参数默认 `None`。`amm_main` 中构造时未传入 `OrderCache` 实例。
- **影响**: 
  1. `order_manager.load_from_cache()` 从未被调用（main.py 中也没有调用）
  2. 所有 `if self._order_cache is not None:` 分支永远跳过
  3. 重启后 `active_orders` 为空字典，旧订单成为孤儿单直到 reconciler 或 TTL 清理
- **修复建议**: 
  ```python
  from src.amm.cache.order_cache import OrderCache
  order_cache = OrderCache(typed_redis_client)
  order_mgr = OrderManager(api=api, cache=inventory_cache, order_cache=order_cache)
  await order_mgr.load_from_cache(ctx.market_id)
  ```

---

#### [A-006] 梯度在 WIDEN 前计算一次，WIDEN 后重新计算 — 第一次结果被丢弃
- **严重程度**: P3
- **位置**: `main.py:193-196`（第一次）→ `main.py:234-237`（第二次）
- **现象**: Step 2 末尾计算了 `base_qty`、`ask_ladder`、`bid_ladder`，但 Step 3.5 之后又完全重新计算（即使 defense 不是 WIDEN）。第一次计算浪费 CPU。
- **影响**: 纯性能浪费，无逻辑错误。非 WIDEN 路径两次计算结果相同。
- **修复建议**: 删除第一次梯度计算（`main.py:193-196`），仅在 WIDEN 判断之后计算一次。

---

#### [A-007] `handle_winding_down` 从未被调用 — 市场结束时无清盘逻辑
- **严重程度**: P1
- **位置**: `lifecycle/winding_down.py`（整个文件）+ `main.py`（无 import）
- **现象**: `winding_down.py` 实现了完整的清盘流程（cancel orders → burn pairs → set shutdown），但 `main.py` 和 `quote_cycle` 中从未 import 或调用。`quote_cycle` 中的 `market_status` 检查仅用于 `DefenseStack.evaluate()` 的 `market_active` 参数。
- **影响**: 当市场变为 RESOLVED/SETTLED/VOIDED 时：
  - `market_active` 变为 `False` → `KILL_SWITCH` → `cancel_all` ✅
  - 但 **不会 burn** 剩余的 YES/NO pairs，资金被锁死在无价值头寸中
  - `winding_down` 标志永远不会被设置
- **修复建议**: 在 `quote_cycle` 中 market status 检查后（或在 `run_market` 循环中），检测到终态时调用 `handle_winding_down()`。

---

#### [A-008] 首次 cycle 若 market status API 失败则立即 KILL_SWITCH
- **严重程度**: P2
- **位置**: `models/market_context.py:16`（`last_known_market_active: bool = False`）+ `main.py:215-224`
- **现象**: `MarketContext.last_known_market_active` 默认 `False`。初始化时 `AMMInitializer` 检查了 market status 但未将结果写入 `ctx.last_known_market_active`。第一次 `quote_cycle` 会尝试 API 查询，若失败则 fallback 到默认 `False` → `DefenseStack` 判定 `not active` → `KILL_SWITCH`。
- **影响**: 网络瞬断可导致刚启动的 AMM 立即进入 KILL 状态并取消所有订单。
- **修复建议**: `AMMInitializer.initialize()` 中已验证 market 是 active 的，应设置 `ctx.last_known_market_active = True`。

---

#### [A-009] `protocols.py` 中 `set` 方法被定义了两次
- **严重程度**: P3
- **位置**: `cache/protocols.py:20-28` 和 `cache/protocols.py:30-36`
- **现象**: `AsyncRedisLike` Protocol 中 `set` 方法定义了两次——一次返回 `Awaitable[Any]`，一次是 `async def`。Python 中后者覆盖前者。
- **影响**: 类型检查可能不一致，运行时无影响（Protocol 仅用于类型提示）。
- **修复建议**: 删除其中一个，保留 `async def` 版本。

---

#### [A-010] `auto_reinvest` 仅在 STABILIZATION 阶段触发，但条件写反
- **严重程度**: P2
- **位置**: `main.py:170`
- **现象**: `if ctx.phase == Phase.STABILIZATION: await maybe_auto_reinvest(ctx, api)` — 只在 STABILIZATION 阶段再投资。但 phase 更新在 **之后**（Step 1.5）。所以第一次进入 STABILIZATION 的那个 cycle，`ctx.phase` 还是旧值 EXPLORATION，reinvest 不会触发。要到 **下一个** cycle 才生效。
- **影响**: 延迟一个 cycle（2 秒），影响极小。
- **修复建议**: 将 `maybe_auto_reinvest` 移到 phase 更新之后。

---

## 总结

| 严重程度 | 数量 | 编号 |
|----------|------|------|
| P0 | 1 | A-004（Oracle 类型混淆，LVR 检测失效） |
| P1 | 3 | A-003, A-005, A-007 |
| P2 | 3 | A-001, A-002, A-008, A-010 |
| P3 | 2 | A-006, A-009 |

**最高优先级修复**：A-004（Oracle 双类混淆）和 A-005（OrderCache 未实例化）直接影响核心功能完整性。A-007（清盘缺失）影响市场结束时的资金回收。

---

# AMM R3 审查报告

## 范围
- 已检查 `src/amm/` 全部模块。
- 重点追踪了价格、库存、订单、Oracle 数据路径，`WIDEN/ONE_SIDE/KILL/BUY 拦截/τ=0` 五条防线，以及 `quote_cycle` / `reconciler` / `trade_poller` 的并发交互。

## Step 1：数据流图

```text
Orderbook API
  best_bid/best_ask: int cents [1,99]
        |
        v
ThreeLayerPricing.compute()
  anchor: float cents
  micro: float cents or None
  posterior: float cents
  output: int cents [1,99]
        |
        v
ASEngine
  input mid_price: int cents [1,99]
  input sigma: float probability scale /100
  output ask,bid: int cents [1,99]
        |
        +--> Gradient ask ladder: SELL YES @ ask_cents
        |
        +--> Gradient bid ladder: SELL NO @ (100 - bid_cents)
                  output OrderIntent.price_cents: int cents [1,99]
        |
        v
OrderSanitizer
  clamps price to [1,99], blocks direction != SELL
        |
        v
OrderManager.execute_intents()
  JSON payload: {market_id, side, direction, price_cents, quantity}
        |
        v
AMMApiClient.place_order()/replace_order()
  POST /orders or /amm/orders/replace
```

```text
Trades API
  scenario, quantity, price_cents, buyer/seller fees
        |
        v
TradePoller._apply_trade()
  TRANSFER_YES:
    price_cents interpreted as YES cents
  TRANSFER_NO:
    price_cents first interpreted as YES cents, then converted to NO cents via (100 - price)
        |
        v
InventoryCache.adjust()
  Redis hash fields:
  cash_cents / yes_volume / no_volume / yes_cost_sum_cents / no_cost_sum_cents
        |
        v
quote_cycle() reloads Inventory from Redis into ctx.inventory
```

```text
Oracle CLI
  outcomePrices[0]: float normalized 0-1
        |
        v
src/amm/oracle/polymarket.PolymarketOracle.get_price()
  output: float cents 0-100
        |
        v
main._evaluate_oracle_state()
  compares oracle cents vs internal mid cents
  output: OracleState -> DefenseLevel
        |
        X
        no path into ASEngine inputs
        Oracle only gates defense; it does not feed reservation price/spread math
```

### 跨模块传递点核对

| 发送方 | 发送格式 | 接收方 | 期望格式 | 是否一致 | 说明 |
|---|---|---|---|---|---|
| `ThreeLayerPricing.compute` `src/amm/strategy/pricing/three_layer.py:33-63` | `int` cents `[1,99]` | `ASEngine.bernoulli_sigma/compute_quotes` `src/amm/main.py:219-231`, `src/amm/strategy/as_engine.py:35-84` | `mid_price` 为 cents 整数 | 一致 | A-S 输入输出都在 cents 域内。 |
| `ASEngine.compute_quotes` `src/amm/strategy/as_engine.py:67-84` | `(ask_cents, bid_cents)` | `GradientEngine` `src/amm/main.py:233-235`, `src/amm/strategy/gradient.py:8-50` | `int` cents | 一致 | `build_bid_ladder` 会把 YES bid 映射成 `SELL NO @ 100-bid`。 |
| `GradientEngine` `src/amm/strategy/gradient.py:19-48` | `OrderIntent(price_cents:int)` | `OrderSanitizer` `src/amm/risk/sanitizer.py:18-83` | `price_cents` cents, `direction=="SELL"` | 一致 | Sanitizer 只允许 `SELL`。 |
| `OrderSanitizer` `src/amm/risk/sanitizer.py:75-83` | `OrderIntent` | `OrderManager._place_intent/_atomic_replace` `src/amm/connector/order_manager.py:83-184` | `price_cents`/`quantity` 整数 | 一致 | 最终直接转成 API JSON。 |
| `OrderManager` `src/amm/connector/order_manager.py:103-111,155-160` | `{"price_cents": int}` | `AMMApiClient.place_order/replace_order` `src/amm/connector/api_client.py:86-107` | `json.price_cents` | 一致 | 订单提交始终用 cents 整数。 |
| `API get_balance/get_positions` `src/amm/lifecycle/initializer.py:58-75` | `balance_cents`, `yes_volume`, `yes_cost_sum_cents` 等整数 | `Inventory` `src/amm/models/inventory.py:5-37` | cents/shares 整数 | 一致 | 初始化建模一致。 |
| `TradePoller._apply_trade` `src/amm/connector/trade_poller.py:61-118` | `trade["price_cents"]` | `InventoryCache.adjust` `src/amm/cache/inventory_cache.py:66-87` | 成交额 cents、仓位 shares | 条件一致 | `TRANSFER_NO` 路径要求 API 的 `price_cents` 表示 YES 价，再转成 NO 价；代码内是这样假定的。 |
| `InventoryCache.get` `src/amm/cache/inventory_cache.py:43-64` | Redis hash -> `Inventory` | `quote_cycle` `src/amm/main.py:181-185` | `Inventory(cents/shares)` | 一致 | 但后续与内存态存在并发覆盖问题，见 B-003/B-004。 |
| `polymarket.PolymarketOracle.get_price` `src/amm/oracle/polymarket.py:21-48` | `float` cents `0-100` | `_evaluate_oracle_state` `src/amm/main.py:128-161` | `internal_price_cents: float` | 一致 | 单位一致，但它不进入 A-S，只进入 defense。 |

## Step 2：防线核验

| 防线 | 结论 | 证据 |
|---|---|---|
| `WIDEN` | 有效 | `DefenseStack.evaluate()` 可返回 `WIDEN`，`quote_cycle()` 在 `src/amm/main.py:282-288` 用 `widen_factor` 直接放大 `(ask-mid)` / `(mid-bid)`，随后重建梯度订单 `src/amm/main.py:290-295`。 |
| `ONE_SIDE` | 条件失效 | 只有当 `inventory_skew != 0` 时，`OrderSanitizer` 才会真正裁掉一侧 `src/amm/risk/sanitizer.py:50-58`。若 `DefenseStack` 因 PnL 触发 `ONE_SIDE` 而 `skew == 0`，两侧订单都会继续输出。详见 B-001。 |
| `KILL` | 正常路径有效 | `quote_cycle()` 在 `src/amm/main.py:277-280` 调用 `order_mgr.cancel_all()` 并 `return`；`cancel_all()` 会批量撤单、清空 `active_orders`、同步 pending_sell `src/amm/connector/order_manager.py:218-224`。 |
| Sanitizer BUY 拦截 | 当前主路径有效 | 当前唯一调用 `execute_intents()` 的位置是 `src/amm/main.py:298`，且所有 intents 都先经过 `sanitize()` `src/amm/main.py:294`；`sanitize()` 对 `direction != "SELL"` 直接丢弃 `src/amm/risk/sanitizer.py:38-41`。未发现现有主路径可绕过。 |
| `τ = 0` 边界 | 有效 | `ASEngine.optimal_spread()` 在 `tau_hours=0` 时仍保留深度项 `(2/gamma)*ln(1+gamma/kappa)` `src/amm/strategy/as_engine.py:27-33`；实算 `gamma=0.3,kappa=1.5,mid=50` 时，`delta≈121.55`，`compute_quotes()` 返回 `(99,1)`，是极宽价差而非崩溃。 |

## Step 3：单位一致性结论

- A-S 引擎接受 `mid_price` 为 `1..99` 的 cents 整数，输出 `(ask_cents, bid_cents)` 也是整数 cents；`sigma` 只是中间概率尺度浮点，不出模块。
- three-layer pricing 三层混合后的输出是 `int` cents，`src/amm/strategy/pricing/three_layer.py:33-63` 明确 `round()` 后再 `clamp()`。
- `OrderIntent.price_cents` 从生成到提交 API 一直保持 `int` cents：`GradientEngine -> OrderSanitizer -> OrderManager -> AMMApiClient`。
- `InventoryCache` 存的是 shares + cents 整数，与 `Inventory`、`TradePoller`、`Initializer`、`Reconciler` 的建模一致。
- Oracle 的外部原始值是 `0..1` 浮点，但进入系统后在两个 Oracle 实现里都被乘以 `100` 变成 cents；当前运行主路径里 Oracle 只参与 defense 比较，不参与 A-S 定价输入。

## Step 4：并发安全结论

- Python 内存中的 `OrderManager.active_orders` 没有锁，但当前每个 market 只在各自的 `run_market()` 任务内使用；未发现两个独立 asyncio 任务同时读写同一个 `OrderManager.active_orders` 的主路径。
- `InventoryCache.adjust()` 使用 Redis pipeline，默认 `transaction=True`，因此单次 `adjust()` 内的多个 `HINCRBY` 是原子提交的。
- 但系统整体并不具备“库存读-改-写事务”保证：
  - `TradePoller._apply_trade()` 会先 `get()` 再基于旧值算 `cost_basis`，最后 `adjust()`；
  - `AMMReconciler.reconcile()` 同时可能对同一 Redis key 执行整包 `set()`；
  - `quote_cycle()` 每轮又会把 Redis 整包读回 `ctx.inventory`。
- 因此 `quote_cycle` 与 `reconciler` 并发时，确实可能出现脏读/写和覆盖，详见 B-003 / B-004。

## Step 5：问题单

#### [B-001] `ONE_SIDE` 在 PnL 触发且 `skew == 0` 时不会变成单侧报价
- **严重程度**: P1
- **位置**: `src/amm/risk/defense_stack.py:64-67` → `src/amm/risk/sanitizer.py:50-58` → `src/amm/main.py:269-298`
- **现象**: `DefenseStack` 可以因为 PnL 超限返回 `DefenseLevel.ONE_SIDE`，但 `OrderSanitizer` 只根据 `ctx.inventory.inventory_skew` 的正负去裁掉一侧；若 `skew == 0`，`sanitize()` 不会过滤任何 side，`quote_cycle()` 最终仍会输出双侧订单。
- **影响**: 风控链上“ONE_SIDE”防线并不保证真的单侧报价。最典型的是市场亏损已触发防线，但库存刚好平衡时，系统仍继续双边做市，静默放大风险。
- **修复建议**: 不要让 `ONE_SIDE` 依赖 `skew` 推断。把“允许哪一侧”作为显式防御决策从 `DefenseStack` 传出，或在 `DefenseStack` 返回 `ONE_SIDE` 时同时返回目标侧别；`OrderSanitizer` 再按该显式 side 做过滤。

#### [B-002] Oracle 实际运行路径绕过了带 LVR/配置阈值的实现，导致部分防线代码不可达
- **严重程度**: P1
- **位置**: `src/amm/main.py:39,392-401` → `src/amm/oracle/polymarket.py:13-54`，对比期望实现 `src/amm/oracle/polymarket_oracle.py:38-115`
- **现象**: `main.py` 导入并实例化的是 `src/amm/oracle/polymarket.PolymarketOracle`，构造参数是 `market_slug` 字符串；但完整的 `evaluate()/check_stale()/check_lvr()` 逻辑和 `MarketConfig.oracle_*` 阈值都在另一份实现 `polymarket_oracle.py`。当前运行对象没有 `evaluate`、没有 `check_stale`、没有 `check_lvr`，`_evaluate_oracle_state()` 只能走降级分支，导致 `OracleState.LVR` 永远不会在主路径上出现。
- **影响**: 代码里虽然存在 LVR/KILL_SWITCH 防线和 market 级 oracle 配置，但数据路径上并未生效，属于“防线存在但跑不到”。极端行情下，原本设计的 LVR 立即熔断不会触发。
- **修复建议**: 二选一：
  1. 统一只保留 `polymarket_oracle.py` 版本，并在 `main.py` 传入 `MarketConfig` 实例化；
  2. 或把 `polymarket.py` 扩展到与 `polymarket_oracle.py` 相同接口，并确保 `oracle_stale_seconds / oracle_deviation_cents / oracle_lvr_*` 真正从 `MarketConfig` 进入运行时对象。

#### [B-003] `TradePoller` 与 `Reconciler` 对同一 Redis inventory key 存在非事务性读改写竞态
- **严重程度**: P1
- **位置**: `src/amm/connector/trade_poller.py:92-100,110-118` ↔ `src/amm/lifecycle/reconciler.py:58-71` ↔ `src/amm/cache/inventory_cache.py:66-87`
- **现象**: `TradePoller` 在卖出路径里先 `InventoryCache.get()`，基于旧 `Inventory` 计算 `cost_basis`，再 `adjust()`；与此同时 `AMMReconciler` 可能读取同一 key 并用 API 快照整包 `set()` 覆盖 Redis。`adjust()` 自身是原子的，但“先读再算再写”这整个流程不是原子的。
- **影响**: 会出现脏读/写：
  - `cost_basis` 可能基于过期仓位计算；
  - poller 刚写入的成交增量可能被 reconciler 的旧快照覆盖；
  - 下一轮 `quote_cycle()` 从 Redis 读回的 `ctx.inventory` 可能倒退，进而影响可卖数量、PnL、`ONE_SIDE/KILL` 判定。
- **修复建议**: 给每个 `market_id` 增加统一的 asyncio 锁，串行化 `poll -> reconcile -> quote reload` 的库存路径；若必须跨进程一致，再把 `get+cost_basis+adjust` 改成 Redis Lua 或 `WATCH/MULTI/EXEC` 事务。

#### [B-004] `maybe_auto_reinvest()` 只改内存不改 Redis，导致 `quote_cycle` 与 `reconciler`/下轮 reload 状态分叉
- **严重程度**: P2
- **位置**: `src/amm/main.py:183-187` → `src/amm/lifecycle/reinvest.py:24-37` → `src/amm/main.py:183-185` / `src/amm/lifecycle/reconciler.py:46-71`
- **现象**: `quote_cycle()` 每轮先从 Redis 把 `ctx.inventory` 刷新为缓存真值；随后 `maybe_auto_reinvest()` 只修改 `ctx.inventory` 内存对象，没有调用 `InventoryCache.adjust()/set()` 回写 Redis。这样同一 market 会同时存在“内存已 mint”与“Redis 未 mint”的两个库存版本。
- **影响**: 下一轮 `quote_cycle()` 或 `reconciler` 会把 Redis 旧值重新覆盖回 `ctx.inventory`。如果 API 余额/仓位回写有延迟，系统可能在稳定期重复判断“现金仍然过多”，重复发起 mint，或在下单时用到与 Redis 不一致的可用仓位。
- **修复建议**: `maybe_auto_reinvest()` 成功 mint 后立即同步 `InventoryCache.adjust()`；或者彻底取消内存直改，统一以 API/Redis 回放为唯一库存真值。

## 额外核验说明

- `WIDEN` 路径我确认是生效的，且 `widen_factor` 会实际作用到最终梯度报价。
- `KILL` 在正常 API 成功路径上会停止本轮后续报价并执行批量撤单。
- BUY intent 绕过 sanitizer 的主路径未发现。
- `τ=0` 边界已用实际公式代入验证，结果为超宽价差 `(99,1)`，不是异常值或默认值。
