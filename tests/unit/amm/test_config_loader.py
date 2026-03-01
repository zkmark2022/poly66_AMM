"""Tests for AMM configuration models and YAML loader."""
import pytest
from src.amm.config.models import GlobalConfig, MarketConfig


class TestGlobalConfig:
    def test_defaults(self) -> None:
        cfg = GlobalConfig()
        assert cfg.quote_interval_seconds == 2.0
        assert cfg.reconcile_interval_seconds == 300.0
        assert cfg.base_url == "http://localhost:8000/api/v1"

    def test_redis_override(self) -> None:
        """Redis values should override YAML defaults."""
        cfg = GlobalConfig()
        cfg.quote_interval_seconds = 1.0
        assert cfg.quote_interval_seconds == 1.0


class TestMarketConfig:
    def test_market_defaults(self) -> None:
        cfg = MarketConfig(market_id="mkt-1")
        assert cfg.gamma_tier == "MID"
        assert cfg.initial_mint_quantity == 1000
        assert cfg.max_daily_loss_cents == 100_00  # $100
        assert cfg.spread_min_cents == 2
        assert cfg.gradient_levels == 3

    def test_gamma_value(self) -> None:
        cfg = MarketConfig(market_id="mkt-1", gamma_tier="EARLY")
        assert cfg.gamma == 0.1

        cfg2 = MarketConfig(market_id="mkt-1", gamma_tier="LATE")
        assert cfg2.gamma == 0.8
