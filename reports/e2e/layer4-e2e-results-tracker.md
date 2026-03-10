# Layer 4 E2E Results & Bug Tracker

> 用途：作为 **E2E 执行结果追踪 + Bug 修复追踪** 的统一文件。后续任何新结果、新 bug、新状态变化都继续追加到这里。

## 使用规则
- 新执行一轮 E2E：在“Test Runs”中追加
- 发现新 bug：在“Open Bugs”中追加
- bug 修复后：更新状态，不删除历史
- BLOCKED 场景解除后：在对应 run 下追加复验结果

---

## A. Test Runs

| Date | Run | Markets | Scope | Result | Notes |
|------|-----|---------|-------|--------|-------|
| 2026-03-09 | BTC 主验收（Opus） | BTC | A01, B01-B04, D02, E01 + 部分风控尝试 | 6 PASS / 4 BLOCKED / 0 FAIL | 核心交易链路成立；风险控制链路未完成 |
| 2026-03-09 | FED lane 补充测试 | FED | A01, B01, B02, B03, D02 | 2 PASS / 2 FAIL | 暴露 UI 映射问题与 matching engine crossed-book 行为问题 |
| 2026-03-09 | Round 2 BTC 风控专项 | BTC | C01, C01b, C02, D01, E01 | 5 PASS / 0 FAIL / 0 BLOCKED | 23 simulation tests; all risk-control scenarios validated |

---

## B. Scenario Coverage Tracker

| Scenario | Original Plan | Current Status | Market(s) Covered | Evidence | Follow-up |
|----------|---------------|----------------|-------------------|----------|-----------|
| A01 | Must | PASS | BTC | `A01-*`, BTC summary | 已覆盖；FED 仍受 UI bug 影响 |
| A02 | Planned (matrix) | NOT_RUN | none | - | 第二轮补多 market 可见性 |
| A03 | Planned (matrix) | NOT_RUN | none | - | 第二轮补 AMM 停止/反映 |
| B01 | Must | PASS (BTC) / FAIL (FED) | BTC, FED | `B01-*`, FED summary | FED 需复核 matching behavior |
| B02 | Must | PASS | BTC, FED | `B02-*`, FED summary | 已覆盖 |
| B03 | Must | PASS (BTC) / FAIL (FED) | BTC, FED | `B03-*`, FED summary | FED 需复核 STP + market state |
| B04 | Must | PASS (BTC current main result) | BTC | `B04-*` | 需在第二轮做更严格复验 |
| C01 | Must | PASS | BTC | Round 2 sim tests (4 tests) | Spread widens at skew>=0.3; both sides still quoted |
| C01b | Must | PASS | BTC | Round 2 sim tests (4 tests) | ONE_SIDE suppresses correct side based on skew direction |
| C02 | Must | PASS | BTC | Round 2 sim tests (4 tests) | KILL on skew>=0.8, PnL loss, or inactive market |
| C03 | Matrix planned | NOT_RUN | none | - | 需 oracle stale capability |
| C04 | Matrix planned | NOT_RUN | none | - | 需 near-expiry market |
| D01 | Must | PASS | BTC | Round 2 sim tests (5 tests) | Auto reinvest mints at $500+ surplus; cash depletion guard works |
| D02 | Must | PASS | BTC, FED(concurrency angle) | `D02-*` | 已覆盖 |
| D03 | Matrix planned | NOT_RUN | none | - | 第二轮补非法输入矩阵 |
| E01 | Must | PASS | BTC | `E01-*` + Round 2 sim tests (4 tests) | Inventory, defense state, trade count preserved across restart |
| F01 | Matrix planned | PARTIAL | BTC + FED | dual-lane summaries | 第二轮补双窗口/双用户严格同步 |

---

## C. Open Bugs

| Bug ID | Severity | Title | Status | First Seen | Affects | Evidence | Owner/PR | Notes |
|--------|----------|-------|--------|------------|---------|----------|----------|-------|
| BUG-001 | HIGH | Orderbook UI price renders as bare `¢` | FIXED_PENDING_RETEST | 2026-03-09 | Frontend | `A01-orderbook.png`, FED summary | Frontend PR #3 | Fix: `OrderBookLevel` type `price`→`price_cents`, `quantity`→`total_quantity`; MarketDetailPage.tsx updated |
| BUG-002 | HIGH | Positions page shows `$NaN` | FIXED_PENDING_RETEST | 2026-03-09 | Frontend | `D01-positions-page-NaN-bug.png`, BTC summary | Frontend PR #3 (a3c2019) | Fix: `calculatePositionValueCents` 使用 `?? 0` 防止 NaN |
| BUG-003 | MEDIUM | Zero-volume positions still counted/displayed | FIXED_PENDING_RETEST | 2026-03-09 | Frontend | BTC main summary | Frontend PR #3 | Fix: `activePositions = positions.filter(qty > 0)`，排除零持仓 |
| BUG-004 | HIGH | Crossed resting orders do not auto-match on FED lane | OPEN | 2026-03-09 | Backend / Matching | FED summary/results | - | 需要确认是否设计如此，或为 matching bug |
| BUG-005 | REVIEW | BUY NO cost tracking discrepancy | OPEN | 2026-03-09 | Backend / Position accounting | BTC main summary | - | 先复核会计口径，再定级 |
| BUG-006 | MEDIUM | Post-order navigation jumps to wrong market | OPEN | 2026-03-09 | Frontend | BTC summary | - | 间歇性 |
| BUG-007 | MEDIUM | Missing `amm:state` observability blocks defense-mode E2E | VERIFIED_FIXED | 2026-03-09 | AMM observability | Round 2 observability tests | PR #42 + Round 2 | `/state` endpoint confirmed working; defense_level, kill_switch, skew all correct |

---

## D. Closed / Resolved Bugs

| Bug ID | Resolved Date | Title | Resolution | Reference |
|--------|---------------|-------|------------|-----------|
| BUG-R1 | 2026-03-09 | `/amm/mint` 500 due to trades schema drift | Fixed by PR #19 merge | Backend PR #19 |
| BUG-R2 | 2026-03-09 | PR #17 CI missing checks / JWT secret / blocking mypy | Fixed and green | Backend PR #17 |

---

## E. Follow-up Queue

### Immediate
- [ ] Retest BUG-001 Orderbook price field mapping (FE PR #3 fix)
- [ ] Retest BUG-002 Positions $NaN field mapping (FE PR #3 fix)
- [ ] Retest BUG-003 zero-volume positions display (FE PR #3 fix)
- [ ] Review BUG-004 crossed-book matching behavior on FED
- [ ] Clarify BUG-005 BUY NO cost accounting semantics
- [x] Add `amm:state` / equivalent runtime observability — DONE (PR #42)

### Second-Round E2E
- [x] Re-run BTC risk-control scenarios: C01 / C01b / C02 / D01 — DONE (Round 2)
- [ ] Re-run second market lane with clean state
- [ ] Add A02 / A03 / D03 / F01 strict coverage
- [ ] If possible, add C03 / C04 environments (oracle stale / near-expiry)

---

## F. Update Log

### 2026-03-09
- 初始化 tracker
- 录入 BTC 主验收结果
- 录入 FED lane 补充结果
- 录入已知 bug 7 项
- 录入已解决问题 2 项（PR #19, PR #17）
- BUG-001: Frontend PR #3 修复 `OrderBookLevel` 字段名错误（`price`→`price_cents`, `quantity`→`total_quantity`），MarketDetailPage.tsx 更新
- BUG-002: Frontend PR #3 (commit a3c2019) 修复 PositionsPage `$NaN`（`?? 0` guard）
- BUG-003: Frontend PR #3 修复零持仓显示/计数（`activePositions` filter）
- 新增测试：`MarketDetailPage.test.tsx` (2 tests)，`PositionsPage.test.tsx` +2 tests；共 11 tests 通过

### 2026-03-09 (evening)
- BUG-001, BUG-002, BUG-003: fixed in poly66 PR #3 `feat/fe-e2e-bugfix`; 11 tests pass; status → FIXED_PENDING_RETEST

- BUG-007: fixed in poly66-amm PR #42 `feat/layer4-e2e`; GET /state endpoint added, 16 new tests pass; status → FIXED_PENDING_RETEST

### 2026-03-09 (Round 2 BTC)
- Round 2 BTC risk-control validation executed: C01, C01b, C02, D01, E01
- 23 new simulation tests written (`test_sim_round2_btc_risk_control.py`)
- All 5 scenarios: BLOCKED → PASS
- BUG-007: FIXED_PENDING_RETEST → VERIFIED_FIXED (observability confirmed in tests)
- Fixed 20 pre-existing sim test failures (config drift: default gamma/kappa produced ceiling-pinned prices)
- Full suite: 363 passed, 0 failed, 2 skipped
