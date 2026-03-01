"""Budget / P&L tracking for AMM risk management."""
import logging

from src.amm.models.inventory import Inventory

logger = logging.getLogger(__name__)


class BudgetManager:
    """Track daily P&L and per-market losses."""

    def __init__(self, initial_value_cents: int = 0) -> None:
        self._initial_value_cents = initial_value_cents
        self._realized_pnl_cents = 0

    def compute_pnl(self, current_inventory: Inventory, mid_price_cents: int) -> int:
        """Compute unrealized P&L vs initial value."""
        current_value = current_inventory.total_value_cents(mid_price_cents)
        return current_value - self._initial_value_cents

    def record_realized(self, amount_cents: int) -> None:
        self._realized_pnl_cents += amount_cents

    @property
    def realized_pnl_cents(self) -> int:
        return self._realized_pnl_cents
