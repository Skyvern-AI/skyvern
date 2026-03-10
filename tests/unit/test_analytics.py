from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from skyvern import analytics


def test_capture_includes_dynamic_test_id() -> None:
    original_test_id = analytics.settings.ANALYTICS_TEST_ID
    analytics.settings.ANALYTICS_TEST_ID = "smoke-test-123"
    fake_capture = Mock()
    fake_client = SimpleNamespace(capture=fake_capture)

    try:
        with patch.object(analytics, "_resolve_posthog_client", return_value=fake_client):
            analytics.capture("mcp_request", data={"ok": True}, distinct_id="distinct-123")
    finally:
        analytics.settings.ANALYTICS_TEST_ID = original_test_id

    fake_capture.assert_called_once_with(
        distinct_id="distinct-123",
        event="mcp_request",
        properties={"analytics_test_id": "smoke-test-123", "ok": True},
    )


def test_reconfigure_posthog_client_uses_project_settings() -> None:
    original_api_key = analytics.settings.POSTHOG_PROJECT_API_KEY
    original_host = analytics.settings.POSTHOG_PROJECT_HOST

    analytics.settings.POSTHOG_PROJECT_API_KEY = "phc_test_project_key"
    analytics.settings.POSTHOG_PROJECT_HOST = "https://app.posthog.com"

    try:
        analytics.reconfigure_posthog_client()
        assert analytics.posthog.api_key == "phc_test_project_key"
    finally:
        analytics.settings.POSTHOG_PROJECT_API_KEY = original_api_key
        analytics.settings.POSTHOG_PROJECT_HOST = original_host
        analytics.reconfigure_posthog_client()


def test_capture_can_use_custom_posthog_client() -> None:
    fake_capture = Mock()
    fake_client = SimpleNamespace(capture=fake_capture)

    with patch.object(analytics, "_build_posthog_client", return_value=fake_client):
        analytics._custom_posthog_clients.clear()
        analytics.capture(
            "mcp_tool_call",
            data={"ok": True},
            distinct_id="distinct-123",
            api_key="phc_mcp_project",
            host="https://app.posthog.com",
        )

    fake_capture.assert_called_once_with(
        distinct_id="distinct-123",
        event="mcp_tool_call",
        properties={"ok": True},
    )
