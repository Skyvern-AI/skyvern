from __future__ import annotations

from redis.asyncio import Redis


class RedisClientFactory:
    """Singleton factory for a shared async Redis client.

    Follows the same static set/get pattern as ``CacheFactory``.
    Defaults to ``None`` (no Redis in local/OSS mode).
    """

    __client: Redis | None = None

    @staticmethod
    def set_client(client: Redis) -> None:
        RedisClientFactory.__client = client

    @staticmethod
    def get_client() -> Redis | None:
        return RedisClientFactory.__client
