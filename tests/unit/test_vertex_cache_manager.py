from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from skyvern.forge.sdk.api.llm import vertex_cache_manager


def test_cache_creation_logs_response_metadata_without_body(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = "private-response-sentinel"
    manager = vertex_cache_manager.VertexCacheManager("project-id")
    monkeypatch.setattr(manager, "_get_access_token", lambda: "access-token")
    monkeypatch.setattr(
        vertex_cache_manager.requests,
        "post",
        MagicMock(return_value=SimpleNamespace(status_code=500, text=sentinel)),
    )

    with capture_logs() as events, pytest.raises(Exception, match="HTTP 500") as exc_info:
        manager.create_cache("model", "input", "cache-key")

    event = next(event for event in events if event["event"] == "Failed to create cache")
    assert event["status_code"] == 500
    assert sentinel not in str(exc_info.value)
    assert all(sentinel not in repr(event) for event in events)


def test_cache_deletion_logs_response_metadata_without_body(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = "private-response-sentinel"
    manager = vertex_cache_manager.VertexCacheManager("project-id")
    manager._cache_registry["cache-key"] = {"name": "cachedContents/cache-name"}
    monkeypatch.setattr(manager, "_get_access_token", lambda: "access-token")
    monkeypatch.setattr(
        vertex_cache_manager.requests,
        "delete",
        MagicMock(return_value=SimpleNamespace(status_code=500, text=sentinel)),
    )

    with capture_logs() as events:
        assert manager.delete_cache("cache-key") is False

    event = next(event for event in events if event["event"] == "Failed to delete cache")
    assert event["status_code"] == 500
    assert all(sentinel not in repr(event) for event in events)
