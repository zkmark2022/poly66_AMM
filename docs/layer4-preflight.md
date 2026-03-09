# Layer 4 E2E Preflight — Operator Runbook

## Purpose

Run this preflight **before** launching any Layer 4 E2E scenario test.
It validates that the environment is ready and prevents wasted runs against
dead backends, wrong markets, or broken mint endpoints.

## Quick Start

```bash
# Minimal — one market, local defaults
uv run python -m scripts.preflight --market-id MKT-BTC-100K-2026

# With auth token for mint smoke test
uv run python -m scripts.preflight \
  --market-id MKT-BTC-100K-2026 \
  --auth-token "$AMM_TOKEN"

# Skip mint if no token available
uv run python -m scripts.preflight \
  --market-id MKT-BTC-100K-2026 \
  --skip-mint-if-no-auth

# Custom URLs
uv run python -m scripts.preflight \
  --backend-url http://staging.poly66.local:8000/api/v1 \
  --frontend-url http://staging.poly66.local:3000 \
  --market-id MKT-BTC-100K-2026 \
  --market-id MKT-ETH-5K-2026
```

## What It Checks

| # | Check              | Endpoint                         | PASS condition                 | FAIL action                        |
|---|--------------------|---------------------------------|--------------------------------|------------------------------------|
| 1 | Backend health     | `GET /health`                   | 200 OK, < 10s                 | Start backend, check port 8000     |
| 2 | Frontend entry     | `GET /app`                      | 200 OK                        | Start frontend, check port 3000    |
| 3 | Market status      | `GET /markets/{id}`             | `status == "ACTIVE"`          | Do not use SETTLED markets for E2E |
| 4 | Orderbook          | `GET /markets/{id}/orderbook`   | Has at least 1 bid or 1 ask   | SKIP if empty (AMM not yet posted) |
| 5 | Mint smoke test    | `POST /amm/mint`                | 200 OK                        | Fix mint_service.py schema drift   |

## Output

```
Check                          Status   Latency    Detail
--------------------------------------------------------------------------------
backend_health                 PASS     12.3ms     200 OK (12ms)
frontend_entry                 PASS     8.1ms      200 OK (8ms)
market_status:MKT-BTC-100K-2026 PASS              ACTIVE
orderbook:MKT-BTC-100K-2026   PASS                3 bids, 3 asks
mint_smoke                     PASS     45.2ms     200 OK (45ms)
--------------------------------------------------------------------------------
PREFLIGHT PASS (5/5)
```

Exit code: `0` = all passed, `1` = at least one FAIL, `2` = usage error.

## Status Meanings

- **PASS** — Check succeeded. Proceed.
- **FAIL** — Hard blocker. Fix before running E2E.
- **SKIP** — Not fatal (e.g., empty orderbook before AMM posts, no auth token for mint). E2E can proceed with awareness.

## Common Blockers

### Backend `/health` times out
- Backend not running: `cd poly66win-backend && uv run uvicorn src.main:app --port 8000`
- Port mismatch: verify `--backend-url`

### Market status is SETTLED
- Never run E2E against settled markets — they reject new orders
- Create a new market or use `POST /admin/markets/{id}/reactivate` if available

### Mint returns 500
- Known root cause: `mint_service.py` schema drift (`price_cents` vs `price`, missing `trade_id`/`buy_book_type`/etc.)
- Fix: align INSERT INTO trades with actual DB schema (see checklist item P0-2)

### Orderbook empty (SKIP)
- Normal before AMM has posted its first quotes
- Run AMM, wait one quote cycle, then re-run preflight

## For Agent Consumers

The preflight script returns structured exit codes suitable for CI gates:

```bash
uv run python -m scripts.preflight --market-id "$MARKET_ID" --skip-mint-if-no-auth
if [ $? -ne 0 ]; then
  echo "PREFLIGHT FAILED — aborting E2E"
  exit 1
fi
# proceed with E2E ...
```

## Running Tests

```bash
uv run pytest tests/integration/test_preflight.py -v
```
