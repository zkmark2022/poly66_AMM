## 测试结果
- T-SIM-01 正常报价周期: ✅
- T-SIM-02 Oracle 死锁回归: ✅
- T-SIM-03 WIDEN 防线: ✅
- T-SIM-04 KILL 停止报价: ✅
- T-SIM-05 τ=0 临界: ✅
- T-SIM-06 组件启动完整性: ✅
- T-SIM-07 进程重启恢复: ✅
- T-SIM-08 Oracle 滞后恢复: ✅

总计: 35 passed, 0 xfail（已知 bug）

## 相比初始实现的主要改进
- 将 Group A / Group B 扁平化整合为 `tests/simulation/` 单层套件，消除收集路径碎片和缺失 fixture 问题。
- 合并 Bootstrap fixture 与 Agent A helper，统一提供 mock exchange、fakeredis、真实/半真实服务构建器。
- 重写 T-SIM-02/04/06 中的弱测试或恒真测试，改成具体业务断言与数值断言。
- 为每个 T-SIM 文件补充至少一个参数化 edge case，包括 WIDEN 阈值边界、重复 oracle stale、PnL KILL 阈值、近零 tau 等。
- 明确区分源码真实 contract 与错误测试预期，例如 Oracle 恢复是“下一健康 cycle 立即恢复”，不是走 `DefenseStack` cooldown。

## 审查报告 P0 问题处理
- P0-1 `tests/simulation/__init__.py` 缺失: 已补齐，并将最终套件固定在 `tests/simulation/` 包下。
- P0-2 缺少 `mock_exchange` / `fake_redis_async` fixtures: 已在 `tests/simulation/conftest.py` 中统一提供。
- P0-3 T-SIM-02 纯“无异常”断言: 已删除，改为 stale 单边报价与恢复后双边报价断言。
- P0-4 T-SIM-04 没有预存订单却验证撤单: 已先构造正常报价产生 active orders，再进入 KILL 并验证 batch cancel 与 0 新下单。
- P0-5 T-SIM-06 reconciler 恒真测试: 已改为 `reconcile_loop` 驱动的 drift 更新验证。

## 新发现的源码 Bug（如有）
- 无新增需要 `xfail` 标记的源码 bug。

## 关键模块覆盖率
- `src/amm` 总覆盖率: 52%
- `src/amm/risk/defense_stack.py`: 93%
- `src/amm/strategy/phase_manager.py`: 100%
- `src/amm/strategy/gradient.py`: 92%
- `src/amm/strategy/pricing/three_layer.py`: 96%
