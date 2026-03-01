"""Tests for Inventory and MarketContext data models."""
import pytest
from src.amm.models.inventory import Inventory


class TestInventory:
    def test_yes_available(self) -> None:
        inv = Inventory(
            cash_cents=100000,
            yes_volume=500,
            no_volume=500,
            yes_cost_sum_cents=25000,
            no_cost_sum_cents=25000,
            yes_pending_sell=100,
            no_pending_sell=50,
            frozen_balance_cents=0,
        )
        assert inv.yes_available == 400

    def test_no_available(self) -> None:
        inv = Inventory(
            cash_cents=100000,
            yes_volume=500,
            no_volume=500,
            yes_cost_sum_cents=25000,
            no_cost_sum_cents=25000,
            yes_pending_sell=100,
            no_pending_sell=50,
            frozen_balance_cents=0,
        )
        assert inv.no_available == 450

    def test_inventory_skew_balanced(self) -> None:
        inv = Inventory(
            cash_cents=100000,
            yes_volume=500,
            no_volume=500,
            yes_cost_sum_cents=25000,
            no_cost_sum_cents=25000,
            yes_pending_sell=0,
            no_pending_sell=0,
            frozen_balance_cents=0,
        )
        assert inv.inventory_skew == 0.0

    def test_inventory_skew_positive(self) -> None:
        inv = Inventory(
            cash_cents=100000,
            yes_volume=800,
            no_volume=200,
            yes_cost_sum_cents=40000,
            no_cost_sum_cents=10000,
            yes_pending_sell=0,
            no_pending_sell=0,
            frozen_balance_cents=0,
        )
        assert inv.inventory_skew == pytest.approx(0.6)

    def test_inventory_skew_empty(self) -> None:
        inv = Inventory(
            cash_cents=100000,
            yes_volume=0,
            no_volume=0,
            yes_cost_sum_cents=0,
            no_cost_sum_cents=0,
            yes_pending_sell=0,
            no_pending_sell=0,
            frozen_balance_cents=0,
        )
        assert inv.inventory_skew == 0.0

    def test_total_value_cents(self) -> None:
        """Total portfolio value = cash + yes_volume × mid + no_volume × (100 - mid)."""
        inv = Inventory(
            cash_cents=100000,
            yes_volume=100,
            no_volume=100,
            yes_cost_sum_cents=5000,
            no_cost_sum_cents=5000,
            yes_pending_sell=0,
            no_pending_sell=0,
            frozen_balance_cents=0,
        )
        # At mid_price=50: 100000 + 100*50 + 100*50 = 110000
        assert inv.total_value_cents(mid_price_cents=50) == 110000
