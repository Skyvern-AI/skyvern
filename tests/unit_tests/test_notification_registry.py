"""Tests for NotificationRegistry pub/sub and get_active_verification_requests (SKY-6)."""

import pytest

from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.notification.base import BaseNotificationRegistry
from skyvern.forge.sdk.notification.factory import NotificationRegistryFactory
from skyvern.forge.sdk.notification.local import LocalNotificationRegistry

# === Task 1: NotificationRegistry subscribe / publish / unsubscribe ===


@pytest.mark.asyncio
async def test_subscribe_and_publish():
    """Published messages should be received by subscribers."""
    registry = LocalNotificationRegistry()
    queue = registry.subscribe("org_1")

    registry.publish("org_1", {"type": "verification_code_required", "task_id": "tsk_1"})
    msg = queue.get_nowait()
    assert msg["type"] == "verification_code_required"
    assert msg["task_id"] == "tsk_1"


@pytest.mark.asyncio
async def test_multiple_subscribers():
    """All subscribers for an org should receive the same message."""
    registry = LocalNotificationRegistry()
    q1 = registry.subscribe("org_1")
    q2 = registry.subscribe("org_1")

    registry.publish("org_1", {"type": "verification_code_required"})
    assert not q1.empty()
    assert not q2.empty()
    assert q1.get_nowait() == q2.get_nowait()


@pytest.mark.asyncio
async def test_publish_wrong_org_does_not_leak():
    """Messages for org_A should not appear in org_B's queue."""
    registry = LocalNotificationRegistry()
    q_a = registry.subscribe("org_a")
    q_b = registry.subscribe("org_b")

    registry.publish("org_a", {"type": "test"})
    assert not q_a.empty()
    assert q_b.empty()


@pytest.mark.asyncio
async def test_unsubscribe():
    """After unsubscribe, the queue should no longer receive messages."""
    registry = LocalNotificationRegistry()
    queue = registry.subscribe("org_1")

    registry.unsubscribe("org_1", queue)
    registry.publish("org_1", {"type": "test"})
    assert queue.empty()


@pytest.mark.asyncio
async def test_unsubscribe_idempotent():
    """Unsubscribing a queue that's already removed should not raise."""
    registry = LocalNotificationRegistry()
    queue = registry.subscribe("org_1")
    registry.unsubscribe("org_1", queue)
    registry.unsubscribe("org_1", queue)  # should not raise


# === Task: BaseNotificationRegistry ABC ===


def test_base_notification_registry_cannot_be_instantiated():
    """ABC should not be directly instantiable."""
    with pytest.raises(TypeError):
        BaseNotificationRegistry()


# === Task: NotificationRegistryFactory ===


@pytest.mark.asyncio
async def test_factory_returns_local_by_default():
    """Factory should return a LocalNotificationRegistry by default."""
    registry = NotificationRegistryFactory.get_registry()
    assert isinstance(registry, LocalNotificationRegistry)


@pytest.mark.asyncio
async def test_factory_set_and_get():
    """Factory should allow swapping the registry implementation."""
    original = NotificationRegistryFactory.get_registry()
    try:
        custom = LocalNotificationRegistry()
        NotificationRegistryFactory.set_registry(custom)
        assert NotificationRegistryFactory.get_registry() is custom
    finally:
        NotificationRegistryFactory.set_registry(original)


# === Task 2: get_active_verification_requests DB method ===


def test_get_active_verification_requests_method_exists():
    """AgentDB should have get_active_verification_requests method."""
    assert hasattr(AgentDB, "get_active_verification_requests")
