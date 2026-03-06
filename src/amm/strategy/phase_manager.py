"""AMM phase state machine: EXPLORATION → STABILIZATION."""
import logging

from src.amm.models.enums import Phase
from src.amm.config.models import MarketConfig

logger = logging.getLogger(__name__)


class PhaseManager:
    def __init__(self, config: MarketConfig) -> None:
        self._config = config
        self.current_phase = Phase.EXPLORATION

    def update(self, trade_count: int, elapsed_hours: float) -> Phase:
        """Update phase based on absolute trade count and elapsed time."""
        if self.current_phase == Phase.EXPLORATION:
            if (trade_count >= self._config.stabilization_volume_threshold
                    or elapsed_hours >= self._config.exploration_duration_hours):
                self.current_phase = Phase.STABILIZATION
                logger.info("Phase transition: EXPLORATION → STABILIZATION")

        return self.current_phase
