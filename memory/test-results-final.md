# AMM Test Plan v2.0 - 最终测试报告

日期: 2026-03-02
测试环境: http://localhost:8000

## 测试汇总

| Phase | 测试项 | Pass | XFail | Total |
|-------|--------|------|-------|-------|
| Phase 2 | 定价模型 | 1 | 4 | 5 |
| Phase 4 | 边界情况 | 5 | 0 | 5 |
| Phase 5 | 压力测试 | 2 | 0 | 2 |
| Phase 6 | 状态机 | 2 | 1 | 3 |
| 其他 | 原有测试 | 113+ | 13 | 126+ |

## 发现的 Backend Bug

1. **NO 订单返回 500**
   - `POST /orders` side=NO → HTTP 500
   - 影响: AMM 无法创建 NO 侧订单

2. **Burn 端点返回 500**
   - `POST /amm/burn` → HTTP 500
   - 原因: reserve_balance 约束冲突

3. **无 Quote 引擎 API**
   - 无法测试动态 quote repricing
   - 影响: T2.2-T2.5 只能 xfail

## 压力测试指标

- T5.1: 10 并发用户, total < 5s, p95 < 2s
- T5.2: 100 订单 < 10s, p99 < 250ms

## 新增测试文件

- `tests/integration/amm/test_pricing_real.py`
- `tests/integration/amm/test_boundaries_real.py`
- `tests/integration/amm/test_stress_real.py`
- `tests/integration/amm/test_state_machine_real.py`

## 下一步

1. 修复 Backend NO 订单 500 错误
2. 修复 Burn reserve 约束
3. 添加 Quote 引擎 API 暴露
