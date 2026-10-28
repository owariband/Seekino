from __future__ import annotations

from redis import Redis
from redis.exceptions import RedisError

from ..config.auth import AuthConfig


def create_redis_client(config: AuthConfig) -> Redis | None:
    if config.mode == "local":
        return None
    if not config.redis_url:
        raise ValueError("cloud authentication requires SEEKINO_AUTH_REDIS_URL")
    client = Redis.from_url(
        config.redis_url, decode_responses=True,
        socket_timeout=config.redis_timeout_seconds,
        socket_connect_timeout=config.redis_timeout_seconds,
    )
    try:
        client.ping()
    except RedisError:
        client.close()
        raise RuntimeError("authentication Redis is unavailable") from None
    except BaseException:
        client.close()
        raise
    return client


def close_redis_client(client: Redis | None) -> None:
    if client is not None:
        client.close()
