# Project Scaffolding & pm_common Design

> **Date**: 2026-02-20
> **Scope**: Module 0 (project scaffold + infra) + Module 1 (pm_common)
> **Approach**: Migrations-First — raw SQL DDL from design docs, ORM models added later per module
> **Package Manager**: uv
> **Upstream Design Docs**: `../预测市场平台_完整实施计划_v4_Python.md` (v4.1), `../Detail_Design/01_全局约定与数据库设计.md` (v2.3)

---

## 1. Repository Layout

DEV_MVP/ is the git repo root (mono-repo). Planning docs and code coexist at the top level.

```
DEV_MVP/                            # git repo root
│
├── Planning/                       # All docs (existing, untouched)
│   ├── 预测市场平台_完整实施计划_v4_Python.md
│   ├── 单账本撮合引擎设计方案_v1.md
│   ├── Detail_Design/
│   ├── Implementation/             # NEW: implementation plans
│   │   └── 2026-02-20-scaffolding-design.md  ← this file
│   └── archive/
│
├── src/                            # Application source code
│   ├── pm_common/                  # Module 0: Shared utilities (implemented)
│   │   ├── __init__.py
│   │   ├── enums.py
│   │   ├── errors.py
│   │   ├── response.py
│   │   ├── id_generator.py
│   │   ├── cents.py
│   │   ├── datetime_utils.py
│   │   ├── redis_client.py
│   │   └── database.py
│   │
│   ├── pm_account/                 # Module 2 (skeleton)
│   │   ├── domain/__init__.py
│   │   ├── infrastructure/__init__.py
│   │   ├── application/__init__.py
│   │   └── api/__init__.py
│   │
│   ├── pm_market/                  # Module 3 (skeleton)
│   │   ├── domain/__init__.py
│   │   ├── config/
│   │   ├── application/__init__.py
│   │   └── api/__init__.py
│   │
│   ├── pm_order/                   # Module 4 (skeleton)
│   │   ├── domain/__init__.py
│   │   ├── infrastructure/__init__.py
│   │   ├── application/__init__.py
│   │   └── api/__init__.py
│   │
│   ├── pm_risk/                    # Module 5 (skeleton)
│   │   ├── domain/__init__.py
│   │   ├── rules/__init__.py
│   │   ├── application/__init__.py
│   │   └── api/__init__.py
│   │
│   ├── pm_matching/                # Module 6 (skeleton)
│   │   ├── domain/__init__.py
│   │   ├── engine/__init__.py
│   │   ├── application/__init__.py
│   │   └── api/__init__.py
│   │
│   ├── pm_clearing/                # Module 7 (skeleton)
│   │   ├── domain/
│   │   │   ├── __init__.py
│   │   │   └── scenarios/__init__.py
│   │   ├── infrastructure/__init__.py
│   │   ├── application/__init__.py
│   │   └── api/__init__.py
│   │
│   ├── pm_gateway/                 # Module 8 (skeleton)
│   │   ├── auth/__init__.py
│   │   ├── user/__init__.py
│   │   ├── middleware/__init__.py
│   │   └── api/__init__.py
│   │
│   └── main.py                     # FastAPI entry + uvloop.install()
│
├── tests/
│   ├── conftest.py                 # Shared fixtures (async DB, httpx client)
│   ├── unit/__init__.py
│   ├── integration/__init__.py
│   └── e2e/__init__.py
│
├── config/
│   └── settings.py                 # Pydantic Settings (.env driven)
│
├── scripts/
│   └── __init__.py
│
├── alembic/
│   ├── env.py
│   └── versions/                   # 11 migration files (raw SQL)
│
├── pyproject.toml                  # uv, Python 3.12+, all MVP deps
├── .python-version                 # 3.12
├── alembic.ini
├── docker-compose.yml              # PostgreSQL 16 + Redis 7
├── Dockerfile
├── Makefile
├── mypy.ini
├── ruff.toml
├── .env.example
└── .gitignore
```

### Design Doc Deviation Log

| Design Doc Reference | This Plan | Reason |
|---------------------|-----------|--------|
| `prediction-market/` as project root | `DEV_MVP/` as git root, code in `src/` directly | User chose mono-repo layout with Planning/ at same level |
| `docs/plans/` for design docs | `Planning/Implementation/` | User has existing `Planning/` folder, keep consistent |

---

## 2. Project Root Configuration Files

### 2.1 pyproject.toml

- **Build system**: uv
- **Python**: `>=3.12`
- **Runtime dependencies**:
  - `fastapi>=0.109`
  - `uvicorn[standard]>=0.27`
  - `uvloop>=0.19`
  - `sqlalchemy[asyncio]>=2.0`
  - `asyncpg>=0.29`
  - `alembic>=1.13`
  - `pydantic>=2.5`
  - `pydantic-settings>=2.1`
  - `python-jose[cryptography]>=3.3`
  - `passlib[bcrypt]>=1.7`
  - `redis>=5.0`
- **Dev dependencies**:
  - `pytest>=8.0`
  - `pytest-asyncio>=0.23`
  - `httpx>=0.26`
  - `mypy>=1.8`
  - `ruff>=0.2`

### 2.2 docker-compose.yml

- **postgres**: `postgres:16-alpine`, port 5432, env vars for user/password/db, volume mount, healthcheck (`pg_isready`)
- **redis**: `redis:7-alpine`, port 6379, healthcheck (`redis-cli ping`)

### 2.3 Makefile targets

| Target | Command | Description |
|--------|---------|-------------|
| `make up` | `docker-compose up -d` | Start PostgreSQL + Redis |
| `make down` | `docker-compose down` | Stop containers |
| `make dev` | `uvicorn src.main:app --reload --port 8000` | Dev server |
| `make migrate` | `alembic upgrade head` | Run all migrations |
| `make test` | `pytest tests/ -v` | Run tests |
| `make lint` | `ruff check src/ tests/` | Lint |
| `make format` | `ruff format src/ tests/` | Format |
| `make typecheck` | `mypy src/` | Type check |

### 2.4 config/settings.py

Pydantic `BaseSettings` with `.env` support:
- `DATABASE_URL: str` — default `postgresql+asyncpg://pm_user:pm_pass@localhost:5432/prediction_market`
- `REDIS_URL: str` — default `redis://localhost:6379/0`
- `JWT_SECRET: str` — required (no default)
- `JWT_ALGORITHM: str` — default `HS256`
- `JWT_EXPIRE_MINUTES: int` — default `30`
- `APP_NAME: str` — default `Prediction Market`
- `DEBUG: bool` — default `False`

### 2.5 mypy.ini

Strict mode: `strict = True`, plugins for pydantic and sqlalchemy.

### 2.6 ruff.toml

Target Python 3.12, line length 100, select rules: E, W, F, I, UP, B, SIM.

---

## 3. Database Migrations (Alembic)

All 11 migration files use **raw SQL** (`op.execute()`) to exactly match DDL from design doc `01_全局约定与数据库设计.md` v2.3.

Each file contains:
- `upgrade()`: CREATE TABLE + indexes + triggers + comments (exact DDL from design doc)
- `downgrade()`: DROP TABLE (in reverse order of creation)

```
alembic/versions/
├── 001_create_common_functions.py         # fn_update_timestamp() trigger function
├── 002_create_users.py                    # users + idx_users_email + trigger
├── 003_create_accounts.py                 # accounts + CHECKs + trigger
├── 004_create_markets.py                  # markets + reserve/pnl_pool/shares + all CHECKs
├── 005_create_orders.py                   # orders + book_type/frozen + 3 indexes + trigger
├── 006_create_trades.py                   # trades + scenario + realized_pnl + 4 indexes
├── 007_create_positions.py                # positions + YES/NO merged + CHECKs + trigger
├── 008_create_ledger_entries.py           # ledger_entries (Append-Only) + 3 indexes
├── 009_create_wal_events.py               # wal_events audit log + GIN index
├── 010_create_circuit_breaker_events.py   # circuit_breaker_events + index
└── 011_seed_initial_data.py               # SYSTEM_RESERVE + PLATFORM_FEE + 3 sample markets
```

Seed data (011) per design doc §6.1:
- `SYSTEM_RESERVE` account (available_balance=0, frozen_balance=0)
- `PLATFORM_FEE` account (available_balance=0, frozen_balance=0)
- 3 sample markets: `MKT-BTC-100K-2026`, `MKT-ETH-10K-2026`, `MKT-FED-RATE-CUT-2026Q2`

---

## 4. pm_common Module (Full Implementation)

### 4.1 enums.py

All enums from design doc §4.1, each inheriting `str, Enum`:
- `MarketStatus` — DRAFT, ACTIVE, SUSPENDED, HALTED, RESOLVED, SETTLED, VOIDED
- `BookType` — NATIVE_BUY, NATIVE_SELL, SYNTHETIC_BUY, SYNTHETIC_SELL
- `TradeScenario` — MINT, TRANSFER_YES, TRANSFER_NO, BURN
- `FrozenAssetType` — FUNDS, YES_SHARES, NO_SHARES
- `OriginalSide` — YES, NO
- `OrderDirection` — BUY, SELL
- `PriceType` — LIMIT
- `TimeInForce` — GTC, IOC
- `OrderStatus` — NEW, OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED
- `LedgerEntryType` — all 16 types (DEPOSIT through SETTLEMENT_VOID)
- `ResolutionResult` — YES, NO, VOID

### 4.2 errors.py

- Base `AppError(Exception)` with `code: int`, `message: str`, `http_status: int`
- Error code ranges per API doc §error codes:
  - 1xxx: common (1001 VALIDATION_ERROR, 1002 NOT_FOUND, etc.)
  - 2xxx: account (2001 INSUFFICIENT_BALANCE, 2002 ACCOUNT_NOT_FOUND, etc.)
  - 3xxx: market (3001 MARKET_NOT_FOUND, 3002 MARKET_NOT_ACTIVE, etc.)
  - 4xxx: order (4001 ORDER_NOT_FOUND, 4002 DUPLICATE_ORDER, etc.)
  - 5xxx: risk (5001 BALANCE_CHECK_FAILED, 5002 POSITION_CHECK_FAILED, 5003 SELF_TRADE, etc.)
  - 6xxx: matching
  - 7xxx: clearing
  - 8xxx: gateway (8001 UNAUTHORIZED, 8002 TOKEN_EXPIRED, etc.)

### 4.3 response.py

- `ApiResponse[T]` — Generic Pydantic model: `{"success": bool, "data": T | None, "error": {"code": int, "message": str} | None}`
- Helper functions: `success_response(data)`, `error_response(code, message)`

### 4.4 id_generator.py

- Snowflake-style ID generator for trade IDs and other business IDs
- Parameters: machine_id configurable via settings
- Thread-safe, monotonically increasing, returns `str`

### 4.5 cents.py

```python
def validate_price(price: int) -> None:
    """Assert 1 <= price <= 99"""

def cents_to_display(cents: int) -> str:
    """6500 -> '$65.00'"""

def calculate_fee(trade_value: int, fee_rate_bps: int) -> int:
    """Ceiling: (trade_value * fee_rate_bps + 9999) // 10000"""
```

### 4.6 datetime_utils.py

- `utc_now() -> datetime` — timezone-aware UTC now

### 4.7 redis_client.py

- `create_redis_pool(url: str) -> redis.asyncio.Redis`
- Connection pool from settings, used for rate limiting and session management only

### 4.8 database.py

- `create_async_engine(url: str)` with asyncpg driver
- `async_sessionmaker` for `AsyncSession`
- `get_db_session()` — async generator for FastAPI `Depends()`
- `Base = declarative_base()` — shared base for all ORM models across modules

---

## 5. FastAPI Entry Point (main.py)

- `uvloop.install()` at module level
- `FastAPI(title="Prediction Market", version="0.1.0")`
- Lifespan context manager: create engine + Redis pool on startup, dispose on shutdown
- `GET /health` → `{"status": "ok", "version": "0.1.0"}`
- Global exception handler: `AppError` → `ApiResponse` with appropriate HTTP status

---

## 6. Tests (Scaffold)

### 6.1 tests/conftest.py

- `@pytest.fixture` for async DB session (using test database or SQLite for unit tests)
- `@pytest.fixture` for httpx `AsyncClient` against the FastAPI app

### 6.2 tests/unit/test_common.py

Basic tests for pm_common utilities:
- `test_validate_price_valid` / `test_validate_price_boundary` / `test_validate_price_invalid`
- `test_cents_to_display`
- `test_calculate_fee` / `test_calculate_fee_ceiling`
- `test_all_enums_are_str`
- `test_id_generator_unique`
- `test_api_response_success` / `test_api_response_error`

---

## 7. Acceptance Criteria

Per design doc Module 0:

```bash
docker-compose up -d    # PostgreSQL 16 + Redis 7 running
make migrate            # All 9 tables created with correct schema
make dev                # Swagger UI at http://localhost:8000/docs
```

Verify:
1. PostgreSQL has all 9 tables with correct constraints, indexes, triggers, comments
2. Seed data: SYSTEM_RESERVE, PLATFORM_FEE, 3 sample markets exist
3. `GET /health` returns `{"status": "ok", "version": "0.1.0"}`
4. `make lint` passes with zero errors
5. `make typecheck` passes with zero errors
6. `make test` passes (pm_common unit tests)

---

*Document version: v1.0 | Generated: 2026-02-20*
