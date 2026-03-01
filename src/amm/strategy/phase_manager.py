"""AMM phase state machine: EXPLORATION → STABILIZATION."""
import logging

from src.amm.models.enums import Phase
from src.amm.config.models import MarketConfig

logger = logging.getLogger(__name__)


class PhaseManager:
    def __init__(self, config: MarketConfig) -> None:
        self._config = config
        self.current_phase = Phase.EXPLORATION
        self._trade_count = 0

    def update(self, new_trades: int, elapsed_hours: float) -> Phase:
        """Update phase based on trade count and elapsed time."""
        self._trade_count += new_trades

        if self.current_phase == Phase.EXPLORATION:
            if (self._trade_count >= self._config.stabilization_volume_threshold
                    or elapsed_hours >= self._config.exploration_duration_hours):
                self.current_phase = Phase.STABILIZATION
                logger.info("Phase transition: EXPLORATION → STABILIZATION")

        return self.current_phase
