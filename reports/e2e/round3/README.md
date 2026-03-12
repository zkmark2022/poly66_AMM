# Layer 4 E2E Round 3 — Evidence Summary

**Date:** 2026-03-12
**Agent:** Claude Sonnet 4.6 (subagent)

## Test Results

### A02 — Multi-ACTIVE Market Visibility: ✅ PASS

Both BTC + FED markets had double-sided AMM orderbook quotes simultaneously.

**Fix applied:** AMM bot `order_manager.py` was missing `client_order_id` in order payloads
(backend schema updated to require this field). Added UUID-based generation in `_place_intent`
and `_atomic_replace`.

Evidence:
- `A02-fed-orderbook.json` — FED: YES asks @60¢, NO bids @40¢
- `A02-btc-orderbook.json` — BTC: YES bids + YES asks visible at multiple price levels
- `A02-amm-state.json` — AMM health state: 2 markets ACTIVE, EXPLORATION phase

### A03 — AMM Stop Correctly Reflected: ✅ PASS

After `kill $AMM_PID`:
- FED market: all AMM orders disappeared (0 YES asks, 0 NO bids)
- BTC market: AMM YES asks @61¢ and @62¢ disappeared
- AMM health server (port 8001): not responding — expected
- AMM OPEN orders count: 0 (confirmed via /orders?status=OPEN)
- CANCELLED orders confirmed: YES SELL@62¢, YES SELL@61¢

Evidence:
- `A03-pre-stop-fed-orderbook.json` / `A03-pre-stop-btc-orderbook.json`
- `A03-post-stop-fed-orderbook.json` / `A03-post-stop-btc-orderbook.json`
- `A03-post-stop-state.json` — health server offline

### F01 — Strict Concurrent Order Placement: ✅ PASS

Two users (demo + demo2) placed concurrent BUY YES orders via `asyncio.gather`:
- Submission elapsed: 10ms (true concurrency)
- demo: HTTP 201, order OPEN, balance correctly frozen
- demo2: HTTP 201, order OPEN, balance correctly frozen
- No race conditions, deadlocks, or 5xx errors
- Both balances updated correctly post-order

Evidence:
- `F01-concurrent-evidence.json` — full before/after balances, positions, order responses

### BUG-006 — Post-Order Navigation: ✅ CANNOT_REPRODUCE

Tested on both FED and BTC markets:
- Placed order on FED → stayed on `/markets/MKT-FED-RATE-CUT-2026Q2` ✅
- Placed order on BTC → stayed on `/markets/MKT-BTC-100K-2026` ✅
- No wrong-market navigation observed in 2 independent tests

Evidence:
- `BUG006-fed-order-post.png`
- `BUG006-btc-order-post.png`

## Infrastructure Notes

- Backend: uvicorn (http://localhost:8000)
- Frontend: Next.js (http://localhost:3000)
- AMM Bot: started with `AMM_MARKETS=MKT-FED-RATE-CUT-2026Q2,MKT-BTC-100K-2026`
- AMM fix: `client_order_id` added to `place_order` and `replace_order` calls
