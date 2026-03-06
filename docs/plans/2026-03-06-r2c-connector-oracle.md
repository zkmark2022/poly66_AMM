# R2C Connector Oracle Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate the remaining connector/oracle branch regressions by making `pyright` pass without breaking the existing test suite.

**Architecture:** Keep runtime behavior unchanged where possible and fix the type surface instead: widen protocol definitions where the runtime contract is already valid, introduce explicit oracle protocols for helper functions in `main.py`, and tighten test fixtures so static analysis matches the actual usage patterns.

**Tech Stack:** Python 3.12, pytest, pyright, ruff, uv

---

### Task 1: Redis Protocol Surface

**Files:**
- Modify: `src/amm/cache/protocols.py`
- Modify: `src/amm/cache/inventory_cache.py`

**Step 1: Write the failing test**

Run: `uv run --extra dev pyright`

Expected: failures on `AsyncRedisLike` missing `set` and incompatible fake Redis signatures.

**Step 2: Write minimal implementation**

- Expand `AsyncRedisLike` to match the methods actually consumed.
- Accept awaitable-compatible return types used by real/fake redis clients.

**Step 3: Run verification**

Run: `uv run --extra dev pyright`

Expected: Redis-related errors removed.

### Task 2: Oracle/Main Typing

**Files:**
- Modify: `src/amm/main.py`

**Step 1: Write the failing test**

Run: `uv run --extra dev pyright`

Expected: failures on oracle helper signatures, unbound `_oracle_data`, and `OrderManager` constructor mismatch.

**Step 2: Write minimal implementation**

- Introduce narrow protocols for helper functions that operate on oracle-like objects.
- Fix `OrderManager` constructor call and bind oracle config defaults explicitly.

**Step 3: Run verification**

Run: `uv run --extra dev pyright`

Expected: `main.py` errors removed.

### Task 3: Test Fixture Typing

**Files:**
- Modify: `tests/unit/amm/test_main.py`
- Modify: `tests/integration/amm/test_amm_quote_cycle.py`
- Modify: `tests/integration/test_amm_phase9_race_conditions.py`
- Modify: `tests/unit/amm/test_p0_critical_fixes.py`

**Step 1: Write the failing test**

Run: `uv run --extra dev pyright`

Expected: remaining fixture/type-construction errors in tests.

**Step 2: Write minimal implementation**

- Replace loosely typed fixtures with typed stubs/casts where needed.
- Avoid dict literals that widen to `str` and break config construction.

**Step 3: Run verification**

Run: `uv run --extra dev pyright`

Expected: test typing errors removed.

### Task 4: Full Verification

**Files:**
- No planned source changes

**Step 1: Run tests**

Run: `uv run --extra dev pytest -q`

Expected: all tests pass.

**Step 2: Run lint**

Run: `uv run --extra dev ruff check .`

Expected: clean.

**Step 3: Run types**

Run: `uv run --extra dev pyright`

Expected: zero errors.

**Step 4: Commit**

Run:

```bash
git add -A
git commit -m "fix: resolve connector oracle typing regressions"
git push origin feat/r2c-connector-oracle
```
