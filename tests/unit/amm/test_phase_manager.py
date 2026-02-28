"""Test phase manager state machine. See AMM design §6.2."""
import pytest
from src.amm.config.models import MarketConfig
from src.amm.models.enums import Phase
from src.amm.strategy.phase_manager import PhaseManager


class TestPhaseManager:
    def test_initial_phase_is_exploration(self) -> None:
        pm = PhaseManager(config=MarketConfig(market_id="mkt-1"))
        assert pm.current_phase == Phase.EXPLORATION

    def test_transitions_on_volume_threshold(self) -> None:
        """Cumulative trades ≥ threshold triggers STABILIZATION."""
        cfg = MarketConfig(
            market_id="mkt-1",
            stabilization_volume_threshold=10,
            exploration_duration_hours=9999.0,  # disable time trigger
        )
        pm = PhaseManager(config=cfg)
        pm.record_trade_count(10)
        phase = pm.update(
            elapsed_hours=1.0, volatility_5min=0.01, daily_pnl=0, budget_cents=10000
        )
        assert phase == Phase.STABILIZATION

    def test_transitions_on_time_expiry(self) -> None:
        """Elapsed hours ≥ exploration_duration triggers STABILIZATION."""
        cfg = MarketConfig(
            market_id="mkt-1",
            exploration_duration_hours=24.0,
            stabilization_volume_threshold=99999,  # disable volume trigger
        )
        pm = PhaseManager(config=cfg)
        phase = pm.update(
            elapsed_hours=25.0, volatility_5min=0.01, daily_pnl=0, budget_cents=10000
        )
        assert phase == Phase.STABILIZATION

    def test_no_transition_before_threshold(self) -> None:
        """Stays in EXPLORATION if neither condition is met."""
        cfg = MarketConfig(
            market_id="mkt-1",
            exploration_duration_hours=24.0,
            stabilization_volume_threshold=100,
        )
        pm = PhaseManager(config=cfg)
        pm.record_trade_count(50)
        phase = pm.update(
            elapsed_hours=12.0, volatility_5min=0.01, daily_pnl=0, budget_cents=10000
        )
        assert phase == Phase.EXPLORATION

    def test_rollback_on_high_volatility(self) -> None:
        """STABILIZATION → EXPLORATION on high volatility (after debounce)."""
        cfg = MarketConfig(market_id="mkt-1", stabilization_volume_threshold=1)
        pm = PhaseManager(config=cfg, rollback_debounce_cycles=3)
        # First: transition to STABILIZATION
        pm.record_trade_count(1)
        pm.update(elapsed_hours=1.0, volatility_5min=0.01, daily_pnl=0, budget_cents=10000)
        assert pm.current_phase == Phase.STABILIZATION
        # High volatility — needs 3 cycles of debounce
        pm.update(elapsed_hours=2.0, volatility_5min=0.15, daily_pnl=0, budget_cents=10000)
        assert pm.current_phase == Phase.STABILIZATION  # still (1/3)
        pm.update(elapsed_hours=2.0, volatility_5min=0.15, daily_pnl=0, budget_cents=10000)
        assert pm.current_phase == Phase.STABILIZATION  # still (2/3)
        pm.update(elapsed_hours=2.0, volatility_5min=0.15, daily_pnl=0, budget_cents=10000)
        assert pm.current_phase == Phase.EXPLORATION  # rolled back after 3 cycles

    def test_rollback_on_budget_breach(self) -> None:
        """STABILIZATION → EXPLORATION when daily_pnl < -budget/2 (with debounce)."""
        cfg = MarketConfig(market_id="mkt-1", stabilization_volume_threshold=1)
        pm = PhaseManager(config=cfg, rollback_debounce_cycles=2)
        pm.record_trade_count(1)
        pm.update(elapsed_hours=1.0, volatility_5min=0.01, daily_pnl=0, budget_cents=10000)
        assert pm.current_phase == Phase.STABILIZATION
        # daily_pnl = -6000 (60% of budget) → breach
        pm.update(elapsed_hours=2.0, volatility_5min=0.01, daily_pnl=-6000, budget_cents=10000)
        assert pm.current_phase == Phase.STABILIZATION  # (1/2)
        pm.update(elapsed_hours=2.0, volatility_5min=0.01, daily_pnl=-6000, budget_cents=10000)
        assert pm.current_phase == Phase.EXPLORATION  # rolled back

    def test_debounce_resets_on_condition_clear(self) -> None:
        """Rollback debounce counter resets if conditions clear before threshold."""
        cfg = MarketConfig(market_id="mkt-1", stabilization_volume_threshold=1)
        pm = PhaseManager(config=cfg, rollback_debounce_cycles=5)
        pm.record_trade_count(1)
        pm.update(elapsed_hours=1.0, volatility_5min=0.01, daily_pnl=0, budget_cents=10000)
        assert pm.current_phase == Phase.STABILIZATION
        # High volatility for 2 cycles (not enough for debounce=5)
        pm.update(elapsed_hours=2.0, volatility_5min=0.15, daily_pnl=0, budget_cents=10000)
        pm.update(elapsed_hours=2.0, volatility_5min=0.15, daily_pnl=0, budget_cents=10000)
        assert pm.current_phase == Phase.STABILIZATION
        # Volatility clears
        pm.update(elapsed_hours=2.0, volatility_5min=0.05, daily_pnl=0, budget_cents=10000)
        assert pm.current_phase == Phase.STABILIZATION
        # High volatility again — counter should have reset, need full 5 cycles
        pm.update(elapsed_hours=2.0, volatility_5min=0.15, daily_pnl=0, budget_cents=10000)
        assert pm.current_phase == Phase.STABILIZATION  # only 1 cycle, not 5

    def test_record_trade_count_accumulates(self) -> None:
        """Multiple record_trade_count calls accumulate."""
        cfg = MarketConfig(
            market_id="mkt-1",
            stabilization_volume_threshold=100,
            exploration_duration_hours=9999.0,
        )
        pm = PhaseManager(config=cfg)
        pm.record_trade_count(40)
        pm.record_trade_count(35)
        pm.record_trade_count(25)
        phase = pm.update(
            elapsed_hours=1.0, volatility_5min=0.01, daily_pnl=0, budget_cents=10000
        )
        assert phase == Phase.STABILIZATION

    def test_stabilization_is_stable_under_normal_conditions(self) -> None:
        """No rollback occurs when volatility is low and P&L is fine."""
        cfg = MarketConfig(market_id="mkt-1", stabilization_volume_threshold=1)
        pm = PhaseManager(config=cfg, rollback_debounce_cycles=3)
        pm.record_trade_count(1)
        pm.update(elapsed_hours=1.0, volatility_5min=0.01, daily_pnl=0, budget_cents=10000)
        assert pm.current_phase == Phase.STABILIZATION
        for _ in range(10):
            pm.update(
                elapsed_hours=2.0, volatility_5min=0.05, daily_pnl=-100, budget_cents=10000
            )
        assert pm.current_phase == Phase.STABILIZATION
