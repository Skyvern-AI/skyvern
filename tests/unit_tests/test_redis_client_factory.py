"""Tests for RedisClientFactory."""

from unittest.mock import MagicMock

from skyvern.forge.sdk.redis.factory import RedisClientFactory


def test_default_is_none():
    """Factory returns None when no client has been set."""
    # Reset to default state
    RedisClientFactory.set_client(None)  # type: ignore[arg-type]
    assert RedisClientFactory.get_client() is None


def test_set_and_get():
    """Round-trip: set_client then get_client returns the same object."""
    mock_client = MagicMock()
    RedisClientFactory.set_client(mock_client)
    assert RedisClientFactory.get_client() is mock_client

    # Cleanup
    RedisClientFactory.set_client(None)  # type: ignore[arg-type]
