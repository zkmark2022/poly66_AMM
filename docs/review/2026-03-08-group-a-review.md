# Group A (T-SIM-01/03/05) 仿真测试代码审查报告

**日期**: 2026-03-08
**分支**: `feat/sim-agent-a`
**审查者**: Claude Sonnet 4.6 (sim-review-b agent)
**范围**: `tests/simulation/group_a/` — 稳态场景 (Group A)

---

## 一、测试运行结果

```
platform darwin -- Python 3.12.12, pytest-9.0.2
asyncio: mode=Mode.AUTO

collected 34 items

tests/simulation/group_a/test_sim_01_normal_cycle.py  7 passed
tests/simulation/group_a/test_sim_03_widen_defense.py 5 passed
tests/simulation/group_a/test_sim_05_tau_zero.py     22 passed

============================== 34 passed in 0.14s ==============================
```

**注意**: 测试全部通过，但需要同时 checkout `tests/simulation/conftest.py` 和 `tests/simulation/__init__.py`。仅 checkout `tests/simulation/group_a/` 会导致所有测试以 `ModuleNotFoundError` 失败（三个文件均使用 `from tests.simulation.conftest import ...` 直接模块导入）。

---

## 二、总体质量评估

**综合得分: 7 / 10**

| 维度 | 得分 | 说明 |
|------|------|------|
| 断言完整性 | 7/10 | 核心不变量覆盖良好，但价格边界过于宽松 |
| Mock 质量 | 8/10 | patch 路径正确，无状态泄漏，隔离良好 |
| 边界条件 | 5/10 | 缺少阈值临界测试、负 tau、WIDEN 解除 |
| 代码规范 | 8/10 | 结构清晰，注释充分，一个魔法数字问题 |
| 公式正确性 | 9/10 | T-SIM-05 主动修正了 spec 错误（加分项） |

---

## 三、各场景详细审查

### T-SIM-01: 正常报价周期 (`test_sim_01_normal_cycle.py`)

**7 个测试用例，全部通过**

#### 断言质量

**YES+NO 卖单（"各至少 1 张"）**

- `test_yes_sell_orders_submitted` 断言 `len(yes_sells) >= 1` ✓
- `test_no_sell_orders_submitted` 断言 `len(no_sells) >= 1` ✓
- 两个测试各自调用独立的 `_run_cycle()`，而非同一次运行中同时验证两侧。这不是 bug，但意味着理论上存在一次运行只产生一侧订单而两个测试分别通过的可能（概率极低但需注意）。

**无 BUY 方向订单（AMM 核心不变量）**

- `test_no_buy_direction_orders` 明确断言 `buy_orders == []` ✓
- 额外的 `test_all_submitted_orders_are_sell_direction` 提供互补验证 ✓
- **覆盖完整，是所有测试中最关键的不变量验证。**

**价格范围 [10, 90]**

- 配置参数: `anchor=50, spread_min=2, spread_max=30, gradient_levels=3, gradient_price_step=1`
- 实际预期价格范围: YES ≈ `[50 - δ/2 - 2, 50 + δ/2 + 2]`，即大约 `[36, 64]`
- `[10, 90]` 允许两侧各 ≈25 cents 的误差空间，能容忍大量 bug（如梯度计算错误导致的偏移）而不被捕获
- **P1: 建议收紧到 `[35, 65]`（balanced inventory + anchor=50 条件下）**

**PhaseManager.update 调用验证**

- 使用 `patch.object(phase_mgr, "update", wraps=phase_mgr.update)` 正确检测真实调用 ✓
- 使用 `assert_called_once()` 严格验证恰好调用一次 ✓

**reconcile_loop 验证**

- mock sleep 中设置 `ctx.shutdown_requested = True` 使循环在首次 reconcile 后终止
- 与 `main.py:425` 中 `while not any(ctx.shutdown_requested ...)` 逻辑正确匹配 ✓
- 但此测试 mock 了 `reconciler.fetch_balance` 的返回值为自动 AsyncMock（未显式设置），可能在 `AMMReconciler` spec 变更时静默失败

#### 遗漏边界

| 场景 | 优先级 | 说明 |
|------|--------|------|
| Oracle 返回 p=0.5 精确对称时，YES/NO 价格是否镜像 | P2 | 当前 mock 返回 `best_bid=48, best_ask=52`（非对称），未测试完全对称情形 |
| 同一次 `_run_cycle()` 中同时断言 YES≥1 AND NO≥1 | P2 | 两个独立用例理论上可以各自通过但掩盖单侧失效 |

---

### T-SIM-03: WIDEN 防御有效性 (`test_sim_03_widen_defense.py`)

**5 个测试用例，全部通过**

#### 断言质量

**spread ≥ 1.5× 倍数来源**

- `_skewed_ctx()` 显式传入 `make_config(widen_factor=1.5, ...)` ✓
- `make_config` 对应字段默认值也是 `1.5`（`src/amm/config/models.py:57`）
- 但测试中使用本地变量 `widen_factor = 1.5`（硬编码），而非读取 `ctx_widen.config.widen_factor`
- **P1: 应改为 `widen_factor = ctx_widen.config.widen_factor`，防止配置默认值变更时断言与实际行为脱节**

**WIDEN 解除后 spread 是否缩小**

- 当前测试只验证"WIDEN 激活时 spread ≥ 1.5×"，未测试"库存恢复平衡后 spread 是否回落"
- **P1: 缺少 WIDEN→NORMAL 的状态转换验证**

**`compute_effective_spread` 逻辑**

- `effective_ask = min(yes_prices)`，`effective_bid = 100 - min(no_prices)` — 计算最紧的 bid-ask spread ✓
- 对于多层梯度订单，min(yes) 是最优挂单，`100 - min(no)` 是最优 bid 等价，符合语义 ✓
- 函数返回类型标注为 `int` 但 `-1`（无数据时）不应混入断言逻辑，当前各测试有前置断言 `spread > 0` 保护 ✓

**价格有效性范围**

- WIDEN 场景断言价格在 `[1, 99]`，比 T-SIM-01 的 `[10, 90]` 更宽，可接受（WIDEN 下 spread 较大）

#### 遗漏边界

| 场景 | 优先级 | 说明 |
|------|--------|------|
| 阈值临界: skew=0.29 (刚好 < 阈值，应保持 NORMAL) | P1 | 当前只测 0.0 和 0.50，跨越阈值的临界检测缺失 |
| 阈值临界: skew=0.30 (精确等于阈值，行为取决于 `>` vs `>=`) | P1 | 同上，边界语义未验证 |
| WIDEN 解除后 spread 恢复到 ≤ normal × 1.1 | P1 | 状态转换完整性 |
| inventory_skew_one_side 上限约束 (不应超升为 ONE_SIDE) | P2 | 配置中 one_side=0.70，当前 skew=0.50，但未显式断言 defense_level != ONE_SIDE |

---

### T-SIM-05: τ=0 近到期场景 (`test_sim_05_tau_zero.py`)

**22 个测试用例（4×5 参数化 + 2 独立），全部通过**

#### 断言质量

**参数化 tau 覆盖**

- `TAU_VALUES = [0.0, 0.001, 0.1, 24.0]` ✓ 覆盖零值、极小值、中间值、正常值

**规格说明错误修正（加分项）**

- 原始 spec 声称 "τ=0 的 spread ≥ 2× τ=24 的 spread"
- 测试文件注释明确指出这在 A-S 公式下数学上不可能：`δ(τ) = (γσ²τ + depth_component) × 100`，τ=24 项更大，故 `spread(τ=24) ≥ spread(τ=0)`
- 测试 `test_tau24_spread_geq_tau0_spread` 验证了正确的公式性质 ✓
- **这是高质量的主动纠错，说明实现者真正理解了 A-S 模型**

**`test_tau_zero_reservation_price_centered_on_mid`**

- 使用偏斜库存 (yes=600, no=200) + `tau=0` 验证库存调整项消失
- 断言 `|quote_mid - 50| <= 5`（±5 cents 容差）
- 容差较宽（是 spread_min=2 的 2.5 倍）；有效但欠精确。P2 可收紧到 ±3

#### 遗漏边界

| 场景 | 优先级 | 说明 |
|------|--------|------|
| `tau < 0`（负数）的行为保护 | P1 | 目前无保护测试；A-S 公式中 `sqrt(τ)` 如果 τ 为负会产生 NaN 或异常 |
| `tau=inf` 或极大值 | P2 | 数值溢出边界 |
| `spread(τ=0.001) ≈ spread(τ=0.0)` 的连续性 | P2 | 验证极小 tau 不会产生跳变 |

---

## 四、P0 问题清单

本次审查**未发现 P0 问题**（测试均通过，核心逻辑正确）。

> **注意（部署 P0）**: 若按任务工作流仅 checkout `tests/simulation/group_a/` 而不 checkout `tests/simulation/`，所有测试将以 `ModuleNotFoundError: No module named 'tests.simulation.conftest'` 失败。这是工作流文档问题，非代码问题。

---

## 五、P1 建议

| 编号 | 文件 | 问题 | 建议修复 |
|------|------|------|---------|
| P1-01 | `test_sim_01_normal_cycle.py:84` | 价格断言 [10, 90] 过于宽松 | 改为 `[35, 65]`（balanced inventory + anchor=50 配置下） |
| P1-02 | `test_sim_03_widen_defense.py:111` | `widen_factor = 1.5` 硬编码 | 改为 `widen_factor = ctx_widen.config.widen_factor` |
| P1-03 | `test_sim_03_widen_defense.py` | 缺少 WIDEN 解除后 spread 恢复测试 | 新增 `test_widen_spread_recovers_after_rebalance` |
| P1-04 | `test_sim_03_widen_defense.py` | 缺少阈值临界测试（skew=0.29, 0.30, 0.31） | 参数化测试 `skew_threshold_boundary` |
| P1-05 | `test_sim_05_tau_zero.py:74` | `TAU_VALUES` 缺少负值 | 添加 `tau=-1.0`，验证抛出 `ValueError` 或有保护处理 |
| P1-06 | `conftest.py` | 作为常规模块导入而非 pytest fixture 模块 | 将 helper 函数移到 `tests/simulation/helpers.py`，避免混淆 pytest conftest 机制 |

---

## 六、Edge Cases 补充建议

### T-SIM-01
```python
async def test_both_sides_in_single_cycle(self) -> None:
    """在同一次运行中验证 YES >=1 AND NO >=1（而非两个独立运行）。"""
    _, order_mgr, _ = await self._run_cycle()
    yes_sells = [i for i in order_mgr.all_intents if i.side == "YES" and i.direction == "SELL"]
    no_sells  = [i for i in order_mgr.all_intents if i.side == "NO"  and i.direction == "SELL"]
    assert len(yes_sells) >= 1 and len(no_sells) >= 1, ...
```

### T-SIM-03
```python
@pytest.mark.parametrize("skew_yes,skew_no,expected_level", [
    # skew = (yes - no) / (yes + no)
    # yes=329, no=200 → skew = 129/529 ≈ 0.244 (NORMAL, below 0.30 threshold)
    # yes=399, no=200 → skew = 199/599 ≈ 0.332 (WIDEN, above 0.30 threshold)
    (329, 200, DefenseLevel.NORMAL),   # just below threshold
    (399, 200, DefenseLevel.WIDEN),    # just above threshold
])
async def test_widen_threshold_boundary(self, skew_yes, skew_no, expected_level):
    ...
```

### T-SIM-05
```python
@pytest.mark.asyncio
async def test_negative_tau_is_rejected() -> None:
    """tau < 0 应该抛出 ValueError 或产生安全的 fallback（而非 NaN）。"""
    ctx = _ctx_for_tau(-1.0)
    services, _ = make_real_services(ctx)
    with pytest.raises((ValueError, ZeroDivisionError)):
        await quote_cycle(ctx, **services)
    # 或者：如果实现选择 clamp(tau, 0, inf)，断言结果等同于 tau=0
```

---

## 七、总结

Group A 稳态场景测试整体质量良好：

- **优点**: 核心 AMM 不变量（无 BUY 方向）有明确断言；PhaseManager/reconciler 调用有精确的 spy 验证；T-SIM-05 主动修正了规格说明错误，体现出对 A-S 公式的深入理解；mock 隔离干净，无状态泄漏。
- **不足**: 价格边界断言过于宽松；缺少阈值临界和状态转换测试；`tau < 0` 未被保护；widen_factor 硬编码。

建议在合并前优先修复 P1-01（价格范围）和 P1-05（负 tau 保护），其余 P1 可在后续迭代中补充。
