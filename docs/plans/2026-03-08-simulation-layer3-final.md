# Layer 3 Simulation Final Suite Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Consolidate T-SIM-01~08 into one flat, reliable simulation test suite with review P0 fixes, added edge cases, and end-to-end verification evidence.

**Architecture:** Reuse the strongest parts of Agent A's lightweight simulation helpers and Group B's real-service integration paths, but centralize all shared fixtures and builders in `tests/simulation/conftest.py`. Each scenario gets one dedicated test file under `tests/simulation/`, with at least one parameterized edge-case test and explicit numeric assertions.

**Tech Stack:** Python, pytest, pytest-asyncio, respx, fakeredis, httpx

---

### Task 1: Baseline Analysis And Fixture Design

**Files:**
- Create: `docs/review/2026-03-08-agent-c-analysis.md`
- Modify: `tests/simulation/conftest.py`

**Step 1: Write the failing test**

```python
def test_simulation_fixture_surface_exists():
    from tests.simulation.conftest import make_context, compute_effective_spread
    assert callable(make_context)
    assert callable(compute_effective_spread)
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/simulation/test_sim_01_normal_cycle.py -v`
Expected: FAIL because the final flat suite does not yet exist in this branch.

**Step 3: Write minimal implementation**

Implement shared fixture/builders in `tests/simulation/conftest.py` by merging:
- bootstrap `mock_exchange` / fakeredis fixtures
- Agent A helper factories (`make_config`, `make_context`, `make_real_services`)
- small integration helpers needed by final Group B rewrites

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/simulation/test_sim_01_normal_cycle.py -v`
Expected: PASS for collection/import and helper surface.

**Step 5: Commit**

```bash
git add docs/review/2026-03-08-agent-c-analysis.md tests/simulation/conftest.py
git commit -m "test: add simulation suite analysis and shared fixtures"
```

### Task 2: Rebuild T-SIM-01/03/05 In Flat Layout

**Files:**
- Create: `tests/simulation/test_sim_01_normal_cycle.py`
- Create: `tests/simulation/test_sim_03_widen_defense.py`
- Create: `tests/simulation/test_sim_05_tau_zero.py`

**Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_balanced_cycle_places_both_sides():
    ...
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/simulation/test_sim_01_normal_cycle.py::test_balanced_cycle_places_both_sides -v`
Expected: FAIL before the new flat tests are created.

**Step 3: Write minimal implementation**

Create the flat tests by adapting Agent A's scenarios and fixing review gaps:
- tighter price assertions
- threshold-boundary parameterization for WIDEN
- tau edge coverage with explicit spread and mid-point assertions

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/simulation/test_sim_01_normal_cycle.py tests/simulation/test_sim_03_widen_defense.py tests/simulation/test_sim_05_tau_zero.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/simulation/test_sim_01_normal_cycle.py tests/simulation/test_sim_03_widen_defense.py tests/simulation/test_sim_05_tau_zero.py
git commit -m "test: flatten steady-state simulation scenarios"
```

### Task 3: Rewrite T-SIM-02/04/06/07/08 With P0 Fixes

**Files:**
- Create: `tests/simulation/test_sim_02_oracle_deadlock.py`
- Create: `tests/simulation/test_sim_04_kill_stops_quotes.py`
- Create: `tests/simulation/test_sim_06_startup_integrity.py`
- Create: `tests/simulation/test_sim_07_restart_recovery.py`
- Create: `tests/simulation/test_sim_08_oracle_lag_recovery.py`

**Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_kill_switch_cancels_existing_orders_and_freezes_new_placements():
    ...
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/simulation/test_sim_04_kill_stops_quotes.py::test_kill_switch_cancels_existing_orders_and_freezes_new_placements -v`
Expected: FAIL before the rewritten flat tests exist.

**Step 3: Write minimal implementation**

Implement scenario files with:
- real business assertions instead of "no exception"
- pre-existing order setup for KILL
- `reconcile_loop`-based startup verification
- shared-risk cooldown checks for oracle recovery
- restart assertions that verify both exact Redis state and post-restart quoting continuity

**Step 4: Run test to verify it passes**

Run: `python -m pytest tests/simulation/test_sim_02_oracle_deadlock.py tests/simulation/test_sim_04_kill_stops_quotes.py tests/simulation/test_sim_06_startup_integrity.py tests/simulation/test_sim_07_restart_recovery.py tests/simulation/test_sim_08_oracle_lag_recovery.py -v`
Expected: PASS or explicit `xfail` only for confirmed source bugs.

**Step 5: Commit**

```bash
git add tests/simulation/test_sim_02_oracle_deadlock.py tests/simulation/test_sim_04_kill_stops_quotes.py tests/simulation/test_sim_06_startup_integrity.py tests/simulation/test_sim_07_restart_recovery.py tests/simulation/test_sim_08_oracle_lag_recovery.py
git commit -m "test: add final risk and recovery simulation scenarios"
```

### Task 4: Full Verification And Coverage

**Files:**
- Modify: `docs/review/2026-03-08-agent-c-analysis.md`

**Step 1: Write the failing test**

```python
def test_full_suite_expected_to_be_green():
    assert False
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/simulation/ -v --tb=short`
Expected: FAIL on any unresolved scenario or collection issue.

**Step 3: Write minimal implementation**

Fix only the failing tests or mark confirmed source defects with `@pytest.mark.xfail(reason="known bug: ...")`. Update analysis notes with any newly confirmed source bugs.

**Step 4: Run test to verify it passes**

Run:
- `python -m pytest tests/simulation/ -v --tb=short`
- `python -m pytest tests/simulation/ --cov=src/amm --cov-report=term-missing -q`
- `python3 -m pytest -q`
- `npx tsc --noEmit`
- `npm run build`

Expected:
- Simulation suite: `X passed, 0 failed, 0 error`
- Repository checks either pass or explicitly report no matching frontend/build setup

**Step 5: Commit**

```bash
git add docs/review/2026-03-08-agent-c-analysis.md tests/simulation/
git commit -m "test: verify final simulation suite"
```
