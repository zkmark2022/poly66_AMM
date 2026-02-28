# AMM 前置改动（撮合引擎侧）Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement all prerequisite changes in the matching engine before the AMM bot can go live: DB migration (auto_netting_enabled + AMM system account), netting bypass, self-trade exemption, Privileged Mint/Burn APIs, Atomic Replace API, Batch Cancel API.

**Architecture:** Changes are distributed across existing modules (pm_clearing, pm_risk, pm_order, pm_gateway, pm_account). No new top-level module is created. AMM-specific routes are mounted under `/api/v1/amm/` prefix but physically live in pm_order and pm_clearing.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 (async), PostgreSQL 16, Alembic, uv, pytest-asyncio

**Key facts before you start:**
- All existing enums are in `src/pm_common/enums.py` — import, don't redefine
- Existing ORM models: `AccountORM` in `src/pm_account/infrastructure/db_models.py`, `PositionORM` same path, `MarketORM` in `src/pm_market/infrastructure/db_models.py`
- `MatchingEngine` is in `src/pm_matching/engine/engine.py` — the central orchestrator with per-market `asyncio.Lock`
- Fee calculation: `(trade_value * fee_bps + 9999) // 10000` (ceiling division, integer-only)
- AMM user_id: `00000000-0000-4000-a000-000000000001` (fixed UUID v4)
- All amounts in cents (integer). Price range [1, 99].
- `ApiResponse[T]` wrapper exists in `src/pm_common/schemas.py`
- Error handling: `AppError(http_status, error_code, message)` in `src/pm_common/errors.py`
- Run tests: `uv run pytest <path> -v`
- All test functions must have `-> None` return type
- Design doc reference: `Planning/Implementation/2026-02-28-amm-prerequisites-design.md`
- AMM spec docs: `Planning/AMM_Bot_Design/01_*.md` (v1.3), `02_*.md` (v1.4), `03_*.md` (v1.3)

---

## Task 1: AMM System Constants

**Files:**
- Create: `src/pm_account/domain/constants.py`
- Test: `tests/unit/test_amm_constants.py`

**Step 1: Write failing test**

```python
# tests/unit/test_amm_constants.py
"""Verify AMM system account constants match data dictionary v1.3 §3.1."""
import uuid
from src.pm_account.domain.constants import AMM_USER_ID, AMM_USERNAME, AMM_EMAIL


class TestAMMConstants:
    def test_amm_user_id_is_valid_uuid(self) -> None:
        parsed = uuid.UUID(AMM_USER_ID)
        assert str(parsed) == AMM_USER_ID

    def test_amm_user_id_value(self) -> None:
        assert AMM_USER_ID == "00000000-0000-4000-a000-000000000001"

    def test_amm_username(self) -> None:
        assert AMM_USERNAME == "amm_market_maker"
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_amm_constants.py -v
# Expected: ModuleNotFoundError
```

**Step 3: Implement**

```python
# src/pm_account/domain/constants.py
"""AMM system account identity — aligned with data dictionary v1.3 §3.1."""

AMM_USER_ID: str = "00000000-0000-4000-a000-000000000001"
AMM_USERNAME: str = "amm_market_maker"
AMM_EMAIL: str = "amm@system.internal"
```

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_amm_constants.py -v
# Expected: 3 passed
```

**Step 5: Commit**
```bash
git add src/pm_account/domain/constants.py tests/unit/test_amm_constants.py
git commit -m "feat(pm_account): add AMM system account constants"
```

---

## Task 2: DB Migration — auto_netting_enabled + AMM System Account

**Files:**
- Create: `alembic/versions/<auto>_add_amm_prerequisites.py`
- Test: `tests/integration/test_amm_migration.py`

**Step 1: Generate migration**

```bash
uv run alembic revision --autogenerate -m "add_amm_prerequisites"
```

**Step 2: Edit the generated migration**

The migration must:
1. Add `auto_netting_enabled` column to `accounts` table (BOOLEAN, NOT NULL, DEFAULT TRUE)
2. Insert AMM system user into `users` table
3. Insert AMM system account into `accounts` table with `auto_netting_enabled = false`

```python
"""add AMM prerequisites: auto_netting_enabled column + AMM system account

Revision ID: <auto>
"""
from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    # 1. Add auto_netting_enabled column
    op.add_column(
        "accounts",
        sa.Column(
            "auto_netting_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    # 2. Insert AMM system user
    op.execute(
        """
        INSERT INTO users (id, username, email, password_hash, is_active, created_at, updated_at)
        VALUES (
            '00000000-0000-4000-a000-000000000001',
            'amm_market_maker',
            'amm@system.internal',
            '$2b$12$AMMSystemNoLoginPlac662e4a7b8c9d0e1f2a3b4c5d6e7f8',
            true,
            NOW(),
            NOW()
        )
        ON CONFLICT (id) DO NOTHING;
        """
    )

    # 3. Insert AMM system account with auto_netting disabled
    op.execute(
        """
        INSERT INTO accounts (user_id, available_balance, frozen_balance, version, auto_netting_enabled)
        VALUES (
            '00000000-0000-4000-a000-000000000001',
            0,
            0,
            0,
            false
        )
        ON CONFLICT (user_id) DO UPDATE SET auto_netting_enabled = false;
        """
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM accounts WHERE user_id = '00000000-0000-4000-a000-000000000001'"
    )
    op.execute(
        "DELETE FROM users WHERE id = '00000000-0000-4000-a000-000000000001'"
    )
    op.drop_column("accounts", "auto_netting_enabled")
```

**Step 3: Update AccountORM**

Add the new column to the SQLAlchemy ORM model:

```python
# In src/pm_account/infrastructure/db_models.py — add to AccountORM class:
    auto_netting_enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("true")
    )
```

**Step 4: Run migration**
```bash
uv run alembic upgrade head
```

**Step 5: Write integration test to verify**

```python
# tests/integration/test_amm_migration.py
"""Verify AMM system account exists and auto_netting_enabled works."""
import pytest
from sqlalchemy import text
from src.pm_account.domain.constants import AMM_USER_ID


@pytest.mark.asyncio
async def test_amm_user_exists(db_session) -> None:
    result = await db_session.execute(
        text("SELECT id, username FROM users WHERE id = :uid"),
        {"uid": AMM_USER_ID},
    )
    row = result.fetchone()
    assert row is not None
    assert row.username == "amm_market_maker"


@pytest.mark.asyncio
async def test_amm_account_netting_disabled(db_session) -> None:
    result = await db_session.execute(
        text("SELECT auto_netting_enabled FROM accounts WHERE user_id = :uid"),
        {"uid": AMM_USER_ID},
    )
    row = result.fetchone()
    assert row is not None
    assert row.auto_netting_enabled is False


@pytest.mark.asyncio
async def test_normal_user_netting_enabled(db_session, test_user_id) -> None:
    """Normal users should default to auto_netting_enabled = true."""
    result = await db_session.execute(
        text("SELECT auto_netting_enabled FROM accounts WHERE user_id = :uid"),
        {"uid": test_user_id},
    )
    row = result.fetchone()
    assert row is not None
    assert row.auto_netting_enabled is True
```

**Step 6: Run to verify PASS**
```bash
uv run pytest tests/integration/test_amm_migration.py -v
```

**Step 7: Commit**
```bash
git add alembic/versions/ src/pm_account/infrastructure/db_models.py tests/integration/test_amm_migration.py
git commit -m "feat(db): add auto_netting_enabled column and AMM system account"
```

---

## Task 3: Netting Bypass for AMM

**Files:**
- Modify: `src/pm_clearing/domain/netting.py`
- Test: `tests/unit/test_netting_amm_bypass.py`

**Step 1: Write failing test**

```python
# tests/unit/test_netting_amm_bypass.py
"""Test that execute_netting_if_needed skips AMM account."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.pm_account.domain.constants import AMM_USER_ID


class TestNettingAMMBypass:
    @pytest.mark.asyncio
    async def test_amm_user_skips_netting(self) -> None:
        """AMM account (auto_netting_enabled=false) should skip netting entirely."""
        from src.pm_clearing.domain.netting import execute_netting_if_needed

        db = AsyncMock()
        # Mock: SELECT auto_netting_enabled returns False
        mock_result = MagicMock()
        mock_result.scalar.return_value = False
        db.execute.return_value = mock_result

        result = await execute_netting_if_needed(AMM_USER_ID, "mkt-1", MagicMock(), db)
        assert result == 0  # No netting performed

    @pytest.mark.asyncio
    async def test_normal_user_proceeds_with_netting(self) -> None:
        """Normal user (auto_netting_enabled=true) should proceed with netting."""
        from src.pm_clearing.domain.netting import execute_netting_if_needed

        db = AsyncMock()
        # Mock: SELECT auto_netting_enabled returns True
        mock_result = MagicMock()
        mock_result.scalar.return_value = True
        db.execute.return_value = mock_result

        # This test verifies the function doesn't short-circuit for normal users.
        # The function should proceed past the auto_netting check.
        # We'll need to mock further DB calls for positions, etc.
        # For now, just verify it doesn't return 0 immediately.
        # (Full netting logic tested in existing test_auto_netting.py)
        # We patch the rest of the function to isolate the check:
        with patch(
            "src.pm_clearing.domain.netting._do_netting", new_callable=AsyncMock
        ) as mock_do:
            mock_do.return_value = 5
            result = await execute_netting_if_needed(
                "normal-user-id", "mkt-1", MagicMock(), db
            )
            assert mock_do.called

    @pytest.mark.asyncio
    async def test_netting_bypass_no_db_column_found(self) -> None:
        """If auto_netting_enabled lookup returns None (no row), treat as enabled (fail-safe)."""
        from src.pm_clearing.domain.netting import execute_netting_if_needed

        db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar.return_value = None  # no account row
        db.execute.return_value = mock_result

        # Should not skip — fail-safe defaults to netting enabled
        with patch(
            "src.pm_clearing.domain.netting._do_netting", new_callable=AsyncMock
        ) as mock_do:
            mock_do.return_value = 0
            await execute_netting_if_needed("unknown-user", "mkt-1", MagicMock(), db)
            assert mock_do.called
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_netting_amm_bypass.py -v
```

**Step 3: Modify `netting.py`**

Add the auto_netting_enabled check at the top of `execute_netting_if_needed`:

```python
# In src/pm_clearing/domain/netting.py
# At the START of execute_netting_if_needed, BEFORE any existing logic:

async def execute_netting_if_needed(
    user_id: str, market_id: str, market, db
) -> int:
    """Execute auto-netting if user has it enabled.

    AMM system account has auto_netting_enabled=false to preserve
    dual-sided inventory. See data dictionary v1.3 §3.3.
    """
    # --- AMM prerequisite: check auto_netting_enabled ---
    netting_check = await db.execute(
        text(
            "SELECT auto_netting_enabled FROM accounts WHERE user_id = :uid"
        ),
        {"uid": user_id},
    )
    auto_netting = netting_check.scalar()
    if auto_netting is False:  # explicit False, not None
        return 0  # skip netting for this user
    # --- end AMM prerequisite ---

    return await _do_netting(user_id, market_id, market, db)
```

**Important**: Extract the existing netting logic body into a `_do_netting` helper function, so the auto_netting check is cleanly separated. The existing function body becomes `_do_netting`.

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_netting_amm_bypass.py -v
# Also run existing netting tests to ensure no regression:
uv run pytest tests/unit/test_auto_netting.py -v
```

**Step 5: Commit**
```bash
git add src/pm_clearing/domain/netting.py tests/unit/test_netting_amm_bypass.py
git commit -m "feat(pm_clearing): add auto_netting_enabled bypass for AMM account"
```

---

## Task 4: Self-Trade Exemption for AMM

**Files:**
- Modify: `src/pm_risk/rules/self_trade.py`
- Test: `tests/unit/test_self_trade_exempt.py`

**Step 1: Write failing test**

```python
# tests/unit/test_self_trade_exempt.py
"""Test self-trade exemption for AMM. See data dictionary v1.3 §3.4 方案 A."""
from src.pm_risk.rules.self_trade import is_self_trade
from src.pm_account.domain.constants import AMM_USER_ID


class TestSelfTradeExemption:
    def test_amm_incoming_not_self_trade(self) -> None:
        """AMM as incoming order should NOT trigger self-trade."""
        assert is_self_trade(AMM_USER_ID, AMM_USER_ID) is False

    def test_amm_vs_normal_not_self_trade(self) -> None:
        assert is_self_trade(AMM_USER_ID, "user-123") is False

    def test_normal_vs_amm_not_self_trade(self) -> None:
        """Normal user incoming vs AMM resting — not self-trade (different users)."""
        assert is_self_trade("user-123", AMM_USER_ID) is False

    def test_normal_same_user_is_self_trade(self) -> None:
        """Normal user vs same user — still self-trade."""
        assert is_self_trade("user-123", "user-123") is True

    def test_normal_different_users_not_self_trade(self) -> None:
        assert is_self_trade("user-123", "user-456") is False
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_self_trade_exempt.py -v
```

**Step 3: Modify `self_trade.py`**

```python
# src/pm_risk/rules/self_trade.py
"""Self-trade detection with AMM exemption.

AMM is exempt from self-trade detection because it legitimately
needs to have its YES buy orders match against its own NO sell orders
(which appear as SELL on the book). See data dictionary v1.3 §3.4.
"""
from src.pm_account.domain.constants import AMM_USER_ID

# Extensible set: add more market-maker user_ids if needed in the future
SELF_TRADE_EXEMPT_USERS: frozenset[str] = frozenset({AMM_USER_ID})


def is_self_trade(incoming_user_id: str, resting_user_id: str) -> bool:
    """Check if incoming and resting orders would constitute a self-trade.

    Returns False (exempt) if incoming_user_id is in SELF_TRADE_EXEMPT_USERS.
    """
    if incoming_user_id in SELF_TRADE_EXEMPT_USERS:
        return False
    return incoming_user_id == resting_user_id
```

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_self_trade_exempt.py -v
# Also run existing self-trade tests:
uv run pytest tests/ -k "self_trade" -v
```

**Step 5: Commit**
```bash
git add src/pm_risk/rules/self_trade.py tests/unit/test_self_trade_exempt.py
git commit -m "feat(pm_risk): add self-trade exemption for AMM user"
```

---

## Task 5: AMM Authentication Dependency

**Files:**
- Modify: `src/pm_gateway/auth/dependencies.py`
- Test: `tests/unit/test_amm_auth.py`

**Step 1: Write failing test**

```python
# tests/unit/test_amm_auth.py
"""Test AMM-only endpoint authentication dependency."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.pm_account.domain.constants import AMM_USER_ID


class TestRequireAMMUser:
    @pytest.mark.asyncio
    async def test_amm_user_passes(self) -> None:
        from src.pm_gateway.auth.dependencies import require_amm_user

        mock_user = MagicMock()
        mock_user.id = AMM_USER_ID
        # Should return the user without raising
        result = await require_amm_user(current_user=mock_user)
        assert result.id == AMM_USER_ID

    @pytest.mark.asyncio
    async def test_non_amm_user_rejected(self) -> None:
        from src.pm_gateway.auth.dependencies import require_amm_user
        from src.pm_common.errors import AppError

        mock_user = MagicMock()
        mock_user.id = "normal-user-id"
        with pytest.raises(AppError) as exc_info:
            await require_amm_user(current_user=mock_user)
        assert exc_info.value.http_status == 403
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_amm_auth.py -v
```

**Step 3: Implement**

```python
# Add to src/pm_gateway/auth/dependencies.py:

from src.pm_account.domain.constants import AMM_USER_ID
from src.pm_common.errors import AppError


async def require_amm_user(
    current_user=Depends(get_current_user),
):
    """Dependency that ensures the authenticated user is the AMM system account.

    MVP: Uses standard JWT + user_id check.
    Phase 1.5: Will check Service Token + account_type == SYSTEM_BOT.
    """
    if str(current_user.id) != AMM_USER_ID:
        raise AppError(
            http_status=403,
            error_code=6099,
            message="This endpoint is restricted to AMM system account",
        )
    return current_user
```

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_amm_auth.py -v
```

**Step 5: Commit**
```bash
git add src/pm_gateway/auth/dependencies.py tests/unit/test_amm_auth.py
git commit -m "feat(pm_gateway): add require_amm_user authentication dependency"
```

---

## Task 6: Privileged Mint API

**Files:**
- Create: `src/pm_clearing/application/amm_schemas.py`
- Create: `src/pm_clearing/domain/mint_service.py`
- Create: `src/pm_clearing/api/amm_router.py`
- Test: `tests/unit/test_mint_service.py`
- Test: `tests/integration/test_amm_mint_api.py`

**Step 1: Write failing unit test**

```python
# tests/unit/test_mint_service.py
"""Test privileged mint business logic. See interface contract v1.4 §3.3."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal
from src.pm_account.domain.constants import AMM_USER_ID


class TestPrivilegedMint:
    @pytest.mark.asyncio
    async def test_mint_success_deducts_balance(self) -> None:
        """Mint 1000 shares costs 100000 cents (1000 × 100)."""
        from src.pm_clearing.domain.mint_service import execute_privileged_mint

        db = AsyncMock()
        # Mock: no existing idempotency record
        db.execute.side_effect = self._mock_mint_db_calls(
            idempotent_exists=False,
            market_status="ACTIVE",
            available_balance=500000,
            version=1,
        )

        result = await execute_privileged_mint(
            user_id=AMM_USER_ID,
            market_id="mkt-1",
            quantity=1000,
            idempotency_key="mint-001",
            db=db,
        )
        assert result["cost_cents"] == 100000
        assert result["minted_quantity"] == 1000

    @pytest.mark.asyncio
    async def test_mint_idempotent_returns_existing(self) -> None:
        """Duplicate idempotency_key returns previous result without re-executing."""
        from src.pm_clearing.domain.mint_service import execute_privileged_mint

        db = AsyncMock()
        db.execute.side_effect = self._mock_mint_db_calls(
            idempotent_exists=True,
        )

        result = await execute_privileged_mint(
            user_id=AMM_USER_ID,
            market_id="mkt-1",
            quantity=1000,
            idempotency_key="mint-001",
            db=db,
        )
        assert result.get("idempotent_hit") is True

    @pytest.mark.asyncio
    async def test_mint_insufficient_balance_raises(self) -> None:
        """Balance < cost should raise AppError 2001."""
        from src.pm_clearing.domain.mint_service import execute_privileged_mint
        from src.pm_common.errors import AppError

        db = AsyncMock()
        db.execute.side_effect = self._mock_mint_db_calls(
            idempotent_exists=False,
            market_status="ACTIVE",
            available_balance=5000,  # only 50 dollars, need 1000 dollars
            version=1,
        )

        with pytest.raises(AppError) as exc_info:
            await execute_privileged_mint(
                user_id=AMM_USER_ID,
                market_id="mkt-1",
                quantity=1000,
                idempotency_key="mint-002",
                db=db,
            )
        assert exc_info.value.error_code == 2001

    @pytest.mark.asyncio
    async def test_mint_inactive_market_raises(self) -> None:
        """Non-ACTIVE market should raise AppError 3002."""
        from src.pm_clearing.domain.mint_service import execute_privileged_mint
        from src.pm_common.errors import AppError

        db = AsyncMock()
        db.execute.side_effect = self._mock_mint_db_calls(
            idempotent_exists=False,
            market_status="SUSPENDED",
            available_balance=500000,
            version=1,
        )

        with pytest.raises(AppError) as exc_info:
            await execute_privileged_mint(
                user_id=AMM_USER_ID,
                market_id="mkt-1",
                quantity=1000,
                idempotency_key="mint-003",
                db=db,
            )
        assert exc_info.value.error_code == 3002

    @staticmethod
    def _mock_mint_db_calls(**kwargs):
        """Helper to create mock DB call side effects for mint tests."""
        calls = []
        if kwargs.get("idempotent_exists"):
            # First call: idempotency check returns existing record
            mock_result = MagicMock()
            mock_result.fetchone.return_value = MagicMock(
                amount_cents=-100000, reference_id="mint-001"
            )
            calls.append(mock_result)
        else:
            # First call: idempotency check returns None
            mock_result = MagicMock()
            mock_result.fetchone.return_value = None
            calls.append(mock_result)

            # Second call: market status check
            mock_market = MagicMock()
            mock_market.fetchone.return_value = MagicMock(
                status=kwargs.get("market_status", "ACTIVE")
            )
            calls.append(mock_market)

            # Third call: account balance check
            mock_account = MagicMock()
            mock_account.fetchone.return_value = MagicMock(
                available_balance=kwargs.get("available_balance", 0),
                version=kwargs.get("version", 0),
            )
            calls.append(mock_account)

            # Subsequent calls: UPDATE/INSERT operations return mock results
            for _ in range(10):
                mock_op = MagicMock()
                mock_op.rowcount = 1
                calls.append(mock_op)

        return calls
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_mint_service.py -v
```

**Step 3: Implement schemas**

```python
# src/pm_clearing/application/amm_schemas.py
"""AMM-specific request/response schemas for Mint and Burn APIs."""
from pydantic import BaseModel, Field


class MintRequest(BaseModel):
    market_id: str
    quantity: int = Field(gt=0, description="Number of YES+NO share pairs to mint")
    idempotency_key: str = Field(
        min_length=1, max_length=128, description="Unique key to prevent duplicate mints"
    )


class MintResponse(BaseModel):
    market_id: str
    minted_quantity: int
    cost_cents: int
    new_yes_inventory: int
    new_no_inventory: int
    remaining_balance_cents: int


class BurnRequest(BaseModel):
    market_id: str
    quantity: int = Field(gt=0, description="Number of YES+NO share pairs to burn")
    idempotency_key: str = Field(
        min_length=1, max_length=128, description="Unique key to prevent duplicate burns"
    )


class BurnResponse(BaseModel):
    market_id: str
    burned_quantity: int
    recovered_cents: int
    new_yes_inventory: int
    new_no_inventory: int
    remaining_balance_cents: int
```

**Step 4: Implement mint service**

```python
# src/pm_clearing/domain/mint_service.py
"""Privileged Mint — AMM special operation to create YES+NO share pairs.

Aligned with interface contract v1.4 §3.3:
- Deducts cost from AMM account (quantity × 100 cents)
- Increases market reserve_balance
- Increases market total_yes/no_shares
- Creates/updates AMM position
- Writes ledger entries (MINT_COST + MINT_RESERVE_IN)
- Writes audit trade record (scenario=MINT)
- Idempotent via idempotency_key → ledger_entries.reference_id
"""
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_common.errors import AppError
from src.pm_account.domain.constants import AMM_USER_ID

logger = logging.getLogger(__name__)

COST_PER_SHARE_CENTS = 100
INITIAL_FAIR_COST_PER_SHARE = 50  # YES/NO each at 50 cents initial cost basis


async def execute_privileged_mint(
    user_id: str,
    market_id: str,
    quantity: int,
    idempotency_key: str,
    db: AsyncSession,
) -> dict:
    """Execute privileged mint within the caller's transaction.

    Returns dict with mint result data.
    Raises AppError on validation failure.
    """
    # Step 1: Idempotency check
    existing = await db.execute(
        text(
            "SELECT amount_cents, reference_id FROM ledger_entries "
            "WHERE reference_type = 'AMM_MINT' AND reference_id = :key"
        ),
        {"key": idempotency_key},
    )
    row = existing.fetchone()
    if row is not None:
        logger.info("Mint idempotency hit: key=%s", idempotency_key)
        return {"idempotent_hit": True, "idempotency_key": idempotency_key}

    # Step 2: Validate market status
    market_result = await db.execute(
        text("SELECT status FROM markets WHERE id = :mid"),
        {"mid": market_id},
    )
    market_row = market_result.fetchone()
    if market_row is None:
        raise AppError(http_status=404, error_code=3001, message="Market not found")
    if market_row.status != "ACTIVE":
        raise AppError(
            http_status=422, error_code=3002, message="Market is not active"
        )

    # Step 3: Calculate cost
    cost_cents = quantity * COST_PER_SHARE_CENTS

    # Step 4: Deduct from AMM account (optimistic locking)
    account_result = await db.execute(
        text(
            "SELECT available_balance, version FROM accounts "
            "WHERE user_id = :uid FOR UPDATE"
        ),
        {"uid": user_id},
    )
    account_row = account_result.fetchone()
    if account_row is None or account_row.available_balance < cost_cents:
        raise AppError(
            http_status=422,
            error_code=2001,
            message=f"Insufficient balance: need {cost_cents}, "
            f"have {account_row.available_balance if account_row else 0}",
        )

    await db.execute(
        text(
            "UPDATE accounts SET available_balance = available_balance - :cost, "
            "version = version + 1 WHERE user_id = :uid"
        ),
        {"cost": cost_cents, "uid": user_id},
    )

    # Step 5: Increase reserve
    await db.execute(
        text(
            "UPDATE markets SET reserve_balance = reserve_balance + :cost "
            "WHERE id = :mid"
        ),
        {"cost": cost_cents, "mid": market_id},
    )

    # Step 6: Increase shares
    await db.execute(
        text(
            "UPDATE markets SET "
            "total_yes_shares = total_yes_shares + :qty, "
            "total_no_shares = total_no_shares + :qty "
            "WHERE id = :mid"
        ),
        {"qty": quantity, "mid": market_id},
    )

    # Step 7: Update/insert positions
    await db.execute(
        text(
            "INSERT INTO positions (user_id, market_id, yes_volume, yes_cost_sum, "
            "no_volume, no_cost_sum, yes_pending_sell, no_pending_sell) "
            "VALUES (:uid, :mid, :qty, :cost_half, :qty, :cost_half, 0, 0) "
            "ON CONFLICT (user_id, market_id) DO UPDATE SET "
            "yes_volume = positions.yes_volume + :qty, "
            "yes_cost_sum = positions.yes_cost_sum + :cost_half, "
            "no_volume = positions.no_volume + :qty, "
            "no_cost_sum = positions.no_cost_sum + :cost_half"
        ),
        {
            "uid": user_id,
            "mid": market_id,
            "qty": quantity,
            "cost_half": quantity * INITIAL_FAIR_COST_PER_SHARE,
        },
    )

    # Step 8: Write ledger entries
    # NOTE: ledger_entries.id is BIGSERIAL (auto-increment), NOT UUID.
    # Do NOT specify id — let PostgreSQL generate it automatically.
    # (Aligned with 01_全局约定与数据库设计 v2.3 §2.7)
    await db.execute(
        text(
            "INSERT INTO ledger_entries "
            "(user_id, entry_type, amount_cents, reference_type, reference_id, created_at) "
            "VALUES (:uid, 'MINT_COST', :amount, 'AMM_MINT', :ref, NOW())"
        ),
        {
            "uid": user_id,
            "amount": -cost_cents,
            "ref": idempotency_key,
        },
    )
    await db.execute(
        text(
            "INSERT INTO ledger_entries "
            "(user_id, entry_type, amount_cents, reference_type, reference_id, created_at) "
            "VALUES (:uid, 'MINT_RESERVE_IN', :amount, 'AMM_MINT', :ref, NOW())"
        ),
        {
            "uid": "SYSTEM",
            "amount": cost_cents,
            "ref": idempotency_key,
        },
    )

    # Step 9: Audit trade record
    # NOTE: trades.id is also ULID/auto-generated per existing convention.
    # Use ULID generation consistent with pm_clearing existing pattern.
    import uuid as _uuid

    trade_id = str(_uuid.uuid4())
    await db.execute(
        text(
            "INSERT INTO trades "
            "(id, market_id, buy_order_id, sell_order_id, buy_user_id, sell_user_id, "
            "scenario, price_cents, quantity, maker_fee, taker_fee, created_at) "
            "VALUES (:id, :mid, :id, :id, :buyer, 'SYSTEM', "
            "'MINT', :fair_price, :qty, 0, 0, NOW())"
        ),
        {
            "id": trade_id,
            "mid": market_id,
            "buyer": user_id,
            "qty": quantity,
            "fair_price": INITIAL_FAIR_COST_PER_SHARE,
        },
    )

    # Build response data
    # Read updated positions and balance
    pos_result = await db.execute(
        text(
            "SELECT yes_volume, no_volume FROM positions "
            "WHERE user_id = :uid AND market_id = :mid"
        ),
        {"uid": user_id, "mid": market_id},
    )
    pos_row = pos_result.fetchone()

    bal_result = await db.execute(
        text("SELECT available_balance FROM accounts WHERE user_id = :uid"),
        {"uid": user_id},
    )
    bal_row = bal_result.fetchone()

    return {
        "market_id": market_id,
        "minted_quantity": quantity,
        "cost_cents": cost_cents,
        "new_yes_inventory": pos_row.yes_volume if pos_row else quantity,
        "new_no_inventory": pos_row.no_volume if pos_row else quantity,
        "remaining_balance_cents": bal_row.available_balance if bal_row else 0,
    }
```

**Step 5: Implement router**

```python
# src/pm_clearing/api/amm_router.py
"""AMM-specific endpoints: Privileged Mint and Burn.

Mounted at /api/v1/amm/ in main.py.
"""
import logging
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_common.schemas import ApiResponse
from src.pm_common.database import get_db
from src.pm_gateway.auth.dependencies import require_amm_user
from src.pm_clearing.application.amm_schemas import (
    MintRequest,
    MintResponse,
    BurnRequest,
    BurnResponse,
)
from src.pm_clearing.domain.mint_service import execute_privileged_mint
from src.pm_clearing.domain.burn_service import execute_privileged_burn

logger = logging.getLogger(__name__)

router = APIRouter(tags=["AMM"])


@router.post("/mint", response_model=ApiResponse[MintResponse], status_code=201)
async def privileged_mint(
    request: MintRequest,
    current_user=Depends(require_amm_user),
    db: AsyncSession = Depends(get_db),
):
    """Privileged Mint: create YES+NO share pairs for AMM.

    See interface contract v1.4 §3.3.
    """
    async with db.begin():
        result = await execute_privileged_mint(
            user_id=str(current_user.id),
            market_id=request.market_id,
            quantity=request.quantity,
            idempotency_key=request.idempotency_key,
            db=db,
        )

    if result.get("idempotent_hit"):
        # Return 200 for idempotent hit instead of 201
        return ApiResponse(code=0, message="Mint already processed (idempotent)", data=result)

    return ApiResponse(
        code=0,
        message="Shares minted successfully",
        data=MintResponse(**result),
    )


@router.post("/burn", response_model=ApiResponse[BurnResponse])
async def privileged_burn(
    request: BurnRequest,
    current_user=Depends(require_amm_user),
    db: AsyncSession = Depends(get_db),
):
    """Privileged Burn (Auto-Merge): destroy YES+NO share pairs, recover cash.

    See interface contract v1.4 §3.4.
    """
    async with db.begin():
        result = await execute_privileged_burn(
            user_id=str(current_user.id),
            market_id=request.market_id,
            quantity=request.quantity,
            idempotency_key=request.idempotency_key,
            db=db,
        )

    if result.get("idempotent_hit"):
        return ApiResponse(code=0, message="Burn already processed (idempotent)", data=result)

    return ApiResponse(
        code=0,
        message="Shares burned (auto-merge) successfully",
        data=BurnResponse(**result),
    )
```

**Step 6: Run to verify PASS**
```bash
uv run pytest tests/unit/test_mint_service.py -v
```

**Step 7: Commit**
```bash
git add src/pm_clearing/application/amm_schemas.py src/pm_clearing/domain/mint_service.py src/pm_clearing/api/amm_router.py tests/unit/test_mint_service.py
git commit -m "feat(pm_clearing): add Privileged Mint API for AMM"
```

---

## Task 7: Privileged Burn API

**Files:**
- Create: `src/pm_clearing/domain/burn_service.py`
- Test: `tests/unit/test_burn_service.py`

**Step 1: Write failing test**

```python
# tests/unit/test_burn_service.py
"""Test privileged burn business logic. See interface contract v1.4 §3.4."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.pm_account.domain.constants import AMM_USER_ID


class TestPrivilegedBurn:
    @pytest.mark.asyncio
    async def test_burn_success_recovers_cash(self) -> None:
        """Burn 200 shares recovers 20000 cents (200 × 100)."""
        from src.pm_clearing.domain.burn_service import execute_privileged_burn

        db = AsyncMock()
        db.execute.side_effect = self._mock_burn_db_calls(
            idempotent_exists=False,
            market_status="ACTIVE",
            yes_volume=1000,
            no_volume=1000,
            yes_pending_sell=0,
            no_pending_sell=0,
        )

        result = await execute_privileged_burn(
            user_id=AMM_USER_ID,
            market_id="mkt-1",
            quantity=200,
            idempotency_key="burn-001",
            db=db,
        )
        assert result["recovered_cents"] == 20000
        assert result["burned_quantity"] == 200

    @pytest.mark.asyncio
    async def test_burn_insufficient_yes_shares_raises(self) -> None:
        """Not enough YES shares should raise AppError 5001."""
        from src.pm_clearing.domain.burn_service import execute_privileged_burn
        from src.pm_common.errors import AppError

        db = AsyncMock()
        db.execute.side_effect = self._mock_burn_db_calls(
            idempotent_exists=False,
            market_status="ACTIVE",
            yes_volume=100,  # only 100, need 200
            no_volume=1000,
            yes_pending_sell=0,
            no_pending_sell=0,
        )

        with pytest.raises(AppError) as exc_info:
            await execute_privileged_burn(
                user_id=AMM_USER_ID,
                market_id="mkt-1",
                quantity=200,
                idempotency_key="burn-002",
                db=db,
            )
        assert exc_info.value.error_code == 5001

    @pytest.mark.asyncio
    async def test_burn_respects_pending_sell(self) -> None:
        """Available = volume - pending_sell. Should fail if available < quantity."""
        from src.pm_clearing.domain.burn_service import execute_privileged_burn
        from src.pm_common.errors import AppError

        db = AsyncMock()
        db.execute.side_effect = self._mock_burn_db_calls(
            idempotent_exists=False,
            market_status="ACTIVE",
            yes_volume=500,
            no_volume=500,
            yes_pending_sell=400,  # available = 100, need 200
            no_pending_sell=0,
        )

        with pytest.raises(AppError) as exc_info:
            await execute_privileged_burn(
                user_id=AMM_USER_ID,
                market_id="mkt-1",
                quantity=200,
                idempotency_key="burn-003",
                db=db,
            )
        assert exc_info.value.error_code == 5001

    @pytest.mark.asyncio
    async def test_burn_idempotent(self) -> None:
        from src.pm_clearing.domain.burn_service import execute_privileged_burn

        db = AsyncMock()
        db.execute.side_effect = self._mock_burn_db_calls(idempotent_exists=True)

        result = await execute_privileged_burn(
            user_id=AMM_USER_ID,
            market_id="mkt-1",
            quantity=200,
            idempotency_key="burn-001",
            db=db,
        )
        assert result.get("idempotent_hit") is True

    @staticmethod
    def _mock_burn_db_calls(**kwargs):
        calls = []
        if kwargs.get("idempotent_exists"):
            mock_result = MagicMock()
            mock_result.fetchone.return_value = MagicMock(
                amount_cents=20000, reference_id="burn-001"
            )
            calls.append(mock_result)
        else:
            # Idempotency check: not found
            mock_idem = MagicMock()
            mock_idem.fetchone.return_value = None
            calls.append(mock_idem)

            # Market status check
            mock_market = MagicMock()
            mock_market.fetchone.return_value = MagicMock(
                status=kwargs.get("market_status", "ACTIVE")
            )
            calls.append(mock_market)

            # Position check
            mock_pos = MagicMock()
            mock_pos.fetchone.return_value = MagicMock(
                yes_volume=kwargs.get("yes_volume", 0),
                no_volume=kwargs.get("no_volume", 0),
                yes_pending_sell=kwargs.get("yes_pending_sell", 0),
                no_pending_sell=kwargs.get("no_pending_sell", 0),
                yes_cost_sum=kwargs.get("yes_volume", 0) * 50,
                no_cost_sum=kwargs.get("no_volume", 0) * 50,
            )
            calls.append(mock_pos)

            # Subsequent UPDATE/INSERT calls
            for _ in range(10):
                mock_op = MagicMock()
                mock_op.rowcount = 1
                calls.append(mock_op)

        return calls
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_burn_service.py -v
```

**Step 3: Implement**

```python
# src/pm_clearing/domain/burn_service.py
"""Privileged Burn (Auto-Merge) — AMM destroys YES+NO share pairs, recovers cash.

Aligned with interface contract v1.4 §3.4:
- Validates sufficient available inventory (volume - pending_sell)
- Deducts YES and NO positions
- Releases cost_sum proportionally (weighted average)
- Reduces market reserve_balance
- Credits AMM account available_balance
- Writes ledger entries (BURN_REVENUE + BURN_RESERVE_OUT)
- Writes audit trade record (scenario=BURN)
- Idempotent via idempotency_key
"""
import logging
import uuid
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_common.errors import AppError

logger = logging.getLogger(__name__)

RECOVERY_PER_SHARE_CENTS = 100


async def execute_privileged_burn(
    user_id: str,
    market_id: str,
    quantity: int,
    idempotency_key: str,
    db: AsyncSession,
) -> dict:
    """Execute privileged burn within the caller's transaction."""
    # Step 1: Idempotency check
    existing = await db.execute(
        text(
            "SELECT amount_cents, reference_id FROM ledger_entries "
            "WHERE reference_type = 'AMM_BURN' AND reference_id = :key"
        ),
        {"key": idempotency_key},
    )
    if existing.fetchone() is not None:
        logger.info("Burn idempotency hit: key=%s", idempotency_key)
        return {"idempotent_hit": True, "idempotency_key": idempotency_key}

    # Step 2: Validate market
    market_result = await db.execute(
        text("SELECT status FROM markets WHERE id = :mid"),
        {"mid": market_id},
    )
    market_row = market_result.fetchone()
    if market_row is None:
        raise AppError(http_status=404, error_code=3001, message="Market not found")
    if market_row.status != "ACTIVE":
        raise AppError(http_status=422, error_code=3002, message="Market is not active")

    # Step 3: Validate positions (available = volume - pending_sell)
    pos_result = await db.execute(
        text(
            "SELECT yes_volume, no_volume, yes_pending_sell, no_pending_sell, "
            "yes_cost_sum, no_cost_sum "
            "FROM positions WHERE user_id = :uid AND market_id = :mid FOR UPDATE"
        ),
        {"uid": user_id, "mid": market_id},
    )
    pos_row = pos_result.fetchone()
    if pos_row is None:
        raise AppError(
            http_status=422, error_code=5001, message="No positions found"
        )

    yes_available = pos_row.yes_volume - pos_row.yes_pending_sell
    no_available = pos_row.no_volume - pos_row.no_pending_sell
    max_burnable = min(yes_available, no_available)

    if quantity > max_burnable:
        raise AppError(
            http_status=422,
            error_code=5001,
            message=f"Insufficient available shares: can burn max {max_burnable}, "
            f"requested {quantity}",
        )

    # Step 4: Deduct positions + release cost_sum (weighted average)
    yes_cost_release = (
        (pos_row.yes_cost_sum * quantity) // pos_row.yes_volume
        if pos_row.yes_volume > 0
        else 0
    )
    no_cost_release = (
        (pos_row.no_cost_sum * quantity) // pos_row.no_volume
        if pos_row.no_volume > 0
        else 0
    )

    await db.execute(
        text(
            "UPDATE positions SET "
            "yes_volume = yes_volume - :qty, "
            "yes_cost_sum = yes_cost_sum - :yes_cost, "
            "no_volume = no_volume - :qty, "
            "no_cost_sum = no_cost_sum - :no_cost "
            "WHERE user_id = :uid AND market_id = :mid"
        ),
        {
            "qty": quantity,
            "yes_cost": yes_cost_release,
            "no_cost": no_cost_release,
            "uid": user_id,
            "mid": market_id,
        },
    )

    # Step 5: Reduce reserve
    recovery_cents = quantity * RECOVERY_PER_SHARE_CENTS
    await db.execute(
        text(
            "UPDATE markets SET reserve_balance = reserve_balance - :amount "
            "WHERE id = :mid"
        ),
        {"amount": recovery_cents, "mid": market_id},
    )

    # Step 6: Credit AMM account
    await db.execute(
        text(
            "UPDATE accounts SET available_balance = available_balance + :amount, "
            "version = version + 1 WHERE user_id = :uid"
        ),
        {"amount": recovery_cents, "uid": user_id},
    )

    # Step 7: Write ledger entries
    # NOTE: ledger_entries.id is BIGSERIAL — omit id, let PostgreSQL auto-generate.
    await db.execute(
        text(
            "INSERT INTO ledger_entries "
            "(user_id, entry_type, amount_cents, reference_type, reference_id, created_at) "
            "VALUES (:uid, 'BURN_REVENUE', :amount, 'AMM_BURN', :ref, NOW())"
        ),
        {
            "uid": user_id,
            "amount": recovery_cents,
            "ref": idempotency_key,
        },
    )
    await db.execute(
        text(
            "INSERT INTO ledger_entries "
            "(user_id, entry_type, amount_cents, reference_type, reference_id, created_at) "
            "VALUES (:uid, 'BURN_RESERVE_OUT', :amount, 'AMM_BURN', :ref, NOW())"
        ),
        {
            "uid": "SYSTEM",
            "amount": -recovery_cents,
            "ref": idempotency_key,
        },
    )

    # Step 8: Audit trade record
    # NOTE: trades.id uses ULID (same convention as pm_clearing)
    import uuid as _uuid

    await db.execute(
        text(
            "INSERT INTO trades "
            "(id, market_id, buy_order_id, sell_order_id, buy_user_id, sell_user_id, "
            "scenario, price_cents, quantity, maker_fee, taker_fee, created_at) "
            "VALUES (:id, :mid, :id, :id, 'SYSTEM', :seller, "
            "'BURN', :fair_price, :qty, 0, 0, NOW())"
        ),
        {
            "id": str(_uuid.uuid4()),
            "mid": market_id,
            "seller": user_id,
            "qty": quantity,
            "fair_price": RECOVERY_PER_SHARE_CENTS // 2,  # 50 cents per side
        },
    )

    # Read updated state
    pos_final = await db.execute(
        text(
            "SELECT yes_volume, no_volume FROM positions "
            "WHERE user_id = :uid AND market_id = :mid"
        ),
        {"uid": user_id, "mid": market_id},
    )
    pos_f = pos_final.fetchone()

    bal_final = await db.execute(
        text("SELECT available_balance FROM accounts WHERE user_id = :uid"),
        {"uid": user_id},
    )
    bal_f = bal_final.fetchone()

    return {
        "market_id": market_id,
        "burned_quantity": quantity,
        "recovered_cents": recovery_cents,
        "new_yes_inventory": pos_f.yes_volume if pos_f else 0,
        "new_no_inventory": pos_f.no_volume if pos_f else 0,
        "remaining_balance_cents": bal_f.available_balance if bal_f else 0,
    }
```

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_burn_service.py -v
```

**Step 5: Commit**
```bash
git add src/pm_clearing/domain/burn_service.py tests/unit/test_burn_service.py
git commit -m "feat(pm_clearing): add Privileged Burn (Auto-Merge) API for AMM"
```

---

## Task 8: Atomic Replace API

**Files:**
- Create: `src/pm_order/application/amm_schemas.py`
- Create: `src/pm_order/api/amm_router.py`
- Modify: `src/pm_matching/engine/engine.py` (add `replace_order` method)
- Test: `tests/unit/test_replace_logic.py`

**Step 1: Write failing test**

```python
# tests/unit/test_replace_logic.py
"""Test Atomic Replace logic. See interface contract v1.4 §3.1."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.pm_account.domain.constants import AMM_USER_ID


class TestAtomicReplace:
    @pytest.mark.asyncio
    async def test_replace_nonexistent_order_returns_6002(self) -> None:
        """old_order_id not found → AppError 6002."""
        from src.pm_common.errors import AppError

        engine = self._make_engine()
        db = AsyncMock()

        # Mock: order not found
        mock_result = MagicMock()
        mock_result.fetchone.return_value = None
        db.execute.return_value = mock_result

        with pytest.raises(AppError) as exc_info:
            await engine.replace_order(
                old_order_id="nonexistent",
                new_order_params=self._make_new_params(),
                user_id=AMM_USER_ID,
                db=db,
            )
        assert exc_info.value.error_code == 6002

    @pytest.mark.asyncio
    async def test_replace_non_amm_order_returns_6004(self) -> None:
        """old_order belongs to different user → AppError 6004."""
        from src.pm_common.errors import AppError

        engine = self._make_engine()
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.fetchone.return_value = MagicMock(
            user_id="other-user", status="OPEN", filled_quantity=0, market_id="mkt-1"
        )
        db.execute.return_value = mock_result

        with pytest.raises(AppError) as exc_info:
            await engine.replace_order(
                old_order_id="order-1",
                new_order_params=self._make_new_params(),
                user_id=AMM_USER_ID,
                db=db,
            )
        assert exc_info.value.error_code == 6004

    @pytest.mark.asyncio
    async def test_replace_filled_order_returns_6003(self) -> None:
        """old_order already fully filled → AppError 6003."""
        from src.pm_common.errors import AppError

        engine = self._make_engine()
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.fetchone.return_value = MagicMock(
            user_id=AMM_USER_ID, status="FILLED", filled_quantity=100, market_id="mkt-1"
        )
        db.execute.return_value = mock_result

        with pytest.raises(AppError) as exc_info:
            await engine.replace_order(
                old_order_id="order-1",
                new_order_params=self._make_new_params(),
                user_id=AMM_USER_ID,
                db=db,
            )
        assert exc_info.value.error_code == 6003

    @pytest.mark.asyncio
    async def test_replace_partially_filled_returns_6001(self) -> None:
        """old_order partially filled → cancel remainder, return 6001."""
        from src.pm_common.errors import AppError

        engine = self._make_engine()
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.fetchone.return_value = MagicMock(
            user_id=AMM_USER_ID,
            status="PARTIALLY_FILLED",
            filled_quantity=30,
            remaining_quantity=70,
            market_id="mkt-1",
            frozen_amount=4550,
            frozen_asset_type="FUNDS",
        )
        db.execute.return_value = mock_result

        with pytest.raises(AppError) as exc_info:
            await engine.replace_order(
                old_order_id="order-1",
                new_order_params=self._make_new_params(),
                user_id=AMM_USER_ID,
                db=db,
            )
        assert exc_info.value.error_code == 6001

    @pytest.mark.asyncio
    async def test_replace_market_mismatch_returns_6005(self) -> None:
        """new_order market_id != old_order market_id → AppError 6005."""
        from src.pm_common.errors import AppError

        engine = self._make_engine()
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.fetchone.return_value = MagicMock(
            user_id=AMM_USER_ID, status="OPEN", filled_quantity=0, market_id="mkt-1"
        )
        db.execute.return_value = mock_result

        new_params = self._make_new_params(market_id="mkt-2")

        with pytest.raises(AppError) as exc_info:
            await engine.replace_order(
                old_order_id="order-1",
                new_order_params=new_params,
                user_id=AMM_USER_ID,
                db=db,
            )
        assert exc_info.value.error_code == 6005

    @staticmethod
    def _make_engine():
        """Create a minimal MatchingEngine mock for replace_order testing."""
        # Import and create with minimal setup
        # The actual replace_order method will be added to MatchingEngine
        from src.pm_matching.engine.engine import MatchingEngine
        return MatchingEngine()

    @staticmethod
    def _make_new_params(market_id="mkt-1"):
        return MagicMock(
            client_order_id="amm_replace_001",
            market_id=market_id,
            side="YES",
            direction="SELL",
            price_cents=54,
            quantity=100,
            time_in_force="GTC",
        )
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_replace_logic.py -v
```

**Step 3: Implement schemas**

```python
# src/pm_order/application/amm_schemas.py
"""AMM-specific schemas for Replace and Batch Cancel."""
from pydantic import BaseModel, Field
from src.pm_order.application.schemas import PlaceOrderRequest


class ReplaceRequest(BaseModel):
    old_order_id: str
    new_order: PlaceOrderRequest


class ReplaceResponse(BaseModel):
    old_order_id: str
    old_order_status: str
    old_order_filled_quantity: int
    old_order_original_quantity: int
    new_order: dict
    trades: list[dict]


class PartialFillRejection(BaseModel):
    old_order_id: str
    old_order_status: str
    filled_quantity: int
    remaining_quantity_cancelled: int
    unfrozen_amount: int
    unfrozen_asset_type: str


class BatchCancelRequest(BaseModel):
    market_id: str
    cancel_scope: str = Field(
        default="ALL", pattern="^(ALL|BUY_ONLY|SELL_ONLY)$"
    )


class BatchCancelResponse(BaseModel):
    market_id: str
    cancelled_count: int
    total_unfrozen_funds_cents: int
    total_unfrozen_yes_shares: int
    total_unfrozen_no_shares: int
```

**Step 4: Add `replace_order` method to MatchingEngine**

Add the following method to `src/pm_matching/engine/engine.py` MatchingEngine class:

```python
async def replace_order(
    self, old_order_id: str, new_order_params, user_id: str, db
) -> dict:
    """Atomic Replace: cancel old order + place new order in a single transaction.

    See interface contract v1.4 §3.1.

    Error codes:
    - 6002: old order not found
    - 6004: old order not owned by user_id
    - 6003: old order already fully filled
    - 6001: old order partially filled (cancelled remainder, new order rejected)
    - 6005: new order market_id != old order market_id
    """
    from src.pm_common.errors import AppError
    from sqlalchemy import text

    # Step 0: Idempotency check on new_order.client_order_id
    idem_result = await db.execute(
        text("SELECT id FROM orders WHERE client_order_id = :coid AND user_id = :uid"),
        {"coid": new_order_params.client_order_id, "uid": user_id},
    )
    existing_order = idem_result.fetchone()
    if existing_order is not None:
        # Idempotent hit: return existing order
        return {"idempotent_hit": True, "existing_order_id": existing_order.id}

    # Step 1: Load old order
    old_result = await db.execute(
        text("SELECT * FROM orders WHERE id = :oid FOR UPDATE"),
        {"oid": old_order_id},
    )
    old_order = old_result.fetchone()

    if old_order is None:
        raise AppError(http_status=404, error_code=6002, message="Old order not found")

    if str(old_order.user_id) != str(user_id):
        raise AppError(
            http_status=403, error_code=6004, message="Old order not owned by AMM"
        )

    if old_order.status == "FILLED":
        raise AppError(
            http_status=422,
            error_code=6003,
            message="Old order already fully filled",
        )

    if old_order.filled_quantity > 0:
        # Partially filled: cancel remainder, reject replacement
        # Cancel the remaining portion
        await self._cancel_order_internal(old_order, db)
        raise AppError(
            http_status=422,
            error_code=6001,
            message="Old order partially filled, replacement rejected",
            data={
                "old_order_id": old_order_id,
                "old_order_status": "CANCELLED",
                "filled_quantity": old_order.filled_quantity,
                "remaining_quantity_cancelled": old_order.remaining_quantity,
                "unfrozen_amount": old_order.frozen_amount,
                "unfrozen_asset_type": old_order.frozen_asset_type,
            },
        )

    # Step 2: Validate market_id consistency
    if str(old_order.market_id) != str(new_order_params.market_id):
        raise AppError(
            http_status=422,
            error_code=6005,
            message="New order market_id must match old order market_id",
        )

    market_id = str(old_order.market_id)

    # Step 3: Atomic replace within market lock
    # (Lock is acquired by the caller or we acquire it here)
    #
    # CRITICAL TRANSACTION SAFETY NOTE:
    # Both _cancel_order_internal and place_order run within the SAME
    # db.begin() transaction (opened by the caller in amm_router.py).
    # If place_order raises AppError (e.g., 2001 insufficient balance after
    # old order unfreeze doesn't cover new order's freeze requirement),
    # the exception MUST propagate up WITHOUT being caught internally.
    # This ensures the entire transaction rolls back atomically —
    # the old order's cancellation is also reverted, preserving its OPEN state.
    # Do NOT wrap place_order in try/except within this method.
    async with self._market_locks[market_id]:
        # Cancel old order (unfreeze old funds)
        await self._cancel_order_internal(old_order, db)

        # Place new order through standard pipeline (freeze new funds)
        # If this raises (e.g., insufficient balance), the whole transaction
        # rolls back and old order remains OPEN — this is correct behavior.
        result = await self.place_order(new_order_params, db)

    return {
        "old_order_id": old_order_id,
        "old_order_status": "CANCELLED",
        "old_order_filled_quantity": 0,
        "old_order_original_quantity": old_order.quantity,
        "new_order": result.get("order", {}),
        "trades": result.get("trades", []),
    }
```

**Step 5: Implement AMM order router**

```python
# src/pm_order/api/amm_router.py
"""AMM-specific order endpoints: Atomic Replace and Batch Cancel.

Mounted at /api/v1/amm/orders/ in main.py.
"""
import logging
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.pm_common.schemas import ApiResponse
from src.pm_common.database import get_db
from src.pm_gateway.auth.dependencies import require_amm_user
from src.pm_order.application.amm_schemas import (
    ReplaceRequest,
    ReplaceResponse,
    BatchCancelRequest,
    BatchCancelResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["AMM Orders"])


@router.post("/replace", response_model=ApiResponse[ReplaceResponse])
async def atomic_replace(
    request: ReplaceRequest,
    current_user=Depends(require_amm_user),
    db: AsyncSession = Depends(get_db),
):
    """Atomic Replace: cancel old order + place new order atomically.

    See interface contract v1.4 §3.1.
    """
    from src.pm_matching.application.service import get_matching_engine

    engine = get_matching_engine()

    async with db.begin():
        result = await engine.replace_order(
            old_order_id=request.old_order_id,
            new_order_params=request.new_order,
            user_id=str(current_user.id),
            db=db,
        )

    return ApiResponse(
        code=0,
        message="Order replaced successfully",
        data=ReplaceResponse(**result),
    )


@router.post("/batch-cancel", response_model=ApiResponse[BatchCancelResponse])
async def batch_cancel(
    request: BatchCancelRequest,
    current_user=Depends(require_amm_user),
    db: AsyncSession = Depends(get_db),
):
    """Batch Cancel: cancel all AMM orders in a market.

    See interface contract v1.4 §3.2.
    """
    from src.pm_matching.application.service import get_matching_engine

    engine = get_matching_engine()

    async with db.begin():
        result = await engine.batch_cancel(
            market_id=request.market_id,
            user_id=str(current_user.id),
            cancel_scope=request.cancel_scope,
            db=db,
        )

    return ApiResponse(
        code=0,
        message="Batch cancel completed",
        data=BatchCancelResponse(**result),
    )
```

**Step 6: Run to verify PASS**
```bash
uv run pytest tests/unit/test_replace_logic.py -v
```

**Step 7: Commit**
```bash
git add src/pm_order/application/amm_schemas.py src/pm_order/api/amm_router.py src/pm_matching/engine/engine.py tests/unit/test_replace_logic.py
git commit -m "feat(pm_order): add Atomic Replace API for AMM"
```

---

## Task 9: Batch Cancel API

**Files:**
- Modify: `src/pm_matching/engine/engine.py` (add `batch_cancel` method)
- Test: `tests/unit/test_batch_cancel.py`

**Step 1: Write failing test**

```python
# tests/unit/test_batch_cancel.py
"""Test Batch Cancel logic. See interface contract v1.4 §3.2."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from src.pm_account.domain.constants import AMM_USER_ID


class TestBatchCancel:
    @pytest.mark.asyncio
    async def test_cancel_all_returns_count(self) -> None:
        """Cancel ALL scope should cancel all AMM orders in market."""
        from src.pm_matching.engine.engine import MatchingEngine

        engine = MatchingEngine()
        db = AsyncMock()

        # Mock: 3 active AMM orders
        mock_orders = MagicMock()
        mock_orders.fetchall.return_value = [
            MagicMock(id="o1", frozen_amount=1000, frozen_asset_type="FUNDS", original_direction="BUY"),
            MagicMock(id="o2", frozen_amount=500, frozen_asset_type="YES_SHARES", original_direction="SELL"),
            MagicMock(id="o3", frozen_amount=300, frozen_asset_type="NO_SHARES", original_direction="SELL"),
        ]
        db.execute.return_value = mock_orders

        result = await engine.batch_cancel(
            market_id="mkt-1",
            user_id=AMM_USER_ID,
            cancel_scope="ALL",
            db=db,
        )
        assert result["cancelled_count"] == 3

    @pytest.mark.asyncio
    async def test_cancel_no_active_orders(self) -> None:
        """No active orders → cancelled_count = 0, not an error."""
        from src.pm_matching.engine.engine import MatchingEngine

        engine = MatchingEngine()
        db = AsyncMock()

        mock_orders = MagicMock()
        mock_orders.fetchall.return_value = []
        db.execute.return_value = mock_orders

        result = await engine.batch_cancel(
            market_id="mkt-1",
            user_id=AMM_USER_ID,
            cancel_scope="ALL",
            db=db,
        )
        assert result["cancelled_count"] == 0

    @pytest.mark.asyncio
    async def test_cancel_buy_only_filters_correctly(self) -> None:
        """BUY_ONLY scope: cancel only BUY original_direction orders."""
        from src.pm_matching.engine.engine import MatchingEngine

        engine = MatchingEngine()
        db = AsyncMock()

        mock_orders = MagicMock()
        mock_orders.fetchall.return_value = [
            MagicMock(id="o1", frozen_amount=1000, frozen_asset_type="FUNDS", original_direction="BUY"),
            MagicMock(id="o2", frozen_amount=2000, frozen_asset_type="FUNDS", original_direction="BUY"),
        ]
        db.execute.return_value = mock_orders

        result = await engine.batch_cancel(
            market_id="mkt-1",
            user_id=AMM_USER_ID,
            cancel_scope="BUY_ONLY",
            db=db,
        )
        assert result["cancelled_count"] == 2
```

**Step 2: Run to verify FAIL**
```bash
uv run pytest tests/unit/test_batch_cancel.py -v
```

**Step 3: Add `batch_cancel` method to MatchingEngine**

```python
async def batch_cancel(
    self, market_id: str, user_id: str, cancel_scope: str, db
) -> dict:
    """Batch cancel all AMM orders in a market.

    See interface contract v1.4 §3.2.
    cancel_scope based on original_direction (user intent):
    - ALL: cancel all
    - BUY_ONLY: cancel BUY original_direction (NATIVE_BUY + SYNTHETIC_SELL book_type)
    - SELL_ONLY: cancel SELL original_direction (NATIVE_SELL + SYNTHETIC_BUY book_type)
    """
    from sqlalchemy import text

    # Build direction filter
    direction_filter = ""
    params = {"uid": user_id, "mid": market_id}

    if cancel_scope == "BUY_ONLY":
        direction_filter = "AND original_direction = 'BUY'"
    elif cancel_scope == "SELL_ONLY":
        direction_filter = "AND original_direction = 'SELL'"

    # Get all active orders
    result = await db.execute(
        text(
            f"SELECT id, frozen_amount, frozen_asset_type, original_direction "
            f"FROM orders "
            f"WHERE user_id = :uid AND market_id = :mid "
            f"AND status IN ('OPEN', 'PARTIALLY_FILLED') "
            f"{direction_filter} "
            f"FOR UPDATE"
        ),
        params,
    )
    orders = result.fetchall()

    if not orders:
        return {
            "market_id": market_id,
            "cancelled_count": 0,
            "total_unfrozen_funds_cents": 0,
            "total_unfrozen_yes_shares": 0,
            "total_unfrozen_no_shares": 0,
        }

    total_funds = 0
    total_yes = 0
    total_no = 0

    async with self._market_locks[market_id]:
        orderbook = self._orderbooks.get(market_id)
        for order in orders:
            # Remove from orderbook memory via official cancel method.
            # CRITICAL: Do NOT manually pop from _order_index — that only removes
            # the index entry but leaves the order in the price-level deque,
            # causing "ghost order" matches or null pointer exceptions.
            # Use OrderBook.remove_order() which cleans both the index AND
            # the price-level data structure (bids/asks deque + best_bid/ask cursor).
            if orderbook is not None:
                orderbook.remove_order(order.id)

            # Track unfrozen amounts
            if order.frozen_asset_type == "FUNDS":
                total_funds += order.frozen_amount
            elif order.frozen_asset_type == "YES_SHARES":
                total_yes += order.frozen_amount
            elif order.frozen_asset_type == "NO_SHARES":
                total_no += order.frozen_amount

    # Bulk cancel in DB
    order_ids = [o.id for o in orders]
    await db.execute(
        text(
            "UPDATE orders SET status = 'CANCELLED', updated_at = NOW() "
            "WHERE id = ANY(:ids)"
        ),
        {"ids": order_ids},
    )

    # Bulk unfreeze
    if total_funds > 0:
        await db.execute(
            text(
                "UPDATE accounts SET available_balance = available_balance + :amt, "
                "frozen_balance = frozen_balance - :amt, version = version + 1 "
                "WHERE user_id = :uid"
            ),
            {"amt": total_funds, "uid": user_id},
        )
    if total_yes > 0:
        await db.execute(
            text(
                "UPDATE positions SET yes_pending_sell = yes_pending_sell - :amt "
                "WHERE user_id = :uid AND market_id = :mid"
            ),
            {"amt": total_yes, "uid": user_id, "mid": market_id},
        )
    if total_no > 0:
        await db.execute(
            text(
                "UPDATE positions SET no_pending_sell = no_pending_sell - :amt "
                "WHERE user_id = :uid AND market_id = :mid"
            ),
            {"amt": total_no, "uid": user_id, "mid": market_id},
        )

    # Write ORDER_UNFREEZE ledger entries for each cancelled order.
    # CRITICAL: Per API contract v1.1 §3.2, each cancelled order must produce
    # an ORDER_UNFREEZE ledger entry. Without these, the global zero-sum
    # invariant check will fail and violate the append-only audit principle.
    # NOTE: ledger_entries.id is BIGSERIAL — omit id field.
    for order in orders:
        await db.execute(
            text(
                "INSERT INTO ledger_entries "
                "(user_id, entry_type, amount_cents, reference_type, reference_id, created_at) "
                "VALUES (:uid, 'ORDER_UNFREEZE', :amt, 'ORDER', :ref_id, NOW())"
            ),
            {
                "uid": user_id,
                "amt": order.frozen_amount,
                "ref_id": str(order.id),
            },
        )

    return {
        "market_id": market_id,
        "cancelled_count": len(orders),
        "total_unfrozen_funds_cents": total_funds,
        "total_unfrozen_yes_shares": total_yes,
        "total_unfrozen_no_shares": total_no,
    }
```

**Step 4: Run to verify PASS**
```bash
uv run pytest tests/unit/test_batch_cancel.py -v
```

**Step 5: Commit**
```bash
git add src/pm_matching/engine/engine.py tests/unit/test_batch_cancel.py
git commit -m "feat(pm_order): add Batch Cancel API for AMM"
```

---

## Task 10: Register AMM Routes in main.py

**Files:**
- Modify: `src/main.py`

**Step 1: Add AMM routers**

```python
# In src/main.py — add imports:
from src.pm_clearing.api.amm_router import router as amm_clearing_router
from src.pm_order.api.amm_router import router as amm_order_router

# Add router registrations (after existing include_router calls):
app.include_router(amm_clearing_router, prefix="/api/v1/amm")
app.include_router(amm_order_router, prefix="/api/v1/amm/orders")
```

**Step 2: Verify startup**
```bash
uv run python -c "from src.main import app; print('Routes:', [r.path for r in app.routes])"
```

Expected output should include:
- `/api/v1/amm/mint`
- `/api/v1/amm/burn`
- `/api/v1/amm/orders/replace`
- `/api/v1/amm/orders/batch-cancel`

**Step 3: Commit**
```bash
git add src/main.py
git commit -m "feat(main): register AMM API routes under /api/v1/amm/"
```

---

## Task 11: Integration Tests

**Files:**
- Create: `tests/integration/test_amm_mint_api.py`
- Create: `tests/integration/test_amm_burn_api.py`
- Create: `tests/integration/test_amm_replace_api.py`
- Create: `tests/integration/test_amm_netting_bypass.py`

**Step 1: Write Mint integration test**

```python
# tests/integration/test_amm_mint_api.py
"""End-to-end test for Privileged Mint API."""
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from src.pm_account.domain.constants import AMM_USER_ID


@pytest.mark.asyncio
class TestMintAPIIntegration:
    async def test_mint_creates_shares_and_deducts_balance(
        self, client: AsyncClient, amm_auth_headers: dict, db_session, active_market_id: str,
    ) -> None:
        """Full mint flow: balance deduction, position creation, ledger entries."""
        # Pre-fund AMM account
        await db_session.execute(
            text("UPDATE accounts SET available_balance = 500000 WHERE user_id = :uid"),
            {"uid": AMM_USER_ID},
        )
        await db_session.commit()

        resp = await client.post(
            "/api/v1/amm/mint",
            json={
                "market_id": active_market_id,
                "quantity": 100,
                "idempotency_key": "test-mint-001",
            },
            headers=amm_auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["minted_quantity"] == 100
        assert data["cost_cents"] == 10000  # 100 × 100
        assert data["remaining_balance_cents"] == 490000

        # Verify DB: positions exist
        pos = await db_session.execute(
            text(
                "SELECT yes_volume, no_volume FROM positions "
                "WHERE user_id = :uid AND market_id = :mid"
            ),
            {"uid": AMM_USER_ID, "mid": active_market_id},
        )
        pos_row = pos.fetchone()
        assert pos_row.yes_volume == 100
        assert pos_row.no_volume == 100

        # Verify DB: ledger entries exist
        ledger = await db_session.execute(
            text(
                "SELECT COUNT(*) FROM ledger_entries "
                "WHERE reference_type = 'AMM_MINT' AND reference_id = 'test-mint-001'"
            ),
        )
        assert ledger.scalar() == 2  # MINT_COST + MINT_RESERVE_IN

    async def test_mint_idempotent(
        self, client: AsyncClient, amm_auth_headers: dict, db_session, active_market_id: str,
    ) -> None:
        """Second call with same idempotency_key returns 200 without re-executing."""
        await db_session.execute(
            text("UPDATE accounts SET available_balance = 500000 WHERE user_id = :uid"),
            {"uid": AMM_USER_ID},
        )
        await db_session.commit()

        # First call
        await client.post(
            "/api/v1/amm/mint",
            json={"market_id": active_market_id, "quantity": 100, "idempotency_key": "idem-001"},
            headers=amm_auth_headers,
        )
        # Second call
        resp = await client.post(
            "/api/v1/amm/mint",
            json={"market_id": active_market_id, "quantity": 100, "idempotency_key": "idem-001"},
            headers=amm_auth_headers,
        )
        assert resp.status_code == 200  # idempotent hit

    async def test_mint_insufficient_balance(
        self, client: AsyncClient, amm_auth_headers: dict, active_market_id: str,
    ) -> None:
        """Mint with insufficient balance returns 422/2001."""
        resp = await client.post(
            "/api/v1/amm/mint",
            json={"market_id": active_market_id, "quantity": 999999, "idempotency_key": "fail-001"},
            headers=amm_auth_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == 2001

    async def test_mint_non_amm_user_forbidden(
        self, client: AsyncClient, normal_auth_headers: dict, active_market_id: str,
    ) -> None:
        """Non-AMM user calling mint should get 403."""
        resp = await client.post(
            "/api/v1/amm/mint",
            json={"market_id": active_market_id, "quantity": 100, "idempotency_key": "normal-001"},
            headers=normal_auth_headers,
        )
        assert resp.status_code == 403
```

**Step 2: Write Burn integration test** (similar pattern, in `test_amm_burn_api.py`)

**Step 3: Write Replace integration test** (in `test_amm_replace_api.py`)

**Step 4: Write Netting bypass integration test**

```python
# tests/integration/test_amm_netting_bypass.py
"""Verify AMM trades do NOT trigger auto-netting."""
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from src.pm_account.domain.constants import AMM_USER_ID


@pytest.mark.asyncio
class TestNettingBypassIntegration:
    async def test_amm_trade_preserves_dual_inventory(
        self, client: AsyncClient, amm_auth_headers: dict, db_session, active_market_id: str,
    ) -> None:
        """After AMM trade, both YES and NO positions should remain intact.

        Setup: AMM holds 100 YES + 100 NO.
        Action: A normal user buys YES from AMM (TRANSFER_YES scenario).
        Verify: AMM still has NO position (netting did NOT destroy it).
        """
        # Setup: Mint shares for AMM
        await db_session.execute(
            text("UPDATE accounts SET available_balance = 500000 WHERE user_id = :uid"),
            {"uid": AMM_USER_ID},
        )
        await db_session.commit()

        # Mint 100 pairs
        await client.post(
            "/api/v1/amm/mint",
            json={"market_id": active_market_id, "quantity": 100, "idempotency_key": "netting-test-mint"},
            headers=amm_auth_headers,
        )

        # AMM places sell YES order at 60
        await client.post(
            "/api/v1/orders",
            json={
                "client_order_id": "amm_sell_yes",
                "market_id": active_market_id,
                "side": "YES",
                "direction": "SELL",
                "price_cents": 60,
                "quantity": 10,
                "time_in_force": "GTC",
            },
            headers=amm_auth_headers,
        )

        # Normal user buys YES at 60 (triggers TRANSFER_YES)
        # ... (normal user places buy order)

        # Verify AMM positions: NO should still be 100
        pos = await db_session.execute(
            text(
                "SELECT yes_volume, no_volume FROM positions "
                "WHERE user_id = :uid AND market_id = :mid"
            ),
            {"uid": AMM_USER_ID, "mid": active_market_id},
        )
        pos_row = pos.fetchone()
        # YES should be reduced by trade, but NO should be INTACT
        assert pos_row.no_volume == 100  # Netting did NOT reduce NO
```

**Step 5: Run all integration tests**
```bash
uv run pytest tests/integration/test_amm_*.py -v
```

**Step 6: Commit**
```bash
git add tests/integration/test_amm_*.py
git commit -m "test(integration): add AMM API end-to-end tests"
```

---

## Task 12: Final Verification

**Step 1: Run full test suite**
```bash
uv run pytest tests/ -v --tb=short
```

**Step 2: Verify no regressions in existing tests**
```bash
uv run pytest tests/unit/test_auto_netting.py tests/unit/test_matching_engine.py tests/unit/test_order_api.py -v
```

**Step 3: Verify all AMM routes are registered**
```bash
uv run python -c "
from src.main import app
amm_routes = [r.path for r in app.routes if 'amm' in r.path.lower()]
print('AMM routes:', amm_routes)
assert '/api/v1/amm/mint' in amm_routes
assert '/api/v1/amm/burn' in amm_routes
assert '/api/v1/amm/orders/replace' in amm_routes
assert '/api/v1/amm/orders/batch-cancel' in amm_routes
print('All AMM routes verified ✓')
"
```

**Step 4: Commit final**
```bash
git add -A
git commit -m "chore: Phase A AMM prerequisites complete — all tests passing"
```

---

*Implementation plan ends — 12 Tasks, ~58 test scenarios*
