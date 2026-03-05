# AMM Phase 9 Security & Race Conditions Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add Phase 9 security/race-condition integration tests and minimal code changes so P0 tests pass and P1 tests are covered.

**Architecture:** Extend `AMMApiClient` and `OrderManager` with minimal idempotency/retry guards, and verify behaviors via integration tests using existing `respx` fixtures. Keep modifications localized to connector and poller layers.

**Tech Stack:** Python 3, pytest, pytest-asyncio, httpx, respx, fakeredis.

---

### Task 1: Create security auth rejection test (T9.1)

**Files:**
- Create: `tests/integration/test_amm_phase9_security.py`
- Modify: `tests/integration/amm/conftest.py`

**Step 1: Write failing test**
- Add test asserting normal-user/invalid JWT leads to `401/403` and raises `httpx.HTTPStatusError`.

**Step 2: Run test to verify RED**
- Run: `pytest -q tests/integration/test_amm_phase9_security.py -k unauthorized`

**Step 3: Minimal implementation**
- Ensure request path does not mask `403`, and 401 refresh fallback still fails with proper error when unauthorized.

**Step 4: Run test to verify GREEN**
- Run same command and ensure pass.

### Task 2: Create rate-limit + batch cancel tests (T9.2, T9.6)

**Files:**
- Create: `tests/integration/test_amm_phase9_rate_limit.py`
- Modify: `src/amm/connector/api_client.py`

**Step 1: Write failing tests**
- T9.2: repeated replace under 429 returns status error after retry ceiling.
- T9.6: batch cancel result with `cancelled_count=N` is asserted and treated as N unfreeze events contract-wise.

**Step 2: RED verify**
- Run: `pytest -q tests/integration/test_amm_phase9_rate_limit.py`

**Step 3: Minimal implementation**
- Add deterministic retry behavior and no hidden swallow of 429.

**Step 4: GREEN verify**
- Run same command.

### Task 3: Create race/idempotency tests (T9.3, T9.4, T9.5)

**Files:**
- Create: `tests/integration/test_amm_phase9_race_conditions.py`
- Modify: `src/amm/connector/api_client.py`
- Modify: `src/amm/connector/order_manager.py`
- Modify: `src/amm/connector/trade_poller.py` (if needed)

**Step 1: Write failing tests**
- T9.3: replace timeout + retry does not produce duplicate orders.
- T9.4: restart/recovery simulation avoids duplicate order placement.
- T9.5: duplicate trade IDs processed once.

**Step 2: RED verify**
- Run: `pytest -q tests/integration/test_amm_phase9_race_conditions.py`

**Step 3: Minimal implementation**
- Add replace request fingerprint dedupe cache in client.
- Add order manager idempotency keying for repeated place intents.
- Keep trade poller processed-id guard behavior explicit.

**Step 4: GREEN verify**
- Run same command.

### Task 4: Full verification and delivery checks

**Files:**
- No code target; verification only.

**Step 1: Run project-required checks**
- `cd backend 2>/dev/null || cd .`
- `pip install -r requirements.txt 2>/dev/null || true`
- `python3 -m pytest -q 2>/dev/null || echo "No tests"`
- `cd frontend 2>/dev/null || cd .`
- `npm install 2>/dev/null || true`
- `npx tsc --noEmit 2>/dev/null || echo "No TypeScript"`
- `npm run build 2>/dev/null || echo "No build script"`

**Step 2: Run focused tests**
- `pytest -q tests/integration/test_amm_phase9_security.py tests/integration/test_amm_phase9_rate_limit.py tests/integration/test_amm_phase9_race_conditions.py`

**Step 3: Prepare commit**
- Commit message can follow: `test: Phase 9 - Security & Race Conditions`

