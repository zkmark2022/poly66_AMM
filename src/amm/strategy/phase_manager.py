"""Phase manager: EXPLORATION ↔ STABILIZATION state machine.

See AMM design §6.2 and data dictionary v1.0 §6.2.

Transition rules:
  EXPLORATION → STABILIZATION when ANY:
    - elapsed_hours >= exploration_duration_hours
    - cumulative_trades >= stabilization_volume_threshold

  STABILIZATION → EXPLORATION (emergency rollback) when ANY:
    - 5-minute volatility > 10%
    - daily_pnl < -(max_daily_loss_cents * 0.5)
  Rollback requires a 10-minute debounce (cooldown) period to prevent oscillation.
"""
import logging

from src.amm.config.models import MarketConfig
from src.amm.models.enums import Phase

logger = logging.getLogger(__name__)

# 10-minute cooldown expressed in update cycles (assumed 2-second cycle = 300 cycles)
# But PhaseManager doesn't know the cycle time — expose debounce_cycles as config
_DEFAULT_ROLLBACK_DEBOUNCE_CYCLES = 300  # 300 × 2s = 600s = 10 minutes


class PhaseManager:
    """Manages AMM strategy phase transitions with debounce protection."""

    def __init__(
        self,
        config: MarketConfig,
        rollback_debounce_cycles: int = _DEFAULT_ROLLBACK_DEBOUNCE_CYCLES,
    ):
        self._config = config
        self._rollback_debounce_cycles = rollback_debounce_cycles
        self.current_phase: Phase = Phase.EXPLORATION
        self._cumulative_trades: int = 0
        self._rollback_candidate_cycles: int = 0  # consecutive cycles where rollback is needed

    def record_trade_count(self, count: int) -> None:
        """Accumulate trade count toward STABILIZATION threshold."""
        self._cumulative_trades += count

    def update(
        self,
        elapsed_hours: float,
        volatility_5min: float,
        daily_pnl: int,
        budget_cents: int,
    ) -> Phase:
        """Evaluate phase transitions based on current market state.

        Args:
            elapsed_hours: Hours since market opened.
            volatility_5min: 5-minute price volatility (0.0–1.0).
            daily_pnl: Current daily P&L in cents (negative = loss).
            budget_cents: Total daily budget in cents (positive value).

        Returns:
            Current phase after evaluating transitions.
        """
        if self.current_phase == Phase.EXPLORATION:
            return self._evaluate_exploration(elapsed_hours)
        else:
            return self._evaluate_stabilization(volatility_5min, daily_pnl, budget_cents)

    def _evaluate_exploration(self, elapsed_hours: float) -> Phase:
        """Check if EXPLORATION → STABILIZATION transition triggers."""
        time_expired = elapsed_hours >= self._config.exploration_duration_hours
        volume_reached = (
            self._cumulative_trades >= self._config.stabilization_volume_threshold
        )

        if time_expired or volume_reached:
            reason = "TIME_EXPIRED" if time_expired else "VOLUME_REACHED"
            logger.info(
                "Phase transition EXPLORATION → STABILIZATION (reason=%s, "
                "elapsed=%.1fh, trades=%d)",
                reason,
                elapsed_hours,
                self._cumulative_trades,
            )
            self.current_phase = Phase.STABILIZATION
            self._rollback_candidate_cycles = 0

        return self.current_phase

    def _evaluate_stabilization(
        self, volatility_5min: float, daily_pnl: int, budget_cents: int
    ) -> Phase:
        """Check if STABILIZATION → EXPLORATION rollback triggers (with debounce)."""
        high_volatility = volatility_5min > 0.10
        budget_half_breached = daily_pnl < -(budget_cents // 2)
        needs_rollback = high_volatility or budget_half_breached

        if needs_rollback:
            self._rollback_candidate_cycles += 1
            if self._rollback_candidate_cycles >= self._rollback_debounce_cycles:
                logger.warning(
                    "Phase ROLLBACK STABILIZATION → EXPLORATION "
                    "(volatility=%.3f, pnl=%d, budget=%d, cycles=%d)",
                    volatility_5min,
                    daily_pnl,
                    budget_cents,
                    self._rollback_candidate_cycles,
                )
                self.current_phase = Phase.EXPLORATION
                self._rollback_candidate_cycles = 0
                self._cumulative_trades = 0  # reset to allow re-stabilization
        else:
            self._rollback_candidate_cycles = 0

        return self.current_phase
