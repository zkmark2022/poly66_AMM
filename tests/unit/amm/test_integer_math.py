"""Integer-only math for AMM. No floats in financial calculations."""
from src.amm.utils.integer_math import ceiling_div, calculate_fee, clamp


class TestCeilingDiv:
    def test_exact_division(self) -> None:
        assert ceiling_div(100, 10) == 10

    def test_rounds_up(self) -> None:
        assert ceiling_div(101, 10) == 11

    def test_one(self) -> None:
        assert ceiling_div(1, 10000) == 1  # 1/10000 rounds up to 1


class TestCalculateFee:
    def test_standard_fee(self) -> None:
        # trade_value=6500, bps=20 → 6500*20/10000 = 13 → ceiling = 13
        assert calculate_fee(6500, 20) == 13

    def test_ceiling_behavior(self) -> None:
        # trade_value=100, bps=20 → 100*20=2000 → (2000+9999)//10000 = 1
        assert calculate_fee(100, 20) == 1

    def test_zero_value(self) -> None:
        assert calculate_fee(0, 20) == 0


class TestClamp:
    def test_within_range(self) -> None:
        assert clamp(50, 1, 99) == 50

    def test_below_min(self) -> None:
        assert clamp(0, 1, 99) == 1

    def test_above_max(self) -> None:
        assert clamp(100, 1, 99) == 99
