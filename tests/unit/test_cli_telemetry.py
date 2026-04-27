from __future__ import annotations

from unittest.mock import Mock, patch

from skyvern import analytics
from skyvern.cli.core import telemetry


def test_capture_cli_tool_call_uses_mcp_posthog_project() -> None:
    fake_capture = Mock()

    with patch.object(analytics, "capture", fake_capture):
        telemetry.capture_cli_tool_call("skyvern_navigate", ok=True)

    fake_capture.assert_called_once()
    event_name = fake_capture.call_args.args[0]
    kwargs = fake_capture.call_args.kwargs
    assert event_name == "mcp_tool_call"
    assert kwargs["data"]["tool"] == "skyvern_navigate"
    assert kwargs["data"]["ok"] is True
    assert kwargs["data"]["operation"] == "cli/call"
    assert kwargs["data"]["runtime_mode"] == "cli"
    assert kwargs["data"]["distinct_id_source"] == "analytics_id"
    assert kwargs["distinct_id"] == analytics.settings.ANALYTICS_ID
    assert kwargs["api_key"] == analytics.settings.MCP_POSTHOG_PROJECT_API_KEY
    assert kwargs["host"] == analytics.settings.MCP_POSTHOG_PROJECT_HOST


def test_capture_cli_tool_call_error_omits_error_message() -> None:
    fake_capture = Mock()

    with patch.object(analytics, "capture", fake_capture):
        telemetry.capture_cli_tool_call("skyvern_click", ok=False, error=ValueError("do not leak this"))

    payload = fake_capture.call_args.kwargs["data"]
    assert payload["error_type"] == "ValueError"
    assert "error_message" not in payload


def test_flush_cli_telemetry_uses_mcp_posthog_project() -> None:
    fake_flush = Mock()

    with patch.object(analytics, "flush", fake_flush):
        telemetry.flush_cli_telemetry()

    fake_flush.assert_called_once_with(
        api_key=analytics.settings.MCP_POSTHOG_PROJECT_API_KEY,
        host=analytics.settings.MCP_POSTHOG_PROJECT_HOST,
    )


def test_register_cli_telemetry_flush_is_idempotent() -> None:
    fake_register = Mock()
    original = telemetry._flush_registered

    try:
        with patch("skyvern.cli.core.telemetry.atexit.register", fake_register):
            telemetry._flush_registered = False
            telemetry.register_cli_telemetry_flush()
            telemetry.register_cli_telemetry_flush()

        fake_register.assert_called_once_with(telemetry.flush_cli_telemetry)
    finally:
        telemetry._flush_registered = original
