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
| 2026-03-09 | FED Round 2 二轮对照验证 | FED | A01, B01-B04, D02, D03, F01 | **8 PASS / 0 FAIL** | 所有 4 向交易成功；验证取消、非法输入、双市场隔离 |

---

## B. Scenario Coverage Tracker

| Scenario | Original Plan | Current Status | Market(s) Covered | Evidence | Follow-up |
|----------|---------------|----------------|-------------------|----------|-----------|
| A01 | Must | **PASS** | BTC, FED | `A01-*`, BTC summary, FED R2 summary | BTC + FED 双市场均通过 |
| A02 | Planned (matrix) | NOT_RUN | none | - | 第二轮补多 market 可见性 |
| A03 | Planned (matrix) | NOT_RUN | none | - | 第二轮补 AMM 停止/反映 |
| B01 | Must | **PASS** | BTC, FED | `B01-*`, FED R2 evidence | FED R2: IOC 正确填单；R1 使用 GTC 未触发 matching（by-design） |
| B02 | Must | **PASS** | BTC, FED | `B02-*`, FED R2 evidence | 双市场一致通过 |
| B03 | Must | **PASS** | BTC, FED | `B03-*`, FED R2 evidence | FED R2: 正确卖出 YES @45c；R1 STP 问题已通过正确对手方解决 |
| B04 | Must | **PASS** | BTC, FED | `B04-*`, FED R2 evidence | FED R2: 新增覆盖，SELL NO @45c 正确执行 |
| C01 | Must | BLOCKED | BTC | `C01-orderbook-post-stress.json` | 需 clean state + observability |
| C01b | Must | BLOCKED | none | - | 第二轮重点补 |
| C02 | Must | BLOCKED | none | - | 第二轮重点补 |
| C03 | Matrix planned | NOT_RUN | none | - | 需 oracle stale capability |
| C04 | Matrix planned | NOT_RUN | none | - | 需 near-expiry market |
| D01 | Must (matrix) / BLOCKED in main run | BLOCKED | BTC | BTC summary | 需 Auto Reinvest / Mint 条件 |
| D02 | Must | **PASS** | BTC, FED | `D02-*`, FED R2 evidence | 双市场均通过：下单→确认OPEN→取消→冻结释放 |
| D03 | Matrix planned | **PASS** | FED | `D03-*`, FED R2 evidence | 6 项无效输入全部被拒：price 0/100/101/-1, qty 0/-1 |
| E01 | Must | PASS | BTC | `E01-*` | 已覆盖；后续补真正 AMM 重启专项 |
| F01 | Matrix planned | **PASS** | BTC + FED | FED R2 `F01-*` evidence | FED 订单不影响 BTC 订单簿；双市场独立运行验证 |

---

## C. Open Bugs

| Bug ID | Severity | Title | Status | First Seen | Affects | Evidence | Owner/PR | Notes |
|--------|----------|-------|--------|------------|---------|----------|----------|-------|
| BUG-001 | HIGH | Orderbook UI price renders as bare `¢` | FIXED_PENDING_RETEST | 2026-03-09 | Frontend | `A01-orderbook.png`, FED summary | Frontend PR #3 | Fix: `OrderBookLevel` type `price`→`price_cents`, `quantity`→`total_quantity`; MarketDetailPage.tsx updated |
| BUG-002 | HIGH | Positions page shows `$NaN` | FIXED_PENDING_RETEST | 2026-03-09 | Frontend | `D01-positions-page-NaN-bug.png`, BTC summary | Frontend PR #3 (a3c2019) | Fix: `calculatePositionValueCents` 使用 `?? 0` 防止 NaN |
| BUG-003 | MEDIUM | Zero-volume positions still counted/displayed | FIXED_PENDING_RETEST | 2026-03-09 | Frontend | BTC main summary | Frontend PR #3 | Fix: `activePositions = positions.filter(qty > 0)`，排除零持仓 |
| BUG-004 | LOW | Crossed resting orders do not auto-match on FED lane | BY_DESIGN | 2026-03-09 | Backend / Matching | FED summary/results, FED R2 evidence | - | **R2 结论**: 确认为设计行为。GTC maker-maker 不自动撮合；IOC taker 触发 MINT 正确填单。降级为 LOW/BY_DESIGN |
| BUG-005 | REVIEW | BUY NO cost tracking discrepancy | OPEN | 2026-03-09 | Backend / Position accounting | BTC main summary | - | 先复核会计口径，再定级 |
| BUG-006 | MEDIUM | Post-order navigation jumps to wrong market | OPEN | 2026-03-09 | Frontend | BTC summary | - | 间歇性 |
| BUG-007 | MEDIUM | Missing `amm:state` observability blocks defense-mode E2E | FIXED_PENDING_RETEST | 2026-03-09 | AMM observability | BTC summaries/results | - | 影响 C01/C01b/C02/D01 |

---

## D. Closed / Resolved Bugs

| Bug ID | Resolved Date | Title | Resolution | Reference |
|--------|---------------|-------|------------|-----------|
| BUG-R1 | 2026-03-09 | `/amm/mint` 500 due to trades schema drift | Fixed by PR #19 merge | Backend PR #19 |
| BUG-R2 | 2026-03-09 | PR #17 CI missing checks / JWT secret / blocking mypy | Fixed and green | Backend PR #17 |

---

## E. Follow-up Queue

### Immediate
- [ ] Fix BUG-001 Orderbook price field mapping
- [ ] Fix BUG-002 Positions $NaN field mapping
- [ ] Fix BUG-003 zero-volume positions display
- [ ] Review BUG-004 crossed-book matching behavior on FED
- [ ] Clarify BUG-005 BUY NO cost accounting semantics
- [ ] Add `amm:state` / equivalent runtime observability

### Second-Round E2E
- [ ] Re-run BTC risk-control scenarios: C01 / C01b / C02 / D01
- [x] Re-run second market lane with clean state — **FED R2: 8/8 PASS**
- [x] Add D03 strict coverage — **FED R2: 6 invalid inputs all rejected**
- [x] Add F01 strict coverage — **FED R2: dual-market isolation verified**
- [ ] Add A02 / A03 coverage
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

### 2026-03-09 (late night) — Round 2 FED Market Validation
- FED Round 2 完成：8/8 PASS (A01, B01-B04, D02, D03, F01)
- 新增场景覆盖：D03 (非法输入 6 项全部被拒)、F01 (双市场隔离验证)、B04 (SELL NO on FED)
- BUG-004: 降级为 BY_DESIGN — GTC maker 不自动撮合为正常行为，IOC taker 正确触发 MINT
- 发现 netting 行为：BUY YES 持有 NO 时自动对冲赎回，非 bug
- 测试脚本：`tests/e2e/test_fed_market_e2e.py`
- 证据目录：`reports/e2e/evidence/fed-round2/`
- 完整报告：`reports/e2e/2026-03-09-fed-round2-summary.md`
