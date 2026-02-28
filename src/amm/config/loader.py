"""AMM config loader: YAML base + Redis hot-override layer.

Security model:
- Only keys in REDIS_ALLOWED_OVERRIDES may be written via the Redis hot-update path.
- Infrastructure settings (base_url, redis_url) and critical risk parameters
  (max_daily_loss_cents, defense thresholds) are LOCKED — Redis overrides for
  these keys are silently dropped.
- AMM_PASSWORD is always sourced from the AMM_PASSWORD environment variable.
  It is never read from YAML or Redis.
"""
import os
from dataclasses import asdict, fields
from typing import Any

from src.amm.config.models import GlobalConfig, MarketConfig

# Keys permitted for hot-update via Redis amm:config:{market_id}.
# CRITICAL: do NOT add infrastructure credentials or risk-limit parameters here.
REDIS_ALLOWED_OVERRIDES: frozenset[str] = frozenset({
    "quote_interval_seconds",
    "spread_min_cents",
    "spread_max_cents",
    "gradient_levels",
    "gamma_tier",
    "kappa",
    "anchor_price_cents",
    "gradient_quantity_decay",
    "gradient_price_step_cents",
    "auto_reinvest_enabled",
    "auto_merge_threshold",
    "exploration_duration_hours",
    "stabilization_volume_threshold",
    # DO NOT add: base_url, redis_url, amm_password, amm_username,
    # max_daily_loss_cents, max_per_market_loss_cents, inventory_skew_*,
    # widen_factor, defense_cooldown_cycles
})


def load_global_config(yaml_overrides: dict[str, Any] | None = None) -> GlobalConfig:
    """Build GlobalConfig from defaults + YAML.  AMM_PASSWORD always from env."""
    cfg = GlobalConfig(**(yaml_overrides or {}))
    cfg.amm_password = os.environ.get("AMM_PASSWORD", "")
    return cfg


def load_market_config(
    market_id: str,
    yaml_overrides: dict[str, Any] | None = None,
) -> MarketConfig:
    """Build MarketConfig from defaults + YAML overrides."""
    return MarketConfig(market_id=market_id, **(yaml_overrides or {}))


def _apply_redis_overrides(config: MarketConfig, redis_data: dict[str, Any]) -> MarketConfig:
    """Return a new MarketConfig with allowed Redis overrides applied.

    Keys not in REDIS_ALLOWED_OVERRIDES are silently dropped — this prevents
    untrusted Redis data from modifying credentials or risk parameters.
    """
    valid_fields = {f.name for f in fields(MarketConfig)}
    updates: dict[str, Any] = {}

    for key, value in redis_data.items():
        if key not in REDIS_ALLOWED_OVERRIDES:
            continue  # locked — discard silently
        if key not in valid_fields:
            continue  # unknown field — discard
        updates[key] = value

    if not updates:
        return config

    current = asdict(config)
    current.update(updates)
    return MarketConfig(**current)


def apply_redis_overrides(config: MarketConfig, redis_data: dict[str, Any]) -> MarketConfig:
    """Public API — delegates to internal implementation."""
    return _apply_redis_overrides(config, redis_data)
