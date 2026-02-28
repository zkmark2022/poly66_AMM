"""Budget Manager — daily P&L tracking and budget breach detection."""
import logging
from collections import defaultdict
from src.amm.config.models import MarketConfig

logger = logging.getLogger(__name__)


class BudgetManager:
    """Tracks daily and per-market P&L for budget breach detection.

    P&L is computed incrementally from trade events. Reset daily at midnight.
    """

    def __init__(self) -> None:
        # market_id → cumulative daily P&L in cents
        self._market_pnl: dict[str, int] = defaultdict(int)

    def record_trade(self, market_id: str, pnl_cents: int) -> None:
        """Record realized P&L from a single trade execution."""
        self._market_pnl[market_id] += pnl_cents
        logger.debug(
            "record_trade market=%s pnl_delta=%d total=%d",
            market_id, pnl_cents, self._market_pnl[market_id],
        )

    def get_daily_pnl(self, market_id: str) -> int:
        """Return today's cumulative P&L for a specific market (cents)."""
        return self._market_pnl[market_id]

    def get_total_daily_pnl(self) -> int:
        """Return today's total P&L summed across all markets (cents)."""
        return sum(self._market_pnl.values())

    def is_market_budget_breached(self, market_id: str, config: MarketConfig) -> bool:
        """True if this market's daily loss exceeds per-market limit."""
        pnl = self._market_pnl[market_id]
        return pnl <= -config.max_per_market_loss_cents

    def is_daily_budget_breached(self, config: MarketConfig) -> bool:
        """True if total daily loss across all markets exceeds global limit."""
        return self.get_total_daily_pnl() <= -config.max_daily_loss_cents

    def reset_daily(self) -> None:
        """Reset all P&L buckets — call at midnight UTC."""
        self._market_pnl.clear()
        logger.info("Daily P&L reset complete")
