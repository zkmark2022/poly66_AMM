# Group B 代码审查报告

审查时间: 2026-03-08
审查人: Agent A-Review (Claude Sonnet 4.6)

---

## 测试运行结果

```
============================= test session starts ==============================
collected 0 items / 5 errors
!!!!!!!!!!!!!!!!!!! Interrupted: 5 errors during collection !!!!!!!!!!!!!!!!!!!
5 errors in 0.21s
```

**所有 5 个测试文件均无法收集，0 tests passed, 5 collection errors。**

根本原因（见下方 P0-1、P0-2）：`tests/simulation/__init__.py` 缺失 + 测试所依赖的 fixtures（`mock_exchange`、`fake_redis_async`）在任何 conftest.py 中均未定义。

---

## 总体质量评估: 4/10

测试逻辑设计有一定水准（T-SIM-07、T-SIM-08 的数值断言思路正确），但因两个结构性 P0 问题导致**测试集整体无法运行**。此外存在 1 个空壳测试（T-SIM-06 reconciler）、1 个弱测试（T-SIM-02 无业务断言）、以及多处数值断言精度不足的问题。

---

## 各场景详细审查

### T-SIM-02 Oracle 死锁

**文件**: `test_sim_02_oracle_deadlock.py`

**断言质量**:
- `test_oracle_stale_does_not_permanently_lock_one_side`：有业务断言（defense_level 回到 NORMAL、YES/NO 双边订单均存在）。质量 **中等**。
- `test_oracle_deadlock_no_exceptions`：**仅断言不抛异常**，无任何业务断言。典型弱测试（P0）。

**Mock 策略**:
- `FakeOracle` 直接控制 `_refreshed` 标志，绕过了真实的网络调用 — 合理。
- 但该 FakeOracle **未模拟竞态条件本身**：原始 bug 是 `maybe_auto_reinvest` 与 `inventory_lock` 之间的并发死锁，而测试只模拟了 STALE→NORMAL 状态机的串行转换。测试覆盖的是「状态机正确性」而非「并发锁安全性」。
- Oracle 连续 N 次返回 None 后首次成功的路径未覆盖（FakeOracle.refresh() 一次调用即从 STALE 变 NORMAL，没有"多次失败后才成功"的场景）。

**发现问题**:
- **P0**: `test_oracle_deadlock_no_exceptions` 只检查无异常，无业务逻辑验证。任何执行路径被完全 mock 掉都能通过此测试。
- **P1**: FakeOracle 未覆盖"连续多次 None，第 K 次才成功"场景；双边恢复测试仅运行 `cooldown_cycles + 2` 次，没有验证"每一轮 cycle 后 defense_level 的单调递减"路径。

**建议**:
- 将 `test_oracle_deadlock_no_exceptions` 改为验证：运行后 `ctx.defense_level in (NORMAL, ONE_SIDE)` 且不是 KILL_SWITCH；加上"orders_placed 数量在 NORMAL 态 > 0"断言。
- 补充 `FakeOracle` 支持 `fail_count` 参数，模拟"N 次 None 后成功"。

---

### T-SIM-04 KILL Switch 停止报价

**文件**: `test_sim_04_kill_stops_quotes.py`

**断言质量**:
- `test_kill_switch_stops_quoting`：断言 KILL_SWITCH 状态、0 个新订单、batch-cancel 被调用。**基本覆盖**，但有结构缺陷。
- `test_kill_from_pnl_loss`：只断言 `KILL_SWITCH` 和 `orders_placed == 0`，缺少 batch-cancel 验证。**偏弱**。

**Mock 策略**:
- `call_log` 记录 API 调用路径，batch-cancel 验证合理。
- 但 `orders_placed` 是从 `mock_exchange` fixture 取得——该 fixture 未定义（P0-2），实际无法运行。

**发现问题**:
- **P0**: 注释中说"先运行正常周期再切换到倾斜库存"以模拟存量订单，但**代码中完全没有这一步骤**（注释后直接跳到"Run 3 cycles with KILL-triggering inventory"）。这意味着没有预先存在的订单，batch-cancel 可能对空订单列表操作，无法验证"存量订单被撤销"的核心场景。
- **P1**: `assert len(cancel_calls) >= 1` 只验证 API 被调用次数，未验证撤单内容（order_ids 列表非空）。
- **P1**: `test_kill_from_pnl_loss` 缺少 batch-cancel 调用断言，与场景描述不一致。
- **P1**: 连续 3 次 KILL cycle 后未分别验证每次 cycle 的 `orders_placed` 增量均为 0（当前用总量，若 cycle 1 提前下单而 cycle 2-3 是 0，总量仍可能 > 0）。

**建议**:
- 在 `test_kill_switch_stops_quoting` 开头，先用平衡库存运行 1 个正常 cycle（需独立 OrderManager），使 `order_mgr.active_orders` 非空，再切换至倾斜库存并验证撤单内容不为空。
- 在 `test_kill_from_pnl_loss` 中补充 batch-cancel 断言。

---

### T-SIM-06 启动完整性

**文件**: `test_sim_06_startup_integrity.py`

**断言质量**:
- `test_reconciler_called_at_least_once`：**空壳测试**（P0）。测试体自行调用 `await reconciler.reconcile([config.market_id])`，然后断言 `spy.call_count >= 1`。这等价于断言"调用方法后调用次数 >= 1"——该断言恒成立，不验证任何业务逻辑。
- `test_health_endpoint_bound`：检查路由注册是否正确，**可接受**，但未通过 TestClient 验证实际 HTTP 响应。
- `test_phase_manager_advances_from_exploration`：有 mock 时间 + 状态断言，**质量较好**。
- `test_phase_advances_by_volume`：有 trade_count 预置 + phase 状态断言，**质量较好**。

**Mock 策略**:
- `patch("time.monotonic", return_value=fake_time)` 覆盖面过广，可能影响 `market_status_checked_at` 之外的其他计时逻辑（如 defense cooldown），需确认无副作用。

**发现问题**:
- **P0**: `test_reconciler_called_at_least_once` 是一个恒真测试，等同于没有测试。需要重写为"在 main loop 中 reconciler 被集成调用"的验证。
- **P1**: `test_health_endpoint_bound` 建议补充用 `starlette.testclient.TestClient` 发起 GET /health 验证返回 200 及 JSON 结构。
- **P1**: `phase_mgr.current_phase` 与 `ctx.phase` 的双重断言（T-SIM-06 第三个测试）合理，但 `ctx.phase` 更新依赖 `quote_cycle` 内部同步，需确认时序。

**建议**:
- 将 reconciler 测试改为：在独立 task 或 mock main loop 中，验证 reconciler 被 `reconcile_loop` 调用（patch 并 spy `reconcile_loop` 内部的 reconciler 调用）。

---

### T-SIM-07 进程重启

**文件**: `test_sim_07_restart_recovery.py`

**断言质量**:
- `test_restart_recovery_inventory_preserved`：使用两个独立实例 + 共享 `FakeServer`，逐字段精确比对，**设计正确**。
- `test_restart_recovery_no_precision_loss`：使用非整数倍数值验证序列化精度，**设计优秀**，所有字段均有独立断言。

**Mock 策略**:
- 两个独立 Bot 实例（`cache_a`/`cache_b`）共享同一 `FakeServer`，正确模拟了跨进程 Redis 持久化 — **合格**。
- Intent dedup key 清理模拟 TTL 过期 — **合理**。

**发现问题**:
- **P1**: Instance B 的价格范围断言 `1 <= price_cents <= 99` 过于宽松（等同于"不是 0 或 100"）。正常恢复后的价格应在 `[35, 65]` 附近（锚定价格 ±15 cents），建议收窄至更有意义的区间。
- **P1**: `test_restart_recovery_inventory_preserved` 验证了字段精确匹配，但未验证在 Instance A 运行 5 个 cycle 后，库存值相对初始值确实发生了变化（即 Instance A 确实进行了交易，而不是 5 个 cycle 都是空操作）。若 cycle 均空跑，测试等同于只测了 set/get 往返。

**建议**:
- 将价格断言收窄至 `35 <= order.price_cents <= 65`（或依据 anchor_price_cents±widen_factor 计算动态范围）。
- 在 Instance A 结束后增加：`assert final_inv_a != inv_a`（或至少一个字段发生变化）以证明 cycle 确实执行了业务逻辑。

---

### T-SIM-08 Oracle 滞后恢复

**文件**: `test_sim_08_oracle_lag_recovery.py`

**断言质量**:
- `test_oracle_lag_widens_spread_then_recovers`：有三阶段数值对比（spread_normal / spread_passive / spread_recovered），**设计较好**。
- `test_oracle_stale_defense_level_is_one_side`：断言 STALE→ONE_SIDE（非 KILL），**清晰**。

**Mock 策略**:
- `ControllableOracle` 通过 `force_stale` 标志直接控制 STALE/NORMAL 状态，合理。
- 每个阶段使用独立 `OrderManager` 避免状态泄漏，**设计正确**。
- 共享同一个 `DefenseStack`（`shared_risk`）以保证 cooldown 计数连续，**正确**。

**发现问题**:
- **P1**: `_extract_spread` 的 ONE_SIDE fallback 逻辑（`+ 50` 人工加宽）会导致 `spread_passive` 在主测试中条件语句分支走 `if spread_passive is not None: assert spread_passive >= spread_normal`。ONE_SIDE 时单边 ladder 内价差本身并非 spread，该函数计算结果无明确业务含义，注释不足。
- **P1**: 恢复容差 `tolerance = max(3, abs(spread_normal))` 过于宽松。若 `spread_normal = 10`，则容差 = 10 即 100% 容差，导致任何 `spread_recovered ∈ [0, 20]` 都能通过，无法有效验证"恢复到正常"。
- **P1**: Phase 2 中 `oracle.force_stale = True` 后运行 3 个 cycle，但断言 `defense_level in (ONE_SIDE, WIDEN, KILL_SWITCH)` 范围过宽。应至少断言是 `ONE_SIDE`（STALE 映射到 ONE_SIDE），KILL_SWITCH 不应出现。

**建议**:
- 将恢复容差改为固定绝对值（如 `tolerance = 5`）或相对容差（`spread_normal * 0.2`），避免比例容差随 spread_normal 缩放。
- Phase 2 的断言精确化：`assert ctx.defense_level == DefenseLevel.ONE_SIDE`。
- `_extract_spread` 在 ONE_SIDE 时返回 `None` 并在调用方明确处理，而不是返回人工加工的数字。

---

## P0 问题清单（影响测试有效性，Agent C 必须修复）

1. **[P0-1] `tests/simulation/__init__.py` 缺失** — 导致 pytest 无法正确解析包路径，所有 group_b 测试文件收集时抛 `ModuleNotFoundError: No module named 'src'`。需在 `tests/simulation/` 目录下新建 `__init__.py`（内容为空即可）。

2. **[P0-2] fixtures `mock_exchange` 和 `fake_redis_async` 未定义** — `tests/simulation/group_b/` 目录下没有 `conftest.py`，`mock_exchange`（httpx mock + 订单记录 + call_log）和 `fake_redis_async` 在任何 conftest 中均不存在。需在 `tests/simulation/group_b/conftest.py`（或其父目录）中创建这两个 fixture。`mock_exchange` 需提供：`{"client": ..., "orders_placed": [...], "call_log": [...]}`，并 mock 所有 exchange API 端点（/auth/login、/orders、/amm/orders/batch-cancel 等）。

3. **[P0-3] T-SIM-02 `test_oracle_deadlock_no_exceptions`：纯无异常断言** — 该测试没有任何业务逻辑断言，即使 quote_cycle 执行了错误路径也会通过。需补充至少一个业务断言（defense_level 范围、orders_placed 状态等）。

4. **[P0-4] T-SIM-04 预存订单场景缺失** — 注释声称会"先运行正常 cycle 再切换倾斜库存"，但代码未实现。batch-cancel 在空 active_orders 下执行，无法验证"存量订单被撤销"的核心语义。

5. **[P0-5] T-SIM-06 `test_reconciler_called_at_least_once`：恒真测试** — 测试体自行调用 reconciler 然后断言调用次数 >= 1，不测试任何集成行为。需重写为通过 `reconcile_loop` 调用路径验证集成。

---

## P1 建议（不阻塞但值得改进）

1. **T-SIM-04**: `test_kill_from_pnl_loss` 缺少 batch-cancel 调用断言。
2. **T-SIM-04**: 将 `len(orders_placed) == 0` 的断言从"总量"改为"分 cycle 验证每次增量为 0"。
3. **T-SIM-06**: `test_health_endpoint_bound` 补充用 TestClient 发起真实 HTTP 请求验证响应体。
4. **T-SIM-07**: 价格范围断言 `1 <= price <= 99` 收窄至 `[35, 65]`（或 anchor ± 15）。
5. **T-SIM-07**: Instance A 完成后验证库存确实被修改（排除空跑 cycle 假通过）。
6. **T-SIM-08**: 恢复容差 `max(3, abs(spread_normal))` 改为固定绝对值（如 5 cents）。
7. **T-SIM-08**: Phase 2 defense_level 断言从"范围 in (ONE_SIDE, WIDEN, KILL_SWITCH)"精确化为 `== ONE_SIDE`。
8. **T-SIM-08**: `_extract_spread` ONE_SIDE 时返回 None 而非人工加工数字。

---

## Edge Cases 补充建议（供 Agent C 参考）

- **T-SIM-02**: 补充"Oracle 连续失败 N 次（如 5 次 None）后第一次成功"场景，验证 defense_level 单调递减至 NORMAL 而不跳变。
- **T-SIM-04**: 补充"KILL 后 oracle 也 STALE"场景，验证两种 KILL 来源叠加时行为一致（不重复撤单、不产生新订单）。
- **T-SIM-06**: 补充"同一进程启动两个市场，phase_mgr 各自独立推进"场景。
- **T-SIM-07**: 补充"Instance A 运行中途崩溃（中断在 quote_cycle 内部），Instance B 启动后能否正常恢复"场景（通过模拟 partial write 到 Redis 验证原子性）。
- **T-SIM-08**: 补充"Oracle 恢复后 price 偏离内部价格 > oracle_deviation_cents"场景，验证 DEVIATION 状态下 spread 行为与 STALE 不同。
