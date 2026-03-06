# R2D Lifecycle Cache Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the remaining lifecycle/cache correctness gaps by preserving allocated cash in Redis and fixing the main/cache typing issues currently reported by pyright.

**Architecture:** Keep the fix surface local to cache and main modules. Add regression tests for cache round-trips first, then make the minimal persistence and typing changes needed for runtime correctness and static analysis.

**Tech Stack:** Python, pytest, pyright, asyncio, redis/fakeredis

---

### Task 1: Cache regression tests

**Files:**
- Modify: `tests/unit/amm/test_r2d_lifecycle_cache.py`
- Modify: `tests/unit/amm/test_main.py`

**Step 1:** Add a failing test proving `allocated_cash_cents` survives an `InventoryCache.set()/get()` round-trip.
**Step 2:** Run `uv run pytest tests/unit/amm/test_r2d_lifecycle_cache.py -q` and confirm failure.

### Task 2: Minimal implementation

**Files:**
- Modify: `src/amm/cache/inventory_cache.py`
- Modify: `src/amm/cache/protocols.py`
- Modify: `src/amm/main.py`

**Step 1:** Persist and hydrate `allocated_cash_cents` in `InventoryCache`.
**Step 2:** Expand the Redis protocol for methods actually awaited by cache/config code.
**Step 3:** Make `main.py` use correct `AMMReconciler` arguments, bind oracle config safely, and loosen oracle typing to match runtime/test doubles.

### Task 3: Verification

**Files:**
- No new files

**Step 1:** Run targeted tests and pyright.
**Step 2:** Run full test suite.
**Step 3:** Only then stage, commit, and push.
