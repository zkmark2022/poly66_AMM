# 文档 4：故障恢复与运行时防护设计

> **版本**: v1.1 — 单账本撮合引擎架构
> **状态**: 草稿（待 Review）
> **适用范围**: Phase 1 MVP，内存订单簿恢复、事务回滚保护、熔断机制、运行时一致性校验
> **对齐文档**: 《全局约定与数据库设计 v2.3》、《撮合引擎与清算流程设计 v1.2》、《单账本撮合引擎设计方案 v1 §8》
> **日期**: 2026-02-20

---

## 目录

1. [文档范围与核心问题](#第一部分文档范围与核心问题)
2. [恢复策略: DB 全量重建](#第二部分恢复策略-db-全量重建)
3. [内存安全: 事务回滚保护](#第三部分内存安全-事务回滚保护)
4. [WAL 审计日志](#第四部分wal-审计日志)
5. [熔断机制](#第五部分熔断机制)
6. [运行时一致性校验](#第六部分运行时一致性校验)
7. [监控与告警](#第七部分监控与告警)
8. [附录](#附录)

---

## 第一部分：文档范围与核心问题

### 1.1 要解决的核心问题

撮合引擎 (pm_matching) 的内存订单簿 (`OrderBook`) 是纯内存数据结构。当进程重启（计划内部署或意外崩溃）时，内存状态丢失。必须有机制将订单簿恢复到崩溃前的精确状态。

**三个子问题**:

1. **冷启动**: 进程启动时如何重建内存订单簿？
2. **事务回滚保护**: DB 事务回滚后如何防止内存订单簿处于脏状态？
3. **运行时防护**: 如何在运行期间检测并响应状态不一致？

### 1.2 前提设定

| # | 设定 | 来源 |
|---|------|------|
| 1 | **PostgreSQL 是唯一 Source of Truth** — 余额、持仓、成交记录、流水均以 DB 为准，内存订单簿仅为性能缓存 | 撮合设计 v1.2 §2.1 |
| 2 | **恢复策略: 纯 DB 全量重建** — `orders WHERE status IN ('OPEN', 'PARTIALLY_FILLED')` 就是内存订单簿的完美镜像 | v1.1 设计决策 |
| 3 | **WAL 定位: 审计日志** — `wal_events` 表仅用于事后排查和数据分析，不参与崩溃恢复 | v1.1 设计决策 |
| 4 | **熔断策略: HALT + 告警, 不自动回滚** — 恒等式失败时暂停市场并告警，由人工排查后决定处理方式 | v1.0 设计决策 |
| 5 | **事务回滚 = 销毁内存订单簿** — DB 事务失败时强行驱逐该 market 的内存 OrderBook，下次请求触发 lazy rebuild | v1.1 设计决策 |
| 6 | 快照按 **per-market** 粒度存储，与 per-market asyncio.Lock 对齐 | 撮合设计 v1.2 §2.1 |

### 1.3 v1.0 → v1.1 关键架构变更

> **v1.1 核心洞察**: 在同步 DB 事务架构下，PostgreSQL 的 `orders` 表就是每毫秒都绝对精确的订单簿快照。
>
> 传统 LMAX 架构（如 exchange-core）中，撮合在内存异步执行，DB 落库滞后，因此必须依赖 WAL + 快照来恢复丢失的内存状态。但本项目的所有状态变更都在同一个 DB 事务中原子提交（撮合设计 v1.2 §2.1: `async with db.begin()`），DB 永远与内存同步。
>
> 因此 v1.1 做出以下简化:
> - **砍掉**: 状态快照机制（`orderbook_snapshots` 表）、快照+WAL 恢复路径
> - **保留**: DB 全量重建（唯一恢复路径）、WAL 审计日志（不参与恢复）
> - **新增**: 事务回滚时的内存订单簿保护机制

### 1.4 设计边界

**在本文档范围内**:
- DB 全量重建的流程与校验
- 事务回滚时的内存保护（evict + lazy rebuild）
- WAL 审计日志的事件类型与写入
- 市场级熔断机制的触发条件与处理流程
- 运行时恒等式校验的执行策略
- 监控指标与告警规则

**不在本文档范围内**:
- 多节点高可用 / 主从切换（后续 Phase 2+）
- 性能优化细节（批量 WAL 写入等）
- 数据备份 / 灾难恢复

---

## 第二部分：恢复策略 — DB 全量重建

### 2.1 核心思想

```
┌─────────────────────────────────────────────────────────────────────┐
│               恢复策略: PostgreSQL 从不撒谎                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  PostgreSQL 是 Source of Truth (余额/持仓/订单/成交/流水)           │
│  内存订单簿 = orders 表中 OPEN/PARTIALLY_FILLED 订单的性能缓存      │
│                                                                     │
│  唯一恢复路径:                                                      │
│  ┌───────────────────────────────────────────────────┐              │
│  │  DB 全量重建                                       │              │
│  │  SELECT * FROM orders                              │              │
│  │    WHERE market_id = :mid                          │              │
│  │      AND status IN ('OPEN', 'PARTIALLY_FILLED')    │              │
│  │    ORDER BY created_at ASC;                        │              │
│  │  → 逐条插入内存 OrderBook                           │              │
│  │  耗时: < 10ms (有索引, 数千级活跃订单)               │              │
│  └───────────────────────────────────────────────────┘              │
│                                                                     │
│  触发时机:                                                          │
│  1. 进程冷启动                                                      │
│  2. 事务回滚后 lazy rebuild (下次请求自动触发)                       │
│  3. 熔断解除后人工恢复                                               │
│                                                                     │
│  恢复后: 执行一致性校验 → 通过 → 接受请求                            │
│                        → 失败 → 熔断, 拒绝启动                      │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 为什么 DB 重建是唯一正确路径

撮合引擎的所有状态变更都在一个 PostgreSQL 事务中完成。这意味着：

1. **订单入簿** 与 **DB INSERT/UPDATE** 是原子的 — 事务提交后 `orders.status = 'OPEN'` 且内存已更新；回滚则两者都不变。
2. **成交清算** 与 **订单状态更新** 是原子的 — `orders.status = 'FILLED'` 与 `trades` 记录在同一事务中。
3. 因此，`orders WHERE status IN ('OPEN', 'PARTIALLY_FILLED')` **精确等于**"应该在内存订单簿中的订单集合"。

无需快照、无需 WAL 重放。PostgreSQL 就是完美的实时快照。

### 2.3 推荐索引

```sql
-- 加速恢复查询: 按 market 过滤活跃订单
CREATE INDEX idx_orders_market_active
    ON orders (market_id, created_at)
    WHERE status IN ('OPEN', 'PARTIALLY_FILLED');

COMMENT ON INDEX idx_orders_market_active IS '加速内存订单簿恢复, 覆盖 DB 全量重建查询';
```

有了这个部分索引，即使 orders 表有百万级历史订单，重建查询也只扫描活跃订单的小型 B-Tree。

### 2.4 冷启动恢复流程

```python
class MatchingEngineRecovery:
    """
    撮合引擎恢复管理器

    启动时为每个 ACTIVE 市场从 DB 全量重建内存订单簿。
    """

    async def recover_all_markets(self, db: AsyncSession) -> RecoveryReport:
        """
        恢复所有 ACTIVE/SUSPENDED 市场的订单簿

        HALTED 市场不恢复 (需要人工介入)
        DRAFT/RESOLVED/SETTLED/VOIDED 市场无活跃订单, 跳过
        """
        report = RecoveryReport()

        active_markets = await get_markets_by_status(
            ["ACTIVE", "SUSPENDED"], db
        )

        for market in active_markets:
            try:
                result = await self.rebuild_orderbook(market, db)
                report.add_success(market.id, result)
            except RecoveryError as e:
                report.add_failure(market.id, e)
                # 恢复失败 → 熔断该市场
                await trigger_circuit_breaker(
                    market.id,
                    reason="RECOVERY_FAILED",
                    context={"error": str(e)},
                    db=db,
                )

        return report

    async def rebuild_orderbook(
        self, market: Market, db: AsyncSession
    ) -> RecoveryResult:
        """
        DB 全量重建 — 唯一恢复路径

        查询条件:
          market_id = 当前市场
          status IN ('OPEN', 'PARTIALLY_FILLED')
        排序: created_at ASC (保证时间优先)
        """
        rows = await db.execute(
            text("""
                SELECT id, user_id, market_id,
                       book_type, book_direction, book_price,
                       price, remaining_quantity,
                       frozen_asset_type, frozen_amount,
                       time_in_force, created_at
                FROM orders
                WHERE market_id = :market_id
                  AND status IN ('OPEN', 'PARTIALLY_FILLED')
                ORDER BY created_at ASC
            """),
            {"market_id": market.id},
        )

        orderbook = OrderBook(market.id)

        count = 0
        for row in rows:
            book_order = BookOrder(
                order_id=row.id,
                user_id=row.user_id,
                book_type=row.book_type,
                book_direction=row.book_direction,
                book_price=row.book_price,
                remaining_qty=row.remaining_quantity,
                original_price=row.price,
                frozen_asset_type=row.frozen_asset_type,
                frozen_amount=row.frozen_amount,
                time_in_force=row.time_in_force,
                created_at=row.created_at.isoformat(),
            )
            # 直接插入对应价格档位的队列 (不触发撮合)
            orderbook._insert_order_at_price(book_order)
            count += 1

        # 修复游标 (重建后需要重算 best_bid / best_ask)
        orderbook._repair_cursors()

        # 一致性校验
        await self._verify_orderbook_consistency(orderbook, market, db)

        # 注册到引擎
        engine = get_matching_engine()
        engine._orderbooks[market.id] = orderbook

        logger.info(
            f"Market {market.id}: DB 重建完成, "
            f"{count} 笔活跃订单已恢复"
        )

        return RecoveryResult(
            market_id=market.id,
            orders_restored=count,
        )
```

### 2.5 恢复后一致性校验

```python
    async def _verify_orderbook_consistency(
        self,
        orderbook: OrderBook,
        market: Market,
        db: AsyncSession,
    ) -> None:
        """
        恢复后校验: 内存订单簿 vs DB 订单表

        校验项:
        1. 订单 ID 集合一致: 内存 == DB
        2. 游标一致: best_bid / best_ask 与实际订单分布匹配
        """
        # 1. DB 中的活跃订单 ID 集合
        db_order_ids = set()
        rows = await db.execute(
            text("""
                SELECT id FROM orders
                WHERE market_id = :market_id
                  AND status IN ('OPEN', 'PARTIALLY_FILLED')
            """),
            {"market_id": market.id},
        )
        for row in rows:
            db_order_ids.add(row.id)

        # 2. 内存中的订单 ID 集合
        mem_order_ids = set(orderbook._order_index.keys())

        # 3. 比较
        if db_order_ids != mem_order_ids:
            missing_in_mem = db_order_ids - mem_order_ids
            extra_in_mem = mem_order_ids - db_order_ids
            raise RecoveryVerificationError(
                f"Market {market.id}: 订单簿不一致. "
                f"DB 有但内存无: {missing_in_mem}, "
                f"内存有但 DB 无: {extra_in_mem}"
            )

        # 4. 游标校验 + 自动修复
        actual_best_bid = orderbook._scan_best_bid()
        actual_best_ask = orderbook._scan_best_ask()
        if (orderbook._best_bid != actual_best_bid
                or orderbook._best_ask != actual_best_ask):
            logger.warning(
                f"Market {market.id}: 游标偏差, 已自动修复. "
                f"bid: {orderbook._best_bid}→{actual_best_bid}, "
                f"ask: {orderbook._best_ask}→{actual_best_ask}"
            )
            orderbook._best_bid = actual_best_bid
            orderbook._best_ask = actual_best_ask

        logger.info(
            f"Market {market.id}: 恢复校验通过, "
            f"{len(mem_order_ids)} 笔订单"
        )
```

---

## 第三部分：内存安全 — 事务回滚保护

### 3.1 问题本质

```
┌─────────────────────────────────────────────────────────────────────┐
│                    事务回滚内存污染问题                               │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  submit_order 事务内:                                               │
│                                                                     │
│    Step 5: match_order()                                            │
│            → 原地修改内存 OrderBook                                  │
│            → 扣减 resting order 的 remaining_qty                    │
│            → 甚至从 OrderBook 中移除完全成交的 order                 │
│                                                                     │
│    Step 6: execute_clearing()                                       │
│            → DB INSERT trades / UPDATE orders / etc.                │
│            → ❌ 此处若因余额不足或网络抖动触发异常                    │
│                                                                     │
│    async with db.begin():                                           │
│            → DB 完美回滚到事务开始前的状态 ✅                         │
│            → 内存 OrderBook 已被修改, 不会自动回滚 ❌                 │
│                                                                     │
│  后果:                                                              │
│    DB: 挂单 A 仍然 remaining_qty = 100                              │
│    内存: 挂单 A 已被吃掉 (remaining_qty = 60 或已移除)               │
│    → 后续撮合基于脏状态运行 → "幽灵吃单" → 恒等式崩溃               │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 解决方案: Evict + Lazy Rebuild

```python
class MatchingEngine:
    """
    撮合引擎 — 含事务回滚内存保护

    ⚠️ v1.1 P0 修复: 任何 DB 事务失败时, 强行销毁该 market 的内存
    OrderBook, 下次请求自动触发 DB 全量重建。
    """

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._orderbooks: dict[str, OrderBook] = {}

    async def _get_or_rebuild_orderbook(
        self, market_id: str, db: AsyncSession
    ) -> OrderBook:
        """
        获取内存订单簿, 如果不存在则从 DB 重建 (lazy rebuild)

        触发场景:
        1. 进程刚启动, 该 market 尚未恢复
        2. 上次事务失败后 OrderBook 被驱逐
        """
        if market_id not in self._orderbooks:
            logger.info(f"Market {market_id}: 触发 lazy rebuild")
            market = await get_market(market_id, db)
            recovery = MatchingEngineRecovery()
            await recovery.rebuild_orderbook(market, db)

        return self._orderbooks[market_id]

    async def submit_order(
        self, order_request: OrderRequest, db: AsyncSession
    ) -> OrderResult:
        """
        下单入口 — 含事务回滚保护

        关键: try/except 包裹整个事务块。
        如果 DB 事务因任何原因回滚, 立即驱逐该 market 的内存
        OrderBook。下次请求会通过 _get_or_rebuild_orderbook
        自动从 DB 重建, 保证内存与 DB 绝对一致。
        """
        lock = self._get_lock(order_request.market_id)

        async with lock:
            try:
                async with db.begin():
                    # Step 2: 订单转换
                    market = await get_market(order_request.market_id, db)
                    order = transform_order(
                        order_request, market.taker_fee_bps
                    )

                    # Step 4: 风控与冻结
                    await freeze_for_order(order, db)

                    # Step 5: 撮合 (⚠️ 此处原地修改内存 OrderBook)
                    orderbook = await self._get_or_rebuild_orderbook(
                        order.market_id, db
                    )
                    trades = match_order(
                        order, orderbook, market.taker_fee_bps
                    )

                    # Step 6: 清算 (逐笔)
                    for trade in trades:
                        await execute_clearing(trade, db)

                    # Step 7: Auto-Netting
                    affected_user_ids = collect_affected_users(trades)
                    for user_id in affected_user_ids:
                        await execute_netting_if_needed(
                            user_id, order.market_id, db
                        )

                    # Step 8: 订单状态 + 入簿
                    await finalize_order(
                        order, trades, orderbook, market, db
                    )

                    # Step 9: WAL 审计日志 (不参与恢复, 仅审计)
                    await write_wal_audit_events(
                        order, trades, market, db
                    )

                    # 事务 COMMIT → 内存与 DB 一致
                    return OrderResult(order=order, trades=trades)

            except Exception as e:
                # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
                # 🔴 核心安全机制:
                #    DB 事务已回滚, 内存状态必须丢弃
                # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
                logger.error(
                    f"Transaction failed for market "
                    f"{order_request.market_id}: {e}. "
                    f"Evicting memory orderbook."
                )
                self._orderbooks.pop(order_request.market_id, None)
                raise  # 继续向上抛出, 由 API 层返回错误
```

### 3.3 取消订单的同样保护

```python
    async def cancel_order(
        self,
        order_id: str,
        market_id: str,
        user_id: str,
        db: AsyncSession,
    ) -> CancelResult:
        """取消订单 — 同样包含事务回滚保护"""
        lock = self._get_lock(market_id)

        async with lock:
            try:
                async with db.begin():
                    # ... 校验订单归属、状态 ...
                    # ... 解冻余额 ...
                    # ... 从内存 OrderBook 移除 ...
                    # ... 更新 DB orders.status = CANCELLED ...
                    # ... 写 WAL 审计日志 ...
                    pass
            except Exception as e:
                logger.error(
                    f"Cancel failed for market {market_id}: {e}. "
                    f"Evicting memory orderbook."
                )
                self._orderbooks.pop(market_id, None)
                raise
```

### 3.4 Lazy Rebuild 的性能影响

| 场景 | 触发 rebuild | 耗时 | 影响 |
|------|------------|------|------|
| 正常运行 | 永不触发 | 0 | 无 |
| 事务偶尔失败 | 该 market 下次请求 | < 10ms | 仅首次请求延迟一次 |
| 频繁事务失败 | 每次失败后 | < 10ms × N | 需排查根因 (触发告警) |
| 冷启动 | 每个 market 一次 | < 10ms × market数 | 一次性代价 |

> **关键**: 有了 `idx_orders_market_active` 部分索引，即使 orders 表有百万级历史记录，DB 重建也只扫描数千级活跃订单的小型 B-Tree，耗时可控在毫秒级。

---

## 第四部分：WAL 审计日志

> **v1.1 定位变更**: WAL 从"恢复机制"降级为"审计日志"。
> 仅用于事后排查和数据分析，**不参与崩溃恢复**。

### 4.1 数据库 Schema

```sql
-- wal_events: 订单簿变更审计日志
-- 对齐 DB v2.2 命名规范 (小写 + 下划线)
CREATE TABLE wal_events (
    id              BIGSERIAL       PRIMARY KEY,
    market_id       VARCHAR(64)     NOT NULL,       -- → markets.id
    event_type      VARCHAR(30)     NOT NULL,
    payload         JSONB           NOT NULL,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    -- v1.1: 移除 sequence_id (审计日志不需要严格连续)
    -- 使用自增主键 id 保证写入顺序
    CONSTRAINT ck_wal_event_type CHECK (
        event_type IN (
            'ORDER_ACCEPTED',
            'ORDER_MATCHED',
            'ORDER_PARTIALLY_FILLED',
            'ORDER_CANCELLED',
            'ORDER_EXPIRED'
        )
    )
);

-- 按 market 和时间查询审计日志
CREATE INDEX idx_wal_market_time ON wal_events (market_id, created_at);

-- 按订单 ID 查找相关事件
CREATE INDEX idx_wal_order_id ON wal_events
    USING GIN ((payload->'order_id'));

COMMENT ON TABLE wal_events IS '订单簿变更审计日志, 仅用于事后排查和分析, 不参与崩溃恢复。与业务操作同事务提交。';
```

### 4.2 事件类型

| 事件类型 | 触发时机 | Payload 关键字段 |
|---------|---------|-----------------|
| `ORDER_ACCEPTED` | GTC 剩余入簿 (§5.3 finalize_order) | order_id, book_type, book_price, remaining_qty, frozen_amount |
| `ORDER_MATCHED` | resting order 完全成交被移除 | order_id, fill_qty, trade_price, counterparty_order_id |
| `ORDER_PARTIALLY_FILLED` | resting order 部分成交 | order_id, fill_qty, trade_price, new_remaining_qty |
| `ORDER_CANCELLED` | 用户主动取消 | order_id, remaining_qty, frozen_amount |
| `ORDER_EXPIRED` | IOC 剩余自动取消 | order_id, expired_qty, filled_qty |

> **v1.1 变更**: 新增 `ORDER_PARTIALLY_FILLED` 事件。既然 WAL 已降级为审计日志（不需要最小化事件量），记录每次部分成交可以提供完整的订单生命周期追踪。

### 4.3 写入时机

```python
async def write_wal_audit_events(
    order: Order,
    trades: list[TradeResult],
    market: Market,
    db: AsyncSession,
) -> None:
    """
    WAL 审计日志写入 — 在事务 COMMIT 前写入

    ⚠️ 仅用于审计, 不参与恢复。
    与业务操作在同一事务中, 保证"要么全写入, 要么全不写入"。
    """
    for trade in trades:
        resting = (
            trade.buy_order
            if trade.maker_order_id == trade.buy_order.order_id
            else trade.sell_order
        )

        if resting.remaining_qty == 0:
            # 完全成交, 从簿中移除
            await _insert_wal(db, market.id, "ORDER_MATCHED", {
                "order_id": resting.order_id,
                "fill_qty": trade.quantity,
                "trade_price": trade.trade_price,
                "counterparty_order_id": order.order_id,
            })
        else:
            # 部分成交, 仍在簿中
            await _insert_wal(db, market.id, "ORDER_PARTIALLY_FILLED", {
                "order_id": resting.order_id,
                "fill_qty": trade.quantity,
                "trade_price": trade.trade_price,
                "new_remaining_qty": resting.remaining_qty,
                "counterparty_order_id": order.order_id,
            })

    # incoming 订单入簿 (GTC 且有剩余)
    if order.status in ("OPEN", "PARTIALLY_FILLED"):
        await _insert_wal(db, market.id, "ORDER_ACCEPTED", {
            "order_id": order.order_id,
            "user_id": order.user_id,
            "book_type": order.book_type,
            "book_direction": order.book_direction,
            "book_price": order.book_price,
            "remaining_qty": order.remaining_quantity,
            "original_price": order.original_price,
            "frozen_amount": order.frozen_amount,
        })

    # IOC 未完全成交
    if (order.time_in_force == "IOC"
            and order.remaining_quantity > 0
            and order.filled_quantity > 0):
        await _insert_wal(db, market.id, "ORDER_EXPIRED", {
            "order_id": order.order_id,
            "expired_qty": order.remaining_quantity,
            "filled_qty": order.filled_quantity,
        })


async def _insert_wal(
    db: AsyncSession,
    market_id: str,
    event_type: str,
    payload: dict,
) -> None:
    """写入单条 WAL 审计事件"""
    await db.execute(
        text("""
            INSERT INTO wal_events (market_id, event_type, payload)
            VALUES (:market_id, :event_type, :payload)
        """),
        {
            "market_id": market_id,
            "event_type": event_type,
            "payload": json.dumps(payload),
        },
    )
```

### 4.4 审计日志清理

```python
async def cleanup_old_wal_events(
    retention_days: int = 30,
    db: AsyncSession = None,
) -> int:
    """
    定期清理过期的 WAL 审计日志

    默认保留 30 天, 由后台定时任务执行。
    """
    result = await db.execute(
        text("""
            DELETE FROM wal_events
            WHERE created_at < NOW() - INTERVAL ':days days'
        """),
        {"days": retention_days},
    )
    return result.rowcount
```

---

## 第五部分：熔断机制

### 5.1 设计原则

> **对齐权威设计方案 §8.4**: 恒等式校验失败时自动暂停市场 (status → HALTED)。
> **设计决策**: HALT + 告警，不自动回滚。已提交的 PostgreSQL 事务有 ACID 保证，自动回滚可能引入更大风险。

### 5.2 熔断触发条件

```python
class CircuitBreakerTrigger(str, Enum):
    """熔断触发原因分类"""

    # ── 恒等式失败 (最严重) ──
    INVARIANT_RESERVE = "INVARIANT_RESERVE"
    # reserve_balance != total_yes_shares * 100

    INVARIANT_COST_SUM = "INVARIANT_COST_SUM"
    # reserve + pnl_pool != Σ(cost_sum)

    INVARIANT_BALANCE = "INVARIANT_BALANCE"
    # Σ(available + frozen) + reserve + pnl_pool != 常量

    INVARIANT_SHARES = "INVARIANT_SHARES"
    # total_yes_shares != total_no_shares

    # ── 恢复失败 ──
    RECOVERY_FAILED = "RECOVERY_FAILED"
    # 内存订单簿恢复失败

    # ── 运行时异常 ──
    NEGATIVE_BALANCE = "NEGATIVE_BALANCE"
    # 检测到 available_balance 或 frozen_balance < 0

    ORDERBOOK_DESYNC = "ORDERBOOK_DESYNC"
    # 内存订单簿与 DB 订单表不一致

    CLEARING_ERROR = "CLEARING_ERROR"
    # 清算过程中遇到不可恢复错误
```

### 5.3 熔断执行流程

```python
async def trigger_circuit_breaker(
    market_id: str,
    reason: str,
    context: dict,
    db: AsyncSession,
) -> None:
    """
    触发市场熔断

    执行步骤:
    1. 将 market.status 更新为 HALTED
    2. 内存中驱逐该 market 的 OrderBook
    3. 记录熔断事件到 DB
    4. 发送告警
    5. 记录诊断日志

    ⚠️ HALTED 状态下:
    - 拒绝所有新订单
    - 拒绝所有取消请求 (防止状态进一步变化)
    - 允许查询接口 (持仓、成交历史等)
    """
    # 1. DB: market.status → HALTED
    await db.execute(
        text("""
            UPDATE markets
            SET status = 'HALTED', updated_at = NOW()
            WHERE id = :market_id AND status != 'HALTED'
        """),
        {"market_id": market_id},
    )

    # 2. 内存: 驱逐 OrderBook (而非仅标记)
    engine = get_matching_engine()
    engine._orderbooks.pop(market_id, None)

    # 3. DB: 记录熔断事件
    await db.execute(
        text("""
            INSERT INTO circuit_breaker_events
                (market_id, trigger_reason, context, triggered_at)
            VALUES (:market_id, :reason, :context, NOW())
        """),
        {
            "market_id": market_id,
            "reason": reason,
            "context": json.dumps(context),
        },
    )

    # 4. 告警 (异步, 不阻塞)
    await emit_alert(
        level="CRITICAL",
        title=f"市场熔断: {market_id}",
        body=f"原因: {reason}\n上下文: {json.dumps(context, indent=2)}",
        channels=["slack", "pagerduty"],
    )

    # 5. 诊断日志
    logger.critical(
        f"CIRCUIT_BREAKER market={market_id} reason={reason} "
        f"context={context}"
    )
```

### 5.4 熔断事件表

```sql
CREATE TABLE circuit_breaker_events (
    id              BIGSERIAL       PRIMARY KEY,
    market_id       VARCHAR(64)     NOT NULL,       -- → markets.id (逻辑外键, MVP不用DB级FK, 对齐DB v2.3 §1.5)
    trigger_reason  VARCHAR(50)     NOT NULL,
    context         JSONB           NOT NULL DEFAULT '{}',
    resolved_at     TIMESTAMPTZ,
    resolved_by     VARCHAR(50),
    resolution_note TEXT,
    triggered_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_cb_market ON circuit_breaker_events (market_id, triggered_at DESC);

COMMENT ON TABLE circuit_breaker_events IS '熔断事件记录, 含触发原因和人工解除信息';
```

### 5.5 熔断解除 (人工操作)

```python
async def resolve_circuit_breaker(
    market_id: str,
    resolved_by: str,
    resolution_note: str,
    target_status: str,  # "ACTIVE" 或 "SUSPENDED"
    db: AsyncSession,
) -> None:
    """
    人工解除熔断 — 管理员 API

    前置条件:
    1. market.status == HALTED
    2. 管理员已排查并确认问题已修复
    3. 恒等式校验通过

    步骤:
    1. 执行恒等式全量校验
    2. 重建内存订单簿 (从 DB)
    3. 更新 market.status
    4. 记录解除事件
    """
    # 1. 恒等式校验
    violations = await verify_market_invariants_full(market_id, db)
    if violations:
        raise CircuitBreakerResolveError(
            f"恒等式仍未通过: {violations}. 请先修复数据。"
        )

    # 2. 重建订单簿
    market = await get_market(market_id, db)
    recovery = MatchingEngineRecovery()
    await recovery.rebuild_orderbook(market, db)

    # 3. 更新状态
    await db.execute(
        text("""
            UPDATE markets
            SET status = :target_status, updated_at = NOW()
            WHERE id = :market_id AND status = 'HALTED'
        """),
        {"market_id": market_id, "target_status": target_status},
    )

    # 4. 记录解除
    await db.execute(
        text("""
            UPDATE circuit_breaker_events
            SET resolved_at = NOW(),
                resolved_by = :resolved_by,
                resolution_note = :note
            WHERE market_id = :market_id
              AND resolved_at IS NULL
        """),
        {
            "market_id": market_id,
            "resolved_by": resolved_by,
            "note": resolution_note,
        },
    )

    logger.info(
        f"CIRCUIT_BREAKER_RESOLVED market={market_id} "
        f"by={resolved_by} target={target_status}"
    )
```

---

## 第六部分：运行时一致性校验

### 6.1 校验时机

| 校验类型 | 触发时机 | 校验内容 | 失败处理 |
|---------|---------|---------|---------|
| **逐笔校验** | 每笔成交清算完成后 | 轻量级: 双方余额非负 | 熔断 |
| **事务后校验** | 每个下单事务提交前 | 中量级: 市场级恒等式 | 回滚事务 + 熔断 |
| **定时巡检** | 定时 (见 §6.4) | 重量级: 全量恒等式 (撮合设计 v1.2 §11) | 告警 + 可选熔断 |
| **启动校验** | 进程启动恢复后 | 全量校验 | 启动失败 / 熔断 |

### 6.2 逐笔校验 (热路径)

```python
async def verify_post_trade(
    trade: TradeResult,
    market: Market,
    db: AsyncSession,
) -> None:
    """
    成交后即时校验 — 在 execute_clearing 末尾调用

    仅做最轻量的检查, 不能拖慢热路径:
    1. 双方 available_balance >= 0 (CHECK 约束已保证, 此处防御性冗余)
    2. 双方 frozen_balance >= 0
    3. market.reserve_balance >= 0
    """
    for order in [trade.buy_order, trade.sell_order]:
        balance = await get_balance(order.user_id, db)
        if balance.available_balance < 0 or balance.frozen_balance < 0:
            await trigger_circuit_breaker(
                market.id,
                reason="NEGATIVE_BALANCE",
                context={
                    "user_id": order.user_id,
                    "available": balance.available_balance,
                    "frozen": balance.frozen_balance,
                    "trade_id": trade.trade_id,
                },
                db=db,
            )
            raise NegativeBalanceError(
                f"User {order.user_id} balance went negative"
            )

    if market.reserve_balance < 0:
        await trigger_circuit_breaker(
            market.id,
            reason="INVARIANT_RESERVE",
            context={
                "reserve_balance": market.reserve_balance,
                "trade_id": trade.trade_id,
            },
            db=db,
        )
        raise ReserveNegativeError(
            f"Market {market.id} reserve went negative"
        )
```

### 6.3 事务后校验 (中量级)

```python
async def verify_market_invariants_light(
    market: Market,
    db: AsyncSession,
) -> list[str]:
    """
    市场级恒等式校验 — 在下单事务 COMMIT 前调用

    校验项 (对齐撮合设计 v1.2 §11):
    1. total_yes_shares == total_no_shares
    2. reserve_balance == total_yes_shares * 100
    3. reserve + pnl_pool == Σ(positions.yes_cost_sum + no_cost_sum)

    返回违规列表, 空 = 全部通过
    """
    violations = []

    # 1. YES/NO 份数对称
    if market.total_yes_shares != market.total_no_shares:
        violations.append(
            f"SHARES_ASYMMETRY: yes={market.total_yes_shares} "
            f"!= no={market.total_no_shares}"
        )

    # 2. Reserve = shares × 100
    expected_reserve = market.total_yes_shares * 100
    if market.reserve_balance != expected_reserve:
        violations.append(
            f"RESERVE_MISMATCH: actual={market.reserve_balance} "
            f"expected={expected_reserve}"
        )

    # 3. Reserve + pnl_pool == Σ(cost_sum)
    total_cost = await db.execute(
        text("""
            SELECT COALESCE(SUM(yes_cost_sum + no_cost_sum), 0)
            FROM positions WHERE market_id = :mid
        """),
        {"mid": market.id},
    )
    cost_sum = total_cost.scalar()

    if market.reserve_balance + market.pnl_pool != cost_sum:
        violations.append(
            f"COST_INVARIANT: reserve({market.reserve_balance}) + "
            f"pnl_pool({market.pnl_pool}) = "
            f"{market.reserve_balance + market.pnl_pool} "
            f"!= cost_sum={cost_sum}"
        )

    return violations
```

### 6.4 定时巡检 (全量)

```python
async def scheduled_invariant_check(db: AsyncSession) -> None:
    """
    定时全量恒等式校验

    对所有 ACTIVE 市场执行完整的恒等式校验
    (对齐撮合设计 v1.2 §11 的全部恒等式)

    由后台 asyncio Task 驱动, 不在撮合热路径上。

    ⚠️ 性能备注: positions 和 ledger_entries 表随交易增长。
    MVP 初期 60 秒间隔可接受; 中期优化方向:
      - 延长至 5-10 分钟, 或仅在系统低谷期执行
      - 依靠轻量级的逐笔校验 (§6.2) 和事务后校验 (§6.3) 作为主防线
      - 为 positions 表添加 (market_id) 覆盖索引加速 SUM 聚合
    """
    INTERVAL_SECONDS = 60  # MVP; 中期可调整为 300-600

    active_markets = await get_markets_by_status(["ACTIVE"], db)

    for market in active_markets:
        violations = await verify_market_invariants_full(market.id, db)

        if violations:
            logger.error(
                f"INVARIANT_VIOLATION market={market.id} "
                f"violations={violations}"
            )

            # 资金相关违规 → 熔断
            severe = [v for v in violations
                     if v.startswith(("RESERVE_", "COST_", "BALANCE_"))]
            if severe:
                await trigger_circuit_breaker(
                    market.id,
                    reason="INVARIANT_" + severe[0].split(":")[0],
                    context={"violations": violations},
                    db=db,
                )
            else:
                # 非资金违规 (如份数偏差) → 仅告警
                await emit_alert(
                    level="WARNING",
                    title=f"恒等式偏差: {market.id}",
                    body="\n".join(violations),
                    channels=["slack"],
                )


async def verify_market_invariants_full(
    market_id: str,
    db: AsyncSession,
) -> list[str]:
    """
    全量恒等式校验 — 对齐撮合设计 v1.2 §11

    5 项核心恒等式:
    1. total_yes_shares == total_no_shares
    2. reserve_balance == total_yes_shares × 100
    3. reserve + pnl_pool == Σ(cost_sum)
    4. Σ(ledger_entries.amount) == 0 (复式记账守恒)
    5. 每个用户: yes_volume >= pending_sell_yes, no_volume >= pending_sell_no
    """
    violations = []
    market = await get_market(market_id, db)

    # 1. 份数对称
    if market.total_yes_shares != market.total_no_shares:
        violations.append(
            f"SHARES_ASYMMETRY: yes={market.total_yes_shares} "
            f"!= no={market.total_no_shares}"
        )

    # 2. Reserve 一致
    expected_reserve = market.total_yes_shares * 100
    if market.reserve_balance != expected_reserve:
        violations.append(
            f"RESERVE_MISMATCH: actual={market.reserve_balance} "
            f"vs expected={expected_reserve}"
        )

    # 3. 成本恒等式
    cost_result = await db.execute(
        text("""
            SELECT COALESCE(SUM(yes_cost_sum + no_cost_sum), 0)
            FROM positions WHERE market_id = :mid
        """),
        {"mid": market_id},
    )
    total_cost = cost_result.scalar()
    if market.reserve_balance + market.pnl_pool != total_cost:
        violations.append(
            f"COST_INVARIANT: reserve+pnl="
            f"{market.reserve_balance + market.pnl_pool} "
            f"!= cost_sum={total_cost}"
        )

    # 4. 复式记账守恒
    ledger_result = await db.execute(
        text("""
            SELECT COALESCE(SUM(amount), 0)
            FROM ledger_entries WHERE market_id = :mid
        """),
        {"mid": market_id},
    )
    ledger_sum = ledger_result.scalar()
    if ledger_sum != 0:
        violations.append(
            f"LEDGER_IMBALANCE: SUM(amount)={ledger_sum} != 0"
        )

    # 5. 持仓 >= 冻结
    oversold = await db.execute(
        text("""
            SELECT user_id,
                   yes_volume, yes_pending_sell,
                   no_volume, no_pending_sell
            FROM positions
            WHERE market_id = :mid
              AND (yes_volume < yes_pending_sell
                   OR no_volume < no_pending_sell)
        """),
        {"mid": market_id},
    )
    for row in oversold:
        violations.append(
            f"OVERSOLD: user={row.user_id} "
            f"yes_vol={row.yes_volume}<pending={row.yes_pending_sell} or "
            f"no_vol={row.no_volume}<pending={row.no_pending_sell}"
        )

    return violations
```

---

## 第七部分：监控与告警

### 7.1 核心监控指标

| 指标名 | 类型 | 含义 | 告警阈值 |
|--------|------|------|---------|
| `matching.recovery.duration_ms` | Histogram | 单个 market DB 重建耗时 | > 100ms WARNING |
| `matching.recovery.count` | Counter | DB 重建次数 (含 lazy rebuild) | 短期激增 WARNING |
| `matching.eviction.count` | Counter | 事务失败触发的 OrderBook 驱逐次数 | > 0 WARNING |
| `matching.wal.write_duration_ms` | Histogram | WAL 审计事件写入耗时 | P99 > 5ms WARNING |
| `matching.invariant.check_duration_ms` | Histogram | 恒等式校验耗时 | P99 > 100ms WARNING |
| `matching.invariant.violations` | Counter | 恒等式违规次数 | > 0 CRITICAL |
| `matching.circuit_breaker.triggered` | Counter | 熔断触发次数 | > 0 CRITICAL |
| `matching.orderbook.order_count` | Gauge | 内存订单簿中的订单数 | 用于容量规划 |
| `matching.transaction.rollback_count` | Counter | 撮合事务回滚次数 | 短期激增 WARNING |

### 7.2 告警通道配置

```python
ALERT_CONFIG = {
    "CRITICAL": {
        "channels": ["slack", "pagerduty", "email"],
        "escalation_minutes": 5,
        "description": "资金安全风险, 需立即响应",
    },
    "WARNING": {
        "channels": ["slack"],
        "escalation_minutes": 30,
        "description": "性能或一致性偏差, 需关注",
    },
    "INFO": {
        "channels": ["slack"],
        "escalation_minutes": None,
        "description": "常规运维事件, 仅记录",
    },
}
```

### 7.3 健康检查端点

```python
# GET /api/v1/health/matching

async def health_check(db: AsyncSession) -> dict:
    """
    撮合引擎健康检查

    返回各 market 的状态摘要, 供负载均衡器和监控系统使用。
    """
    engine = get_matching_engine()
    markets = await get_markets_by_status(
        ["ACTIVE", "SUSPENDED", "HALTED"], db
    )

    market_health = {}
    for market in markets:
        orderbook = engine._orderbooks.get(market.id)
        market_health[market.id] = {
            "status": market.status,
            "orderbook_loaded": orderbook is not None,
            "order_count": (
                len(orderbook._order_index) if orderbook else 0
            ),
        }

    halted_count = sum(1 for m in markets if m.status == "HALTED")

    return {
        "status": "degraded" if halted_count > 0 else "healthy",
        "halted_markets": halted_count,
        "active_markets": len(markets) - halted_count,
        "markets": market_health,
    }
```

---

## 附录

### 附录 A：DB 新增表汇总

本文档引入 2 张新表（v1.0 的 `orderbook_snapshots` 在 v1.1 中移除）：

| 表名 | 用途 | 关联 |
|------|------|------|
| `wal_events` | 订单簿变更审计日志 (不参与恢复) | §4.1 |
| `circuit_breaker_events` | 熔断事件记录 | §5.4 |

新增 1 个索引：

| 索引名 | 用途 | 关联 |
|--------|------|------|
| `idx_orders_market_active` | 加速 DB 全量重建查询 | §2.3 |

这些应追加到 DB v2.2 的下一次更新中。

### 附录 B：orders 表必需字段确认

DB 全量重建 (§2.4) 依赖 orders 表包含以下 BookOrder 恢复所需的字段：

| 字段 | 用途 | DB v2.2 现状 |
|------|------|-------------|
| `book_type` | NATIVE_BUY / SYNTHETIC_SELL 等 | ✅ DB v2.2 VARCHAR(20) |
| `book_direction` | BUY / SELL (订单簿视角) | ✅ DB v2.2 VARCHAR(10) |
| `book_price` | 订单簿价格 (YES 视角) | ✅ DB v2.2 SMALLINT |
| `frozen_asset_type` | FUNDS / SHARES | ✅ DB v2.2 VARCHAR(20) |
| `remaining_quantity` | 剩余未成交量 | ✅ DB v2.2 INT |
| `frozen_amount` | 当前冻结额 | ✅ DB v2.2 BIGINT |

所有恢复所需字段均已在 DB v2.2 的 orders 表中持久化，DB 全量重建可直接使用。

### 附录 C：跨文档引用

| 引用内容 | 所在文档 | 章节 |
|---------|---------|------|
| 内存订单簿 OrderBook 数据结构 | 撮合设计 v1.2 | §3.1 |
| per-market asyncio.Lock | 撮合设计 v1.2 | §2.1 |
| finalize_order 入簿逻辑 | 撮合设计 v1.2 | §5.3 |
| match_order 撮合算法 | 撮合设计 v1.2 | §5.1 |
| execute_clearing 清算入口 | 撮合设计 v1.2 | §7.1 |
| 取消订单 cancel_order | 撮合设计 v1.2 | §9.2 |
| 恒等式定义 | 撮合设计 v1.2 | §11 |
| market.status 状态机 | DB v2.2 | §2.3 |
| HALTED 状态定义 | DB v2.2 | §2.3 |
| orders 表 schema | DB v2.2 | §2.4 |
| ledger_entries 表 | DB v2.2 | §2.7 |
| 熔断机制 (权威) | 设计方案 v1 | §8.4 |

### 附录 D：版本历史

| 版本 | 日期 | 主要变更 |
|------|------|---------|
| v1.0 | 2026-02-20 | 初始版本。双路径恢复: 快照+WAL / DB重建。WAL存储: PostgreSQL表。 |
| v1.1 | 2026-02-20 | **架构简化**: 砍掉快照+WAL恢复路径, 纯 DB 全量重建。**P0-1**: 新增事务回滚内存保护 (evict + lazy rebuild)。**P0-3**: WAL 降级为审计日志 (新增 ORDER_PARTIALLY_FILLED 事件)。**P1-4**: 移除 WAL sequence_id 严格连续性要求。**P2-5**: 添加巡检频率优化备注。 |

---

*文档结束*
