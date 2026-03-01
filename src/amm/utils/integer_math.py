"""Integer-safe mathematical operations for AMM.

All financial calculations MUST use integer arithmetic to avoid
floating-point precision issues. Prices in cents [1, 99].
"""


def ceiling_div(numerator: int, denominator: int) -> int:
    """Integer ceiling division: ⌈a/b⌉. Always rounds UP."""
    if numerator == 0:
        return 0
    return (numerator + denominator - 1) // denominator


def calculate_fee(trade_value_cents: int, fee_bps: int) -> int:
    """Calculate fee with ceiling rounding. Formula: ⌈value × bps / 10000⌉."""
    if trade_value_cents == 0:
        return 0
    return (trade_value_cents * fee_bps + 9999) // 10000


def clamp(value: int, minimum: int, maximum: int) -> int:
    """Clamp value to [minimum, maximum] range."""
    return max(minimum, min(value, maximum))
