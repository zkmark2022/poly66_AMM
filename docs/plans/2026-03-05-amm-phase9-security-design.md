# AMM Phase 9 Security & Race Conditions Design

**Scope:** Implement test coverage and minimal runtime safeguards for Phase 9 cases T9.1-T9.6.

## Constraints
- Priority: P0 (T9.1, T9.3, T9.4) must pass.
- TDD: write failing tests first, then minimal implementation.
- Keep existing AMM bot architecture unchanged; patch at connector/poller/order manager boundaries.

## Approach Options
1. Integration-only black box via mocked API responses.
- Pros: Fast, stable in current repo.
- Cons: Less deep than full DB-backed E2E.

2. Add new runtime state and integration tests around it.
- Pros: Covers race/retry/idempotency behavior directly.
- Cons: Slightly increases connector complexity.

3. Rebuild around durable WAL simulation.
- Pros: Most realistic crash recovery.
- Cons: Overkill for current codebase.

## Selected Design
- Use option 2 with lightweight in-memory idempotency guards:
- `AMMApiClient.replace_order` gets retry timeout protection with request fingerprint dedupe.
- `OrderManager` stores “inflight restart-safe keys” to prevent duplicate placements in same process lifecycle path under retries.
- `TradePoller` dedupe behavior is made explicit in integration tests for duplicate trade IDs.
- Unauthorized response behavior validated against `AMMApiClient` request/refresh flow.
- Rate-limit behavior validated with repeated replace calls returning 429.
- Batch cancel UNFREEZE behavior validated by asserting response accounting and call payload semantics.

## Test Mapping
- T9.1 -> `tests/integration/test_amm_phase9_security.py`
- T9.2, T9.6 -> `tests/integration/test_amm_phase9_rate_limit.py`
- T9.3, T9.4, T9.5 -> `tests/integration/test_amm_phase9_race_conditions.py`
