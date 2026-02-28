"""AMM configuration loader. YAML + Redis overlay."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from src.amm.config.models import GlobalConfig, MarketConfig

if TYPE_CHECKING:
    import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent


class ConfigLoader:
    """Load AMM configuration from YAML file with optional Redis overlay."""

    def __init__(self, redis_client: "aioredis.Redis | None" = None,
                 yaml_path: Path | None = None) -> None:
        self._redis = redis_client
        self._yaml_path = yaml_path or (_CONFIG_DIR / "default.yaml")
        self._yaml_data: dict = {}

    def _load_yaml(self) -> dict:
        if not self._yaml_data:
            if self._yaml_path.exists():
                with open(self._yaml_path) as f:
                    self._yaml_data = yaml.safe_load(f) or {}
        return self._yaml_data

    async def load_global(self) -> GlobalConfig:
        data = self._load_yaml().get("global", {})
        cfg = GlobalConfig(**{k: v for k, v in data.items()
                              if hasattr(GlobalConfig, k)})

        if self._redis:
            try:
                redis_data = await self._redis.hgetall("amm:config:global")
                for k, v in redis_data.items():
                    key = k.decode() if isinstance(k, bytes) else k
                    val = v.decode() if isinstance(v, bytes) else v
                    if hasattr(cfg, key):
                        setattr(cfg, key, type(getattr(cfg, key))(val))
            except Exception as e:
                logger.warning("Failed to load global config from Redis: %s", e)

        return cfg

    async def load_market(self, market_id: str) -> MarketConfig:
        markets_data = self._load_yaml().get("markets", {})
        base = markets_data.get("default", {})
        override = markets_data.get(market_id, {})
        data = {**base, **override}

        cfg = MarketConfig(market_id=market_id,
                           **{k: v for k, v in data.items()
                              if hasattr(MarketConfig, k) and k != "market_id"})

        if self._redis:
            try:
                redis_data = await self._redis.hgetall(f"amm:config:{market_id}")
                for k, v in redis_data.items():
                    key = k.decode() if isinstance(k, bytes) else k
                    val = v.decode() if isinstance(v, bytes) else v
                    if hasattr(cfg, key) and key != "market_id":
                        setattr(cfg, key, type(getattr(cfg, key))(val))
            except Exception as e:
                logger.warning("Failed to load market config from Redis: %s", e)

        return cfg
