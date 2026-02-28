"""AMM configuration loader: YAML base + Redis hot-override layer."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from src.amm.config.models import GlobalConfig, MarketConfig

logger = logging.getLogger(__name__)

_DEFAULT_YAML = Path(__file__).parent / "default.yaml"


def load_global_config(yaml_path: Path | None = None) -> GlobalConfig:
    """Load GlobalConfig from YAML file, falling back to defaults."""
    path = yaml_path or _DEFAULT_YAML
    if not path.exists():
        logger.warning("Config YAML not found at %s, using defaults.", path)
        return GlobalConfig()

    with path.open() as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}

    global_data = data.get("global", {})
    cfg = GlobalConfig()

    # Map YAML keys to dataclass fields
    for key, value in global_data.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)

    return cfg


def apply_redis_overrides(cfg: GlobalConfig, overrides: dict[str, Any]) -> GlobalConfig:
    """Apply Redis key-value overrides onto a GlobalConfig instance in-place."""
    for key, value in overrides.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
        else:
            logger.warning("Unknown config key from Redis: %s", key)
    return cfg


def load_market_config(
    market_id: str,
    yaml_path: Path | None = None,
    redis_overrides: dict[str, Any] | None = None,
) -> MarketConfig:
    """Load MarketConfig for a specific market from YAML + Redis overlay."""
    path = yaml_path or _DEFAULT_YAML
    cfg = MarketConfig(market_id=market_id)

    if path.exists():
        with path.open() as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}

        # Apply market-defaults section
        market_defaults = data.get("market_defaults", {})
        for key, value in market_defaults.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)

        # Apply market-specific overrides
        market_specific = data.get("markets", {}).get(market_id, {})
        for key, value in market_specific.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)

    # Apply Redis hot-overrides (highest priority)
    if redis_overrides:
        for key, value in redis_overrides.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
            else:
                logger.warning("Unknown market config key from Redis: %s", key)

    return cfg
