"""Unit tests for AMM config loader security — REDIS_ALLOWED_OVERRIDES allowlist."""
import os

import pytest

from src.amm.config.loader import (
    REDIS_ALLOWED_OVERRIDES,
    apply_redis_overrides,
    load_global_config,
    load_market_config,
)
from src.amm.config.models import GlobalConfig, MarketConfig


class TestRedisAllowedOverrides:
    def test_allowed_keys_present(self) -> None:
        expected = {
            "quote_interval_seconds",
            "spread_min_cents",
            "spread_max_cents",
            "gradient_levels",
            "gamma_tier",
        }
        assert expected.issubset(REDIS_ALLOWED_OVERRIDES)

    def test_sensitive_keys_excluded(self) -> None:
        locked = {
            "base_url",
            "redis_url",
            "amm_password",
            "max_daily_loss_cents",
            "inventory_skew_widen",
            "inventory_skew_one_side",
            "inventory_skew_kill",
            "defense_cooldown_cycles",
            "max_per_market_loss_cents",
            "widen_factor",
        }
        assert locked.isdisjoint(REDIS_ALLOWED_OVERRIDES), (
            f"Sensitive keys found in allowlist: {locked & REDIS_ALLOWED_OVERRIDES}"
        )

    def test_is_frozenset(self) -> None:
        assert isinstance(REDIS_ALLOWED_OVERRIDES, frozenset)


class TestApplyRedisOverrides:
    def _base(self) -> MarketConfig:
        return load_market_config("mkt-1")

    def test_allowed_key_applied(self) -> None:
        cfg = apply_redis_overrides(self._base(), {"spread_min_cents": 5})
        assert cfg.spread_min_cents == 5

    def test_multiple_allowed_keys_applied(self) -> None:
        cfg = apply_redis_overrides(
            self._base(),
            {"spread_min_cents": 3, "spread_max_cents": 30, "gradient_levels": 5},
        )
        assert cfg.spread_min_cents == 3
        assert cfg.spread_max_cents == 30
        assert cfg.gradient_levels == 5

    def test_locked_key_dropped_silently(self) -> None:
        original = self._base()
        cfg = apply_redis_overrides(original, {"max_daily_loss_cents": 1})
        assert cfg.max_daily_loss_cents == original.max_daily_loss_cents

    def test_redis_url_dropped(self) -> None:
        original = self._base()
        # MarketConfig has no redis_url, but we simulate a poisoned payload
        cfg = apply_redis_overrides(original, {"redis_url": "redis://evil:6379/0"})
        # Should be identical — unknown field silently dropped
        assert cfg == original

    def test_amm_password_not_in_market_config(self) -> None:
        original = self._base()
        cfg = apply_redis_overrides(original, {"amm_password": "stolen"})
        assert cfg == original

    def test_unknown_key_dropped(self) -> None:
        original = self._base()
        cfg = apply_redis_overrides(original, {"__proto__": "exploit"})
        assert cfg == original

    def test_empty_redis_data_returns_equal(self) -> None:
        original = self._base()
        cfg = apply_redis_overrides(original, {})
        assert cfg == original

    def test_base_url_dropped(self) -> None:
        """base_url is a GlobalConfig field — must not bleed into MarketConfig."""
        original = self._base()
        cfg = apply_redis_overrides(original, {"base_url": "http://evil.example.com"})
        assert cfg == original

    def test_defense_threshold_locked(self) -> None:
        original = self._base()
        cfg = apply_redis_overrides(
            original,
            {
                "inventory_skew_widen": 0.01,
                "inventory_skew_one_side": 0.01,
                "inventory_skew_kill": 0.01,
                "widen_factor": 100.0,
                "defense_cooldown_cycles": 0,
            },
        )
        assert cfg.inventory_skew_widen == original.inventory_skew_widen
        assert cfg.inventory_skew_one_side == original.inventory_skew_one_side
        assert cfg.inventory_skew_kill == original.inventory_skew_kill
        assert cfg.widen_factor == original.widen_factor
        assert cfg.defense_cooldown_cycles == original.defense_cooldown_cycles

    def test_market_id_preserved(self) -> None:
        cfg = apply_redis_overrides(self._base(), {"spread_min_cents": 4})
        assert cfg.market_id == "mkt-1"


class TestLoadGlobalConfig:
    def test_defaults(self) -> None:
        cfg = load_global_config()
        assert cfg.quote_interval_seconds == 2.0
        assert cfg.base_url == "http://localhost:8000/api/v1"

    def test_password_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AMM_PASSWORD", "supersecret")
        cfg = load_global_config()
        assert cfg.amm_password == "supersecret"

    def test_password_empty_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AMM_PASSWORD", raising=False)
        cfg = load_global_config()
        assert cfg.amm_password == ""

    def test_yaml_override_does_not_set_password(self) -> None:
        """YAML override dict must not be able to inject amm_password."""
        # amm_password is a valid field on GlobalConfig, but load_global_config
        # always overwrites it from env — verify env takes precedence.
        os.environ.pop("AMM_PASSWORD", None)
        cfg = load_global_config(yaml_overrides={"amm_password": "yaml_injected"})
        # env var is unset → should be ""
        assert cfg.amm_password == ""

    def test_yaml_override_applied(self) -> None:
        cfg = load_global_config(yaml_overrides={"quote_interval_seconds": 5.0})
        assert cfg.quote_interval_seconds == 5.0


class TestLoadMarketConfig:
    def test_defaults(self) -> None:
        cfg = load_market_config("mkt-1")
        assert cfg.gamma_tier == "MID"
        assert cfg.initial_mint_quantity == 1000
        assert cfg.max_daily_loss_cents == 100_00
        assert cfg.spread_min_cents == 2
        assert cfg.gradient_levels == 3

    def test_gamma_early(self) -> None:
        cfg = load_market_config("mkt-1", yaml_overrides={"gamma_tier": "EARLY"})
        assert cfg.gamma == 0.1

    def test_gamma_late(self) -> None:
        cfg = load_market_config("mkt-1", yaml_overrides={"gamma_tier": "LATE"})
        assert cfg.gamma == 0.8
