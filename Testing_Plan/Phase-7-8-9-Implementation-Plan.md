# AMM Phase 7-8-9 实施计划

> **日期**: 2026-03-04
> **目标**: 完成 AMM 测试计划 v2.0 的 Phase 7、8、9
> **方法**: TDD + SuperPower + Agent Swarm + Gemini Code Assist Review

---

## 今日完成情况

### ✅ 已完成
1. **Matching Engine 修复** (PR #12)
   - 修复 orderbook rebuild 的双重订单 bug
   - 空状态的 lazy rebuild

2. **Frontend Bug 修复**
   - OrderBook 和 Positions 页面的 null price/quantity 显示
   - 添加 naked short selling 验证

3. **基础设施**
   - SuperPower TDD skill 安装完成
   - PolyMarket CLI 已配置

### ⚠️ 待完成
- Phase 7: 资金与铸造 (P1)
- Phase 8: LVR 与 Oracle 防御 (P2) **← 需要 PolyMarket CLI**
- Phase 9: 安全与竞态 (P0/P1)

---

## Phase 7: 资金与铸造 (P1)

### 测试用例

| ID | 场景 | 预期结果 |
|----|------|---------|
| T7.1 | Auto-Reinvest Mint | cash > $500 → 自动 Mint; cash 减少, YES/NO 增加 |
| T7.2 | Mint 一致性 | Mint N 份: yes+=N, no+=N, reserve+=N×100, total_yes+=N |
| T7.3 | 现金耗尽 | cash=0 + no_reinvest → 停止 BUY 挂单 |
| T7.4 | WINDING_DOWN Burn | 市场结束: 所有持仓 Burn → cash; 最终 YES=NO=0 |

### 实施步骤

1. **创建测试文件** (`tests/integration/test_amm_phase7_funds.py`)
   ```python
   # T7.1: Auto-Reinvest Mint
   # T7.2: Mint Consistency
   # T7.3: Cash Exhaustion
   # T7.4: WINDING_DOWN Burn
   ```

2. **TDD 流程** (SuperPower)
   - 先写测试 (红灯)
   - 实现功能 (绿灯)
   - 重构优化

3. **需要的功能**
   - `src/amm/lifecycle/reinvest.py` - Auto-reinvest logic
   - `src/amm/lifecycle/winding_down.py` - Market closure burn

4. **预计工作量**: 4-6 小时

---

## Phase 8: LVR 与 Oracle 防御 (P2) **← 重点**

### 测试用例

| ID | 场景 | 预期结果 |
|----|------|---------|
| T8.1 | LVR 快速库存损失 | 500ms 内损失 >20% → KILL |
| T8.2 | Oracle 滞后 | 价格停滞 >3s → PASSIVE_MODE |
| T8.3 | Oracle 偏差 | 偏差 >20% → AMM_PAUSE; 恢复后自动重启 |

### PolyMarket Oracle 集成

#### 1. Oracle 模块设计

```python
# src/amm/oracle/polymarket_oracle.py

import subprocess
import json
from datetime import datetime
from typing import Optional

class PolyMarketOracle:
    """PolyMarket CLI Oracle 集成"""
    
    def __init__(self, market_slug: str):
        self.market_slug = market_slug
        self.last_price: Optional[float] = None
        self.last_update: Optional[datetime] = None
    
    def get_yes_price(self) -> float:
        """获取 YES 价格 (0-100 cents)"""
        result = subprocess.run(
            ["polymarket", "-o", "json", "markets", "get", self.market_slug],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            raise OracleError(f"Failed to fetch: {result.stderr}")
        
        data = json.loads(result.stdout)
        price = float(data["outcomePrices"][0]) * 100
        
        self.last_price = price
        self.last_update = datetime.utcnow()
        return price
    
    def check_stale(self, threshold_seconds: int = 3) -> bool:
        """检查价格是否过期"""
        if not self.last_update:
            return True
        elapsed = (datetime.utcnow() - self.last_update).total_seconds()
        return elapsed > threshold_seconds
    
    def check_deviation(self, internal_price: float, threshold: float = 20.0) -> bool:
        """检测偏差是否超过阈值"""
        if not self.last_price:
            return False
        deviation = abs(internal_price - self.last_price)
        return deviation > threshold
```

#### 2. 集成到 AMM 主循环

```python
# src/amm/main.py

from amm.oracle.polymarket_oracle import PolyMarketOracle

class AMMBot:
    def __init__(self, config):
        self.oracle = PolyMarketOracle(config.polymarket_slug)
        # ...
    
    async def quote_cycle(self):
        # 1. 获取外部价格
        try:
            external_price = self.oracle.get_yes_price()
        except Exception as e:
            logger.error(f"Oracle fetch failed: {e}")
            if self.oracle.check_stale(threshold_seconds=3):
                await self.enter_passive_mode()  # T8.2
                return
        
        # 2. 计算内部价格
        internal_price = self.calculate_mid_price()
        
        # 3. 检测偏差
        if self.oracle.check_deviation(internal_price, threshold=20.0):
            logger.warning(f"Price deviation detected: {abs(internal_price - external_price)}¢")
            await self.amm_pause()  # T8.3
            return
        
        # 4. 正常报价...
        await self.place_orders()
```

#### 3. 测试文件

```python
# tests/integration/test_amm_phase8_oracle.py

@pytest.mark.integration
class TestPhase8Oracle:
    
    def test_t8_1_lvr_kill_switch(self):
        """T8.1: 500ms 内损失 >20% → KILL"""
        # Mock rapid inventory loss
        # Assert KILL_SWITCH triggered
        pass
    
    def test_t8_2_oracle_stale(self):
        """T8.2: 价格停滞 >3s → PASSIVE_MODE"""
        # Mock stale oracle
        # Assert enters PASSIVE_MODE
        pass
    
    def test_t8_3_oracle_deviation(self):
        """T8.3: 偏差 >20% → AMM_PAUSE"""
        # Mock 25¢ deviation
        # Assert AMM_PAUSE + auto-restart
        pass
```

### 配置文件

```yaml
# config/amm_config.yaml

oracle:
  enabled: true
  provider: "polymarket"
  market_slug: "will-trump-win-the-2024-election"  # 示例
  stale_threshold_seconds: 3
  deviation_threshold_cents: 20
  check_interval_seconds: 5

risk:
  lvr_time_window_ms: 500
  lvr_loss_threshold_pct: 20
```

### 预计工作量
- Oracle 模块开发: 2-3 小时
- 集成到主循环: 1-2 小时
- 测试编写: 2-3 小时
- **总计**: 5-8 小时

---

## Phase 9: 安全与竞态 (P0/P1)

### 测试用例

| ID | 场景 | 预期结果 | 优先级 |
|----|------|---------|--------|
| T9.1 | 未授权访问拒绝 | 普通用户 JWT → Mint/Burn/Replace 返回 401/403 | P0 |
| T9.2 | Replace 速率限制 | >400/min → 429; AMM 退避正常 | P1 |
| T9.3 | Replace 超时重试 | 超时后重试不产生重复订单 | P0 |
| T9.4 | 崩溃后恢复不重复下单 | 下单成功+崩溃 → 重启后识别已有订单 | P0 |
| T9.5 | 重复 Trade 不重复计入 | 同一 trade_id 轮询两次 → 库存只变一次 | P1 |
| T9.6 | Batch Cancel Unfreeze | 撤单 N 个 → N 条 ORDER_UNFREEZE ledger | P1 |
| T9.7 | AMM 账户权限隔离 | AMM Token 无法访问普通用户资产 | P2 |

### 实施步骤

1. **权限测试** (`tests/integration/test_amm_phase9_security.py`)
   - T9.1: 验证 JWT 权限
   - T9.7: 账户隔离

2. **竞态测试** (`tests/integration/test_amm_phase9_race_conditions.py`)
   - T9.3: 超时重试幂等性
   - T9.4: 崩溃恢复
   - T9.5: Trade 去重

3. **速率限制** (`tests/integration/test_amm_phase9_rate_limit.py`)
   - T9.2: 429 响应处理
   - T9.6: Batch cancel ledger

4. **预计工作量**: 6-8 小时

---

## Agent Swarm 执行计划

### Task 1: Phase 7 - 资金与铸造

**Prompt**:
```markdown
实现 AMM 测试计划 v2.0 的 Phase 7 (资金与铸造)。

**要求**:
1. 使用 TDD 方法 (superpowers skill)
2. 先编写 tests/integration/test_amm_phase7_funds.py
3. 实现以下测试用例:
   - T7.1: Auto-Reinvest Mint
   - T7.2: Mint Consistency
   - T7.3: Cash Exhaustion
   - T7.4: WINDING_DOWN Burn
4. 确保所有测试通过
5. 完成后创建 PR，标题: "test: Phase 7 - Funds & Minting"

**参考**:
- Testing_Plan/AMM-Test-Plan-v2.md
- Implementation_Plan/2026-02-28-amm-bot-plan.md

**工作目录**: ~/clawd/poly66-amm
**使用 superpowers TDD skill 强制 test-first 开发**
```

### Task 2: Phase 8 - Oracle 集成

**Prompt**:
```markdown
实现 AMM 测试计划 v2.0 的 Phase 8 (LVR 与 Oracle 防御)。

**要求**:
1. 使用 TDD 方法 (superpowers skill)
2. 创建 Oracle 模块: src/amm/oracle/polymarket_oracle.py
3. 集成 PolyMarket CLI (已安装，命令: polymarket)
4. 实现测试用例:
   - T8.1: LVR 快速库存损失检测
   - T8.2: Oracle 滞后检测 (>3s)
   - T8.3: Oracle 偏差检测 (>20¢)
5. 更新 config/amm_config.yaml 添加 oracle 配置
6. 确保所有测试通过
7. 完成后创建 PR，标题: "feat: Phase 8 - Oracle Defense"

**参考**:
- Testing_Plan/Phase-7-8-9-Implementation-Plan.md
- ~/.openclaw/skills/polymarket-cli/SKILL.md

**工作目录**: ~/clawd/poly66-amm
**使用 superpowers TDD skill 强制 test-first 开发**
```

### Task 3: Phase 9 - 安全与竞态

**Prompt**:
```markdown
实现 AMM 测试计划 v2.0 的 Phase 9 (安全与竞态)。

**要求**:
1. 使用 TDD 方法 (superpowers skill)
2. 编写三个测试文件:
   - tests/integration/test_amm_phase9_security.py (T9.1, T9.7)
   - tests/integration/test_amm_phase9_race_conditions.py (T9.3, T9.4, T9.5)
   - tests/integration/test_amm_phase9_rate_limit.py (T9.2, T9.6)
3. 重点测试 P0 优先级的用例
4. 确保所有测试通过
5. 完成后创建 PR，标题: "test: Phase 9 - Security & Race Conditions"

**参考**:
- Testing_Plan/AMM-Test-Plan-v2.md
- Testing_Plan/Phase-7-8-9-Implementation-Plan.md

**工作目录**: ~/clawd/poly66-amm
**使用 superpowers TDD skill 强制 test-first 开发**
```

---

## PR Review 流程

每个 PR 完成后：

1. **使用 Gemini Code Assist** (免费) 进行自动化 review
   ```bash
   gh pr view <PR_NUMBER> --json body,files | gemini code review
   ```

2. **检查清单**:
   - [ ] 测试覆盖率 >90%
   - [ ] TDD 流程验证 (test-first)
   - [ ] 所有测试通过
   - [ ] 代码风格符合项目规范
   - [ ] 文档更新

3. **合并条件**:
   - CI/CD 全绿
   - Gemini review 通过
   - 人工确认关键逻辑

---

## 时间估算

| Phase | 预计工作量 | Agent 分配 |
|-------|-----------|-----------|
| Phase 7 | 4-6h | Task 1 |
| Phase 8 | 5-8h | Task 2 |
| Phase 9 | 6-8h | Task 3 |
| **总计** | **15-22h** | 3 Agents |

**并行执行**: 3 个 Agent 同时工作，预计完成时间: **6-8 小时**

---

## 依赖关系

```
Phase 7 (独立)
    ↓
Phase 8 (依赖 Oracle CLI) ← 已安装
    ↓
Phase 9 (依赖 Phase 7+8 的功能)
```

**建议执行顺序**:
1. Phase 7 先启动 (Task 1)
2. Phase 8 并行启动 (Task 2)
3. Phase 9 在 7+8 完成 50% 后启动 (Task 3)

---

## 验收标准

### P0 - 必须全通过
- [ ] T7.2, T7.4 (Mint 一致性, WINDING_DOWN)
- [ ] T9.1, T9.3, T9.4 (权限, 超时重试, 崩溃恢复)

### P1 - 应达到 >80%
- [ ] Phase 7 全部 (4/4)
- [ ] Phase 8 全部 (3/3)
- [ ] Phase 9 其余 (T9.2, T9.5, T9.6)

### P2 - 后续迭代
- [ ] T9.7 (高级权限隔离)

---

**准备启动 Agent Swarm？确认后我会按顺序 spawn 3 个 agents。**
