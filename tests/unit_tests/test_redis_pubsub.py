"""Tests for RedisPubSub (generic pub/sub layer).

All tests use a mock Redis client — no real Redis instance required.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.redis.pubsub import RedisPubSub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_redis() -> MagicMock:
    """Return a mock redis.asyncio.Redis client."""
    redis = MagicMock()
    redis.publish = AsyncMock()
    redis.pubsub = MagicMock()
    return redis


def _make_mock_pubsub(messages: list[dict] | None = None, *, block: bool = False) -> MagicMock:
    """Return a mock PubSub that yields *messages* from ``listen()``.

    Each entry in *messages* should look like:
        {"type": "message", "data": '{"key": "val"}'}

    If *block* is True the async generator will hang forever after
    exhausting *messages*, which keeps the listener task alive so that
    cancellation semantics can be tested.
    """
    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    pubsub.close = AsyncMock()

    async def _listen():
        for msg in messages or []:
            yield msg
        if block:
            await asyncio.Event().wait()

    pubsub.listen = _listen
    return pubsub


PREFIX = "skyvern:test:"


# ---------------------------------------------------------------------------
# Tests: subscribe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_creates_queue_and_starts_listener():
    """subscribe() should return a queue and start a background listener task."""
    redis = _make_mock_redis()
    pubsub = _make_mock_pubsub()
    redis.pubsub.return_value = pubsub

    ps = RedisPubSub(redis, channel_prefix=PREFIX)
    queue = ps.subscribe("key_1")

    assert isinstance(queue, asyncio.Queue)
    assert "key_1" in ps._listener_tasks
    task = ps._listener_tasks["key_1"]
    assert isinstance(task, asyncio.Task)

    await ps.close()


@pytest.mark.asyncio
async def test_subscribe_reuses_listener_for_same_key():
    """A second subscribe for the same key should NOT create a new listener task."""
    redis = _make_mock_redis()
    pubsub = _make_mock_pubsub()
    redis.pubsub.return_value = pubsub

    ps = RedisPubSub(redis, channel_prefix=PREFIX)
    ps.subscribe("key_1")
    first_task = ps._listener_tasks["key_1"]

    ps.subscribe("key_1")
    assert ps._listener_tasks["key_1"] is first_task

    await ps.close()


# ---------------------------------------------------------------------------
# Tests: unsubscribe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsubscribe_cancels_listener_when_last_subscriber_leaves():
    """When the last subscriber unsubscribes, the listener task should be cancelled."""
    redis = _make_mock_redis()
    pubsub = _make_mock_pubsub(block=True)
    redis.pubsub.return_value = pubsub

    ps = RedisPubSub(redis, channel_prefix=PREFIX)
    queue = ps.subscribe("key_1")

    await asyncio.sleep(0)

    task = ps._listener_tasks["key_1"]

    ps.unsubscribe("key_1", queue)
    assert "key_1" not in ps._listener_tasks

    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()

    await ps.close()


@pytest.mark.asyncio
async def test_unsubscribe_keeps_listener_when_subscribers_remain():
    """If other subscribers remain, the listener task should stay alive."""
    redis = _make_mock_redis()
    pubsub = _make_mock_pubsub()
    redis.pubsub.return_value = pubsub

    ps = RedisPubSub(redis, channel_prefix=PREFIX)
    q1 = ps.subscribe("key_1")
    ps.subscribe("key_1")

    ps.unsubscribe("key_1", q1)
    assert "key_1" in ps._listener_tasks

    await ps.close()


# ---------------------------------------------------------------------------
# Tests: publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_calls_redis_publish():
    """publish() should fire-and-forget a Redis PUBLISH with prefixed channel."""
    redis = _make_mock_redis()
    ps = RedisPubSub(redis, channel_prefix=PREFIX)

    ps.publish("key_1", {"type": "event"})

    await asyncio.sleep(0)

    redis.publish.assert_awaited_once_with(
        f"{PREFIX}key_1",
        json.dumps({"type": "event"}),
    )

    await ps.close()


# ---------------------------------------------------------------------------
# Tests: _dispatch_local
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_local_fans_out_to_all_queues():
    """_dispatch_local should put the message into every local queue for the key."""
    redis = _make_mock_redis()
    pubsub = _make_mock_pubsub()
    redis.pubsub.return_value = pubsub

    ps = RedisPubSub(redis, channel_prefix=PREFIX)
    q1 = ps.subscribe("key_1")
    q2 = ps.subscribe("key_1")

    msg = {"type": "test", "value": 42}
    ps._dispatch_local("key_1", msg)

    assert q1.get_nowait() == msg
    assert q2.get_nowait() == msg

    await ps.close()


@pytest.mark.asyncio
async def test_dispatch_local_does_not_leak_across_keys():
    """Messages dispatched for key_a should not appear in key_b queues."""
    redis = _make_mock_redis()
    pubsub = _make_mock_pubsub()
    redis.pubsub.return_value = pubsub

    ps = RedisPubSub(redis, channel_prefix=PREFIX)
    q_a = ps.subscribe("key_a")
    q_b = ps.subscribe("key_b")

    ps._dispatch_local("key_a", {"type": "test"})
    assert not q_a.empty()
    assert q_b.empty()

    await ps.close()


# ---------------------------------------------------------------------------
# Tests: close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_cancels_all_listeners_and_clears_state():
    """close() should cancel every listener task and empty subscriber maps."""
    redis = _make_mock_redis()
    pubsub = _make_mock_pubsub(block=True)
    redis.pubsub.return_value = pubsub

    ps = RedisPubSub(redis, channel_prefix=PREFIX)
    ps.subscribe("key_1")
    ps.subscribe("key_2")

    await asyncio.sleep(0)

    task_1 = ps._listener_tasks["key_1"]
    task_2 = ps._listener_tasks["key_2"]

    await ps.close()

    await asyncio.gather(task_1, task_2, return_exceptions=True)
    assert task_1.cancelled()
    assert task_2.cancelled()
    assert len(ps._listener_tasks) == 0
    assert len(ps._subscribers) == 0


# ---------------------------------------------------------------------------
# Tests: prefix isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_prefixes_do_not_interfere():
    """Two RedisPubSub instances with different prefixes use separate channels."""
    redis = _make_mock_redis()

    ps_a = RedisPubSub(redis, channel_prefix="prefix_a:")
    ps_b = RedisPubSub(redis, channel_prefix="prefix_b:")

    ps_a.publish("key_1", {"from": "a"})
    ps_b.publish("key_1", {"from": "b"})

    await asyncio.sleep(0)

    calls = redis.publish.await_args_list
    assert len(calls) == 2

    channels = {call.args[0] for call in calls}
    assert "prefix_a:key_1" in channels
    assert "prefix_b:key_1" in channels

    await ps_a.close()
    await ps_b.close()
