"""Redis-backed notification registry for multi-pod deployments.

Thin adapter around :class:`RedisPubSub` — all Redis pub/sub logic
lives in the generic layer; this class maps the ``organization_id``
domain concept onto generic string keys.
"""

import asyncio

from redis.asyncio import Redis

from skyvern.forge.sdk.notification.base import BaseNotificationRegistry
from skyvern.forge.sdk.redis.pubsub import RedisPubSub


class RedisNotificationRegistry(BaseNotificationRegistry):
    """Fan-out pub/sub backed by Redis.  One Redis PubSub channel per org."""

    def __init__(self, redis_client: Redis) -> None:
        self._pubsub = RedisPubSub(redis_client, channel_prefix="skyvern:notifications:")

    # ------------------------------------------------------------------
    # Property accessors (used by existing tests)
    # ------------------------------------------------------------------

    @property
    def _listener_tasks(self) -> dict[str, asyncio.Task[None]]:
        return self._pubsub._listener_tasks

    @property
    def _subscribers(self) -> dict[str, list[asyncio.Queue[dict]]]:
        return self._pubsub._subscribers

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def subscribe(self, organization_id: str) -> asyncio.Queue[dict]:
        return self._pubsub.subscribe(organization_id)

    def unsubscribe(self, organization_id: str, queue: asyncio.Queue[dict]) -> None:
        self._pubsub.unsubscribe(organization_id, queue)

    def publish(self, organization_id: str, message: dict) -> None:
        self._pubsub.publish(organization_id, message)

    async def close(self) -> None:
        await self._pubsub.close()

    # ------------------------------------------------------------------
    # Internal helper (exposed for tests)
    # ------------------------------------------------------------------

    def _dispatch_local(self, organization_id: str, message: dict) -> None:
        self._pubsub._dispatch_local(organization_id, message)
