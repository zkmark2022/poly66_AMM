"""Tests for AMM configuration models and YAML loader."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from src.amm.config.models import GlobalConfig, MarketConfig
from src.amm.config.loader import ConfigLoader


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

    async def test_load_market_coerces_optional_float_from_redis(
        self, tmp_path: Path,
    ) -> None:
        yaml_path = tmp_path / "amm.yaml"
        yaml_path.write_text("markets:\n  default: {}\n", encoding="utf-8")
        redis = AsyncMock()
        redis.hgetall.return_value = {b"remaining_hours_override": b"12.5"}

        loader = ConfigLoader(redis_client=redis, yaml_path=yaml_path)

        cfg = await loader.load_market("mkt-1")

        assert cfg.remaining_hours_override == 12.5

    async def test_load_market_coerces_bool_false_from_redis(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "amm.yaml"
        yaml_path.write_text("markets:\n  default: {}\n", encoding="utf-8")
        redis = AsyncMock()
        redis.hgetall.return_value = {b"auto_reinvest_enabled": b"false"}

        loader = ConfigLoader(redis_client=redis, yaml_path=yaml_path)

        cfg = await loader.load_market("mkt-1")

        assert cfg.auto_reinvest_enabled is False

    async def test_load_market_coerces_bool_true_from_redis(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "amm.yaml"
        yaml_path.write_text("markets:\n  default: {}\n", encoding="utf-8")
        redis = AsyncMock()
        redis.hgetall.return_value = {b"auto_reinvest_enabled": b"true"}

        loader = ConfigLoader(redis_client=redis, yaml_path=yaml_path)

        cfg = await loader.load_market("mkt-1")

        assert cfg.auto_reinvest_enabled is True
