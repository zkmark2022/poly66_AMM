# Layer 4 E2E Test Report

**Date**: 2026-03-08 23:28:13 UTC
**Market**: MKT-BTC-100K-2026
**Backend**: http://localhost:8000/api/v1

## Results Summary

| Test | Status | Details |
|------|--------|---------|
| E2E-01: Orderbook has AMM quotes | PASS | YES asks=4, YES bids=10, NO asks=10, NO bids=4, total_entries=28 |
| E2E-02: Trade execution | PASS | Order: BUY YES @65c x2, filled=2, trades=1 | Demo cash: 9678 → 9547 (changed=True) | AMM YES vol: 450 → 448 (changed=True) |
| E2E-03: Kill stops AMM quotes | PASS | Before kill: YES asks=4, NO asks=10 | After kill: YES asks=3, NO asks=10 | Orders reduced: True |

**Overall**: ALL PASSED

## Evidence Files

- `screenshots/e2e01_orderbook.json`
- `screenshots/e2e02_after_amm_positions.json`
- `screenshots/e2e02_after_demo_balance.json`
- `screenshots/e2e02_amm_sell_order.json`
- `screenshots/e2e02_before_amm_positions.json`
- `screenshots/e2e02_before_demo_balance.json`
- `screenshots/e2e02_order_response.json`
- `screenshots/e2e03_orderbook_after_kill.json`
- `screenshots/e2e03_orderbook_before_kill.json`

## Bugs Fixed During E2E Testing

1. **`api_client.py:get_positions`** — API is `/positions` (list all), not `/positions/{id}` (404)
2. **`api_client.py:place_order`** — Missing `client_order_id` required by backend
3. **`api_client.py:get_orderbook`** — Backend returns `yes.bids[]`/`yes.asks[]` arrays, not `best_bid`/`best_ask` scalars
4. **`initializer.py:_build_inventory`** — Balance field is `available_balance_cents`, not `balance_cents`; cost sum field is `yes_cost_sum`, not `yes_cost_sum_cents`
5. **`reconciler.py`** — Same field name mismatches as initializer
6. **`main.py`** — `AMMReconciler(cache=...)` should be `AMMReconciler(inventory_cache=...)`
7. **`order_manager.py`** — Error logging didn't include response body for debugging

## Test Details

### E2E-01: Orderbook has AMM quotes
**Status**: PASS
**Details**: YES asks=4, YES bids=10, NO asks=10, NO bids=4, total_entries=28
```json
{
  "orderbook_summary": "YES asks=4, YES bids=10, NO asks=10, NO bids=4, total_entries=28",
  "has_sell_orders": true
}
```

### E2E-02: Trade execution
**Status**: PASS
**Details**: Order: BUY YES @65c x2, filled=2, trades=1 | Demo cash: 9678 → 9547 (changed=True) | AMM YES vol: 450 → 448 (changed=True)
```json
{
  "demo_cash_before": 9678,
  "demo_cash_after": 9547,
  "amm_yes_before": 450,
  "amm_yes_after": 448,
  "filled_quantity": 2,
  "trades_count": 1
}
```

### E2E-03: Kill stops AMM quotes
**Status**: PASS
**Details**: Before kill: YES asks=4, NO asks=10 | After kill: YES asks=3, NO asks=10 | Orders reduced: True
```json
{
  "before_yes_asks": 4,
  "before_no_asks": 10,
  "after_yes_asks": 3,
  "after_no_asks": 10
}
```
