# Layer 3 Simulation Final Analysis

日期: 2026-03-08
分支目标: `feat/sim-layer3-final`

## 总体裁决

- T-SIM-01/03/05 以 Agent A 为主，因为这些测试已可运行，且对 A-S 定价与核心 AMM 不变量理解更准确。
- T-SIM-02/04/06/07/08 以 Agent B 为素材重写，因为场景选择对，但原实现存在结构性 P0 和若干“看起来像测试、实际上没验证业务”的弱断言。
- 最终套件统一扁平化到 `tests/simulation/`，共享一套 fixtures / helpers，避免 `group_a/group_b` 导入碎片化问题。

## 按场景对比

### T-SIM-01 正常报价周期

- Agent A 优点: 已验证双边卖单、无 BUY、不空跑、`PhaseManager.update` 与 `reconcile_loop` 集成路径。
- Agent A 缺点: 价格范围 `[10, 90]` 过宽，YES/NO 双边订单在不同测试中分开验证。
- Agent B: 无对应实现。
- 最终决策: 采用 A 的结构，收紧价格范围，并新增“同一 cycle 同时产出 YES/NO”参数化边界。

### T-SIM-02 Oracle 死锁回归

- Agent B 优点: 识别了 “STALE -> NORMAL -> 双边恢复” 这一业务链路。
- Agent B 缺点: 没有真正覆盖原 deadlock 风险，`test_oracle_deadlock_no_exceptions` 是纯弱测试；未覆盖“连续失败 N 次后恢复”。
- Agent A: 无对应实现。
- 最终决策: 保留 FakeOracle 驱动思路，但重写为多轮失败恢复、冷却计数与双边报价恢复的数值断言。

### T-SIM-03 WIDEN 防线

- Agent A 优点: 使用真实 `DefenseStack` 与 spread 计算验证 WIDEN 生效，方向正确。
- Agent A 缺点: `widen_factor` 硬编码；未测阈值临界；未测回落到 NORMAL。
- Agent B: 无对应实现。
- 最终决策: 采用 A，补阈值边界与恢复场景，断言直接读取配置值。

### T-SIM-04 KILL 停止报价

- Agent B 优点: 场景目标正确，覆盖了 skew KILL 与 PnL KILL 两条来源。
- Agent B 缺点: 没有先造“存量订单”，导致撤单断言不具业务意义；缺少每轮新增订单冻结验证。
- Agent A: 无对应实现。
- 最终决策: 重写，先跑正常周期建立 active orders，再切换至 KILL，验证 batch cancel、生效后新增下单数严格为 0。

### T-SIM-05 τ=0 临界

- Agent A 优点: 主动识别原 spec 中 “τ=0 spread ≥ 2x τ=24” 的公式错误，并改为正确断言 `spread(24) >= spread(0)`。
- Agent A 缺点: 有“只要不抛异常”型测试；未覆盖极小 tau 连续性边界。
- Agent B: 无对应实现。
- 最终决策: 采用 A 的公式结论，删掉弱测试表达，改成每个 tau 都有具体报价与 spread 数值断言，并补极小 tau 参数化边界。

### T-SIM-06 组件启动完整性

- Agent B 优点: 识别了 health、phase、reconciler 三个启动子系统。
- Agent B 缺点: `reconciler` 测试是恒真测试；health 只看路由不看响应；没有真正测试 loop 集成。
- Agent A: T-SIM-01 中已有更真实的 `reconcile_loop` 调用方式，可借用。
- 最终决策: 用 `reconcile_loop` 重写 reconciler 集成，用 `TestClient` 验证 `/health` 与 `/readiness`，保留 phase 推进但补时间/成交量双触发边界。

### T-SIM-07 进程重启恢复

- Agent B 优点: 共享 fakeredis server 模拟跨进程恢复，库存字段逐项比对到位。
- Agent B 缺点: 恢复后价格范围断言过宽；未证明 A 实例运行后状态与初始相比发生了业务变化。
- Agent A: 无对应实现。
- 最终决策: 基本采用 B，但增加 pending-sell / active order continuity 与更窄价格带断言。

### T-SIM-08 Oracle 滞后恢复

- Agent B 优点: 三阶段 `normal -> stale -> recovered` 结构正确，共享 risk stack 的冷却逻辑也正确。
- Agent B 缺点: STALE 期 defense 断言过宽；恢复容差过宽；单边时人为构造 spread 数字没有业务意义。
- Agent A: 无对应实现。
- 最终决策: 保留三阶段结构，但单边阶段只断言 `ONE_SIDE` 和单边报价；恢复阶段用固定容差验证回归正常 spread。

## 审查报告中的 P0 汇总

### 来自 Group B review

1. `tests/simulation/__init__.py` 缺失会导致收集失败。
2. `mock_exchange` / `fake_redis_async` 等共享 fixture 缺失，整个 Group B 无法收集。
3. T-SIM-02 存在纯“无异常”弱测试。
4. T-SIM-04 没有预先存在的订单，无法验证 KILL 后撤单。
5. T-SIM-06 `reconciler` 测试为恒真测试。

### 来自 Group A review

- 无代码级 P0。
- 但工作流级风险是真实存在的: 如果只 checkout `group_a/` 而不带 `tests/simulation/conftest.py` / `__init__.py`，同样会收集失败。最终套件必须消除这种依赖碎片。

## A 和 B 都遗漏的 Edge Cases

这是本次整合最重要的补充项。

1. Oracle 多次连续失败后恢复。
原因: B 只测一次 `refresh()` 立即恢复，A 没有 oracle 场景。真实线上更可能是连续 N 次 stale/None 之后恢复。

2. WIDEN 阈值临界值。
原因: A 只测明显超过阈值与完全平衡，B 无对应实现。必须确认 `>= inventory_skew_widen` 的边界语义没有回归。

3. WIDEN 恢复到 NORMAL 时 spread 回落。
原因: A 只测升级，不测降级；B 无对应实现。风险是 cooldown 逻辑卡死在高防线。

4. KILL 触发后重复 cycle 不应重复下新单。
原因: B 只看总量，不看每轮增量；A 无对应实现。需要证明 freeze 行为持续有效。

5. Startup 中 readiness 状态切换。
原因: B 只检查路由存在，不检查 503 -> 200 语义；A 无 health 场景。

6. Restart 后 dedupe key 清理前后行为差异。
原因: B 清理了 dedupe keys，但没证明为什么必须清理，也没验证恢复前后下单行为差异。

7. Oracle 恢复后的 cooldown 精确轮次。
原因: B 只跑 “大于 cooldown” 的模糊轮次；A 无对应实现。需要确认不是提前一轮或晚一轮恢复。

8. 极小 tau 的连续性。
原因: A 测了多组 tau，但没有断言 `tau=0.0` 与 `tau=0.001` 的报价几乎连续；B 无对应实现。

## 最终实现决策

- `tests/simulation/conftest.py`
  - 采用 Bootstrap 的 `mock_exchange` / fakeredis fixture。
  - 合并 Agent A 的 builder/helper。
  - 增加少量 final-suite 专用辅助函数，避免各文件重复构建真实服务。

- `test_sim_01_normal_cycle.py`
  - 基于 Agent A 重写，保留真实 quote/reconcile/phase 验证。

- `test_sim_02_oracle_deadlock.py`
  - 基于 Agent B 的 FakeOracle 思路重写，不保留“无异常”测试。

- `test_sim_03_widen_defense.py`
  - 基于 Agent A 重写，新增参数化阈值边界与恢复测试。

- `test_sim_04_kill_stops_quotes.py`
  - 基于 Agent B 完全重写，加入预存订单和逐轮增量断言。

- `test_sim_05_tau_zero.py`
  - 基于 Agent A 重写，保留正确公式裁决，强化具体数值断言。

- `test_sim_06_startup_integrity.py`
  - 参考 Agent B 场景，但核心逻辑重写，采用真实 `reconcile_loop` + `TestClient`。

- `test_sim_07_restart_recovery.py`
  - 基本采用 Agent B，收紧断言并增加“实例 A 确实写入了有意义状态”的验证。

- `test_sim_08_oracle_lag_recovery.py`
  - 基于 Agent B 重写，移除无业务意义的单边 spread 伪数值，改为 defense/side-count/recovery spread 的组合断言。

## 初步源码风险判断

- 当前源码中 `tau < 0` 不会被拒绝，而是会把 A-S spread 直接算小甚至为负后再被边界钳制。这更像“缺保护”而不是测试应当迎合的既定行为。
- `quote_cycle()` 中 KILL 会直接 `cancel_all()` 并返回，这意味着要验证 KILL 撤单，测试必须先构造 active orders；否则测试本身就是错的。
- `DefenseStack` 的降级逻辑依赖连续 cooldown 次数，因此所有恢复类场景都必须复用同一个 `DefenseStack` 实例，否则是在测错对象。
- Oracle 防线与 `DefenseStack` cooldown 是两套机制：源码中 Oracle 从 `STALE` 恢复后会在下一次健康 cycle 立即回到 `NORMAL`。因此任何把 oracle 恢复写成“必须经过 cooldown” 的测试都属于测试预期错误，而不是源码 bug。
