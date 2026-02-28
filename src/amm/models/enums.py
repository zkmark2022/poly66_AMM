"""AMM-specific enumerations."""
from enum import StrEnum

# StrEnum alphabetical order (K < N < O < W) != severity order, so we
# maintain an explicit severity map for correct comparison semantics.
_DEFENSE_SEVERITY: dict[str, int] = {
    "NORMAL": 0,
    "WIDEN": 1,
    "ONE_SIDE": 2,
    "KILL_SWITCH": 3,
}


class DefenseLevel(StrEnum):
    """Risk defense escalation levels. Order matters — higher = more restrictive."""

    NORMAL = "NORMAL"
    WIDEN = "WIDEN"
    ONE_SIDE = "ONE_SIDE"
    KILL_SWITCH = "KILL_SWITCH"

    def _severity(self) -> int:
        return _DEFENSE_SEVERITY[self.value]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, DefenseLevel):
            return NotImplemented
        return self._severity() < other._severity()

    def __le__(self, other: object) -> bool:
        if not isinstance(other, DefenseLevel):
            return NotImplemented
        return self._severity() <= other._severity()

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, DefenseLevel):
            return NotImplemented
        return self._severity() > other._severity()

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, DefenseLevel):
            return NotImplemented
        return self._severity() >= other._severity()

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
