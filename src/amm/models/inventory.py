"""AMM inventory model. All values in integer cents/shares."""
from dataclasses import dataclass


@dataclass
class Inventory:
    cash_cents: int
    yes_volume: int
    no_volume: int
    yes_cost_sum_cents: int
    no_cost_sum_cents: int
    yes_pending_sell: int
    no_pending_sell: int
    frozen_balance_cents: int

    @property
    def yes_available(self) -> int:
        return self.yes_volume - self.yes_pending_sell

    @property
    def no_available(self) -> int:
        return self.no_volume - self.no_pending_sell

    @property
    def inventory_skew(self) -> float:
        """q = (yes - no) / (yes + no). Range [-1, 1]."""
        total = self.yes_volume + self.no_volume
        if total == 0:
            return 0.0
        return (self.yes_volume - self.no_volume) / total

    def total_value_cents(self, mid_price_cents: int) -> int:
        """Total portfolio value in cents."""
        yes_value = self.yes_volume * mid_price_cents
        no_value = self.no_volume * (100 - mid_price_cents)
        return self.cash_cents + yes_value + no_value + self.frozen_balance_cents
