"""Redis async client factory for AMM cache layer."""
import redis.asyncio as aioredis


def create_redis_client(url: str = "redis://localhost:6379/0") -> aioredis.Redis:
    """Create an async Redis client from a connection URL."""
    return aioredis.from_url(url, decode_responses=True)
