"""AMM-specific enumerations."""
from enum import IntEnum, StrEnum


class DefenseLevel(IntEnum):
    """Risk defense escalation levels. Order matters — higher = more restrictive."""
    NORMAL = 0
    WIDEN = 1
    ONE_SIDE = 2
    KILL_SWITCH = 3

    @property
    def is_quoting_active(self) -> bool:
        return self != DefenseLevel.KILL_SWITCH


class Phase(StrEnum):
    """AMM strategy phases."""
    EXPLORATION = "EXPLORATION"
    STABILIZATION = "STABILIZATION"


class QuoteAction(StrEnum):
    """Order intent actions from strategy layer."""
    PLACE = "PLACE"
    REPLACE = "REPLACE"
    CANCEL = "CANCEL"
    HOLD = "HOLD"
