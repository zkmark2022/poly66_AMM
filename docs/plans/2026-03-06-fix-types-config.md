# Fix Types Config Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate the Redis typing errors, make Redis config overlays safe for bool and optional fields, and wire reconciler and health background tasks into `amm_main()`.

**Architecture:** Add a narrow async Redis protocol for type-checking, shift config overlay parsing to dataclass field metadata instead of runtime instance types, and treat reconciler and health as explicit background tasks managed by `amm_main()` alongside market tasks. Keep runtime behavior local to cache/config/main so `strategy/`, `risk/`, and `MarketContext` remain untouched.

**Tech Stack:** Python, pytest, pyright, ruff, asyncio, FastAPI/uvicorn, redis/fakeredis

---

### Task 1: Config Overlay Coercion

**Files:**
- Modify: `src/amm/config/loader.py`
- Test: `tests/unit/amm/test_config_loader.py`

**Step 1: Write the failing test**

Add tests that load Redis overlay values and assert:
- `"false"` becomes `False`
- `"true"` becomes `True`
- optional numeric fields accept string input such as `"12.5"`

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/amm/test_config_loader.py -v`
Expected: FAIL because current coercion uses `type(getattr(...))(val)`.

**Step 3: Write minimal implementation**

Add a field-aware coercion helper driven by `dataclasses.fields()` that unwraps `Optional[T]`, special-cases booleans, and otherwise calls the target type.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/amm/test_config_loader.py -v`
Expected: PASS

### Task 2: Redis Typing Protocol

**Files:**
- Create: `src/amm/cache/protocols.py`
- Modify: `src/amm/cache/order_cache.py`
- Modify: `src/amm/cache/inventory_cache.py`
- Modify: `src/amm/config/loader.py`

**Step 1: Write the failing check**

Run pyright on the affected modules to capture the current awaitability errors.

**Step 2: Verify it fails**

Run: `uv run pyright src/amm/cache src/amm/config/loader.py`
Expected: FAIL with async Redis method awaitability errors.

**Step 3: Write minimal implementation**

Introduce `AsyncRedisLike` and update constructor annotations to use it under `TYPE_CHECKING`.

**Step 4: Run check to verify it passes**

Run: `uv run pyright src/amm/cache src/amm/config/loader.py`
Expected: PASS

### Task 3: Reconciler And Health Integration

**Files:**
- Modify: `src/amm/main.py`
- Modify: `src/amm/lifecycle/health.py`
- Test: `tests/unit/amm/test_main.py`

**Step 1: Write the failing test**

Add tests that assert:
- `amm_main()` starts a reconcile loop task that calls `reconcile()` repeatedly on the configured interval
- health state is created, readiness flips to `True` after initialization, and active market count decreases when market tasks exit

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/amm/test_main.py -v`
Expected: FAIL because background task orchestration is not implemented yet.

**Step 3: Write minimal implementation**

Create the background tasks in `amm_main()`, make the reconcile loop respect shutdown flags, start the health server task, and adjust `markets_active` on task exit.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/amm/test_main.py -v`
Expected: PASS

### Task 4: Full Verification

**Files:**
- No new files

**Step 1: Run targeted and full suites**

Run:
- `uv run pytest tests/unit/amm/test_config_loader.py tests/unit/amm/test_main.py tests/unit/amm/test_reconciler.py -v`
- `uv run pytest tests/ -v`
- `uv run ruff check src/`
- `uv run pyright src/`
- `python3 -m pytest -q`
- `npx tsc --noEmit || echo "No TypeScript"`
- `npm run build || echo "No build script"`

**Step 2: Confirm success**

Only after all checks pass, send:
- `openclaw system event --text "Done: fix-types-config complete. AsyncRedisLike Protocol, Config type-safe coercion, Reconciler integrated, Health server wired. pyright errors down." --mode now`
