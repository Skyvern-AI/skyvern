from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastmcp import Client
from fastmcp.server.middleware import MiddlewareContext

from skyvern import analytics
from skyvern.cli.mcp_tools import mcp
from skyvern.cli.mcp_tools.blocks import skyvern_block_schema
from skyvern.cli.mcp_tools.telemetry import (
    MCPTelemetryMiddleware,
    configure_mcp_telemetry_runtime,
    reset_mcp_telemetry_runtime,
)


def _expected_response_bytes(result: object) -> int:
    # Mirrors production telemetry semantics: count UTF-8 bytes for text content blocks only.
    return sum(
        len(content.text.encode("utf-8"))
        for content in (getattr(result, "content", None) or [])
        if isinstance(getattr(content, "text", None), str)
    )


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    reset_mcp_telemetry_runtime()
    yield
    reset_mcp_telemetry_runtime()


@pytest.mark.asyncio
async def test_mcp_tool_call_emits_telemetry() -> None:
    events: list[tuple[str, dict | None, str | None, str | None, str | None]] = []

    def fake_capture(
        event: str,
        data: dict | None = None,
        distinct_id: str | None = None,
        api_key: str | None = None,
        host: str | None = None,
    ) -> None:
        events.append((event, data, distinct_id, api_key, host))

    configure_mcp_telemetry_runtime(server_mode="local_cli", transport="stdio")

    with patch.object(analytics, "capture", side_effect=fake_capture):
        async with Client(mcp) as client:
            result = await client.call_tool("skyvern_block_schema", {})

    assert result.is_error is False
    tool_events = [event for event in events if event[0] == "mcp_tool_call"]
    assert len(tool_events) == 1

    _, payload, distinct_id, api_key, host = tool_events[0]
    assert payload is not None
    assert payload["operation"] == "tools/call"
    assert payload["tool"] == "skyvern_block_schema"
    assert payload["ok"] is True
    assert payload["runtime_mode"] == "local_cli"
    assert payload["transport"] == "stdio"
    assert isinstance(payload["duration_ms"], float)
    assert payload["duration_ms"] >= 0
    assert payload["response_bytes"] == _expected_response_bytes(result)
    assert payload["distinct_id_source"] == "analytics_id"
    assert distinct_id == analytics.settings.ANALYTICS_ID
    assert api_key == analytics.settings.MCP_POSTHOG_PROJECT_API_KEY
    assert host == analytics.settings.MCP_POSTHOG_PROJECT_HOST


@pytest.mark.asyncio
async def test_mcp_tool_call_records_text_response_bytes() -> None:
    events: list[tuple[str, dict | None, str | None, str | None, str | None]] = []

    def fake_capture(
        event: str,
        data: dict | None = None,
        distinct_id: str | None = None,
        api_key: str | None = None,
        host: str | None = None,
    ) -> None:
        events.append((event, data, distinct_id, api_key, host))

    context = MiddlewareContext(message=SimpleNamespace(name="skyvern_click"), fastmcp_context=None)
    response = SimpleNamespace(
        is_error=False,
        data={"ok": True},
        content=[SimpleNamespace(text="abc"), SimpleNamespace(text="\u00e9"), SimpleNamespace(data="ignored")],
    )

    async def call_next(_context: MiddlewareContext[object]) -> object:
        return response

    with patch.object(analytics, "capture", side_effect=fake_capture):
        await MCPTelemetryMiddleware().on_call_tool(context, call_next)

    assert len(events) == 1
    _, payload, _, _, _ = events[0]
    assert payload is not None
    assert payload["response_bytes"] == 5
    assert "abc" not in payload.values()
    assert "ignored" not in payload.values()


@pytest.mark.asyncio
async def test_mcp_tool_call_returns_result_when_success_telemetry_fails() -> None:
    context = MiddlewareContext(message=SimpleNamespace(name="skyvern_click"), fastmcp_context=None)
    response = SimpleNamespace(is_error=False, data={"ok": True}, content=[SimpleNamespace(text="ok")])

    async def call_next(_context: MiddlewareContext[object]) -> object:
        return response

    with patch.object(analytics, "capture", side_effect=RuntimeError("telemetry down")):
        result = await MCPTelemetryMiddleware().on_call_tool(context, call_next)

    assert result is response


@pytest.mark.asyncio
async def test_mcp_tool_call_marks_error_results_as_not_ok() -> None:
    events: list[tuple[str, dict | None, str | None, str | None, str | None]] = []

    def fake_capture(
        event: str,
        data: dict | None = None,
        distinct_id: str | None = None,
        api_key: str | None = None,
        host: str | None = None,
    ) -> None:
        events.append((event, data, distinct_id, api_key, host))

    context = MiddlewareContext(message=SimpleNamespace(name="skyvern_click"), fastmcp_context=None)
    response = SimpleNamespace(is_error=True, data={"ok": False}, content=[SimpleNamespace(text="bad")])

    async def call_next(_context: MiddlewareContext[object]) -> object:
        return response

    with patch.object(analytics, "capture", side_effect=fake_capture):
        await MCPTelemetryMiddleware().on_call_tool(context, call_next)

    assert len(events) == 1
    event_name, payload, _, _, _ = events[0]
    assert event_name == "mcp_tool_call"
    assert payload is not None
    assert payload["ok"] is False
    assert payload["tool"] == "skyvern_click"
    assert payload["response_bytes"] == 3
    assert isinstance(payload["duration_ms"], float)
    assert "error_type" not in payload
    assert "error_message" not in payload


@pytest.mark.asyncio
async def test_mcp_tool_call_exception_omits_error_message() -> None:
    events: list[tuple[str, dict | None, str | None, str | None, str | None]] = []

    def fake_capture(
        event: str,
        data: dict | None = None,
        distinct_id: str | None = None,
        api_key: str | None = None,
        host: str | None = None,
    ) -> None:
        events.append((event, data, distinct_id, api_key, host))

    context = MiddlewareContext(message=SimpleNamespace(name="skyvern_click"), fastmcp_context=None)

    async def call_next(_context: MiddlewareContext[object]) -> object:
        raise ValueError("sensitive input should not leave the process")

    with (
        patch.object(analytics, "capture", side_effect=fake_capture),
        pytest.raises(ValueError, match="sensitive input should not leave the process"),
    ):
        await MCPTelemetryMiddleware().on_call_tool(context, call_next)

    assert len(events) == 1
    event_name, payload, _, _, _ = events[0]
    assert event_name == "mcp_tool_call"
    assert payload is not None
    assert payload["ok"] is False
    assert payload["error_type"] == "ValueError"
    assert isinstance(payload["duration_ms"], float)
    assert payload["duration_ms"] >= 0
    assert "response_bytes" not in payload
    assert "error_message" not in payload


@pytest.mark.asyncio
async def test_mcp_tool_call_exception_preserves_original_error_when_telemetry_fails() -> None:
    context = MiddlewareContext(message=SimpleNamespace(name="skyvern_click"), fastmcp_context=None)

    async def call_next(_context: MiddlewareContext[object]) -> object:
        raise ValueError("original tool error")

    with (
        patch.object(analytics, "capture", side_effect=RuntimeError("telemetry down")),
        pytest.raises(ValueError, match="original tool error"),
    ):
        await MCPTelemetryMiddleware().on_call_tool(context, call_next)


@pytest.mark.asyncio
async def test_list_tools_emits_protocol_request_telemetry() -> None:
    events: list[tuple[str, dict | None, str | None, str | None, str | None]] = []

    def fake_capture(
        event: str,
        data: dict | None = None,
        distinct_id: str | None = None,
        api_key: str | None = None,
        host: str | None = None,
    ) -> None:
        events.append((event, data, distinct_id, api_key, host))

    with patch.object(analytics, "capture", side_effect=fake_capture):
        async with Client(mcp) as client:
            tools = await client.list_tools()

    assert tools
    assert any(
        event == "mcp_request" and payload and payload["operation"] == "initialize"
        for event, payload, _, _, _ in events
    )
    assert any(
        event == "mcp_request" and payload and payload["operation"] == "tools/list"
        for event, payload, _, _, _ in events
    )


@pytest.mark.asyncio
async def test_direct_tool_invocation_does_not_emit_mcp_telemetry() -> None:
    with patch.object(analytics, "capture") as capture_mock:
        result = await skyvern_block_schema()

    assert result["ok"] is True
    capture_mock.assert_not_called()


@pytest.mark.asyncio
async def test_http_request_uses_organization_id_for_distinct_id() -> None:
    events: list[tuple[str, dict | None, str | None, str | None, str | None]] = []

    def fake_capture(
        event: str,
        data: dict | None = None,
        distinct_id: str | None = None,
        api_key: str | None = None,
        host: str | None = None,
    ) -> None:
        events.append((event, data, distinct_id, api_key, host))

    request = SimpleNamespace(
        state=SimpleNamespace(organization_id="o_test123"),
        url=SimpleNamespace(path="/mcp"),
        method="POST",
    )
    context = MiddlewareContext(message=SimpleNamespace(name="skyvern_block_schema"), fastmcp_context=None)
    response = SimpleNamespace(is_error=False, data={"ok": True}, content=[SimpleNamespace(text="abc")])

    async def call_next(_context: MiddlewareContext[object]) -> object:
        return response

    configure_mcp_telemetry_runtime(server_mode="cloud_hosted", transport="streamable-http")
    with (
        patch("skyvern.cli.mcp_tools.telemetry.get_http_request", return_value=request),
        patch.object(analytics, "capture", side_effect=fake_capture),
    ):
        await MCPTelemetryMiddleware().on_call_tool(context, call_next)

    assert len(events) == 1
    event_name, payload, distinct_id, api_key, host = events[0]
    assert event_name == "mcp_tool_call"
    assert payload is not None
    assert payload["organization_id"] == "o_test123"
    assert payload["distinct_id_source"] == "organization_id"
    assert payload["request_path"] == "/mcp"
    assert payload["runtime_mode"] == "cloud_hosted"
    assert payload["transport"] == "streamable-http"
    assert isinstance(payload["duration_ms"], float)
    assert payload["response_bytes"] == 3
    assert distinct_id == "org:o_test123"
    assert api_key == analytics.settings.MCP_POSTHOG_PROJECT_API_KEY
    assert host == analytics.settings.MCP_POSTHOG_PROJECT_HOST


@pytest.mark.asyncio
async def test_mcp_tool_call_respects_global_telemetry_opt_out() -> None:
    fake_capture = Mock()
    fake_client = SimpleNamespace(capture=fake_capture)
    context = MiddlewareContext(message=SimpleNamespace(name="skyvern_click"), fastmcp_context=None)
    response = SimpleNamespace(is_error=False, data={"ok": True}, content=[SimpleNamespace(text="abc")])

    async def call_next(_context: MiddlewareContext[object]) -> object:
        return response

    with (
        patch.object(analytics.settings, "SKYVERN_TELEMETRY", False),
        patch.object(analytics, "_resolve_posthog_client", return_value=fake_client),
    ):
        await MCPTelemetryMiddleware().on_call_tool(context, call_next)

    fake_capture.assert_not_called()
