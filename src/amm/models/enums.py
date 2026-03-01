"""AMM-specific enumerations."""
from enum import StrEnum


class DefenseLevel(StrEnum):
    """Risk defense escalation levels. Order matters — higher = more restrictive."""
    NORMAL = "NORMAL"
    WIDEN = "WIDEN"
    ONE_SIDE = "ONE_SIDE"
    KILL_SWITCH = "KILL_SWITCH"

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
