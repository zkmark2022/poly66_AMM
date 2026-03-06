# Poly66 问题追踪器

更新时间: 2026-03-05 01:00 PST

## 已修复问题 ✅

| ID | 问题 | 修复时间 | PR |
|----|------|----------|-----|
| B1 | NO 订单返回 500 | 2026-03-05 | 已合并 |
| B2 | 订单簿 rebuild 双重订单 | 2026-03-04 | #12 |
| B3 | OrderBook null price 显示 | 2026-03-04 | #13 |
| B4 | Positions $NaN 显示 | 2026-03-04 | #13 |
| B5 | CI 缺少 ruff 依赖 | 2026-03-04 | main |
| B6 | CI 缺少 fastapi/uvicorn | 2026-03-04 | main |

## 待修复问题 🔴

### P0 (阻塞测试)

| ID | 问题 | 影响 | 建议修复 |
|----|------|------|----------|
| F1 | Demo 登录失败 | 前端测试被阻塞 | 后端添加 demo 用户 seed |
| F2 | 市场卡片无路由 | 无法测试交易流程 | 检查 Next.js 路由配置 |

### P1 (功能问题)

| ID | 问题 | 影响 | 建议修复 |
|----|------|------|----------|
| F3 | Burn 端点返回 500 | AMM 无法清算 | 检查 reserve_balance 约束 |
| F4 | 无 Quote 引擎 API | 无法测试动态定价 | 暴露 quote_cycle 状态 |

### P2 (优化建议)

| ID | 问题 | 来源 | 建议 |
|----|------|------|------|
| O1 | 幂等键后端确认 | Claude Review | 确认后端实现幂等处理 |
| O2 | SRP 违反 (InventoryCache) | Claude Review | 拆分 IntentCache |
| O3 | T9.2 测试循环 401 次 | Claude Review | 减少到 3-5 次 |
| O4 | Magic number TTL | Claude Review | 提取为常量 |

## 修复计划

### Sprint 1: 前端阻塞问题

1. **F1 Demo 登录**
   - 添加 seed 脚本创建 demo 用户
   - 后端启动时自动 seed

2. **F2 市场路由**
   - 检查 Next.js pages/app 结构
   - 添加 /markets/[id] 路由

### Sprint 2: 后端稳定性

3. **F3 Burn 端点**
   - 调试 reserve_balance 逻辑
   - 添加错误处理

4. **F4 Quote API**
   - 暴露 AMM 状态端点
   - 添加 /amm/status API

### Sprint 3: 代码质量

5. **O1-O4 Claude Review 建议**
   - 逐一处理优化建议
   - 提高代码质量
