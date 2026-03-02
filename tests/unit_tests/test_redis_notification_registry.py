"""Tests for RedisNotificationRegistry (SKY-6).

All tests use a mock Redis client — no real Redis instance required.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.notification.redis import RedisNotificationRegistry

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
            # Keep the listener alive until cancelled
            await asyncio.Event().wait()

    pubsub.listen = _listen
    return pubsub


# ---------------------------------------------------------------------------
# Tests: subscribe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_creates_queue_and_starts_listener():
    """subscribe() should return a queue and start a background listener task."""
    redis = _make_mock_redis()
    pubsub = _make_mock_pubsub()
    redis.pubsub.return_value = pubsub

    registry = RedisNotificationRegistry(redis)
    queue = registry.subscribe("org_1")

    assert isinstance(queue, asyncio.Queue)
    assert "org_1" in registry._listener_tasks
    task = registry._listener_tasks["org_1"]
    assert isinstance(task, asyncio.Task)

    # Cleanup
    await registry.close()


@pytest.mark.asyncio
async def test_subscribe_reuses_listener_for_same_org():
    """A second subscribe for the same org should NOT create a new listener task."""
    redis = _make_mock_redis()
    pubsub = _make_mock_pubsub()
    redis.pubsub.return_value = pubsub

    registry = RedisNotificationRegistry(redis)
    registry.subscribe("org_1")
    first_task = registry._listener_tasks["org_1"]

    registry.subscribe("org_1")
    assert registry._listener_tasks["org_1"] is first_task

    await registry.close()


# ---------------------------------------------------------------------------
# Tests: unsubscribe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsubscribe_cancels_listener_when_last_subscriber_leaves():
    """When the last subscriber unsubscribes, the listener task should be cancelled."""
    redis = _make_mock_redis()
    pubsub = _make_mock_pubsub(block=True)
    redis.pubsub.return_value = pubsub

    registry = RedisNotificationRegistry(redis)
    queue = registry.subscribe("org_1")

    # Let the listener task start running
    await asyncio.sleep(0)

    task = registry._listener_tasks["org_1"]

    registry.unsubscribe("org_1", queue)
    assert "org_1" not in registry._listener_tasks

    # Wait for the task to fully complete after cancellation
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()

    await registry.close()


@pytest.mark.asyncio
async def test_unsubscribe_keeps_listener_when_subscribers_remain():
    """If other subscribers remain, the listener task should stay alive."""
    redis = _make_mock_redis()
    pubsub = _make_mock_pubsub()
    redis.pubsub.return_value = pubsub

    registry = RedisNotificationRegistry(redis)
    q1 = registry.subscribe("org_1")
    registry.subscribe("org_1")  # second subscriber

    registry.unsubscribe("org_1", q1)
    assert "org_1" in registry._listener_tasks

    await registry.close()


# ---------------------------------------------------------------------------
# Tests: publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_calls_redis_publish():
    """publish() should fire-and-forget a Redis PUBLISH."""
    redis = _make_mock_redis()
    registry = RedisNotificationRegistry(redis)

    registry.publish("org_1", {"type": "verification_code_required"})

    # Allow the fire-and-forget task to execute
    await asyncio.sleep(0)

    redis.publish.assert_awaited_once_with(
        "skyvern:notifications:org_1",
        json.dumps({"type": "verification_code_required"}),
    )

    await registry.close()


# ---------------------------------------------------------------------------
# Tests: _dispatch_local
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_local_fans_out_to_all_queues():
    """_dispatch_local should put the message into every local queue for the org."""
    redis = _make_mock_redis()
    pubsub = _make_mock_pubsub()
    redis.pubsub.return_value = pubsub

    registry = RedisNotificationRegistry(redis)
    q1 = registry.subscribe("org_1")
    q2 = registry.subscribe("org_1")

    msg = {"type": "test", "value": 42}
    registry._dispatch_local("org_1", msg)

    assert q1.get_nowait() == msg
    assert q2.get_nowait() == msg

    await registry.close()


@pytest.mark.asyncio
async def test_dispatch_local_does_not_leak_across_orgs():
    """Messages dispatched for org_a should not appear in org_b queues."""
    redis = _make_mock_redis()
    pubsub = _make_mock_pubsub()
    redis.pubsub.return_value = pubsub

    registry = RedisNotificationRegistry(redis)
    q_a = registry.subscribe("org_a")
    q_b = registry.subscribe("org_b")

    registry._dispatch_local("org_a", {"type": "test"})
    assert not q_a.empty()
    assert q_b.empty()

    await registry.close()


# ---------------------------------------------------------------------------
# Tests: close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_cancels_all_listeners_and_clears_state():
    """close() should cancel every listener task and empty subscriber maps."""
    redis = _make_mock_redis()
    pubsub = _make_mock_pubsub(block=True)
    redis.pubsub.return_value = pubsub

    registry = RedisNotificationRegistry(redis)
    registry.subscribe("org_1")
    registry.subscribe("org_2")

    # Let the listener tasks start running
    await asyncio.sleep(0)

    task_1 = registry._listener_tasks["org_1"]
    task_2 = registry._listener_tasks["org_2"]

    await registry.close()

    # Wait for the tasks to fully complete after cancellation
    await asyncio.gather(task_1, task_2, return_exceptions=True)
    assert task_1.cancelled()
    assert task_2.cancelled()
    assert len(registry._listener_tasks) == 0
    assert len(registry._subscribers) == 0
