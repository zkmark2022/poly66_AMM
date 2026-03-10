"""
Layer 4 E2E — Round 2 Second-Market (FED) Validation.

Scenarios: A01, B01, B02, B03, B04, D02, D03, F01
Target: MKT-FED-RATE-CUT-2026Q2

The AMM bot only runs on BTC market. For FED market E2E, we seed
counterparty liquidity via the amm_market_maker account to test the
matching engine and user-visible trade flows on a second market.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_URL = os.environ.get("E2E_BACKEND_URL", "http://localhost:8000/api/v1")
MARKET_ID = "MKT-FED-RATE-CUT-2026Q2"
EVIDENCE_DIR = Path(__file__).resolve().parent.parent.parent / "reports" / "e2e" / "evidence" / "fed-round2"
AMM_PASSWORD = os.environ.get("AMM_PASSWORD", "otcG8Vdoi4AWd1ba6tGg1yOFrE91uWDLK6GMdkUzBzc")


@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    passed: bool
    details: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    bug_notes: str = ""


def api(
    path: str,
    token: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:800]
        return {"error": e.code, "detail": detail}
    except Exception as e:
        return {"error": str(e)}


def login(username: str, password: str) -> str:
    data = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/auth/login",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=10)
    result = json.loads(resp.read())
    return result["data"]["access_token"]


def save_evidence(name: str, data: Any) -> str:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return str(path)


def get_balance(token: str) -> dict[str, Any]:
    return api("/account/balance", token).get("data", {})


def get_positions(token: str) -> dict[str, Any]:
    return api("/positions", token).get("data", {})


def get_fed_position(token: str) -> dict[str, int]:
    positions = get_positions(token)
    for item in positions.get("items", []):
        if item.get("market_id") == MARKET_ID:
            return {
                "yes_volume": item.get("yes_volume", 0),
                "no_volume": item.get("no_volume", 0),
                "yes_cost_sum": item.get("yes_cost_sum", 0),
                "no_cost_sum": item.get("no_cost_sum", 0),
            }
    return {"yes_volume": 0, "no_volume": 0, "yes_cost_sum": 0, "no_cost_sum": 0}


def get_orderbook(token: str) -> dict[str, Any]:
    return api(f"/markets/{MARKET_ID}/orderbook", token).get("data", {})


def place_order(
    token: str,
    side: str,
    direction: str,
    price_cents: int,
    quantity: int,
    time_in_force: str = "GTC",
) -> dict[str, Any]:
    return api(
        "/orders",
        token,
        method="POST",
        data={
            "market_id": MARKET_ID,
            "side": side,
            "direction": direction,
            "price_cents": price_cents,
            "quantity": quantity,
            "time_in_force": time_in_force,
            "client_order_id": f"e2e-r2-{side.lower()}-{direction.lower()}-{uuid.uuid4().hex[:8]}",
        },
    )


def cancel_order(token: str, order_id: str) -> dict[str, Any]:
    return api(f"/orders/{order_id}/cancel", token, method="POST")


def get_open_orders(token: str) -> list[dict[str, Any]]:
    resp = api("/orders?status=OPEN", token)
    return resp.get("items", resp.get("data", {}).get("items", []))


def cancel_all_fed_orders(token: str) -> int:
    orders = get_open_orders(token)
    count = 0
    for o in orders:
        if o.get("market_id") == MARKET_ID:
            cancel_order(token, o["id"])
            count += 1
    return count


def seed_amm_liquidity(amm_token: str) -> list[str]:
    """Seed AMM liquidity on FED market via BUY orders (cash-backed, no inventory needed).

    Strategy: AMM uses BUY orders which only need cash (AMM has $194K).
    - BUY YES at various prices → creates YES bids (user can sell YES into these)
    - BUY NO at various prices → creates NO bids (user can sell NO into these)
    - For user to BUY YES: AMM places BUY NO which via MINT creates effective YES asks
    - For user to BUY NO: AMM places BUY YES which via MINT creates effective NO asks

    In prediction market mechanics:
    - BUY YES @ P creates a YES bid at P AND a synthetic NO ask at (100-P)
    - BUY NO @ P creates a NO bid at P AND a synthetic YES ask at (100-P)
    Returns list of order IDs for cleanup."""
    order_ids: list[str] = []

    # BUY YES at low prices → YES bids for user SELL YES + synthetic NO asks at 100-P
    for price in [35, 40, 45]:
        resp = place_order(amm_token, "YES", "BUY", price, 5)
        oid = resp.get("order", {}).get("id", "")
        if oid:
            order_ids.append(oid)

    # BUY NO at low prices → NO bids for user SELL NO + synthetic YES asks at 100-P
    for price in [35, 40, 45]:
        resp = place_order(amm_token, "NO", "BUY", price, 5)
        oid = resp.get("order", {}).get("id", "")
        if oid:
            order_ids.append(oid)

    # Also place SELL YES/NO with available inventory if any
    amm_pos = get_fed_position(amm_token)
    if amm_pos["yes_volume"] > 0:
        resp = place_order(amm_token, "YES", "SELL", 55, min(3, amm_pos["yes_volume"]))
        oid = resp.get("order", {}).get("id", "")
        if oid:
            order_ids.append(oid)
    if amm_pos["no_volume"] > 0:
        resp = place_order(amm_token, "NO", "SELL", 55, min(3, amm_pos["no_volume"]))
        oid = resp.get("order", {}).get("id", "")
        if oid:
            order_ids.append(oid)

    return order_ids


# ────────────────────────────────────────────────────────────────
# Scenarios
# ────────────────────────────────────────────────────────────────


def scenario_a01(token: str) -> ScenarioResult:
    """A01: Orderbook visible and numerically correct."""
    ob = get_orderbook(token)
    save_evidence("A01-orderbook.json", ob)

    yes_bids = ob.get("yes", {}).get("bids", [])
    yes_asks = ob.get("yes", {}).get("asks", [])
    no_bids = ob.get("no", {}).get("bids", [])
    no_asks = ob.get("no", {}).get("asks", [])

    total = len(yes_bids) + len(yes_asks) + len(no_bids) + len(no_asks)
    has_both_sides = (len(yes_asks) > 0 or len(no_asks) > 0) and (len(yes_bids) > 0 or len(no_bids) > 0)

    all_prices: list[int] = []
    for level in yes_bids + yes_asks + no_bids + no_asks:
        all_prices.append(level.get("price_cents", 0))
    prices_valid = all(1 <= p <= 99 for p in all_prices) if all_prices else False

    all_qtys = [lv.get("total_quantity", 0) for lv in yes_bids + yes_asks + no_bids + no_asks]
    qtys_valid = all(q > 0 for q in all_qtys) if all_qtys else False

    yes_bid_prices = [lv["price_cents"] for lv in yes_bids]
    yes_ask_prices = [lv["price_cents"] for lv in yes_asks]
    bids_sorted = yes_bid_prices == sorted(yes_bid_prices, reverse=True)
    asks_sorted = yes_ask_prices == sorted(yes_ask_prices)

    passed = has_both_sides and prices_valid and qtys_valid and total >= 4
    details = (
        f"YES bids={len(yes_bids)} asks={len(yes_asks)}, "
        f"NO bids={len(no_bids)} asks={len(no_asks)}, "
        f"total={total}, prices_valid={prices_valid}, qtys_valid={qtys_valid}, "
        f"bids_sorted={bids_sorted}, asks_sorted={asks_sorted}"
    )

    return ScenarioResult(
        scenario_id="A01",
        name="Orderbook visible and numerically correct",
        passed=passed,
        details=details,
        evidence={
            "yes_bids": yes_bids, "yes_asks": yes_asks,
            "no_bids": no_bids, "no_asks": no_asks,
            "prices_valid": prices_valid, "qtys_valid": qtys_valid,
            "has_both_sides": has_both_sides,
        },
    )


def scenario_b01(token: str) -> ScenarioResult:
    """B01: BUY YES — user buys YES shares."""
    bal_before = get_balance(token)
    pos_before = get_fed_position(token)
    ob_before = get_orderbook(token)
    save_evidence("B01-before.json", {"balance": bal_before, "position": pos_before, "orderbook": ob_before})

    yes_asks = ob_before.get("yes", {}).get("asks", [])
    if not yes_asks:
        return ScenarioResult("B01", "BUY YES", False, "No YES asks in orderbook")

    best_ask = yes_asks[0]["price_cents"]
    qty = 1

    resp = place_order(token, "YES", "BUY", best_ask, qty, time_in_force="IOC")
    save_evidence("B01-order-response.json", resp)
    time.sleep(1)

    bal_after = get_balance(token)
    pos_after = get_fed_position(token)
    ob_after = get_orderbook(token)
    save_evidence("B01-after.json", {"balance": bal_after, "position": pos_after, "orderbook": ob_after})

    order_data = resp.get("order", {})
    trades = resp.get("trades", [])
    filled = order_data.get("filled_quantity", 0)
    netting = resp.get("netting_result") or {}
    netting_qty = netting.get("netting_qty", 0)

    cash_before = bal_before.get("available_balance_cents", 0)
    cash_after = bal_after.get("available_balance_cents", 0)
    yes_before = pos_before["yes_volume"]
    yes_after = pos_after["yes_volume"]
    no_before = pos_before["no_volume"]
    no_after = pos_after["no_volume"]

    # If netting occurred (user had NO shares), YES+NO pair is redeemed for 100c.
    # In this case YES vol may not increase (netted away), but NO vol decreases.
    if netting_qty > 0:
        # Netting path: fill succeeded, opposing position reduced, cash includes refund
        no_decreased = no_after < no_before
        passed = filled > 0 and no_decreased
        netting_note = f" | NETTING: {netting_qty} pairs redeemed, NO vol: {no_before}→{no_after}"
    else:
        # Normal path: YES increases, cash decreases
        cash_decreased = cash_after < cash_before
        yes_increased = yes_after > yes_before
        passed = filled > 0 and cash_decreased and yes_increased
        netting_note = ""

    details = (
        f"BUY YES @{best_ask}c x{qty}, filled={filled}, trades={len(trades)} | "
        f"Cash: {cash_before}→{cash_after} (Δ={cash_after - cash_before}) | "
        f"YES vol: {yes_before}→{yes_after}{netting_note}"
    )

    bug = ""
    if filled == 0:
        bug = "BUG-004-retest: IOC BUY YES not filling — matching issue"

    return ScenarioResult("B01", "BUY YES", passed, details,
                          evidence={"filled": filled, "trades": trades, "cash_delta": cash_after - cash_before,
                                    "yes_delta": yes_after - yes_before, "no_delta": no_after - no_before,
                                    "netting": netting, "order": order_data},
                          bug_notes=bug)


def scenario_b02(token: str) -> ScenarioResult:
    """B02: BUY NO — user buys NO shares."""
    bal_before = get_balance(token)
    pos_before = get_fed_position(token)
    ob_before = get_orderbook(token)
    save_evidence("B02-before.json", {"balance": bal_before, "position": pos_before, "orderbook": ob_before})

    no_asks = ob_before.get("no", {}).get("asks", [])
    if not no_asks:
        return ScenarioResult("B02", "BUY NO", False, "No NO asks in orderbook")

    best_ask = no_asks[0]["price_cents"]
    qty = 1

    resp = place_order(token, "NO", "BUY", best_ask, qty, time_in_force="IOC")
    save_evidence("B02-order-response.json", resp)
    time.sleep(1)

    bal_after = get_balance(token)
    pos_after = get_fed_position(token)
    save_evidence("B02-after.json", {"balance": bal_after, "position": pos_after})

    order_data = resp.get("order", {})
    trades = resp.get("trades", [])
    filled = order_data.get("filled_quantity", 0)

    cash_before = bal_before.get("available_balance_cents", 0)
    cash_after = bal_after.get("available_balance_cents", 0)
    no_before = pos_before["no_volume"]
    no_after = pos_after["no_volume"]

    cash_decreased = cash_after < cash_before
    no_increased = no_after > no_before

    passed = filled > 0 and cash_decreased and no_increased
    details = (
        f"BUY NO @{best_ask}c x{qty}, filled={filled}, trades={len(trades)} | "
        f"Cash: {cash_before}→{cash_after} (Δ={cash_after - cash_before}) | "
        f"NO vol: {no_before}→{no_after}"
    )

    return ScenarioResult("B02", "BUY NO", passed, details,
                          evidence={"filled": filled, "trades": trades, "cash_delta": cash_after - cash_before,
                                    "no_delta": no_after - no_before, "order": order_data})


def scenario_b03(token: str, amm_token: str) -> ScenarioResult:
    """B03: SELL YES — user sells YES shares back."""
    pos_check = get_fed_position(token)
    if pos_check["yes_volume"] <= 0:
        # Need to acquire YES shares first. To avoid netting against NO,
        # buy enough YES to exhaust all NO netting plus surplus.
        no_vol = pos_check["no_volume"]
        buy_qty = no_vol + 3  # extra to ensure leftover YES after netting

        # Seed AMM counterparty: BUY NO at 40 creates synthetic YES asks at 60
        for price in [35, 40]:
            place_order(amm_token, "NO", "BUY", price, buy_qty)
        time.sleep(0.5)

        # Buy YES via IOC at 65 (high enough to match synthetic asks at 55-65)
        resp = place_order(token, "YES", "BUY", 65, buy_qty, time_in_force="IOC")
        save_evidence("B03-prep-buy-yes.json", resp)
        time.sleep(1)
        pos_check = get_fed_position(token)
        if pos_check["yes_volume"] <= 0:
            return ScenarioResult("B03", "SELL YES", False,
                                  f"Failed to acquire YES (vol={pos_check['yes_volume']}, "
                                  f"no_vol={pos_check['no_volume']})",
                                  bug_notes="Could not prepare YES position for sell test")

    pos_before = get_fed_position(token)
    bal_before = get_balance(token)
    ob_before = get_orderbook(token)
    save_evidence("B03-before.json", {"balance": bal_before, "position": pos_before, "orderbook": ob_before})

    yes_bids = ob_before.get("yes", {}).get("bids", [])
    if not yes_bids:
        return ScenarioResult("B03", "SELL YES", False, "No YES bids in orderbook")

    best_bid = yes_bids[0]["price_cents"]
    qty = min(1, pos_before["yes_volume"])

    resp = place_order(token, "YES", "SELL", best_bid, qty, time_in_force="IOC")
    save_evidence("B03-order-response.json", resp)
    time.sleep(1)

    bal_after = get_balance(token)
    pos_after = get_fed_position(token)
    save_evidence("B03-after.json", {"balance": bal_after, "position": pos_after})

    order_data = resp.get("order", {})
    trades = resp.get("trades", [])
    filled = order_data.get("filled_quantity", 0)

    cash_before = bal_before.get("available_balance_cents", 0)
    cash_after = bal_after.get("available_balance_cents", 0)
    yes_before = pos_before["yes_volume"]
    yes_after = pos_after["yes_volume"]

    cash_increased = cash_after > cash_before
    yes_decreased = yes_after < yes_before

    passed = filled > 0 and cash_increased and yes_decreased
    details = (
        f"SELL YES @{best_bid}c x{qty}, filled={filled}, trades={len(trades)} | "
        f"Cash: {cash_before}→{cash_after} (Δ={cash_after - cash_before}) | "
        f"YES vol: {yes_before}→{yes_after}"
    )

    return ScenarioResult("B03", "SELL YES", passed, details,
                          evidence={"filled": filled, "trades": trades, "cash_delta": cash_after - cash_before,
                                    "yes_delta": yes_after - yes_before, "order": order_data})


def scenario_b04(token: str, amm_token: str) -> ScenarioResult:
    """B04: SELL NO — user sells NO shares back."""
    pos_check = get_fed_position(token)
    if pos_check["no_volume"] <= 0:
        # Acquire NO shares first. Buy enough to exhaust YES netting.
        yes_vol = pos_check["yes_volume"]
        buy_qty = yes_vol + 3

        # Seed AMM: BUY YES at 40 creates synthetic NO asks at 60
        for price in [35, 40]:
            place_order(amm_token, "YES", "BUY", price, buy_qty)
        time.sleep(0.5)

        resp = place_order(token, "NO", "BUY", 65, buy_qty, time_in_force="IOC")
        save_evidence("B04-prep-buy-no.json", resp)
        time.sleep(1)
        pos_check = get_fed_position(token)
        if pos_check["no_volume"] <= 0:
            return ScenarioResult("B04", "SELL NO", False,
                                  f"Failed to acquire NO (vol={pos_check['no_volume']}, "
                                  f"yes_vol={pos_check['yes_volume']})",
                                  bug_notes="Could not prepare NO position for sell test")

    pos_before = pos_check

    bal_before = get_balance(token)
    ob_before = get_orderbook(token)
    save_evidence("B04-before.json", {"balance": bal_before, "position": pos_before, "orderbook": ob_before})

    no_bids = ob_before.get("no", {}).get("bids", [])
    if not no_bids:
        return ScenarioResult("B04", "SELL NO", False, "No NO bids in orderbook")

    best_bid = no_bids[0]["price_cents"]
    qty = min(1, pos_before["no_volume"])

    resp = place_order(token, "NO", "SELL", best_bid, qty, time_in_force="IOC")
    save_evidence("B04-order-response.json", resp)
    time.sleep(1)

    bal_after = get_balance(token)
    pos_after = get_fed_position(token)
    save_evidence("B04-after.json", {"balance": bal_after, "position": pos_after})

    order_data = resp.get("order", {})
    trades = resp.get("trades", [])
    filled = order_data.get("filled_quantity", 0)

    cash_before = bal_before.get("available_balance_cents", 0)
    cash_after = bal_after.get("available_balance_cents", 0)
    no_before = pos_before["no_volume"]
    no_after = pos_after["no_volume"]

    cash_increased = cash_after > cash_before
    no_decreased = no_after < no_before

    passed = filled > 0 and cash_increased and no_decreased
    details = (
        f"SELL NO @{best_bid}c x{qty}, filled={filled}, trades={len(trades)} | "
        f"Cash: {cash_before}→{cash_after} (Δ={cash_after - cash_before}) | "
        f"NO vol: {no_before}→{no_after}"
    )

    return ScenarioResult("B04", "SELL NO", passed, details,
                          evidence={"filled": filled, "trades": trades, "cash_delta": cash_after - cash_before,
                                    "no_delta": no_after - no_before, "order": order_data})


def scenario_d02(token: str) -> ScenarioResult:
    """D02: Cancel order — place a GTC order then cancel it."""
    bal_before = get_balance(token)

    resp = place_order(token, "YES", "BUY", 5, 1, time_in_force="GTC")
    save_evidence("D02-place-order.json", resp)

    order_data = resp.get("order", {})
    order_id = order_data.get("id", "")
    if not order_id:
        return ScenarioResult("D02", "Cancel order", False, f"Failed to place order: {resp}")

    orders = get_open_orders(token)
    found = any(o.get("id") == order_id for o in orders)
    if not found:
        return ScenarioResult("D02", "Cancel order", False, f"Order {order_id} not found in OPEN list")

    cancel_resp = cancel_order(token, order_id)
    save_evidence("D02-cancel-response.json", cancel_resp)
    time.sleep(0.5)

    orders_after = get_open_orders(token)
    still_open = any(o.get("id") == order_id for o in orders_after)

    bal_after = get_balance(token)
    frozen_released = bal_after.get("available_balance_cents", 0) >= bal_before.get("available_balance_cents", 0)

    passed = not still_open and frozen_released
    details = (
        f"Placed GTC BUY YES @5c x1 (id={order_id}) | "
        f"Cancel: still_open={still_open}, frozen_released={frozen_released}"
    )

    return ScenarioResult("D02", "Cancel order", passed, details,
                          evidence={"order_id": order_id, "cancel_resp": cancel_resp,
                                    "still_open": still_open, "frozen_released": frozen_released})


def scenario_d03(token: str) -> ScenarioResult:
    """D03: Invalid input / validation — test boundary prices and quantities."""
    results: list[dict[str, Any]] = []
    all_rejected = True

    test_cases = [
        ("price=0", {"side": "YES", "direction": "BUY", "price_cents": 0, "quantity": 1}),
        ("price=100", {"side": "YES", "direction": "BUY", "price_cents": 100, "quantity": 1}),
        ("price=101", {"side": "YES", "direction": "BUY", "price_cents": 101, "quantity": 1}),
        ("price=-1", {"side": "YES", "direction": "BUY", "price_cents": -1, "quantity": 1}),
        ("qty=0", {"side": "YES", "direction": "BUY", "price_cents": 50, "quantity": 0}),
        ("qty=-1", {"side": "YES", "direction": "BUY", "price_cents": 50, "quantity": -1}),
    ]

    for label, params in test_cases:
        params["market_id"] = MARKET_ID
        params["client_order_id"] = f"e2e-d03-{uuid.uuid4().hex[:8]}"
        resp = api("/orders", token, method="POST", data=params)
        rejected = "error" in resp or resp.get("code", 0) != 0
        results.append({"label": label, "rejected": rejected,
                        "resp_code": resp.get("error", resp.get("code")),
                        "detail": str(resp.get("detail", ""))[:200]})
        if not rejected:
            all_rejected = False

    save_evidence("D03-validation-results.json", results)

    details = " | ".join(f"{r['label']}={'REJECTED' if r['rejected'] else 'ACCEPTED(!)'}" for r in results)
    passed = all_rejected
    bug = "" if passed else "Some invalid inputs were accepted by the API"

    return ScenarioResult("D03", "Invalid input validation", passed, details,
                          evidence={"test_results": results}, bug_notes=bug)


def scenario_f01(demo_token: str, amm_token: str) -> ScenarioResult:
    """F01: Dual-lane consistency — FED orders don't affect BTC orderbook."""
    fed_ob = get_orderbook(demo_token)
    btc_ob = api("/markets/MKT-BTC-100K-2026/orderbook", demo_token).get("data", {})
    save_evidence("F01-fed-orderbook.json", fed_ob)
    save_evidence("F01-btc-orderbook.json", btc_ob)

    fed_total = (len(fed_ob.get("yes", {}).get("asks", []))
                 + len(fed_ob.get("yes", {}).get("bids", []))
                 + len(fed_ob.get("no", {}).get("asks", []))
                 + len(fed_ob.get("no", {}).get("bids", [])))
    btc_total = (len(btc_ob.get("yes", {}).get("asks", []))
                 + len(btc_ob.get("yes", {}).get("bids", []))
                 + len(btc_ob.get("no", {}).get("asks", []))
                 + len(btc_ob.get("no", {}).get("bids", [])))

    fed_has_quotes = fed_total > 0
    btc_has_quotes = btc_total > 0

    # Place a FED order and verify BTC unchanged
    btc_snapshot = api("/markets/MKT-BTC-100K-2026/orderbook", demo_token).get("data", {})
    resp = place_order(demo_token, "YES", "BUY", 5, 1, time_in_force="GTC")
    order_id = resp.get("order", {}).get("id", "")
    time.sleep(0.5)
    btc_after = api("/markets/MKT-BTC-100K-2026/orderbook", demo_token).get("data", {})
    if order_id:
        cancel_order(demo_token, order_id)

    btc_unchanged = (
        btc_snapshot.get("yes", {}).get("asks", []) == btc_after.get("yes", {}).get("asks", [])
        and btc_snapshot.get("yes", {}).get("bids", []) == btc_after.get("yes", {}).get("bids", [])
        and btc_snapshot.get("no", {}).get("asks", []) == btc_after.get("no", {}).get("asks", [])
        and btc_snapshot.get("no", {}).get("bids", []) == btc_after.get("no", {}).get("bids", [])
    )

    passed = fed_has_quotes and btc_has_quotes and btc_unchanged
    details = (
        f"FED levels={fed_total}, BTC levels={btc_total}, "
        f"BTC unchanged after FED order={btc_unchanged}"
    )

    return ScenarioResult("F01", "Dual-lane consistency", passed, details,
                          evidence={"fed_total": fed_total, "btc_total": btc_total,
                                    "btc_unchanged": btc_unchanged})


# ────────────────────────────────────────────────────────────────
# Main runner
# ────────────────────────────────────────────────────────────────


def run_all() -> list[ScenarioResult]:
    ts = datetime.now(timezone.utc)
    print("=" * 60)
    print("Layer 4 E2E Round 2 — FED Market Validation")
    print(f"Market: {MARKET_ID}")
    print(f"Backend: {BASE_URL}")
    print(f"Timestamp: {ts.isoformat()}")
    print("=" * 60)

    # Login both accounts
    print("\n[Setup] Logging in...")
    demo_token = login("demo", "Demo123!")
    amm_token = login("amm_market_maker", AMM_PASSWORD)
    print("  demo + amm_market_maker logged in")

    # Cleanup stale FED orders
    for tok, name in [(demo_token, "demo"), (amm_token, "amm")]:
        cancelled = cancel_all_fed_orders(tok)
        if cancelled:
            print(f"  Cleaned up {cancelled} stale {name} FED order(s)")
    time.sleep(0.5)

    # Seed AMM liquidity
    print("\n[Setup] Seeding AMM liquidity on FED market...")
    amm_order_ids = seed_amm_liquidity(amm_token)
    time.sleep(1)
    print(f"  Seeded {len(amm_order_ids)} AMM orders")

    results: list[ScenarioResult] = []

    # Run scenarios sequentially (B03 depends on B01 for YES position)
    scenarios: list[tuple[str, Any]] = [
        ("A01", lambda: scenario_a01(demo_token)),
        ("B01", lambda: scenario_b01(demo_token)),
        ("B02", lambda: scenario_b02(demo_token)),
        ("B03", lambda: scenario_b03(demo_token, amm_token)),
        ("B04", lambda: scenario_b04(demo_token, amm_token)),
        ("D02", lambda: scenario_d02(demo_token)),
        ("D03", lambda: scenario_d03(demo_token)),
        ("F01", lambda: scenario_f01(demo_token, amm_token)),
    ]

    for i, (sid, fn) in enumerate(scenarios, 1):
        print(f"\n[{i}/{len(scenarios)}] {sid}...")
        try:
            result = fn()
        except Exception as e:
            result = ScenarioResult(sid, sid, False, f"Exception: {e}")
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        print(f"  {status}: {result.details[:150]}")
        if result.bug_notes:
            print(f"  NOTE: {result.bug_notes}")

    # Cleanup AMM orders
    print("\n[Cleanup] Cancelling remaining AMM orders...")
    cancel_all_fed_orders(amm_token)

    # Summary
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"\n{'=' * 60}")
    print(f"Result: {passed}/{total} PASS")
    print(f"{'=' * 60}")

    # Save full results
    save_evidence("full-results.json", {
        "timestamp": ts.isoformat(),
        "market_id": MARKET_ID,
        "backend": BASE_URL,
        "summary": f"{passed}/{total} PASS",
        "results": [
            {
                "scenario_id": r.scenario_id,
                "name": r.name,
                "passed": r.passed,
                "details": r.details,
                "evidence": r.evidence,
                "bug_notes": r.bug_notes,
            }
            for r in results
        ],
    })

    return results


if __name__ == "__main__":
    results = run_all()
    all_passed = all(r.passed for r in results)
    sys.exit(0 if all_passed else 1)
