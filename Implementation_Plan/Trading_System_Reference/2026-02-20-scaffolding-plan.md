# Project Scaffolding & pm_common Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Set up the complete project scaffold (Module 0) and implement the pm_common shared utilities (Module 1) for the prediction market platform.

**Architecture:** Mono-repo with DEV_MVP/ as git root. FastAPI async app with SQLAlchemy 2.0 async + asyncpg + PostgreSQL 16. All 9 DB tables created via raw SQL Alembic migrations matching design doc DDL exactly. pm_common provides enums, error handling, response wrapper, cents utilities, ID generation, and database/Redis clients.

**Tech Stack:** Python 3.12+, uv, FastAPI, SQLAlchemy 2.0 async, asyncpg, PostgreSQL 16, Redis 7, Alembic, Pydantic v2, uvloop, pytest, ruff, mypy

**Upstream Design Docs (read these if confused):**
- `Planning/预测市场平台_完整实施计划_v4_Python.md` — master plan v4.1
- `Planning/Detail_Design/01_全局约定与数据库设计.md` — DB schema v2.3 (DDL source of truth)
- `Planning/Detail_Design/02_API接口契约.md` — API contracts v1.2 (error codes, response format)
- `Planning/Implementation/2026-02-20-scaffolding-design.md` — this plan's design doc

---

## Task 1: Git Init + Project Config Files

**Files:**
- Create: `.gitignore`
- Create: `.python-version`
- Create: `pyproject.toml`
- Create: `mypy.ini`
- Create: `ruff.toml`

**Step 1: Initialize git repo**

```bash
cd /Users/pangpanghu007/Documents/Python_Project/predict_market/DEV_MVP
git init
```

**Step 2: Create .gitignore**

```gitignore
# Python
__pycache__/
*.py[cod]
*$py.class
*.egg-info/
dist/
build/
.eggs/

# Virtual env
.venv/
venv/
ENV/

# IDE
.idea/
.vscode/
*.swp
*.swo

# Environment
.env
!.env.example

# Docker
docker-compose.override.yml

# OS
.DS_Store
Thumbs.db

# Testing
.pytest_cache/
htmlcov/
.coverage
coverage.xml

# mypy
.mypy_cache/

# ruff
.ruff_cache/

# uv
uv.lock
```

**Step 3: Create .python-version**

```
3.12
```

**Step 4: Create pyproject.toml**

```toml
[project]
name = "prediction-market"
version = "0.1.0"
description = "Binary Prediction Market Platform — Single-Ledger Matching Engine"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.109",
    "uvicorn[standard]>=0.27",
    "uvloop>=0.19",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.29",
    "alembic>=1.13",
    "pydantic>=2.5",
    "pydantic-settings>=2.1",
    "python-jose[cryptography]>=3.3",
    "passlib[bcrypt]>=1.7",
    "redis>=5.0",
]

[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "httpx>=0.26",
    "mypy>=1.8",
    "ruff>=0.2",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

**Step 5: Create mypy.ini**

```ini
[mypy]
python_version = 3.12
strict = True
warn_return_any = True
warn_unused_configs = True
disallow_untyped_defs = True
plugins = [
    "pydantic.mypy",
]

[mypy.plugins.pydantic-mypy]
init_forbid_extra = True
init_typed = True

[pydantic-mypy]
init_forbid_extra = True
init_typed = True
```

**Step 6: Create ruff.toml**

```toml
target-version = "py312"
line-length = 100

[lint]
select = ["E", "W", "F", "I", "UP", "B", "SIM"]

[format]
quote-style = "double"
```

**Step 7: Install dependencies with uv**

```bash
cd /Users/pangpanghu007/Documents/Python_Project/predict_market/DEV_MVP
uv sync
```

**Step 8: Commit**

```bash
git add .gitignore .python-version pyproject.toml mypy.ini ruff.toml
git commit -m "chore: init project with uv, mypy strict, ruff config"
```

---

## Task 2: Docker Compose + Environment Config

**Files:**
- Create: `docker-compose.yml`
- Create: `.env.example`
- Create: `config/__init__.py`
- Create: `config/settings.py`

**Step 1: Create docker-compose.yml**

```yaml
services:
  postgres:
    image: postgres:16-alpine
    container_name: pm-postgres
    environment:
      POSTGRES_USER: pm_user
      POSTGRES_PASSWORD: pm_pass
      POSTGRES_DB: prediction_market
    ports:
      - "5432:5432"
    volumes:
      - pm_pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U pm_user -d prediction_market"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    container_name: pm-redis
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5

volumes:
  pm_pgdata:
```

**Step 2: Create .env.example**

```env
# Database
DATABASE_URL=postgresql+asyncpg://pm_user:pm_pass@localhost:5432/prediction_market

# Redis
REDIS_URL=redis://localhost:6379/0

# JWT
JWT_SECRET=change-me-in-production
JWT_ALGORITHM=HS256
JWT_EXPIRE_MINUTES=30

# App
APP_NAME=Prediction Market
DEBUG=True
```

**Step 3: Create config/settings.py**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # Database
    DATABASE_URL: str = (
        "postgresql+asyncpg://pm_user:pm_pass@localhost:5432/prediction_market"
    )

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 30

    # App
    APP_NAME: str = "Prediction Market"
    DEBUG: bool = False


settings = Settings()
```

**Step 4: Create config/__init__.py**

```python
from config.settings import settings

__all__ = ["settings"]
```

**Step 5: Start Docker containers to verify**

```bash
docker-compose up -d
docker-compose ps
```
Expected: Both `pm-postgres` and `pm-redis` healthy.

**Step 6: Commit**

```bash
git add docker-compose.yml .env.example config/
git commit -m "chore: add Docker Compose (PG16 + Redis7) and Pydantic settings"
```

---

## Task 3: SQLAlchemy Async Engine + Alembic Init

**Files:**
- Create: `src/__init__.py`
- Create: `src/pm_common/__init__.py`
- Create: `src/pm_common/database.py`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/` (empty dir)

**Step 1: Create src/__init__.py and src/pm_common/__init__.py**

Both empty files (just makes them packages).

**Step 2: Create src/pm_common/database.py**

```python
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from config.settings import settings


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models across modules."""

    pass


engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=20,
    max_overflow=10,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields an AsyncSession, auto-closes after request."""
    async with async_session_factory() as session:
        yield session
```

**Step 3: Initialize Alembic**

```bash
cd /Users/pangpanghu007/Documents/Python_Project/predict_market/DEV_MVP
uv run alembic init alembic
```

**Step 4: Edit alembic.ini**

Set `sqlalchemy.url` to empty (we'll override in env.py):

```ini
[alembic]
script_location = alembic
sqlalchemy.url =

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

**Step 5: Edit alembic/env.py**

Replace the generated env.py with:

```python
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from config.settings import settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None  # Raw SQL migrations, no autogenerate


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = create_async_engine(settings.DATABASE_URL)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

**Step 6: Verify Alembic connects**

```bash
cd /Users/pangpanghu007/Documents/Python_Project/predict_market/DEV_MVP
uv run alembic current
```
Expected: No errors, shows "(head)" or empty.

**Step 7: Commit**

```bash
git add src/ alembic.ini alembic/
git commit -m "chore: add SQLAlchemy async engine and Alembic setup"
```

---

## Task 4: Database Migrations 001–003 (Functions, Users, Accounts)

**Files:**
- Create: `alembic/versions/001_create_common_functions.py`
- Create: `alembic/versions/002_create_users.py`
- Create: `alembic/versions/003_create_accounts.py`

**Ref:** Design doc `Planning/Detail_Design/01_全局约定与数据库设计.md` §2.0–§2.2

**Step 1: Create 001_create_common_functions.py**

```python
"""001: create common functions

Revision ID: 001
Revises:
Create Date: 2026-02-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION fn_update_timestamp()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS fn_update_timestamp();")
```

**Step 2: Create 002_create_users.py**

```python
"""002: create users table

Revision ID: 002
Revises: 001
Create Date: 2026-02-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE users (
            id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            username        VARCHAR(64)     NOT NULL,
            email           VARCHAR(255)    NOT NULL,
            password_hash   VARCHAR(255)    NOT NULL,
            is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_users_username    UNIQUE (username),
            CONSTRAINT uq_users_email       UNIQUE (email),
            CONSTRAINT ck_users_username_len CHECK (LENGTH(username) >= 3)
        );
    """)
    op.execute("CREATE INDEX idx_users_email ON users (email);")
    op.execute("""
        CREATE TRIGGER trg_users_updated_at
            BEFORE UPDATE ON users
            FOR EACH ROW EXECUTE FUNCTION fn_update_timestamp();
    """)
    op.execute("COMMENT ON TABLE users IS '用户表 — 注册/登录认证';")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS users CASCADE;")
```

**Step 3: Create 003_create_accounts.py**

```python
"""003: create accounts table

Revision ID: 003
Revises: 002
Create Date: 2026-02-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE accounts (
            id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id             VARCHAR(64) NOT NULL,
            available_balance   BIGINT      NOT NULL DEFAULT 0,
            frozen_balance      BIGINT      NOT NULL DEFAULT 0,
            version             BIGINT      NOT NULL DEFAULT 0,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_accounts_user_id          UNIQUE (user_id),
            CONSTRAINT ck_accounts_available_gte_0  CHECK (available_balance >= 0),
            CONSTRAINT ck_accounts_frozen_gte_0     CHECK (frozen_balance >= 0)
        );
    """)
    op.execute("""
        CREATE TRIGGER trg_accounts_updated_at
            BEFORE UPDATE ON accounts
            FOR EACH ROW EXECUTE FUNCTION fn_update_timestamp();
    """)
    op.execute(
        "COMMENT ON TABLE accounts IS "
        "'用户资金账户 — 所有金额单位: 美分 (cents)';"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS accounts CASCADE;")
```

**Step 4: Run migrations to verify**

```bash
uv run alembic upgrade head
```
Expected: 3 migrations applied successfully.

**Step 5: Verify tables exist**

```bash
docker exec pm-postgres psql -U pm_user -d prediction_market -c "\dt"
```
Expected: `users` and `accounts` tables listed.

**Step 6: Commit**

```bash
git add alembic/versions/001_create_common_functions.py alembic/versions/002_create_users.py alembic/versions/003_create_accounts.py
git commit -m "feat: add migrations 001-003 (functions, users, accounts)"
```

---

## Task 5: Database Migrations 004–006 (Markets, Orders, Trades)

**Files:**
- Create: `alembic/versions/004_create_markets.py`
- Create: `alembic/versions/005_create_orders.py`
- Create: `alembic/versions/006_create_trades.py`

**Ref:** Design doc `Planning/Detail_Design/01_全局约定与数据库设计.md` §2.3–§2.5

**Step 1: Create 004_create_markets.py**

```python
"""004: create markets table

Revision ID: 004
Revises: 003
Create Date: 2026-02-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE markets (
            id                      VARCHAR(64)     PRIMARY KEY,
            title                   VARCHAR(500)    NOT NULL,
            description             TEXT,
            category                VARCHAR(64),
            status                  VARCHAR(20)     NOT NULL DEFAULT 'DRAFT',
            min_price_cents         SMALLINT        NOT NULL DEFAULT 1,
            max_price_cents         SMALLINT        NOT NULL DEFAULT 99,
            max_order_quantity      INT             NOT NULL DEFAULT 10000,
            max_position_per_user   INT             NOT NULL DEFAULT 25000,
            max_order_amount_cents  BIGINT          NOT NULL DEFAULT 1000000,
            maker_fee_bps           SMALLINT        NOT NULL DEFAULT 10,
            taker_fee_bps           SMALLINT        NOT NULL DEFAULT 20,
            trading_start_at        TIMESTAMPTZ,
            trading_end_at          TIMESTAMPTZ,
            resolution_date         TIMESTAMPTZ,
            resolved_at             TIMESTAMPTZ,
            settled_at              TIMESTAMPTZ,
            resolution_result       VARCHAR(10),
            reserve_balance         BIGINT          NOT NULL DEFAULT 0,
            pnl_pool                BIGINT          NOT NULL DEFAULT 0,
            total_yes_shares        BIGINT          NOT NULL DEFAULT 0,
            total_no_shares         BIGINT          NOT NULL DEFAULT 0,
            created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_markets_reserve_gte_0          CHECK (reserve_balance >= 0),
            CONSTRAINT ck_markets_yes_shares_gte_0       CHECK (total_yes_shares >= 0),
            CONSTRAINT ck_markets_no_shares_gte_0        CHECK (total_no_shares >= 0),
            CONSTRAINT ck_markets_shares_balanced        CHECK (total_yes_shares = total_no_shares),
            CONSTRAINT ck_markets_reserve_consistency    CHECK (reserve_balance = total_yes_shares * 100),
            CONSTRAINT ck_markets_status CHECK (
                status IN ('DRAFT', 'ACTIVE', 'SUSPENDED', 'HALTED', 'RESOLVED', 'SETTLED', 'VOIDED')
            ),
            CONSTRAINT ck_markets_price_range CHECK (
                min_price_cents >= 1 AND max_price_cents <= 99
                AND min_price_cents < max_price_cents
            ),
            CONSTRAINT ck_markets_fee CHECK (
                maker_fee_bps >= 0 AND maker_fee_bps <= 1000
                AND taker_fee_bps >= 0 AND taker_fee_bps <= 1000
            ),
            CONSTRAINT ck_markets_resolution CHECK (
                resolution_result IS NULL OR resolution_result IN ('YES', 'NO', 'VOID')
            )
        );
    """)
    op.execute("CREATE INDEX idx_markets_status ON markets (status);")
    op.execute("""
        CREATE TRIGGER trg_markets_updated_at
            BEFORE UPDATE ON markets
            FOR EACH ROW EXECUTE FUNCTION fn_update_timestamp();
    """)
    op.execute(
        "COMMENT ON TABLE markets IS "
        "'预测话题 — 交易规则/风控参数/单账本托管状态/裁决结果';"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS markets CASCADE;")
```

**Step 2: Create 005_create_orders.py**

```python
"""005: create orders table

Revision ID: 005
Revises: 004
Create Date: 2026-02-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE orders (
            id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            client_order_id     VARCHAR(64)     NOT NULL,
            market_id           VARCHAR(64)     NOT NULL,
            user_id             VARCHAR(64)     NOT NULL,
            original_side       VARCHAR(10)     NOT NULL,
            original_direction  VARCHAR(10)     NOT NULL,
            original_price      SMALLINT        NOT NULL,
            book_type           VARCHAR(20)     NOT NULL,
            book_direction      VARCHAR(10)     NOT NULL,
            book_price          SMALLINT        NOT NULL,
            price_type          VARCHAR(20)     NOT NULL DEFAULT 'LIMIT',
            time_in_force       VARCHAR(10)     NOT NULL DEFAULT 'GTC',
            quantity            INT             NOT NULL,
            filled_quantity     INT             NOT NULL DEFAULT 0,
            remaining_quantity  INT             NOT NULL,
            frozen_amount       BIGINT          NOT NULL DEFAULT 0,
            frozen_asset_type   VARCHAR(20)     NOT NULL,
            status              VARCHAR(20)     NOT NULL DEFAULT 'NEW',
            cancel_reason       VARCHAR(100),
            created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_orders_client_order_id    UNIQUE (client_order_id),
            CONSTRAINT ck_orders_original_side      CHECK (original_side IN ('YES', 'NO')),
            CONSTRAINT ck_orders_original_direction CHECK (original_direction IN ('BUY', 'SELL')),
            CONSTRAINT ck_orders_original_price     CHECK (original_price BETWEEN 1 AND 99),
            CONSTRAINT ck_orders_book_type          CHECK (
                book_type IN ('NATIVE_BUY', 'NATIVE_SELL', 'SYNTHETIC_BUY', 'SYNTHETIC_SELL')
            ),
            CONSTRAINT ck_orders_book_direction     CHECK (book_direction IN ('BUY', 'SELL')),
            CONSTRAINT ck_orders_book_price         CHECK (book_price BETWEEN 1 AND 99),
            CONSTRAINT ck_orders_price_type         CHECK (price_type IN ('LIMIT')),
            CONSTRAINT ck_orders_tif                CHECK (time_in_force IN ('GTC', 'IOC')),
            CONSTRAINT ck_orders_quantity           CHECK (quantity > 0),
            CONSTRAINT ck_orders_filled             CHECK (filled_quantity >= 0 AND filled_quantity <= quantity),
            CONSTRAINT ck_orders_remaining          CHECK (remaining_quantity >= 0 AND remaining_quantity <= quantity),
            CONSTRAINT ck_orders_fill_consistency   CHECK (filled_quantity + remaining_quantity = quantity),
            CONSTRAINT ck_orders_frozen_amount_gte_0 CHECK (frozen_amount >= 0),
            CONSTRAINT ck_orders_frozen_asset_type  CHECK (frozen_asset_type IN ('FUNDS', 'YES_SHARES', 'NO_SHARES')),
            CONSTRAINT ck_orders_book_type_dir_match CHECK (
                (book_type IN ('NATIVE_BUY', 'SYNTHETIC_BUY') AND book_direction = 'BUY') OR
                (book_type IN ('NATIVE_SELL', 'SYNTHETIC_SELL') AND book_direction = 'SELL')
            ),
            CONSTRAINT ck_orders_status             CHECK (
                status IN ('NEW', 'OPEN', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'REJECTED')
            )
        );
    """)
    op.execute(
        "CREATE INDEX idx_orders_user_status "
        "ON orders (user_id, status, created_at DESC);"
    )
    op.execute(
        "CREATE INDEX idx_orders_market_active "
        "ON orders (market_id, created_at) "
        "WHERE status IN ('OPEN', 'PARTIALLY_FILLED');"
    )
    op.execute(
        "CREATE INDEX idx_orders_self_trade "
        "ON orders (market_id, user_id, book_direction) "
        "WHERE status IN ('OPEN', 'PARTIALLY_FILLED');"
    )
    op.execute("""
        CREATE TRIGGER trg_orders_updated_at
            BEFORE UPDATE ON orders
            FOR EACH ROW EXECUTE FUNCTION fn_update_timestamp();
    """)
    op.execute(
        "COMMENT ON TABLE orders IS "
        "'用户订单 — 单账本架构, 同时记录原始意图和订单簿视角';"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS orders CASCADE;")
```

**Step 3: Create 006_create_trades.py**

```python
"""006: create trades table

Revision ID: 006
Revises: 005
Create Date: 2026-02-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE trades (
            id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
            trade_id        VARCHAR(64)     NOT NULL,
            market_id       VARCHAR(64)     NOT NULL,
            scenario        VARCHAR(20)     NOT NULL,
            buy_order_id    UUID            NOT NULL,
            sell_order_id   UUID            NOT NULL,
            buy_user_id     VARCHAR(64)     NOT NULL,
            sell_user_id    VARCHAR(64)     NOT NULL,
            buy_book_type   VARCHAR(20)     NOT NULL,
            sell_book_type  VARCHAR(20)     NOT NULL,
            price           SMALLINT        NOT NULL,
            quantity        INT             NOT NULL,
            maker_order_id  UUID            NOT NULL,
            taker_order_id  UUID            NOT NULL,
            maker_fee       BIGINT          NOT NULL DEFAULT 0,
            taker_fee       BIGINT          NOT NULL DEFAULT 0,
            buy_realized_pnl  BIGINT        DEFAULT NULL,
            sell_realized_pnl BIGINT        DEFAULT NULL,
            executed_at     TIMESTAMPTZ     NOT NULL,
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_trades_trade_id       UNIQUE (trade_id),
            CONSTRAINT ck_trades_scenario       CHECK (
                scenario IN ('MINT', 'TRANSFER_YES', 'TRANSFER_NO', 'BURN')
            ),
            CONSTRAINT ck_trades_buy_book_type  CHECK (
                buy_book_type IN ('NATIVE_BUY', 'SYNTHETIC_BUY')
            ),
            CONSTRAINT ck_trades_sell_book_type CHECK (
                sell_book_type IN ('NATIVE_SELL', 'SYNTHETIC_SELL')
            ),
            CONSTRAINT ck_trades_price          CHECK (price BETWEEN 1 AND 99),
            CONSTRAINT ck_trades_quantity       CHECK (quantity > 0),
            CONSTRAINT ck_trades_fee_gte_0      CHECK (maker_fee >= 0 AND taker_fee >= 0),
            CONSTRAINT ck_trades_diff_users     CHECK (buy_user_id != sell_user_id)
        );
    """)
    op.execute(
        "CREATE INDEX idx_trades_buy_user ON trades (buy_user_id, executed_at DESC);"
    )
    op.execute(
        "CREATE INDEX idx_trades_sell_user ON trades (sell_user_id, executed_at DESC);"
    )
    op.execute(
        "CREATE INDEX idx_trades_market_time ON trades (market_id, executed_at DESC);"
    )
    op.execute(
        "CREATE INDEX idx_trades_scenario ON trades (market_id, scenario);"
    )
    op.execute(
        "COMMENT ON TABLE trades IS '撮合成交记录 — 单账本架构, 含撮合场景';"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trades CASCADE;")
```

**Step 4: Run migrations**

```bash
uv run alembic upgrade head
```
Expected: 3 new migrations applied (004, 005, 006).

**Step 5: Commit**

```bash
git add alembic/versions/004_create_markets.py alembic/versions/005_create_orders.py alembic/versions/006_create_trades.py
git commit -m "feat: add migrations 004-006 (markets, orders, trades)"
```

---

## Task 6: Database Migrations 007–010 (Positions, Ledger, WAL, Circuit Breaker)

**Files:**
- Create: `alembic/versions/007_create_positions.py`
- Create: `alembic/versions/008_create_ledger_entries.py`
- Create: `alembic/versions/009_create_wal_events.py`
- Create: `alembic/versions/010_create_circuit_breaker_events.py`

**Ref:** Design doc `Planning/Detail_Design/01_全局约定与数据库设计.md` §2.6–§2.8

**Step 1: Create 007_create_positions.py**

```python
"""007: create positions table

Revision ID: 007
Revises: 006
Create Date: 2026-02-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE positions (
            id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id             VARCHAR(64) NOT NULL,
            market_id           VARCHAR(64) NOT NULL,
            yes_volume          INT         NOT NULL DEFAULT 0,
            yes_cost_sum        BIGINT      NOT NULL DEFAULT 0,
            yes_pending_sell    INT         NOT NULL DEFAULT 0,
            no_volume           INT         NOT NULL DEFAULT 0,
            no_cost_sum         BIGINT      NOT NULL DEFAULT 0,
            no_pending_sell     INT         NOT NULL DEFAULT 0,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_positions_user_market         UNIQUE (user_id, market_id),
            CONSTRAINT ck_positions_yes_volume_gte_0    CHECK (yes_volume >= 0),
            CONSTRAINT ck_positions_yes_cost_gte_0      CHECK (yes_cost_sum >= 0),
            CONSTRAINT ck_positions_yes_pending_gte_0   CHECK (yes_pending_sell >= 0),
            CONSTRAINT ck_positions_yes_pending_lte_vol CHECK (yes_pending_sell <= yes_volume),
            CONSTRAINT ck_positions_no_volume_gte_0     CHECK (no_volume >= 0),
            CONSTRAINT ck_positions_no_cost_gte_0       CHECK (no_cost_sum >= 0),
            CONSTRAINT ck_positions_no_pending_gte_0    CHECK (no_pending_sell >= 0),
            CONSTRAINT ck_positions_no_pending_lte_vol  CHECK (no_pending_sell <= no_volume)
        );
    """)
    op.execute("CREATE INDEX idx_positions_user ON positions (user_id);")
    op.execute("""
        CREATE TRIGGER trg_positions_updated_at
            BEFORE UPDATE ON positions
            FOR EACH ROW EXECUTE FUNCTION fn_update_timestamp();
    """)
    op.execute(
        "COMMENT ON TABLE positions IS "
        "'用户持仓 — 单账本架构, 每用户每话题一行, YES/NO 合并';"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS positions CASCADE;")
```

**Step 2: Create 008_create_ledger_entries.py**

```python
"""008: create ledger_entries table

Revision ID: 008
Revises: 007
Create Date: 2026-02-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ledger_entries (
            id              BIGSERIAL       PRIMARY KEY,
            user_id         VARCHAR(64)     NOT NULL,
            entry_type      VARCHAR(30)     NOT NULL,
            amount          BIGINT          NOT NULL,
            balance_after   BIGINT          NOT NULL,
            reference_type  VARCHAR(30),
            reference_id    VARCHAR(64),
            description     VARCHAR(500),
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_ledger_entry_type CHECK (
                entry_type IN (
                    'DEPOSIT', 'WITHDRAW',
                    'ORDER_FREEZE', 'ORDER_UNFREEZE',
                    'MINT_COST', 'MINT_RESERVE_IN',
                    'BURN_REVENUE', 'BURN_RESERVE_OUT',
                    'TRANSFER_PAYMENT', 'TRANSFER_RECEIPT',
                    'NETTING', 'NETTING_RESERVE_OUT',
                    'FEE', 'FEE_REVENUE',
                    'SETTLEMENT_PAYOUT', 'SETTLEMENT_VOID'
                )
            ),
            CONSTRAINT ck_ledger_balance_gte_0 CHECK (balance_after >= 0)
        );
    """)
    op.execute(
        "CREATE INDEX idx_ledger_user_time "
        "ON ledger_entries (user_id, created_at DESC);"
    )
    op.execute(
        "CREATE INDEX idx_ledger_reference "
        "ON ledger_entries (reference_type, reference_id) "
        "WHERE reference_id IS NOT NULL;"
    )
    op.execute(
        "CREATE INDEX idx_ledger_type "
        "ON ledger_entries (entry_type, created_at);"
    )
    op.execute(
        "COMMENT ON TABLE ledger_entries IS "
        "'资金流水 — Append-Only, 永不修改/删除, 所有金额单位: 美分';"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ledger_entries CASCADE;")
```

**Step 3: Create 009_create_wal_events.py**

```python
"""009: create wal_events table

Revision ID: 009
Revises: 008
Create Date: 2026-02-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE wal_events (
            id              BIGSERIAL       PRIMARY KEY,
            market_id       VARCHAR(64)     NOT NULL,
            event_type      VARCHAR(30)     NOT NULL,
            payload         JSONB           NOT NULL,
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
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
    """)
    op.execute(
        "CREATE INDEX idx_wal_market_time "
        "ON wal_events (market_id, created_at);"
    )
    op.execute(
        "CREATE INDEX idx_wal_order_id "
        "ON wal_events USING GIN ((payload->'order_id'));"
    )
    op.execute(
        "COMMENT ON TABLE wal_events IS "
        "'订单簿变更审计日志 — Append-Only, 仅用于事后排查和分析';"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS wal_events CASCADE;")
```

**Step 4: Create 010_create_circuit_breaker_events.py**

```python
"""010: create circuit_breaker_events table

Revision ID: 010
Revises: 009
Create Date: 2026-02-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE circuit_breaker_events (
            id              BIGSERIAL       PRIMARY KEY,
            market_id       VARCHAR(64)     NOT NULL,
            trigger_reason  VARCHAR(50)     NOT NULL,
            context         JSONB           NOT NULL DEFAULT '{}',
            resolved_at     TIMESTAMPTZ,
            resolved_by     VARCHAR(50),
            resolution_note TEXT,
            triggered_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
        );
    """)
    op.execute(
        "CREATE INDEX idx_cb_market "
        "ON circuit_breaker_events (market_id, triggered_at DESC);"
    )
    op.execute(
        "COMMENT ON TABLE circuit_breaker_events IS "
        "'熔断事件记录 — 含触发原因和人工解除信息';"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS circuit_breaker_events CASCADE;")
```

**Step 5: Run migrations**

```bash
uv run alembic upgrade head
```
Expected: 4 new migrations applied (007–010).

**Step 6: Commit**

```bash
git add alembic/versions/007_create_positions.py alembic/versions/008_create_ledger_entries.py alembic/versions/009_create_wal_events.py alembic/versions/010_create_circuit_breaker_events.py
git commit -m "feat: add migrations 007-010 (positions, ledger, wal, circuit breaker)"
```

---

## Task 7: Migration 011 — Seed Data

**Files:**
- Create: `alembic/versions/011_seed_initial_data.py`

**Ref:** Design doc `Planning/Detail_Design/01_全局约定与数据库设计.md` §6.1

**Step 1: Create 011_seed_initial_data.py**

```python
"""011: seed initial data

Revision ID: 011
Revises: 010
Create Date: 2026-02-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # System special accounts
    op.execute("""
        INSERT INTO accounts (user_id, available_balance, frozen_balance, version)
        VALUES ('SYSTEM_RESERVE', 0, 0, 0);
    """)
    op.execute("""
        INSERT INTO accounts (user_id, available_balance, frozen_balance, version)
        VALUES ('PLATFORM_FEE', 0, 0, 0);
    """)

    # Sample markets
    op.execute("""
        INSERT INTO markets (
            id, title, description, category, status,
            min_price_cents, max_price_cents,
            max_order_quantity, max_position_per_user, max_order_amount_cents,
            maker_fee_bps, taker_fee_bps,
            trading_start_at, resolution_date
        ) VALUES
            ('MKT-BTC-100K-2026',
             'Will BTC exceed $100,000 by end of 2026?',
             'Resolves YES if Bitcoin price exceeds $100,000 on any major exchange before 2027-01-01 00:00 UTC.',
             'crypto', 'ACTIVE',
             1, 99, 10000, 25000, 1000000, 10, 20,
             '2026-01-01T00:00:00Z', '2026-12-31T23:59:59Z'),
            ('MKT-ETH-10K-2026',
             'Will ETH exceed $10,000 by end of 2026?',
             'Resolves YES if Ethereum price exceeds $10,000 on any major exchange before 2027-01-01 00:00 UTC.',
             'crypto', 'ACTIVE',
             1, 99, 10000, 25000, 1000000, 10, 20,
             '2026-01-01T00:00:00Z', '2026-12-31T23:59:59Z'),
            ('MKT-FED-RATE-CUT-2026Q2',
             'Will the Fed cut rates in Q2 2026?',
             'Resolves YES if the Federal Reserve announces a rate cut at any FOMC meeting in April, May, or June 2026.',
             'economics', 'ACTIVE',
             1, 99, 10000, 25000, 1000000, 10, 20,
             '2026-01-01T00:00:00Z', '2026-06-30T23:59:59Z');
    """)


def downgrade() -> None:
    op.execute("DELETE FROM markets WHERE id IN ('MKT-BTC-100K-2026', 'MKT-ETH-10K-2026', 'MKT-FED-RATE-CUT-2026Q2');")
    op.execute("DELETE FROM accounts WHERE user_id IN ('SYSTEM_RESERVE', 'PLATFORM_FEE');")
```

**Step 2: Run migration**

```bash
uv run alembic upgrade head
```

**Step 3: Verify seed data**

```bash
docker exec pm-postgres psql -U pm_user -d prediction_market -c "SELECT user_id, available_balance FROM accounts WHERE user_id IN ('SYSTEM_RESERVE', 'PLATFORM_FEE');"
docker exec pm-postgres psql -U pm_user -d prediction_market -c "SELECT id, status FROM markets;"
```
Expected: 2 system accounts + 3 markets.

**Step 4: Verify all 9 tables**

```bash
docker exec pm-postgres psql -U pm_user -d prediction_market -c "\dt"
```
Expected: users, accounts, markets, orders, trades, positions, ledger_entries, wal_events, circuit_breaker_events (9 tables).

**Step 5: Commit**

```bash
git add alembic/versions/011_seed_initial_data.py
git commit -m "feat: add migration 011 (seed SYSTEM_RESERVE, PLATFORM_FEE, sample markets)"
```

---

## Task 8: pm_common/enums.py — TDD

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/unit/test_enums.py`
- Create: `src/pm_common/enums.py`

**Ref:** Design doc `Planning/Detail_Design/01_全局约定与数据库设计.md` §4.1

**Step 1: Write the failing test**

Create `tests/__init__.py` and `tests/unit/__init__.py` (empty).

Create `tests/unit/test_enums.py`:

```python
"""Tests for pm_common.enums — all enum values must match DB CHECK constraints."""

from src.pm_common.enums import (
    BookType,
    FrozenAssetType,
    LedgerEntryType,
    MarketStatus,
    OrderDirection,
    OrderStatus,
    OriginalSide,
    PriceType,
    ResolutionResult,
    TimeInForce,
    TradeScenario,
)


class TestAllEnumsAreStr:
    """All enums inherit from (str, Enum) for JSON serialization."""

    def test_book_type_is_str(self) -> None:
        assert isinstance(BookType.NATIVE_BUY, str)
        assert BookType.NATIVE_BUY == "NATIVE_BUY"

    def test_trade_scenario_is_str(self) -> None:
        assert isinstance(TradeScenario.MINT, str)
        assert TradeScenario.MINT == "MINT"

    def test_order_status_is_str(self) -> None:
        assert isinstance(OrderStatus.NEW, str)
        assert OrderStatus.NEW == "NEW"


class TestBookType:
    def test_all_values(self) -> None:
        expected = {"NATIVE_BUY", "NATIVE_SELL", "SYNTHETIC_BUY", "SYNTHETIC_SELL"}
        assert {bt.value for bt in BookType} == expected


class TestTradeScenario:
    def test_all_values(self) -> None:
        expected = {"MINT", "TRANSFER_YES", "TRANSFER_NO", "BURN"}
        assert {ts.value for ts in TradeScenario} == expected


class TestFrozenAssetType:
    def test_all_values(self) -> None:
        expected = {"FUNDS", "YES_SHARES", "NO_SHARES"}
        assert {fa.value for fa in FrozenAssetType} == expected


class TestMarketStatus:
    def test_all_values(self) -> None:
        expected = {"DRAFT", "ACTIVE", "SUSPENDED", "HALTED", "RESOLVED", "SETTLED", "VOIDED"}
        assert {ms.value for ms in MarketStatus} == expected


class TestOrderStatus:
    def test_all_values(self) -> None:
        expected = {"NEW", "OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED"}
        assert {os.value for os in OrderStatus} == expected


class TestLedgerEntryType:
    def test_count(self) -> None:
        """16 entry types per design doc."""
        assert len(LedgerEntryType) == 16

    def test_user_side_types(self) -> None:
        user_types = {
            "DEPOSIT", "WITHDRAW", "ORDER_FREEZE", "ORDER_UNFREEZE",
            "MINT_COST", "BURN_REVENUE", "TRANSFER_PAYMENT", "TRANSFER_RECEIPT",
            "NETTING", "FEE", "SETTLEMENT_PAYOUT", "SETTLEMENT_VOID",
        }
        all_values = {le.value for le in LedgerEntryType}
        assert user_types.issubset(all_values)

    def test_system_side_types(self) -> None:
        system_types = {"MINT_RESERVE_IN", "BURN_RESERVE_OUT", "NETTING_RESERVE_OUT", "FEE_REVENUE"}
        all_values = {le.value for le in LedgerEntryType}
        assert system_types.issubset(all_values)


class TestSimpleEnums:
    def test_original_side(self) -> None:
        assert {s.value for s in OriginalSide} == {"YES", "NO"}

    def test_order_direction(self) -> None:
        assert {d.value for d in OrderDirection} == {"BUY", "SELL"}

    def test_price_type(self) -> None:
        assert {p.value for p in PriceType} == {"LIMIT"}

    def test_time_in_force(self) -> None:
        assert {t.value for t in TimeInForce} == {"GTC", "IOC"}

    def test_resolution_result(self) -> None:
        assert {r.value for r in ResolutionResult} == {"YES", "NO", "VOID"}
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_enums.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'src.pm_common.enums'`

**Step 3: Implement src/pm_common/enums.py**

```python
"""Global enums — must match DB CHECK constraints exactly.

Ref: Planning/Detail_Design/01_全局约定与数据库设计.md §4.1
"""

from enum import Enum


class MarketStatus(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    HALTED = "HALTED"
    RESOLVED = "RESOLVED"
    SETTLED = "SETTLED"
    VOIDED = "VOIDED"


class BookType(str, Enum):
    """订单簿身份: 标识订单在单一 YES 订单簿中的来源和角色"""
    NATIVE_BUY = "NATIVE_BUY"
    NATIVE_SELL = "NATIVE_SELL"
    SYNTHETIC_BUY = "SYNTHETIC_BUY"
    SYNTHETIC_SELL = "SYNTHETIC_SELL"


class TradeScenario(str, Enum):
    """撮合场景: 由 buy/sell 的 BookType 组合决定"""
    MINT = "MINT"
    TRANSFER_YES = "TRANSFER_YES"
    TRANSFER_NO = "TRANSFER_NO"
    BURN = "BURN"


class FrozenAssetType(str, Enum):
    """冻结资产类型: 撤单时据此解冻到对应账户"""
    FUNDS = "FUNDS"
    YES_SHARES = "YES_SHARES"
    NO_SHARES = "NO_SHARES"


class OriginalSide(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderDirection(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PriceType(str, Enum):
    LIMIT = "LIMIT"


class TimeInForce(str, Enum):
    GTC = "GTC"
    IOC = "IOC"


class OrderStatus(str, Enum):
    NEW = "NEW"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class LedgerEntryType(str, Enum):
    # Deposit/Withdraw
    DEPOSIT = "DEPOSIT"
    WITHDRAW = "WITHDRAW"
    # Order freeze/unfreeze (user side)
    ORDER_FREEZE = "ORDER_FREEZE"
    ORDER_UNFREEZE = "ORDER_UNFREEZE"
    # Mint (user + system paired)
    MINT_COST = "MINT_COST"
    MINT_RESERVE_IN = "MINT_RESERVE_IN"
    # Burn (user + system paired)
    BURN_REVENUE = "BURN_REVENUE"
    BURN_RESERVE_OUT = "BURN_RESERVE_OUT"
    # Transfer (user side)
    TRANSFER_PAYMENT = "TRANSFER_PAYMENT"
    TRANSFER_RECEIPT = "TRANSFER_RECEIPT"
    # Netting (user + system paired)
    NETTING = "NETTING"
    NETTING_RESERVE_OUT = "NETTING_RESERVE_OUT"
    # Fee (user + system paired)
    FEE = "FEE"
    FEE_REVENUE = "FEE_REVENUE"
    # Settlement (Phase 2)
    SETTLEMENT_PAYOUT = "SETTLEMENT_PAYOUT"
    SETTLEMENT_VOID = "SETTLEMENT_VOID"


class ResolutionResult(str, Enum):
    YES = "YES"
    NO = "NO"
    VOID = "VOID"
```

**Step 4: Run test to verify it passes**

```bash
uv run pytest tests/unit/test_enums.py -v
```
Expected: All PASS.

**Step 5: Commit**

```bash
git add src/pm_common/enums.py tests/
git commit -m "feat: add pm_common enums with tests (all match DB CHECK constraints)"
```

---

## Task 9: pm_common/cents.py — TDD

**Files:**
- Create: `tests/unit/test_cents.py`
- Create: `src/pm_common/cents.py`

**Ref:** Design doc v4.1 §原则1, `01_全局约定与数据库设计.md` §1.1

**Step 1: Write the failing test**

```python
"""Tests for pm_common.cents — integer arithmetic utilities."""

import pytest

from src.pm_common.cents import calculate_fee, cents_to_display, validate_price


class TestValidatePrice:
    def test_valid_prices(self) -> None:
        for p in [1, 50, 99]:
            validate_price(p)  # Should not raise

    def test_boundary_low(self) -> None:
        validate_price(1)  # OK

    def test_boundary_high(self) -> None:
        validate_price(99)  # OK

    def test_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="1.*99"):
            validate_price(0)

    def test_hundred_raises(self) -> None:
        with pytest.raises(ValueError, match="1.*99"):
            validate_price(100)

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="1.*99"):
            validate_price(-5)


class TestCentsToDisplay:
    def test_basic(self) -> None:
        assert cents_to_display(6500) == "$65.00"

    def test_zero(self) -> None:
        assert cents_to_display(0) == "$0.00"

    def test_one_cent(self) -> None:
        assert cents_to_display(1) == "$0.01"

    def test_large(self) -> None:
        assert cents_to_display(150000) == "$1,500.00"

    def test_exact_dollar(self) -> None:
        assert cents_to_display(10000) == "$100.00"


class TestCalculateFee:
    def test_basic(self) -> None:
        # 6500 * 20 / 10000 = 13.0 → 13
        assert calculate_fee(6500, 20) == 13

    def test_ceiling_rounds_up(self) -> None:
        # 6501 * 20 / 10000 = 13.002 → ceil = 14
        assert calculate_fee(6501, 20) == 14

    def test_zero_fee_rate(self) -> None:
        assert calculate_fee(6500, 0) == 0

    def test_zero_value(self) -> None:
        assert calculate_fee(0, 20) == 0

    def test_small_value(self) -> None:
        # 1 * 20 / 10000 = 0.002 → ceil = 1
        assert calculate_fee(1, 20) == 1

    def test_exact_division(self) -> None:
        # 10000 * 10 / 10000 = 10.0 → 10
        assert calculate_fee(10000, 10) == 10
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_cents.py -v
```
Expected: FAIL — `ModuleNotFoundError`

**Step 3: Implement src/pm_common/cents.py**

```python
"""Integer arithmetic utilities for cents-based prediction market.

All prices, amounts, and balances use int (cents). No float, no Decimal.
Ref: Planning/Detail_Design/01_全局约定与数据库设计.md §1.1
"""


def validate_price(price: int) -> None:
    """Validate that price is in the range [1, 99] cents."""
    if not (1 <= price <= 99):
        raise ValueError(f"Price must be between 1 and 99 cents, got {price}")


def cents_to_display(cents: int) -> str:
    """Convert cents to display string: 6500 -> '$65.00'."""
    dollars = cents // 100
    remainder = cents % 100
    return f"${dollars:,}.{remainder:02d}"


def calculate_fee(trade_value: int, fee_rate_bps: int) -> int:
    """Calculate fee with ceiling division (platform never loses).

    fee = ceil(trade_value * fee_rate_bps / 10000)
    Using integer ceiling: (a + b - 1) // b
    """
    if trade_value == 0 or fee_rate_bps == 0:
        return 0
    return (trade_value * fee_rate_bps + 9999) // 10000
```

**Step 4: Run test to verify it passes**

```bash
uv run pytest tests/unit/test_cents.py -v
```
Expected: All PASS.

**Step 5: Commit**

```bash
git add src/pm_common/cents.py tests/unit/test_cents.py
git commit -m "feat: add pm_common cents utilities (validate_price, cents_to_display, calculate_fee)"
```

---

## Task 10: pm_common/errors.py + response.py — TDD

**Files:**
- Create: `tests/unit/test_errors.py`
- Create: `src/pm_common/errors.py`
- Create: `src/pm_common/response.py`

**Ref:** API doc `Planning/Detail_Design/02_API接口契约.md` §1.3, §1.6

**Step 1: Write the failing test**

```python
"""Tests for pm_common.errors and pm_common.response."""

from src.pm_common.errors import (
    AppError,
    InsufficientBalanceError,
    MarketNotActiveError,
    MarketNotFoundError,
    OrderNotFoundError,
    SelfTradeError,
)
from src.pm_common.response import ApiResponse, error_response, success_response


class TestAppError:
    def test_base_error(self) -> None:
        err = AppError(code=9002, message="Internal error")
        assert err.code == 9002
        assert err.message == "Internal error"
        assert err.http_status == 500

    def test_custom_http_status(self) -> None:
        err = AppError(code=1001, message="Username taken", http_status=409)
        assert err.http_status == 409

    def test_is_exception(self) -> None:
        err = AppError(code=1001, message="test")
        assert isinstance(err, Exception)


class TestSpecificErrors:
    def test_insufficient_balance(self) -> None:
        err = InsufficientBalanceError(required=6500, available=3000)
        assert err.code == 2001
        assert err.http_status == 422
        assert "6500" in err.message
        assert "3000" in err.message

    def test_market_not_found(self) -> None:
        err = MarketNotFoundError("MKT-123")
        assert err.code == 3001
        assert err.http_status == 404

    def test_market_not_active(self) -> None:
        err = MarketNotActiveError("MKT-123")
        assert err.code == 3002
        assert err.http_status == 422

    def test_order_not_found(self) -> None:
        err = OrderNotFoundError("order-abc")
        assert err.code == 4004
        assert err.http_status == 404

    def test_self_trade(self) -> None:
        err = SelfTradeError()
        assert err.code == 4003
        assert err.http_status == 422


class TestApiResponse:
    def test_success(self) -> None:
        resp = success_response({"id": "abc"})
        assert resp.code == 0
        assert resp.message == "success"
        assert resp.data == {"id": "abc"}

    def test_error(self) -> None:
        resp = error_response(2001, "Insufficient balance")
        assert resp.code == 2001
        assert resp.message == "Insufficient balance"
        assert resp.data is None

    def test_serialization(self) -> None:
        resp = success_response({"price": 65})
        d = resp.model_dump()
        assert "code" in d
        assert "message" in d
        assert "data" in d
        assert "timestamp" in d
        assert "request_id" in d
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_errors.py -v
```
Expected: FAIL

**Step 3: Implement src/pm_common/errors.py**

```python
"""Unified error codes and custom exceptions.

Error code ranges (per API doc §1.6):
  1xxx: Auth/User
  2xxx: Account
  3xxx: Market
  4xxx: Order
  5xxx: Position
  9xxx: System
"""


class AppError(Exception):
    """Base application error."""

    def __init__(
        self,
        code: int,
        message: str,
        http_status: int = 500,
    ) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


# --- 1xxx: Auth/User ---

class UsernameExistsError(AppError):
    def __init__(self) -> None:
        super().__init__(1001, "Username already exists", 409)


class EmailExistsError(AppError):
    def __init__(self) -> None:
        super().__init__(1002, "Email already exists", 409)


class InvalidCredentialsError(AppError):
    def __init__(self) -> None:
        super().__init__(1003, "Invalid username or password", 401)


class AccountDisabledError(AppError):
    def __init__(self) -> None:
        super().__init__(1004, "Account is disabled", 403)


class InvalidRefreshTokenError(AppError):
    def __init__(self) -> None:
        super().__init__(1005, "Refresh token is invalid or expired", 401)


# --- 2xxx: Account ---

class InsufficientBalanceError(AppError):
    def __init__(self, required: int, available: int) -> None:
        super().__init__(
            2001,
            f"Insufficient balance: required {required} cents, available {available} cents",
            422,
        )


class AccountNotFoundError(AppError):
    def __init__(self, user_id: str) -> None:
        super().__init__(2002, f"Account not found for user {user_id}", 404)


# --- 3xxx: Market ---

class MarketNotFoundError(AppError):
    def __init__(self, market_id: str) -> None:
        super().__init__(3001, f"Market not found: {market_id}", 404)


class MarketNotActiveError(AppError):
    def __init__(self, market_id: str) -> None:
        super().__init__(3002, f"Market is not active: {market_id}", 422)


# --- 4xxx: Order ---

class PriceOutOfRangeError(AppError):
    def __init__(self, price: int) -> None:
        super().__init__(4001, f"Price out of range: {price}", 422)


class OrderLimitExceededError(AppError):
    def __init__(self, detail: str) -> None:
        super().__init__(4002, f"Order limit exceeded: {detail}", 422)


class SelfTradeError(AppError):
    def __init__(self) -> None:
        super().__init__(4003, "Self-trade prevented", 422)


class OrderNotFoundError(AppError):
    def __init__(self, order_id: str) -> None:
        super().__init__(4004, f"Order not found: {order_id}", 404)


class DuplicateOrderError(AppError):
    def __init__(self, client_order_id: str) -> None:
        super().__init__(4005, f"Duplicate client_order_id: {client_order_id}", 409)


class OrderNotCancellableError(AppError):
    def __init__(self, order_id: str, status: str) -> None:
        super().__init__(4006, f"Order {order_id} in status {status} cannot be cancelled", 422)


# --- 5xxx: Position ---

class InsufficientPositionError(AppError):
    def __init__(self, detail: str) -> None:
        super().__init__(5001, f"Insufficient position: {detail}", 422)


# --- 9xxx: System ---

class RateLimitError(AppError):
    def __init__(self) -> None:
        super().__init__(9001, "Rate limit exceeded", 429)


class InternalError(AppError):
    def __init__(self, detail: str = "Internal server error") -> None:
        super().__init__(9002, detail, 500)
```

**Step 4: Implement src/pm_common/response.py**

```python
"""Unified API response wrapper.

All API endpoints return this format:
{
    "code": 0,           // 0=success, non-0=error code
    "message": "success",
    "data": { ... },     // null on error
    "timestamp": "...",
    "request_id": "..."
}

Ref: Planning/Detail_Design/02_API接口契约.md §1.3
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class ApiResponse(BaseModel):
    code: int = 0
    message: str = "success"
    data: Any = None
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    request_id: str = Field(default_factory=lambda: f"req_{uuid.uuid4().hex[:12]}")


def success_response(data: Any = None) -> ApiResponse:
    return ApiResponse(code=0, message="success", data=data)


def error_response(code: int, message: str) -> ApiResponse:
    return ApiResponse(code=code, message=message, data=None)
```

**Step 5: Run test to verify it passes**

```bash
uv run pytest tests/unit/test_errors.py -v
```
Expected: All PASS.

**Step 6: Commit**

```bash
git add src/pm_common/errors.py src/pm_common/response.py tests/unit/test_errors.py
git commit -m "feat: add pm_common errors (error codes per API doc) and ApiResponse wrapper"
```

---

## Task 11: pm_common/id_generator.py + datetime_utils.py — TDD

**Files:**
- Create: `tests/unit/test_id_generator.py`
- Create: `src/pm_common/id_generator.py`
- Create: `src/pm_common/datetime_utils.py`

**Step 1: Write the failing test**

```python
"""Tests for pm_common.id_generator and pm_common.datetime_utils."""

from datetime import datetime, timezone

from src.pm_common.datetime_utils import utc_now
from src.pm_common.id_generator import SnowflakeIdGenerator


class TestSnowflakeIdGenerator:
    def test_returns_str(self) -> None:
        gen = SnowflakeIdGenerator(machine_id=1)
        result = gen.next_id()
        assert isinstance(result, str)

    def test_unique_ids(self) -> None:
        gen = SnowflakeIdGenerator(machine_id=1)
        ids = {gen.next_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_monotonically_increasing(self) -> None:
        gen = SnowflakeIdGenerator(machine_id=1)
        prev = int(gen.next_id())
        for _ in range(100):
            current = int(gen.next_id())
            assert current > prev
            prev = current


class TestUtcNow:
    def test_returns_aware_datetime(self) -> None:
        now = utc_now()
        assert isinstance(now, datetime)
        assert now.tzinfo is not None

    def test_is_utc(self) -> None:
        now = utc_now()
        assert now.tzinfo == timezone.utc
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/unit/test_id_generator.py -v
```
Expected: FAIL

**Step 3: Implement src/pm_common/id_generator.py**

```python
"""Snowflake-style ID generator for business IDs (trade_id, etc.).

Generates monotonically increasing, unique string IDs.
Not a full Twitter Snowflake — simplified for single-process MVP.
"""

import threading
import time


class SnowflakeIdGenerator:
    """Simple snowflake ID generator.

    Layout (64 bits):
      - 41 bits: millisecond timestamp (since custom epoch)
      - 10 bits: machine_id (0-1023)
      - 12 bits: sequence (0-4095 per millisecond)
    """

    _EPOCH_MS = 1_700_000_000_000  # 2023-11-14 approx
    _MACHINE_BITS = 10
    _SEQUENCE_BITS = 12
    _MAX_SEQUENCE = (1 << _SEQUENCE_BITS) - 1

    def __init__(self, machine_id: int = 0) -> None:
        if not (0 <= machine_id < (1 << self._MACHINE_BITS)):
            raise ValueError(f"machine_id must be 0-{(1 << self._MACHINE_BITS) - 1}")
        self._machine_id = machine_id
        self._sequence = 0
        self._last_timestamp_ms = -1
        self._lock = threading.Lock()

    def next_id(self) -> str:
        with self._lock:
            ts = self._current_ms()
            if ts == self._last_timestamp_ms:
                self._sequence = (self._sequence + 1) & self._MAX_SEQUENCE
                if self._sequence == 0:
                    ts = self._wait_next_ms(ts)
            else:
                self._sequence = 0

            self._last_timestamp_ms = ts
            id_int = (
                ((ts - self._EPOCH_MS) << (self._MACHINE_BITS + self._SEQUENCE_BITS))
                | (self._machine_id << self._SEQUENCE_BITS)
                | self._sequence
            )
            return str(id_int)

    def _current_ms(self) -> int:
        return int(time.time() * 1000)

    def _wait_next_ms(self, last_ts: int) -> int:
        ts = self._current_ms()
        while ts <= last_ts:
            ts = self._current_ms()
        return ts
```

**Step 4: Implement src/pm_common/datetime_utils.py**

```python
"""UTC datetime utilities."""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return timezone-aware UTC now."""
    return datetime.now(timezone.utc)
```

**Step 5: Run test to verify it passes**

```bash
uv run pytest tests/unit/test_id_generator.py -v
```
Expected: All PASS.

**Step 6: Commit**

```bash
git add src/pm_common/id_generator.py src/pm_common/datetime_utils.py tests/unit/test_id_generator.py
git commit -m "feat: add pm_common snowflake ID generator and datetime utils"
```

---

## Task 12: pm_common/redis_client.py + __init__.py

**Files:**
- Create: `src/pm_common/redis_client.py`
- Modify: `src/pm_common/__init__.py`

**Step 1: Create src/pm_common/redis_client.py**

```python
"""Redis client factory — used for rate limiting and sessions only.

NOT used for balance caching or freezing (those go through PostgreSQL).
Ref: Planning/Detail_Design/01_全局约定与数据库设计.md §3
"""

import redis.asyncio as aioredis

from config.settings import settings

_redis_pool: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Get or create the Redis connection pool."""
    global _redis_pool  # noqa: PLW0603
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )
    return _redis_pool


async def close_redis() -> None:
    """Close the Redis connection pool."""
    global _redis_pool  # noqa: PLW0603
    if _redis_pool is not None:
        await _redis_pool.close()
        _redis_pool = None
```

**Step 2: Update src/pm_common/__init__.py**

```python
"""pm_common — shared utilities for the prediction market platform."""
```

**Step 3: Commit**

```bash
git add src/pm_common/redis_client.py src/pm_common/__init__.py
git commit -m "feat: add pm_common Redis client (rate limiting/sessions only)"
```

---

## Task 13: FastAPI Entry Point (main.py)

**Files:**
- Create: `src/main.py`

**Step 1: Create src/main.py**

```python
"""FastAPI application entry point.

Run with: uvicorn src.main:app --reload --port 8000
"""

import uvloop

uvloop.install()

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config.settings import settings
from src.pm_common.database import engine
from src.pm_common.errors import AppError
from src.pm_common.redis_client import close_redis, get_redis
from src.pm_common.response import ApiResponse, error_response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: verify DB + Redis connections. Shutdown: dispose."""
    # Startup
    async with engine.connect() as conn:
        await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
    await get_redis()
    yield
    # Shutdown
    await engine.dispose()
    await close_redis()


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    resp = error_response(exc.code, exc.message)
    return JSONResponse(
        status_code=exc.http_status,
        content=resp.model_dump(),
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}
```

**Step 2: Test health endpoint manually**

```bash
uv run uvicorn src.main:app --port 8000 &
sleep 2
curl http://localhost:8000/health
kill %1
```
Expected: `{"status":"ok","version":"0.1.0"}`

**Step 3: Commit**

```bash
git add src/main.py
git commit -m "feat: add FastAPI entry point with uvloop, health check, error handler"
```

---

## Task 14: Module Directory Skeletons

**Files:**
- Create: All `__init__.py` files for skeleton module directories

**Step 1: Create all skeleton directories and __init__.py files**

Run a script or manually create these directories with empty `__init__.py` files:

```
src/pm_account/__init__.py
src/pm_account/domain/__init__.py
src/pm_account/infrastructure/__init__.py
src/pm_account/application/__init__.py
src/pm_account/api/__init__.py

src/pm_market/__init__.py
src/pm_market/domain/__init__.py
src/pm_market/config/              (empty dir, no __init__.py — holds markets.json later)
src/pm_market/application/__init__.py
src/pm_market/api/__init__.py

src/pm_order/__init__.py
src/pm_order/domain/__init__.py
src/pm_order/infrastructure/__init__.py
src/pm_order/application/__init__.py
src/pm_order/api/__init__.py

src/pm_risk/__init__.py
src/pm_risk/domain/__init__.py
src/pm_risk/rules/__init__.py
src/pm_risk/application/__init__.py
src/pm_risk/api/__init__.py

src/pm_matching/__init__.py
src/pm_matching/domain/__init__.py
src/pm_matching/engine/__init__.py
src/pm_matching/application/__init__.py
src/pm_matching/api/__init__.py

src/pm_clearing/__init__.py
src/pm_clearing/domain/__init__.py
src/pm_clearing/domain/scenarios/__init__.py
src/pm_clearing/infrastructure/__init__.py
src/pm_clearing/application/__init__.py
src/pm_clearing/api/__init__.py

src/pm_gateway/__init__.py
src/pm_gateway/auth/__init__.py
src/pm_gateway/user/__init__.py
src/pm_gateway/middleware/__init__.py
src/pm_gateway/api/__init__.py

scripts/__init__.py

tests/integration/__init__.py
tests/e2e/__init__.py
```

Each `__init__.py` is an empty file.

For `src/pm_market/config/`, create a `.gitkeep` file to track the empty directory.

**Step 2: Commit**

```bash
git add src/pm_account/ src/pm_market/ src/pm_order/ src/pm_risk/ src/pm_matching/ src/pm_clearing/ src/pm_gateway/ scripts/ tests/integration/ tests/e2e/
git commit -m "chore: add module directory skeletons (empty __init__.py for all future modules)"
```

---

## Task 15: Makefile + Dockerfile + tests/conftest.py

**Files:**
- Create: `Makefile`
- Create: `Dockerfile`
- Create: `tests/conftest.py`

**Step 1: Create Makefile**

```makefile
.PHONY: up down dev test migrate lint format typecheck

up:
	docker-compose up -d

down:
	docker-compose down

dev:
	uv run uvicorn src.main:app --reload --port 8000

test:
	uv run pytest tests/ -v

migrate:
	uv run alembic upgrade head

migration:
	uv run alembic revision -m "$(MSG)"

lint:
	uv run ruff check src/ tests/

format:
	uv run ruff format src/ tests/

typecheck:
	uv run mypy src/
```

**Step 2: Create Dockerfile**

```dockerfile
# --- Build stage ---
FROM python:3.12-slim AS builder

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev

COPY . .

# --- Production stage ---
FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Step 3: Create tests/conftest.py**

```python
"""Shared test fixtures."""

import pytest
from httpx import ASGITransport, AsyncClient

from src.main import app


@pytest.fixture
async def client() -> AsyncClient:
    """Async HTTP client for testing FastAPI endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
```

**Step 4: Commit**

```bash
git add Makefile Dockerfile tests/conftest.py
git commit -m "chore: add Makefile, Dockerfile, test conftest"
```

---

## Task 16: Final Verification

**Step 1: Ensure Docker is running**

```bash
make up
docker-compose ps
```
Expected: Both postgres and redis healthy.

**Step 2: Run all migrations from scratch**

```bash
uv run alembic downgrade base
uv run alembic upgrade head
```
Expected: All 11 migrations apply cleanly.

**Step 3: Verify all 9 tables + seed data**

```bash
docker exec pm-postgres psql -U pm_user -d prediction_market -c "\dt"
docker exec pm-postgres psql -U pm_user -d prediction_market -c "SELECT user_id FROM accounts WHERE user_id IN ('SYSTEM_RESERVE', 'PLATFORM_FEE');"
docker exec pm-postgres psql -U pm_user -d prediction_market -c "SELECT id, status FROM markets;"
```

**Step 4: Start dev server and verify Swagger**

```bash
make dev &
sleep 3
curl http://localhost:8000/health
curl http://localhost:8000/docs
kill %1
```
Expected: Health returns `{"status":"ok","version":"0.1.0"}`, Swagger UI accessible.

**Step 5: Run all tests**

```bash
make test
```
Expected: All unit tests pass.

**Step 6: Run lint and type check**

```bash
make lint
make format
make typecheck
```
Expected: Zero errors (typecheck may have some warnings for untyped deps — that's OK for MVP).

**Step 7: Final commit (if any format changes)**

```bash
git add -A
git commit -m "chore: lint and format fixes"
```

---

## Summary

| Task | Description | Key Files |
|------|-------------|-----------|
| 1 | Git init + project config | .gitignore, pyproject.toml, mypy.ini, ruff.toml |
| 2 | Docker Compose + settings | docker-compose.yml, config/settings.py, .env.example |
| 3 | SQLAlchemy + Alembic init | src/pm_common/database.py, alembic/env.py |
| 4 | Migrations 001–003 | users, accounts tables |
| 5 | Migrations 004–006 | markets, orders, trades tables |
| 6 | Migrations 007–010 | positions, ledger_entries, wal_events, circuit_breaker |
| 7 | Migration 011 | Seed data (SYSTEM_RESERVE, PLATFORM_FEE, 3 markets) |
| 8 | pm_common/enums.py | TDD: all enums matching DB CHECK constraints |
| 9 | pm_common/cents.py | TDD: validate_price, cents_to_display, calculate_fee |
| 10 | pm_common/errors.py + response.py | TDD: error codes, ApiResponse |
| 11 | pm_common/id_generator.py + datetime_utils | TDD: snowflake IDs, UTC helper |
| 12 | pm_common/redis_client.py | Redis connection factory |
| 13 | src/main.py | FastAPI + uvloop + health check + error handler |
| 14 | Module directory skeletons | All future module dirs with __init__.py |
| 15 | Makefile + Dockerfile + conftest | Build/run/test tooling |
| 16 | Final verification | Full stack smoke test |

**Acceptance criteria**: `make up && make migrate && make dev` → Swagger accessible, 9 tables, seed data, `/health` returns OK, `make test` all green.

---

*Plan version: v1.0 | Generated: 2026-02-20*
