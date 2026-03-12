# Layer 4 E2E Results & Bug Tracker

> 用途：作为 **E2E 执行结果追踪 + Bug 修复追踪** 的统一文件。后续任何新结果、新 bug、新状态变化都继续追加到这里。

## 使用规则
- 新执行一轮 E2E：在"Test Runs"中追加
- 发现新 bug：在"Open Bugs"中追加
- bug 修复后：更新状态，不删除历史
- BLOCKED 场景解除后：在对应 run 下追加复验结果

---

## A. Test Runs

| Date | Run | Markets | Scope | Result | Notes |
|------|-----|---------|-------|--------|-------|
| 2026-03-09 | BTC 主验收（Opus） | BTC | A01, B01-B04, D02, E01 + 部分风控尝试 | 6 PASS / 4 BLOCKED / 0 FAIL | 核心交易链路成立；风险控制链路未完成 |
| 2026-03-09 | FED lane 第1轮补充测试 | FED | A01, B01, B02, B03, D02 | 2 PASS / 2 FAIL | 暴露 UI 映射 bug；B01/B03 FAIL 因测试账号 STP（同账号下单/对手方）误判为 matching bug |
| 2026-03-09 | FED Round 2 二轮对照验证 | FED | A01, B01-B04, D02, D03, F01 | **8 PASS / 0 FAIL** | 独立账号（demo + amm_market_maker）；所有 4 向交易成功 |
| 2026-03-09 | BTC Round 2 风控专项 | BTC | C01, C01b, C02, D01, E01 | **5 PASS / 0 FAIL** | 23 simulation tests；风控链路全部验证 |
| 2026-03-12 | FED clean-state 验证（BUG-001/002/003 retest） | FED | A01, B01-B04, D02, D03, F01 | **8 PASS / 0 FAIL** | PR #3 merge 后 backend 侧全部通过；prices_valid=True，字段映射正确 |

---

## B. Scenario Coverage Tracker

| Scenario | Original Plan | Current Status | Market(s) Covered | Evidence | Notes |
|----------|---------------|----------------|-------------------|----------|-------|
| A01 | Must | ✅ PASS | BTC, FED | `A01-*`, BTC summary, FED R2 summary | 双市场均通过 |
| A02 | Planned (matrix) | NOT_RUN | none | - | 第三轮补多 market 可见性 |
| A03 | Planned (matrix) | NOT_RUN | none | - | 第三轮补 AMM 停止反映 |
| B01 | Must | ✅ PASS | BTC, FED | `B01-*`, FED R2 evidence | R1 FAIL 因 STP（测试账号问题），R2 正确账号 8/8 PASS |
| B02 | Must | ✅ PASS | BTC, FED | `B02-*`, FED R2 evidence | 双市场一致通过 |
| B03 | Must | ✅ PASS | BTC, FED | `B03-*`, FED R2 evidence | FED R2: SELL YES @45c 正确执行 |
| B04 | Must | ✅ PASS | BTC, FED | `B04-*`, FED R2 evidence | FED R2 新增覆盖：SELL NO @45c 正确执行 |
| C01 | Must | ✅ PASS | BTC | Round 2 sim tests (4 tests) | Spread widens at skew≥0.3；双边持续报价 |
| C01b | Must | ✅ PASS | BTC | Round 2 sim tests (4 tests) | ONE_SIDE 根据 skew 方向正确抑制单边 |
| C02 | Must | ✅ PASS | BTC | Round 2 sim tests (4 tests) | KILL on skew≥0.8 / PnL loss / market inactive |
| C03 | Matrix planned | NOT_RUN | none | - | 需 oracle stale 环境 |
| C04 | Matrix planned | NOT_RUN | none | - | 需 near-expiry market 环境 |
| D01 | Must | ✅ PASS | BTC | Round 2 sim tests (5 tests) | Auto reinvest ≥$500 surplus；cash depletion guard 有效 |
| D02 | Must | ✅ PASS | BTC, FED | `D02-*`, FED R2 evidence | 双市场：下单→OPEN→取消→冻结释放 |
| D03 | Matrix planned | ✅ PASS | FED | `D03-*`, FED R2 evidence | 6 项无效输入全部被拒 |
| E01 | Must | ✅ PASS | BTC | `E01-*` + Round 2 sim tests (4 tests) | inventory/defense state/trade count 重启后恢复 |
| F01 | Matrix planned | PARTIAL | BTC + FED | FED R2 `F01-*` evidence | FED 单不影响 BTC 盘口；第三轮补双用户严格并发测试 |

---

## C. Open Bugs

| Bug ID | Severity | Title | Status | First Seen | Affects | Evidence | Owner/PR | Notes |
|--------|----------|-------|--------|------------|---------|----------|----------|-------|
| BUG-001 | HIGH | Orderbook UI price renders as bare `¢` | ✅ VERIFIED_FIXED | 2026-03-09 | Frontend | `A01-orderbook.png` | Frontend PR #3 (merged 2026-03-11) | Backend 侧 2026-03-12 clean-state rerun prices_valid=True 验证通过 |
| BUG-002 | HIGH | Positions page shows `$NaN` | ✅ VERIFIED_FIXED | 2026-03-09 | Frontend | `D01-positions-page-NaN-bug.png` | Frontend PR #3 (merged 2026-03-11) | Fix: `calculatePositionValueCents` 使用 `?? 0`；backend 侧验证通过 |
| BUG-003 | MEDIUM | Zero-volume positions still counted/displayed | ✅ VERIFIED_FIXED | 2026-03-09 | Frontend | BTC main summary | Frontend PR #3 (merged 2026-03-11) | Fix: `activePositions = positions.filter(qty > 0)`；backend 侧验证通过 |
| BUG-005 | REVIEW | BUY NO cost tracking discrepancy | OPEN | 2026-03-09 | Backend / Position accounting | BTC main summary | - | 先复核会计口径（limit price vs execution price 记账）再定级 |
| BUG-006 | MEDIUM | Post-order navigation jumps to wrong market | OPEN | 2026-03-09 | Frontend | BTC summary | - | 间歇性；需 browser E2E 复现 |

---

## D. Improvements Backlog (非 Bug)

| ID | Priority | Title | Status | Notes |
|----|----------|-------|--------|-------|
| IMPRV-001 | LOW | Rebuild-after-restart crossed-check | OPEN | 服务重启后 `rebuild_orderbook` 只恢复 resting 单到 in-memory book，不重新跑 `match_order`。如 DB 中存在历史交叉单（edge case），重启后永远不会自动撮合，需新 incoming 单触发。修复方向：rebuild 完成后扫描一次 crossed pairs 并撮合。 |

---

## E. Closed / Resolved Bugs

| Bug ID | Closed Date | Title | Resolution | Reference |
|--------|-------------|-------|------------|-----------|
| BUG-R1 | 2026-03-09 | `/amm/mint` 500 due to trades schema drift | Fixed by PR #19 merge | Backend PR #19 |
| BUG-R2 | 2026-03-09 | PR #17 CI missing checks / JWT secret / blocking mypy | Fixed and green | Backend PR #17 |
| BUG-004 | 2026-03-11 | Crossed resting orders do not auto-match on FED lane | **误报关闭** | 代码审查确认：GTC 和 IOC 走相同 `match_order` 路径，每笔 incoming 单都会触发撮合。R1 FAIL 根因是测试账号问题（同账号充当对手方 → STP 阻止撮合），非 matching engine bug。真实 edge case 已记录为 IMPRV-001（低优先级改进）。 |
| BUG-007 | 2026-03-09 | Missing `amm:state` observability blocks defense-mode E2E | VERIFIED_FIXED | PR #42：`/state` endpoint 上线，defense_level/kill_switch/skew 均正常 |

---

## F. Follow-up Queue

### 当前优先级

| Priority | Task | 状态 |
|----------|------|------|
| P0 | ~~BUG-001/002/003 retest~~ | ✅ 完成（2026-03-12 clean-state 8/8 PASS） |
| P0 | ~~BUG-004 定性分析~~ | ✅ 完成（误报，已关闭） |
| P1 | BUG-005 BUY NO 会计口径复核 | OPEN |
| P1 | A02/A03 场景覆盖 | NOT_RUN |
| P1 | F01 严格版（双用户/双窗口并发） | PARTIAL |
| P2 | BUG-006 post-order 导航 browser 复现 | OPEN |
| P2 | IMPRV-001 rebuild crossed-check | LOW |
| P3 | C03/C04 需要特殊环境（oracle stale / near-expiry） | 待排期 |

---

## G. Update Log

### 2026-03-09
- 初始化 tracker
- 录入 BTC 主验收结果、FED 第1轮结果
- 录入 bug 7 项（BUG-001 ~ BUG-007）、已解决 2 项（BUG-R1, BUG-R2）
- BUG-007: PR #42 修复，16 新测试通过 → FIXED_PENDING_RETEST

### 2026-03-09 (evening)
- FED Round 2 完成：8/8 PASS
- BUG-001/002/003: Frontend PR #3 修复，状态 → FIXED_PENDING_RETEST
- BUG-007: → VERIFIED_FIXED（observability 在 Round 2 sim tests 中确认）

### 2026-03-09 (BTC Round 2)
- BTC Round 2 风控专项：C01/C01b/C02/D01/E01 全部 5/5 PASS
- 23 sim tests 新增；全套 363 passed, 0 failed, 2 skipped

### 2026-03-11
- Frontend PR #3 (fe-e2e-bugfix) → MERGED（zkmark2022/poly66）
- AMM PR #47 (layer4-round2-market2) → MERGED（zkmark2022/poly66_AMM）
- BUG-004 代码审查：确认为误报
  - 根因：GTC/IOC 均走相同 `match_order` 路径；R1 FAIL = 测试账号 STP 问题
  - 决策：关闭 BUG-004，记录 IMPRV-001（低优先级 rebuild crossed-check）

### 2026-03-12
- FED clean-state E2E rerun（验证 BUG-001/002/003 fix）：8/8 PASS
- `prices_valid=True`，所有字段映射正确，backend 侧验证完成
- BUG-001/002/003 状态 → VERIFIED_FIXED
- 更新 Tracker：去重 Scenario Coverage 表、整理 Follow-up Queue
