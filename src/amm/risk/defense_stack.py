"""Three-line defense system. See AMM design v7.1 §10."""
import logging

from src.amm.models.enums import DefenseLevel
from src.amm.config.models import MarketConfig

logger = logging.getLogger(__name__)

# Numeric severity: higher = more restrictive. StrEnum comparison is lexicographic,
# so explicit mapping is required for correct escalation logic.
_SEVERITY: dict[DefenseLevel, int] = {
    DefenseLevel.NORMAL: 0,
    DefenseLevel.WIDEN: 1,
    DefenseLevel.ONE_SIDE: 2,
    DefenseLevel.KILL_SWITCH: 3,
}


def _sev(level: DefenseLevel) -> int:
    return _SEVERITY[level]


class DefenseStack:
    def __init__(self, config: MarketConfig) -> None:
        self._config = config
        self.current_level = DefenseLevel.NORMAL
        self._cooldown_counter = 0

    def evaluate(
        self, inventory_skew: float, daily_pnl: int, market_active: bool,
    ) -> DefenseLevel:
        """Evaluate current market conditions and return defense level."""
        target = self._determine_target(inventory_skew, daily_pnl, market_active)

        if _sev(target) > _sev(self.current_level):
            self.current_level = target
            self._cooldown_counter = 0
            logger.warning("Defense ESCALATED to %s (skew=%.2f, pnl=%d)",
                           target, inventory_skew, daily_pnl)
        elif _sev(target) < _sev(self.current_level):
            self._cooldown_counter += 1
            if self._cooldown_counter >= self._config.defense_cooldown_cycles:
                self.current_level = target
                self._cooldown_counter = 0
                logger.info("Defense de-escalated to %s", target)
        else:
            self._cooldown_counter = 0

        return self.current_level

    def _determine_target(
        self, skew: float, pnl: int, active: bool,
    ) -> DefenseLevel:
        abs_skew = abs(skew)

        if not active:
            return DefenseLevel.KILL_SWITCH
        if abs_skew >= self._config.inventory_skew_kill:
            return DefenseLevel.KILL_SWITCH
        if pnl <= -self._config.max_per_market_loss_cents:
            return DefenseLevel.KILL_SWITCH
        if abs_skew >= self._config.inventory_skew_one_side:
            return DefenseLevel.ONE_SIDE
        if pnl <= -(self._config.max_per_market_loss_cents // 2):
            return DefenseLevel.ONE_SIDE
        if abs_skew >= self._config.inventory_skew_widen:
            return DefenseLevel.WIDEN
        return DefenseLevel.NORMAL
