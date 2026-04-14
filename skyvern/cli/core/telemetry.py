from __future__ import annotations

import atexit
from contextlib import suppress
from typing import Any

from skyvern import analytics

_flush_registered = False


def capture_cli_tool_call(
    tool_name: str,
    *,
    ok: bool,
    error: Exception | None = None,
) -> None:
    """Capture a direct CLI tool invocation in the MCP PostHog project."""
    data: dict[str, Any] = {
        **analytics.analytics_metadata(),
        "operation": "cli/call",
        "ok": ok,
        "runtime_mode": "cli",
        "transport": None,
        "is_http": False,
        "request_path": None,
        "request_method": None,
        "organization_id": None,
        "distinct_id_source": "analytics_id",
        "request_id": None,
        "session_id": None,
        "client_id": None,
        "tool": tool_name,
    }
    if error is not None:
        data["error_type"] = type(error).__name__

    analytics.capture(
        "mcp_tool_call",
        data=data,
        distinct_id=analytics.settings.ANALYTICS_ID,
        api_key=analytics.settings.MCP_POSTHOG_PROJECT_API_KEY,
        host=analytics.settings.MCP_POSTHOG_PROJECT_HOST,
    )


def flush_cli_telemetry() -> None:
    """Flush CLI telemetry best-effort on process exit."""
    with suppress(Exception):
        analytics.flush(
            api_key=analytics.settings.MCP_POSTHOG_PROJECT_API_KEY,
            host=analytics.settings.MCP_POSTHOG_PROJECT_HOST,
        )


def register_cli_telemetry_flush() -> None:
    global _flush_registered
    if _flush_registered:
        return
    atexit.register(flush_cli_telemetry)
    _flush_registered = True
