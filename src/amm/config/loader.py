"""AMM configuration loader. YAML + Redis overlay."""
from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args, get_origin

import yaml

from src.amm.config.models import GlobalConfig, MarketConfig

if TYPE_CHECKING:
    from src.amm.cache.protocols import AsyncRedisLike

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent


def _coerce(field_type: Any, val: str) -> Any:
    """Safely coerce Redis string values using dataclass field metadata."""
    origin = get_origin(field_type)
    if origin is not None:
        args = [arg for arg in get_args(field_type) if arg is not type(None)]
        if len(args) == 1:
            field_type = args[0]

    if field_type is bool:
        return val.lower() in ("true", "1", "yes")

    return field_type(val)


class ConfigLoader:
    """Load AMM configuration from YAML file with optional Redis overlay."""

    def __init__(self, redis_client: "AsyncRedisLike | None" = None,
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
        global_fields = {field.name: field.type for field in dataclasses.fields(GlobalConfig)}
        cfg = GlobalConfig(**{k: v for k, v in data.items() if k in global_fields})

        if self._redis:
            try:
                redis_data = await self._redis.hgetall("amm:config:global")
                for k, v in redis_data.items():
                    key = k.decode() if isinstance(k, bytes) else k
                    val = v.decode() if isinstance(v, bytes) else v
                    if key in global_fields:
                        setattr(cfg, key, _coerce(global_fields[key], val))
            except Exception as e:
                logger.warning("Failed to load global config from Redis: %s", e)

        return cfg

    async def load_market(self, market_id: str) -> MarketConfig:
        markets_data = self._load_yaml().get("markets", {})
        base = markets_data.get("default", {})
        override = markets_data.get(market_id, {})
        data = {**base, **override}

        market_fields = {
            field.name: field.type for field in dataclasses.fields(MarketConfig)
        }
        market_field_names = set(market_fields) - {"market_id"}
        cfg = MarketConfig(market_id=market_id,
                           **{k: v for k, v in data.items() if k in market_field_names})

        if self._redis:
            try:
                redis_data = await self._redis.hgetall(f"amm:config:{market_id}")
                for k, v in redis_data.items():
                    key = k.decode() if isinstance(k, bytes) else k
                    val = v.decode() if isinstance(v, bytes) else v
                    if key in market_fields and key != "market_id":
                        setattr(cfg, key, _coerce(market_fields[key], val))
            except Exception as e:
                logger.warning("Failed to load market config from Redis: %s", e)

        return cfg
