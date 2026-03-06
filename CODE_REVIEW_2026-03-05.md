# AMM 代码审查汇总报告
> **日期**: 2026-03-05
> **审查者**: Opus 4.6 (策略/风控/主循环) + Codex GPT 5.4 (连接器/缓存/生命周期/配置/Oracle)
> **项目**: poly66-amm | GitHub: https://github.com/zkmark2022/poly66_AMM
> **基线**: 147 tests ✅ | ruff ✅ | pyright 12 errors ❌

---

## 📊 总览

| 严重程度 | Opus 发现 | Codex 发现 | 合计 |
|----------|-----------|-----------|------|
| P0 (Critical) | 2 | 2 | **4** |
| P1 (High) | 4 | 7 | **11** |
| P2 (Medium) | 8 | 4 | **12** |
| P3 (Low) | 3 | 2 | **5** |
| **总计** | **17** | **15** | **32** |

---

## 🔴 P0 Critical — 生产前必须修复

### 1. Oracle 集成死锁 — 永久 ONE_SIDE 模式
- **来源**: Opus O-001 + Codex C-001 (双方独立发现)
- **文件**: `main.py:94-107`, `oracle/polymarket.py:50-54`
- **问题**: `check_lag()` 在 `last_update is None` 时返回 `True`（启动即触发）。由于 `check_deviation()` 在 `elif` 分支中，永远不执行。而 `get_price()` 只在 `check_deviation()` 内调用，导致 `last_update` 永远为 `None`。
- **影响**: 启用 Oracle 后，bot 永远只单边报价，流动性崩溃。
- **修复**: 初始化时调用 `get_price()`，或将 `check_lag` 和 `check_deviation` 改为独立检查（不用 elif）。
- **工作量**: 小

### 2. τ=0.0 被当作 falsy — 到期时 τ 回退到 24h
- **来源**: Opus O-002
- **文件**: `main.py:74`
- **问题**: `tau = ctx.config.remaining_hours_override or 24.0`。Python 的 `or` 把 `0.0` 当 falsy。
- **影响**: 临近到期时，A-S 模型应大幅收窄价差（τ→0），但实际用了 τ=24，导致最关键时刻定价严重偏差。
- **修复**: 改为 `if ctx.config.remaining_hours_override is not None else 24.0`
- **工作量**: 极小

### 3. 5 分钟对账循环从未启动
- **来源**: Codex C-002 + Opus O-014
- **文件**: `main.py` (缺失), `lifecycle/reconciler.py`
- **问题**: `AMMReconciler` 已实现但 `amm_main()` 从未创建或调度它。
- **影响**: Redis 缓存可以无限期偏离数据库真实状态，AMM 基于错误库存数据运行。
- **修复**: 在 `amm_main()` 中创建定期对账任务。
- **工作量**: 小

### 4. Redis 类型错误 — 12 个 pyright 错误
- **来源**: Codex 类型分析
- **文件**: `cache/order_cache.py`, `cache/inventory_cache.py`, `config/loader.py`, `main.py:204-205`
- **根因**: `redis.asyncio.Redis` 的类型存根将方法解析为非 awaitable 返回类型。`main.py` 动态设置 `MarketContext` 未声明的属性。
- **修复**: (1) 定义 `AsyncRedisLike` Protocol; (2) 将 oracle 阈值移入 `MarketContext` 或 `MarketConfig` 的声明字段。
- **工作量**: 中

---

## 🟠 P1 High — 高优先级

### 5. WIDEN 防线无实际效果
- **来源**: Opus O-003
- **文件**: `risk/sanitizer.py:67-68`, `strategy/as_engine.py`
- **问题**: `widen_factor=1.5` 存在但从未被应用到价差计算中。WIDEN 等同于 NORMAL。
- **修复**: A-S 引擎接受 spread 乘数，或梯度引擎在 WIDEN 时调整价格。
- **工作量**: 中

### 6. PhaseManager 从未集成到报价循环
- **来源**: Opus O-004
- **文件**: `main.py` (缺失), `strategy/phase_manager.py`
- **问题**: PhaseManager 已定义但从未实例化/调用。`ctx.phase` 永远是 `EXPLORATION`。
- **影响**: 三层定价永远使用探索期权重 (0.6, 0.3, 0.1)，不会学习市场活动。
- **修复**: 每个市场创建 PhaseManager，每个周期调用 `update()`。
- **工作量**: 小

### 7. Sanitizer 不拒绝 BUY 方向订单
- **来源**: Opus O-005
- **问题**: 核心业务规则「AMM 永不买入」缺少最后防线检查。
- **修复**: 添加 `if intent.direction != "SELL": return None`
- **工作量**: 极小

### 8. MarketContext 缺少 Oracle 阈值字段
- **来源**: Opus O-006 + Codex 类型分析
- **问题**: `main.py:204-205` 动态注入 `oracle_lag_threshold` / `oracle_deviation_threshold`，pyright 报错。
- **修复**: 在 `MarketContext` dataclass 中声明这两个字段。
- **工作量**: 极小

### 9. OrderManager 忽略 OrderIntent.action，不执行原子替换
- **来源**: Codex H-001
- **文件**: `connector/order_manager.py:29-70`
- **问题**: `execute_intents()` 只做 cancel-stale/place-missing 对比，忽略 `REPLACE` 语义。
- **影响**: 数量变化、部分成交、替换语义丢失。
- **修复**: 尊重 `PLACE/CANCEL/REPLACE` action，使用 API replace 端点。
- **工作量**: 中

### 10. 活跃订单状态未持久化
- **来源**: Codex H-002
- **问题**: `OrderCache` 存在但未使用。`OrderManager.active_orders` 纯内存。进程重启丢失所有订单状态。
- **修复**: 通过 `OrderCache` 持久化，启动时加载，定期对账。
- **工作量**: 中

### 11. Redis 配置覆盖的类型转换不安全
- **来源**: Codex H-003
- **文件**: `config/loader.py:44-49`
- **问题**: `bool("false")` → `True`；`NoneType("12")` 抛异常。
- **修复**: 基于字段注解解析，显式处理 bool/optional/enum。
- **工作量**: 中

### 12. 初始化器部分失败时留下脏 Redis 状态
- **来源**: Codex H-004
- **问题**: Inventory 在 mint 完成前写入 Redis。如果 mint 失败，Redis 有错误快照。
- **修复**: 先本地构建，全部成功后再写 Redis。
- **工作量**: 小

### 13. Health Server 未集成
- **来源**: Codex H-005
- **问题**: `run_health_server()` 和 `HealthState` 已实现但未在 `amm_main()` 中启动。
- **修复**: 启动 health server 后台任务，更新 readiness 状态。
- **工作量**: 小

### 14. 交易去重集合无限增长
- **来源**: Codex H-006
- **文件**: `connector/trade_poller.py:17-18`
- **问题**: `_processed_ids` 是全局 set，无淘汰机制，无 per-market 分区。
- **修复**: 按市场分区 + 基于游标/窗口/TTL 限制大小。
- **工作量**: 小

### 15. API 客户端无瞬态重试策略
- **来源**: Codex H-007
- **问题**: 只重试 401/429，不处理 5xx、超时、连接失败。
- **修复**: 添加 5xx/timeout/transport error 的有界重试 + jitter。
- **工作量**: 中

---

## 🟡 P2 Medium — 中优先级

| # | Issue | 来源 | 文件 | 简述 |
|---|-------|------|------|------|
| 16 | Posterior 层是 VWAP 不是贝叶斯 | O-007 | pricing/posterior.py | 设计要求 Beta-Binomial 贝叶斯更新 |
| 17 | Micro 层无防欺诈/薄订单簿检测 | O-008 | pricing/micro.py | 仅计算 mid，无量级检查 |
| 18 | Budget 管理器无耗尽告警/执行 | O-009 | risk/budget_manager.py | P&L 追踪但无阈值动作 |
| 19 | asyncio.gather 静默吞异常 | O-010 | main.py:237 | return_exceptions=True 但未检查结果 |
| 20 | ONE_SIDE 无折价抛售逻辑 | O-011 | risk/sanitizer.py | 只抑制不调价 |
| 21 | Phase 权重硬编码 | O-012 | pricing/three_layer.py | 应移入 MarketConfig |
| 22 | γ tier 无动态调整 | O-013 | config/models.py | 应按市场年龄自动切换 |
| 23 | Shutdown 仅协作式，无超时 | M-001 | main.py | 无任务取消，SIGTERM 延迟无界 |
| 24 | Oracle 更新间隔配置未使用 | M-002 | oracle.yaml / main.py | update_interval_seconds 未消费 |
| 25 | Token 刷新忽略轮换 refresh token | M-003 | connector/auth.py | 长期运行后失效 |
| 26 | 启动不验证市场状态 | M-004 | lifecycle/initializer.py | 可能对已关闭市场报价 |
| 27 | Reconciler drift 阈值未使用 | L-002 | lifecycle/reconciler.py | _DRIFT_THRESHOLD 是死代码 |

---

## 🟢 P3 Low — 低优先级

| # | Issue | 来源 | 简述 |
|---|-------|------|------|
| 28 | reservation_price 类型注解不一致 | O-015 | float vs int |
| 29 | AnchorPricing.__init__ 存储未使用参数 | O-016 | 死代码 |
| 30 | ceiling_div 负除数行为错误 | O-017 | 缺断言保护 |
| 31 | MarketContext.active_orders 无类型 | L-001 | 裸 dict |
| 32 | Reconciler _DRIFT_THRESHOLD 死配置 | L-002 | 声明未使用 |

---

## ✅ 正面发现（两位审查者共同认可）

1. **A-S 公式正确** — 保留价格和最优价差完全匹配规范，`×100` 单位转换正确
2. **梯度映射正确** — Bid → SELL NO @ (100-P)，Ask → SELL YES，`direction="SELL"` 始终保持
3. **整数数学纪律** — ceil/floor 不用 round，价格钳位 [1,99]，价差交叉保护
4. **伯努利 σ 边界处理** — p 钳位到 [0.01, 0.99]，避免 σ=0
5. **防御栈升降级** — 使用显式严重度映射 + 冷却去抖
6. **库存感知 sanitizer** — 按可用库存钳位数量
7. **优雅停机** — 不遗留孤儿订单
8. **交易 ID 消毒** — 防路径遍历/参数注入
9. **整数成本基础** — 避免浮点漂移

---

## 🔧 修复计划（建议执行顺序）

### Phase 1: P0 Critical (立即修复)
```
1. Oracle 死锁修复 — 初始化调 get_price() + 独立 check 逻辑
2. τ=0.0 falsiness 修复 — `is not None` 替代 `or`
3. Reconciler 集成 — amm_main() 启动定期对账任务
4. 类型错误修复 — AsyncRedisLike Protocol + MarketContext 声明字段
```

### Phase 2: P1 High (本周修复)
```
5. WIDEN 价差乘数接入
6. PhaseManager 集成
7. Sanitizer BUY 拒绝
8. MarketContext oracle 字段声明
9. OrderManager atomic replace
10. OrderCache 持久化
11. Config loader 类型安全
12. Initializer 原子性
13. Health server 集成
14. Trade 去重限制
15. API 重试策略
```

### Phase 3: P2 Medium (后续迭代)
```
16-27. 按优先级逐步修复
```

---

## 📋 设计 vs 实现完整度

| 设计功能 | 实现状态 |
|----------|----------|
| A-S 核心公式 | ✅ 完成 |
| 三层定价 | ⚠️ 框架完成，Posterior/Micro 简化 |
| 梯度挂单 | ✅ 完成 |
| 两阶段策略 | ❌ PhaseManager 未集成 |
| γ 生命周期 | ❌ 静态配置 |
| 三道防线 | ⚠️ WIDEN 无效果 |
| 预算管理 | ⚠️ 追踪但无执行 |
| REST 轮询同步 | ✅ 完成 |
| 定期对账 | ❌ 未启动 |
| 优雅停机 | ✅ 完成 |
| 健康检查 | ❌ 未集成 |
| Oracle 集成 | ❌ 启动即死锁 |
| 原子改单 | ❌ 未实现 |
| 配置热更新 | ⚠️ 类型转换不安全 |
| 反欺诈微观定价 | ❌ 未实现 |
| 贝叶斯后验更新 | ❌ 用 VWAP 替代 |
| 折价抛售 | ❌ 未实现 |

> **完成度评估**: 核心算法约 70% 完成（公式正确但管道未全部连通），生命周期/基础设施约 50% 完成。

---

*报告生成于 2026-03-05 | Opus 4.6 + Codex GPT 5.4 双 AI 审查*
