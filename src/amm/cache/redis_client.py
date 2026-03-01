"""Redis client factory for AMM."""
import redis.asyncio as aioredis


def create_redis_client(url: str = "redis://localhost:6379/0") -> aioredis.Redis:
    """Create an async Redis client."""
    return aioredis.from_url(url, decode_responses=False)
