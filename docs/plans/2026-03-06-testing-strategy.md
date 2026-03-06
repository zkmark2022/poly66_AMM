# AMM Bot 完备测试策略

> **日期**: 2026-03-06  
> **背景**: 两轮 Code Review（32个bug + R2修复）揭示了测试盲区：组件各自通过，但装配层存在严重的静默失效。  
> **目标**: 建立一套在"测试通过"时能提供真实信心的测试体系。

---

## 为什么之前的测试没有抓住这些 Bug

| Bug 类型 | 数量 | 根因 |
|---------|------|------|
| 组件未接入主循环（Reconciler/PhaseManager/Health） | 3 | 单测只测类，不测 amm_main() 调用链 |
| elif 死锁（Oracle 永久 ONE_SIDE） | 1 | 单测 mock 了 Oracle，没跑真实控制流 |
| τ=0 falsy | 1 | 未覆盖临界输入 |
| WIDEN 无实际效果 | 1 | 测了状态机迁移，没测最终 spread 数值 |

**核心结论：单元测试和 API 测试覆盖了组件和接口，但没有覆盖"Bot 整体运行时行为"。**

---

## 四层测试架构

```
┌──────────────────────────────────────────────────────┐
│  Layer 4: 端到端验收测试（UI + 真实后端）             │ ← 少量关键路径
│  • 验证用户看到的结果是否正确                          │
│  • 不用来测算法逻辑                                   │
├──────────────────────────────────────────────────────┤
│  Layer 3: Bot 行为仿真测试 ← 核心缺口，重点补          │ ← 当前最高优先级
│  • Mock 交易所 API + fakeredis                        │
│  • 直接运行真实 amm_main() / quote_cycle()            │
│  • 断言实际下单行为和状态转换                          │
├──────────────────────────────────────────────────────┤
│  Layer 2: API 契约测试（后端 HTTP）                   │ ← 已有，维护即可
│  • 验证 API 接口正确响应                              │
│  • 与算法逻辑解耦                                     │
├──────────────────────────────────────────────────────┤
│  Layer 1: 单元 + 属性测试                             │ ← 已有，补边界值
│  • 组件内部逻辑                                       │
│  • Hypothesis 属性测试（算法不变量）                   │
└──────────────────────────────────────────────────────┘
```

---

## Layer 3：Bot 行为仿真测试（最高优先级）

### 技术方案

```python
# tests/simulation/conftest.py

import fakeredis.aioredis
import httpx
import pytest
from src.amm.main import amm_main, quote_cycle
from src.amm.config.models import MarketConfig

@pytest.fixture
def mock_exchange():
    """Mock 交易所 API，记录所有下单请求"""
    orders_placed = []
    
    def handle_request(request):
        if request.url.path == "/orders":
            body = json.loads(request.content)
            orders_placed.append(body)
            return httpx.Response(200, json={"order_id": "mock-123"})
        # ... 其他端点
    
    transport = httpx.MockTransport(handle_request)
    return httpx.AsyncClient(transport=transport), orders_placed

@pytest.fixture
def fake_redis():
    return fakeredis.aioredis.FakeRedis()
```

### 核心测试场景

#### T-SIM-01：正常报价周期（Smoke Test）
```
场景：库存充足，Oracle 正常，无防御触发
期望：
  - 每个周期产生 YES 卖单 + NO 卖单
  - 下单价格在合理范围（10-90 cents）
  - PhaseManager 被调用（阶段随成交推进）
  - Reconciler 在 5 分钟内运行一次
```

#### T-SIM-02：Oracle 死锁回归测试
```
场景：启动时 Oracle last_update=None（模拟初始状态）
期望：
  - 第一个 quote_cycle 不进入 ONE_SIDE
  - Oracle 完成第一次 get_price() 后，正常双边报价
  - （此测试在修复前会失败，修复后才通过）
```

#### T-SIM-03：WIDEN 防线有效性
```
场景：触发 WIDEN（库存倾斜超阈值）
期望：
  - DefenseLevel 变为 WIDEN
  - 下单 spread 比 NORMAL 状态扩大 ≥ 1.5x
  - 具体数值断言，不只是状态断言
```

#### T-SIM-04：KILL 触发后停止报价
```
场景：库存损失超过 KILL 阈值
期望：
  - 进入 KILL 状态
  - 后续 quote_cycle 不产生任何新订单
  - 存量订单被撤销
```

#### T-SIM-05：τ=0 临近到期
```
场景：remaining_hours=0.0
期望：
  - A-S 引擎正常运行（不 crash）
  - 产生的 spread 明显大于 τ=24 时的 spread
```

#### T-SIM-06：组件启动完整性
```
场景：运行 amm_main() 60 秒后停止
期望：
  - Reconciler 至少运行一次
  - Health Server 响应 /health 为 healthy
  - PhaseManager 被初始化且阶段不永远停留在 EXPLORATION
```

#### T-SIM-07：进程重启后状态恢复
```
场景：运行 N 个周期 → 停止 → 重新启动
期望：
  - 重启后库存数据从 Redis 恢复
  - 下单行为与重启前连续（不重置为初始状态）
```

#### T-SIM-08：Oracle 滞后 → PASSIVE_MODE → 恢复
```
场景：Oracle 连续 3 秒不返回数据 → 然后恢复正常
期望：
  - 进入 PASSIVE_MODE（spread 扩大）
  - Oracle 恢复后，自动退出 PASSIVE_MODE
  - 恢复后重新双边正常报价
```

---

## Layer 1 补充：属性测试（Hypothesis）

### 算法不变量（任意输入下必须成立）

```python
from hypothesis import given, strategies as st

@given(
    yes_vol=st.integers(min_value=0, max_value=10000),
    no_vol=st.integers(min_value=0, max_value=10000),
    cash=st.integers(min_value=0, max_value=10_000_000),
    tau=st.floats(min_value=0.0, max_value=720.0),
    mid_price=st.floats(min_value=0.01, max_value=0.99),
)
def test_amm_never_buys(yes_vol, no_vol, cash, tau, mid_price):
    """AMM 在任何库存状态下，永远不产生 BUY 方向的 OrderIntent"""
    inventory = Inventory(yes_vol, no_vol, cash, ...)
    ctx = make_ctx(inventory=inventory, tau=tau)
    intents = quote_cycle_sync(ctx, mid_price=mid_price)
    for intent in intents:
        assert intent.direction == "SELL", f"Got BUY intent: {intent}"

@given(tau=st.floats(min_value=0.0, max_value=720.0))
def test_spread_positive_for_any_tau(tau):
    """任意 τ（包括 0），A-S spread 必须 > 0"""
    spread = as_engine.calculate_spread(tau=tau, ...)
    assert spread > 0

@given(tau_small=st.floats(min_value=0.0, max_value=1.0),
       tau_large=st.floats(min_value=24.0, max_value=720.0))
def test_spread_widens_as_tau_decreases(tau_small, tau_large):
    """τ 越小（越接近到期），spread 应越大"""
    spread_small = as_engine.calculate_spread(tau=tau_small, ...)
    spread_large = as_engine.calculate_spread(tau=tau_large, ...)
    assert spread_small >= spread_large

@given(price=st.floats(min_value=0.01, max_value=0.99))
def test_sanitizer_rejects_buy_always(price):
    """Sanitizer 必须拒绝任何 BUY 方向的 intent"""
    intent = OrderIntent(direction="BUY", price=price, ...)
    result = sanitizer.sanitize(intent)
    assert result is None
```

### 边界值测试（补充现有单测）

| 输入 | 测试点 | 预期行为 |
|------|--------|---------|
| `tau=0.0` | A-S 引擎 | 不 crash，spread 为合理大值 |
| `cash_cents=0` | quote_cycle | 停止 BUY 挂单，不抛异常 |
| `yes_volume=0` | Sanitizer | 拒绝 SELL YES 订单 |
| `cash = reinvest_threshold` | Reinvest | 恰好在阈值上，触发 mint |
| `cash = reinvest_threshold - 1` | Reinvest | 恰好在阈值下，不触发 mint |
| `deviation = threshold` | Oracle | 恰好触发防御 |
| `deviation = threshold - epsilon` | Oracle | 不触发防御 |

---

## Layer 4：端到端验收测试（UI）

**范围**：仅验证用户可见的关键路径，不用来测算法。

| 测试场景 | 验证点 |
|---------|--------|
| AMM 机器人运行中，订单簿有挂单 | 前端显示 AMM 订单 |
| 用户下单成交 AMM 挂单 | AMM 持仓变化，用户余额变化 |
| AMM 进入 KILL 状态 | 订单簿 AMM 挂单消失 |

**执行频率**：每次 main 合并后运行，不需要每次 PR 都跑。

---

## 执行顺序

```
当前状态
  │
  ├─ [已完成] R1 Code Review (Opus + Codex 分模块)
  ├─ [已完成] R2 Fix + 合并 (PR #27-30, 280 tests pass)
  │
  ▼
Step 1: R3 Code Review（全 context，装配层专项）
  └─ 文档: docs/plans/2026-03-06-r3-review-prompt.md
  └─ 产出: /tmp/amm-r3/r3-combined-report.md
  └─ 验收: P0 bug 数量 = 0 才能进入下一步
  │
  ▼
Step 2: 修复 R3 发现的 P0/P1 问题
  └─ 按 agent-swarm skill 启动修复 agent
  │
  ▼
Step 3: Layer 3 Bot 行为仿真测试（新建）
  └─ 测试文件: tests/simulation/
  └─ 必须通过: T-SIM-01 ~ T-SIM-08
  │
  ▼
Step 4: Layer 1 属性测试补充
  └─ Hypothesis 不变量测试
  └─ 边界值补充
  │
  ▼
Step 5: API 契约测试回归（已有，确认仍然通过）
  │
  ▼
Step 6: Layer 4 端到端验收（少量关键路径）
  │
  ▼
✅ 测试完成，AMM Bot 可以进入生产部署
```

---

## 工作量估算

| 步骤 | 工作量 | 可以 Agent 自动完成？ |
|------|--------|---------------------|
| R3 Review | 1-2h | ✅ 是（spawn 2 agents） |
| R3 Bug 修复 | 视发现问题数 | ✅ 是（agent-swarm） |
| Layer 3 仿真测试框架 | 4-6h | ✅ 是（spawn 1 agent） |
| Layer 3 测试用例（T-SIM-01~08） | 包含在上面 | ✅ 是 |
| Layer 1 属性测试 | 2-3h | ✅ 是 |
| 端到端验收 | 1-2h | ⚠️ 部分（需要环境） |

**全部 Layer 1-3 可以在一次 Agent Swarm 中完成。**
