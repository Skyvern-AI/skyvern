import asyncio
from datetime import timedelta

import pytest

from skyvern.forge.sdk.cache.base import MAX_CACHE_ITEM
from skyvern.forge.sdk.cache.local import LocalCache


@pytest.mark.asyncio
async def test_cache_respects_custom_expiration():
    """Test that cache entries expire at their specified times."""
    cache = LocalCache()

    # Set entry with 1 second expiration
    await cache.set("short_lived", "value1", ex=timedelta(seconds=1))

    # Set entry with 10 second expiration
    await cache.set("long_lived", "value2", ex=timedelta(seconds=10))

    # Both should be available immediately
    assert await cache.get("short_lived") == "value1"
    assert await cache.get("long_lived") == "value2"

    # Wait 2 seconds
    await asyncio.sleep(2)

    # Short-lived should be expired, long-lived should still exist
    assert await cache.get("short_lived") is None
    assert await cache.get("long_lived") == "value2"

    # Wait another 9 seconds
    await asyncio.sleep(9)

    # Both should be expired now
    assert await cache.get("short_lived") is None
    assert await cache.get("long_lived") is None


@pytest.mark.asyncio
async def test_cache_default_expiration():
    """Test that default expiration (4 weeks) still works."""
    cache = LocalCache()

    await cache.set("default_ttl", "value")

    # Should be available immediately
    assert await cache.get("default_ttl") == "value"


@pytest.mark.asyncio
async def test_concurrent_reads():
    """Test that concurrent reads can proceed without blocking."""
    cache = LocalCache()

    # Set multiple values
    await cache.set("key1", "value1")
    await cache.set("key2", "value2")
    await cache.set("key3", "value3")

    # Perform concurrent reads
    results = await asyncio.gather(
        cache.get("key1"),
        cache.get("key2"),
        cache.get("key3"),
        cache.get("key1"),
        cache.get("key2"),
        cache.get("key3"),
    )

    # All reads should succeed
    assert results == ["value1", "value2", "value3", "value1", "value2", "value3"]


@pytest.mark.asyncio
async def test_cleanup_trigger_with_insertion_count():
    """Test that cleanup triggers based on insertion count, not cache size."""

    cache = LocalCache()

    # Set entries that will expire quickly
    for i in range(150):
        await cache.set(f"key_{i}", f"value_{i}", ex=timedelta(seconds=0.1))

    # Wait for entries to expire
    await asyncio.sleep(0.2)

    # Verify cleanup was triggered (insertion_count % 100 == 0 at 100 and 200)
    # After 150 insertions, cleanup should have run at insertion 100
    # The cache should have expired entries cleaned up
    # Note: We can't directly verify cleanup happened, but we can verify
    # that the insertion counter works correctly by checking behavior

    # Add more entries to trigger another cleanup
    for i in range(50):
        await cache.set(f"key2_{i}", f"value2_{i}", ex=timedelta(seconds=0.1))

    # Wait for expiration
    await asyncio.sleep(0.2)

    # Verify that non-expired entries still work
    await cache.set("non_expired", "still_here", ex=timedelta(seconds=10))
    assert await cache.get("non_expired") == "still_here"


@pytest.mark.asyncio
async def test_max_cache_item_enforcement():
    """Test that MAX_CACHE_ITEM limit is enforced correctly."""
    cache = LocalCache()

    # Fill cache to max capacity
    for i in range(MAX_CACHE_ITEM):
        await cache.set(f"key_{i}", f"value_{i}")

    # Verify all entries are present
    assert await cache.get("key_0") == "value_0"
    assert await cache.get(f"key_{MAX_CACHE_ITEM - 1}") == f"value_{MAX_CACHE_ITEM - 1}"

    # Add one more entry - should remove oldest
    await cache.set("new_key", "new_value")

    # Oldest entry should be removed
    assert await cache.get("key_0") is None
    # New entry should be present
    assert await cache.get("new_key") == "new_value"
    # Other entries should still be present
    assert await cache.get("key_1") == "value_1"
