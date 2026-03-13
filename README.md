# poly66-amm — Automated Market Maker Bot

> Python 3.12 · AsyncIO · Redis · Avellaneda-Stoikov 定价模型

AMM Bot 是 Poly66 预测市场平台的自动做市商，负责在多个二元预测市场同时挂单、管理库存风险、执行风控熔断，并与平台 Backend API 全异步通信。

---

## 架构总览

```
poly66-amm/
├── src/amm/
│   ├── main.py                  ← 入口：Quote Cycle 主循环（asyncio）
│   ├── config/                  ← 配置加载（YAML + 环境变量）
│   ├── connector/               ← 与 Backend 通信
│   │   ├── api_client.py        ← HTTP 客户端（httpx，自动重试）
│   │   ├── auth.py              ← JWT Token 管理（自动刷新）
│   │   ├── order_manager.py     ← 下单 / 撤单 / 原子替换
│   │   └── trade_poller.py      ← 轮询成交记录，更新库存
│   ├── cache/                   ← Redis 状态缓存
│   │   ├── inventory_cache.py   ← 库存快照（YES/NO shares + cash）
│   │   └── order_cache.py       ← 活跃订单 ID 缓存
│   ├── strategy/                ← 定价与报价策略
│   │   ├── as_engine.py         ← Avellaneda-Stoikov 最优价差模型
│   │   ├── gradient.py          ← 梯度报价（多档位挂单）
│   │   ├── phase_manager.py     ← 市场阶段管理（Exploration / Active / WindingDown）
│   │   └── pricing/
│   │       ├── anchor.py        ← 锚定价格（Oracle 参考价）
│   │       ├── micro.py         ← 微观价格调整
│   │       ├── posterior.py     ← 后验概率估计
│   │       └── three_layer.py   ← 三层定价聚合
│   ├── oracle/
│   │   ├── polymarket.py        ← Polymarket 价格数据抓取
│   │   └── polymarket_oracle.py ← Oracle 状态管理（stale 检测）
│   ├── risk/
│   │   ├── defense_stack.py     ← 防御状态机（NORMAL→WIDEN→ONE_SIDE→KILL）
│   │   └── sanitizer.py         ← 报价意图过滤（禁止 BUY 方向）
│   └── lifecycle/
│       ├── initializer.py       ← 启动初始化（账号 + 库存 + 市场状态）
│       ├── reconciler.py        ← 重启恢复（从 DB/Redis 重建状态）
│       ├── reinvest.py          ← 自动再投资（cash 超阈值时触发 Mint）
│       ├── health.py            ← 健康检查 HTTP server（port 8001）
│       ├── shutdown.py          ← 优雅退出（撤单 + flush）
│       └── winding_down.py      ← 市场临近结算时的收尾逻辑
└── tests/
    ├── unit/                    ← 单元测试（策略 / 风控 / 工具函数）
    ├── integration/             ← 集成测试（含真实 DB/Redis）
    ├── simulation/              ← Layer 3 Bot 行为仿真（T-SIM-01~08）
    ├── property/                ← Layer 1 Hypothesis 属性测试（4 不变量）
    └── e2e/                     ← Layer 4 端到端 UI 验收测试
```

---

## 核心设计

### 定价模型：Avellaneda-Stoikov

针对预测市场的 A-S 模型改编，两个核心公式：

```
保留价格  r = mid - q · γ · σ² · τ
最优价差  δ = (γ · σ² · τ + 2/γ · ln(1 + γ/κ)) × 100
```

- `q` = 库存偏斜（inventory skew）
- `τ` = 距到期时间（小时）
- `γ / σ / κ` = 风险厌恶 / 波动率 / 订单到达强度

**不变量（Hypothesis 测试覆盖）：**
- AMM 永远只发出 SELL 方向意图（不产生 BUY）
- 任意参数下 spread > 0
- tau 越小 spread 单调越大

### 风控防御状态机

```
库存偏斜 → NORMAL → WIDEN（扩价差）→ ONE_SIDE（单边报价）→ KILL（全部撤单停止）
```

KILL 触发条件：库存偏斜超阈值 / 累计亏损超限 / 市场进入非活跃状态。

### 多市场支持

单进程同时管理多个 ACTIVE market，每个 market 独立 Quote Cycle，共享 Redis 缓存和 HTTP 连接池。

---

## 快速启动

```bash
# 安装依赖
pip install -r requirements.txt

# 配置
cp config/example.yaml config/local.yaml
# 填写 backend URL、账号密码、Redis 地址

# 启动
python src/amm/main.py --config config/local.yaml

# 健康检查
curl http://localhost:8001/health
```

---

## 运行测试

```bash
# 全套测试
pytest

# 仅单元 + 属性测试（无需外部服务）
pytest tests/unit tests/property

# 集成测试（需要 DB + Redis）
pytest tests/integration

# 仿真测试（Layer 3）
pytest tests/simulation
```

---

## 依赖

| 依赖 | 用途 |
|------|------|
| `httpx` | 异步 HTTP 客户端，与 Backend API 通信 |
| `redis` | 库存 + 订单状态缓存 |
| `hypothesis` | 属性测试（A-S 不变量验证） |
| `pytest-asyncio` | 异步测试支持 |

Backend API 文档：参考 `poly66win-backend` 仓库。
