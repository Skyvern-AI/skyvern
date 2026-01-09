import asyncio
from datetime import timedelta

import pytest

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
