"""Test BudgetManager — daily P&L tracking and budget breach detection."""
import pytest
from src.amm.risk.budget_manager import BudgetManager
from src.amm.config.models import MarketConfig


class TestBudgetManagerDailyPnL:
    def test_initial_pnl_zero(self) -> None:
        bm = BudgetManager()
        assert bm.get_daily_pnl("mkt-1") == 0

    def test_record_trade_profit(self) -> None:
        bm = BudgetManager()
        bm.record_trade("mkt-1", pnl_cents=500)
        assert bm.get_daily_pnl("mkt-1") == 500

    def test_record_trade_loss(self) -> None:
        bm = BudgetManager()
        bm.record_trade("mkt-1", pnl_cents=-300)
        assert bm.get_daily_pnl("mkt-1") == -300

    def test_cumulative_pnl(self) -> None:
        bm = BudgetManager()
        bm.record_trade("mkt-1", pnl_cents=500)
        bm.record_trade("mkt-1", pnl_cents=-200)
        bm.record_trade("mkt-1", pnl_cents=100)
        assert bm.get_daily_pnl("mkt-1") == 400

    def test_per_market_isolation(self) -> None:
        """Different markets have separate P&L buckets."""
        bm = BudgetManager()
        bm.record_trade("mkt-1", pnl_cents=1000)
        bm.record_trade("mkt-2", pnl_cents=-500)
        assert bm.get_daily_pnl("mkt-1") == 1000
        assert bm.get_daily_pnl("mkt-2") == -500

    def test_get_total_daily_pnl(self) -> None:
        """Total daily P&L is the sum across all markets."""
        bm = BudgetManager()
        bm.record_trade("mkt-1", pnl_cents=1000)
        bm.record_trade("mkt-2", pnl_cents=-300)
        assert bm.get_total_daily_pnl() == 700

    def test_reset_daily(self) -> None:
        bm = BudgetManager()
        bm.record_trade("mkt-1", pnl_cents=1000)
        bm.reset_daily()
        assert bm.get_daily_pnl("mkt-1") == 0
        assert bm.get_total_daily_pnl() == 0


class TestBudgetManagerBreach:
    def test_no_breach_within_limit(self) -> None:
        cfg = MarketConfig(market_id="mkt-1", max_per_market_loss_cents=5000)
        bm = BudgetManager()
        bm.record_trade("mkt-1", pnl_cents=-3000)
        assert bm.is_market_budget_breached("mkt-1", cfg) is False

    def test_breach_at_limit(self) -> None:
        cfg = MarketConfig(market_id="mkt-1", max_per_market_loss_cents=5000)
        bm = BudgetManager()
        bm.record_trade("mkt-1", pnl_cents=-5000)
        assert bm.is_market_budget_breached("mkt-1", cfg) is True

    def test_breach_beyond_limit(self) -> None:
        cfg = MarketConfig(market_id="mkt-1", max_per_market_loss_cents=5000)
        bm = BudgetManager()
        bm.record_trade("mkt-1", pnl_cents=-7000)
        assert bm.is_market_budget_breached("mkt-1", cfg) is True

    def test_daily_global_breach(self) -> None:
        """Global daily loss across all markets triggers breach."""
        cfg = MarketConfig(market_id="mkt-1", max_daily_loss_cents=10000)
        bm = BudgetManager()
        bm.record_trade("mkt-1", pnl_cents=-6000)
        bm.record_trade("mkt-2", pnl_cents=-5000)
        assert bm.is_daily_budget_breached(cfg) is True

    def test_daily_global_no_breach(self) -> None:
        cfg = MarketConfig(market_id="mkt-1", max_daily_loss_cents=10000)
        bm = BudgetManager()
        bm.record_trade("mkt-1", pnl_cents=-4000)
        bm.record_trade("mkt-2", pnl_cents=-3000)
        assert bm.is_daily_budget_breached(cfg) is False

    def test_profit_does_not_breach(self) -> None:
        cfg = MarketConfig(market_id="mkt-1", max_per_market_loss_cents=5000)
        bm = BudgetManager()
        bm.record_trade("mkt-1", pnl_cents=10000)
        assert bm.is_market_budget_breached("mkt-1", cfg) is False

    def test_breach_resets_after_daily_reset(self) -> None:
        cfg = MarketConfig(market_id="mkt-1", max_per_market_loss_cents=5000)
        bm = BudgetManager()
        bm.record_trade("mkt-1", pnl_cents=-7000)
        assert bm.is_market_budget_breached("mkt-1", cfg) is True
        bm.reset_daily()
        assert bm.is_market_budget_breached("mkt-1", cfg) is False
