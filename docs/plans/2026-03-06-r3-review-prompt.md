# Round 3 Code Review — 全 Context 装配层审查

> **日期**: 2026-03-06  
> **目的**: 以 `main.py` 为中心，沿调用链追踪所有组件的装配完整性  
> **背景**: R1/R2 按模块拆分审查，遗漏了跨模块衔接问题。R3 每个 AI 独立读完整代码库，专项检查不同维度。  
> **代码库**: `src/amm/`（约 2,700 行，25K tokens，完整放入单个 context）

---

## 两个 Agent 的分工

| Agent | 模型 | 审查焦点 |
|-------|------|---------|
| **Agent A** | Claude Opus 4.6 (`claude-opus-4-6`) | 调用链完整性（lifecycle tracing） |
| **Agent B** | Codex GPT-5.4 (`gpt-5.4`) | 数据流类型一致性 + 防线有效性 |

两者读**相同的完整代码**，从**不同角度**审查，避免重复又保持互补。

---

## Agent A Prompt — 调用链完整性

```
你是一名高级 Python 工程师，正在对一个做市商机器人（AMM Bot）进行代码审查。

## 你的任务（严格按照这个顺序）

### Step 1：建立组件清单
读完 `src/amm/main.py`。
列出 amm_main() 函数中所有被 import、实例化、或调度的组件（类/函数/协程任务）。
格式：
| 组件 | 来自模块 | 在 main.py 中的角色 |

### Step 2：逐个追踪生命周期
对清单中每个组件，检查以下三个阶段是否完整：
1. **初始化**：是否被正确构造？必要的参数是否全部传入？
2. **运行**：是否被调用/调度？调用频率/条件是否符合设计意图？
3. **清理**：shutdown 时是否被正确停止/释放？

对每个组件给出结论：✅ 完整 / ⚠️ 部分遗漏 / ❌ 完全缺失

### Step 3：追踪 quote_cycle() 执行路径
从 `quote_cycle()` 入口，完整追踪一次报价周期的执行路径：
- 每个防御状态（NORMAL/WIDEN/ONE_SIDE/KILL）是否都在路径上有触发点？
- PhaseManager 的 update() 是否在每个周期被调用？
- Sanitizer 是否在每次下单前被调用？
- 如果任何组件被跳过，说明在什么条件下会被跳过？

### Step 4：识别"永远不会执行"的代码
找出以下情况：
- 条件永远为 True 或永远为 False 的分支
- 被实例化但从未调用的对象
- 定义了但从未被 quote_cycle() 路径调用的方法

### Step 5：输出报告
格式：

#### [A-xxx] 问题标题
- **严重程度**: P0/P1/P2/P3
- **位置**: 文件名:行号
- **现象**: 描述观察到的问题
- **影响**: 运行时会发生什么
- **修复建议**: 具体修改方案

如果没有发现问题，明确写出"未发现问题"并说明检查了哪些路径。

## 代码库结构
src/amm/
├── main.py          ← 从这里开始
├── strategy/        (as_engine, phase_manager, gradient, pricing/)
├── risk/            (sanitizer, defense_stack)
├── connector/       (api_client, order_manager, trade_poller, auth)
├── cache/           (inventory_cache, order_cache, protocols)
├── lifecycle/       (initializer, reconciler, reinvest, health, shutdown, winding_down)
├── oracle/          (polymarket_oracle, polymarket)
├── config/          (models, loader)
└── models/          (enums, inventory, market_context, orders)

## 重要背景
- AMM 永远只挂卖单（SELL），永远不挂买单
- 防御级别: NORMAL → WIDEN → ONE_SIDE → KILL
- 阶段: EXPLORATION → LEARNING → STEADY
- 对账周期: 5 分钟（AMMReconciler）
- Redis 存储库存快照，重启后需要从 Redis 恢复状态
- Oracle 提供外部参考价格，lag/deviation 超阈值触发防御
```

---

## Agent B Prompt — 数据流类型一致性 + 防线有效性

```
你是一名高级 Python 工程师，正在对一个做市商机器人（AMM Bot）进行代码审查。

## 你的任务（严格按照这个顺序）

### Step 1：绘制数据流图
读完整个 src/amm/，特别关注以下数据在模块间的传递：
- 价格数据（单位：cents？分数？归一化到 0-1？0-100？）
- 库存数据（yes_volume, no_volume, cash_cents）
- 订单数据（OrderIntent → API 请求）
- Oracle 输出 → A-S 引擎输入的转换

对每个跨模块的数据传递点，标注：
- 发出方的数据格式/单位/类型
- 接收方期望的数据格式/单位/类型
- 是否一致？如果不一致，会发生什么？

### Step 2：验证防线有效性
对以下每条防线，验证它是否真的有效（不只是"代码存在"，而是"数据路径上真的生效"）：

| 防线 | 检查点 |
|------|--------|
| WIDEN | widen_factor 是否真的影响最终 spread？追踪从 DefenseLevel.WIDEN → spread 计算的完整路径 |
| ONE_SIDE | 触发后，quote_cycle 是否真的只输出单侧订单？ |
| KILL | 触发后，是否真的停止所有报价且撤销存量订单？ |
| Sanitizer BUY 拦截 | 是否有 direction != SELL 的 intent 能绕过 sanitizer？ |
| τ=0 边界 | remaining_hours=0 时，A-S spread 计算结果是否合理（大价差）而不是崩溃/默认值？ |

### Step 3：检查单位一致性
价格在系统中有多种表示方式（cents 整数、0-100 浮点、0-1 浮点）。
检查：
- A-S 引擎接受的是哪种格式？输出是哪种格式？
- three_layer pricing 的三层混合后，输出格式是什么？
- OrderIntent 中的价格字段，最终提交到 API 时是什么格式？
- inventory_cache 存储的价格/金额单位是否和计算层一致？

### Step 4：检查并发安全
main.py 启动了多个 asyncio 任务（quote_cycle、reconciler、trade_poller、health_server）：
- 是否有对共享状态（inventory、active_orders）的并发读写没有加锁？
- Redis pipeline 操作是否有原子性保证？
- 如果 quote_cycle 和 reconciler 同时运行，是否可能产生脏读/写？

### Step 5：输出报告
格式：

#### [B-xxx] 问题标题
- **严重程度**: P0/P1/P2/P3
- **位置**: 文件名:行号（数据发出方） → 文件名:行号（数据接收方）
- **现象**: 数据格式/类型不匹配 or 防线实际无效
- **影响**: 运行时会产生什么错误或静默的错误行为
- **修复建议**: 具体修改方案

如果没有发现问题，明确写出"未发现问题"并说明检查了哪些路径。

## 代码库结构
src/amm/
├── main.py
├── strategy/        (as_engine, phase_manager, gradient, pricing/)
├── risk/            (sanitizer, defense_stack)
├── connector/       (api_client, order_manager, trade_poller, auth)
├── cache/           (inventory_cache, order_cache, protocols)
├── lifecycle/       (initializer, reconciler, reinvest, health, shutdown, winding_down)
├── oracle/          (polymarket_oracle, polymarket)
├── config/          (models, loader)
└── models/          (enums, inventory, market_context, orders)

## 重要背景
- 价格单位约定：cents 为整数（100 = $1.00），归一化价格为 0.0-1.0
- AMM 只做卖方（SELL YES 或 SELL NO）
- Redis 存储所有持久状态，Python 内存只是缓存
- 防御级别响应链: NORMAL → WIDEN(价差扩大1.5x) → ONE_SIDE(只报一侧) → KILL(停止报价)
```

---

## 执行方式

### 准备代码 Bundle

```bash
# 生成完整代码 bundle 供 AI 读取
cd ~/clawd/poly66-amm

# 生成 Agent A 的输入文件
{
  echo "# poly66-amm 完整源码（用于 R3 审查）"
  echo "# 生成时间: $(date)"
  echo ""
  find src/amm -name "*.py" | sort | while read f; do
    echo "## File: $f"
    echo '```python'
    cat "$f"
    echo '```'
    echo ""
  done
} > /tmp/amm-r3-source-bundle.md

echo "Bundle 大小: $(wc -c < /tmp/amm-r3-source-bundle.md) bytes"
echo "估算 tokens: $(echo "$(wc -c < /tmp/amm-r3-source-bundle.md) / 4" | bc)"
```

### 启动 Agent A（调用链完整性）

```bash
cat /tmp/amm-r3-source-bundle.md <(echo "") <(cat ~/clawd/poly66-amm/docs/plans/2026-03-06-r3-review-prompt.md | sed -n '/Agent A Prompt/,/Agent B Prompt/p' | head -80) | \
claude --model claude-opus-4-6 --dangerously-skip-permissions -p - \
> /tmp/amm-r3/agent-a-output.md 2>&1
```

### 启动 Agent B（数据流 + 防线）

```bash
cat /tmp/amm-r3-source-bundle.md <(echo "") <(cat ~/clawd/poly66-amm/docs/plans/2026-03-06-r3-review-prompt.md | sed -n '/Agent B Prompt/,/执行方式/p' | head -80) | \
claude --model claude-sonnet-4-6 --dangerously-skip-permissions -p - \
> /tmp/amm-r3/agent-b-output.md 2>&1
```

或者由 orchestrator 负责生成 prompt 并启动，参考 agent-swarm skill。

---

## 验收标准

R3 完成后，两份报告合并，按严重程度排序：
- **P0**：必须修复后才能进入测试阶段
- **P1**：进入测试前修复
- **P2/P3**：可以在测试阶段并行修复

合并报告路径：`/tmp/amm-r3/r3-combined-report.md`
