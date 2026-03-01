"""Test AMM-specific enums."""
from src.amm.models.enums import DefenseLevel, Phase, QuoteAction


class TestDefenseLevel:
    def test_escalation_order(self) -> None:
        levels = list(DefenseLevel)
        assert levels == [
            DefenseLevel.NORMAL,
            DefenseLevel.WIDEN,
            DefenseLevel.ONE_SIDE,
            DefenseLevel.KILL_SWITCH,
        ]

    def test_is_active(self) -> None:
        assert DefenseLevel.NORMAL.is_quoting_active is True
        assert DefenseLevel.WIDEN.is_quoting_active is True
        assert DefenseLevel.ONE_SIDE.is_quoting_active is True
        assert DefenseLevel.KILL_SWITCH.is_quoting_active is False


class TestPhase:
    def test_phases(self) -> None:
        assert Phase.EXPLORATION.value == "EXPLORATION"
        assert Phase.STABILIZATION.value == "STABILIZATION"


class TestQuoteAction:
    def test_actions(self) -> None:
        assert QuoteAction.PLACE.value == "PLACE"
        assert QuoteAction.REPLACE.value == "REPLACE"
        assert QuoteAction.CANCEL.value == "CANCEL"
        assert QuoteAction.HOLD.value == "HOLD"
