"""Generic Redis pub/sub layer.

Extracted from ``RedisNotificationRegistry`` so that any feature
(notifications, events, cache invalidation, etc.) can reuse the same
pattern with its own channel prefix.
"""

import asyncio
import json
from collections import defaultdict

import structlog
from redis.asyncio import Redis

LOG = structlog.get_logger()


class RedisPubSub:
    """Fan-out pub/sub backed by Redis.  One Redis PubSub channel per key."""

    def __init__(self, redis_client: Redis, channel_prefix: str) -> None:
        self._redis = redis_client
        self._channel_prefix = channel_prefix
        self._subscribers: dict[str, list[asyncio.Queue[dict]]] = defaultdict(list)
        # One listener task per key channel
        self._listener_tasks: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def subscribe(self, key: str) -> asyncio.Queue[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self._subscribers[key].append(queue)

        # Spin up a Redis listener if this is the first local subscriber
        if key not in self._listener_tasks:
            task = asyncio.get_running_loop().create_task(self._listen(key))
            self._listener_tasks[key] = task

        LOG.info("PubSub subscriber added", key=key, channel_prefix=self._channel_prefix)
        return queue

    def unsubscribe(self, key: str, queue: asyncio.Queue[dict]) -> None:
        queues = self._subscribers.get(key)
        if queues:
            try:
                queues.remove(queue)
            except ValueError:
                pass
            if not queues:
                del self._subscribers[key]
                self._cancel_listener(key)
        LOG.info("PubSub subscriber removed", key=key, channel_prefix=self._channel_prefix)

    def publish(self, key: str, message: dict) -> None:
        """Fire-and-forget Redis PUBLISH."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._publish_to_redis(key, message))
        except RuntimeError:
            LOG.warning(
                "No running event loop; cannot publish via Redis",
                key=key,
                channel_prefix=self._channel_prefix,
            )

    async def close(self) -> None:
        """Cancel all listener tasks and clear state.  Call on shutdown."""
        for key in list(self._listener_tasks):
            self._cancel_listener(key)
        self._subscribers.clear()
        LOG.info("RedisPubSub closed", channel_prefix=self._channel_prefix)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _publish_to_redis(self, key: str, message: dict) -> None:
        channel = f"{self._channel_prefix}{key}"
        try:
            await self._redis.publish(channel, json.dumps(message))
        except Exception:
            LOG.exception("Failed to publish to Redis", key=key, channel_prefix=self._channel_prefix)

    async def _listen(self, key: str) -> None:
        """Subscribe to a Redis channel and fan out messages locally."""
        channel = f"{self._channel_prefix}{key}"
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(channel)
            LOG.info("Redis listener started", channel=channel)
            async for raw_message in pubsub.listen():
                if raw_message["type"] != "message":
                    continue
                try:
                    data = json.loads(raw_message["data"])
                except (json.JSONDecodeError, TypeError):
                    LOG.warning("Invalid JSON on Redis channel", channel=channel)
                    continue
                self._dispatch_local(key, data)
        except asyncio.CancelledError:
            LOG.info("Redis listener cancelled", channel=channel)
            raise
        except Exception:
            LOG.exception("Redis listener error", channel=channel)
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.close()
            except Exception:
                LOG.warning("Error closing Redis pubsub", channel=channel)

    def _dispatch_local(self, key: str, message: dict) -> None:
        """Fan out a message to all local asyncio queues for this key."""
        queues = self._subscribers.get(key, [])
        for queue in queues:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                LOG.warning(
                    "Queue full, dropping message",
                    key=key,
                    channel_prefix=self._channel_prefix,
                )

    def _cancel_listener(self, key: str) -> None:
        task = self._listener_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
