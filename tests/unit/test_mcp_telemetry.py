from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastmcp import Client
from fastmcp.server.middleware import MiddlewareContext

from skyvern import analytics
from skyvern.cli.core.perception_telemetry import (
    PerceptionSnapshotCategory,
    track_perception_probe,
    track_perception_snapshot,
)
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
async def test_mcp_tool_call_reads_ok_from_structured_content() -> None:
    """A ToolResult reporting failure via structured_content (no `data`) is recorded ok=False.

    The argument-validation middleware short-circuits with exactly this shape.
    """
    events: list[tuple[str, dict | None, str | None, str | None, str | None]] = []

    def fake_capture(
        event: str,
        data: dict | None = None,
        distinct_id: str | None = None,
        api_key: str | None = None,
        host: str | None = None,
    ) -> None:
        events.append((event, data, distinct_id, api_key, host))

    context = MiddlewareContext(message=SimpleNamespace(name="skyvern_get_errors"), fastmcp_context=None)
    response = SimpleNamespace(structured_content={"ok": False, "error": {"code": "INVALID_INPUT"}}, content=[])

    async def call_next(_context: MiddlewareContext[object]) -> object:
        return response

    with patch.object(analytics, "capture", side_effect=fake_capture):
        await MCPTelemetryMiddleware().on_call_tool(context, call_next)

    assert len(events) == 1
    _, payload, _, _, _ = events[0]
    assert payload is not None
    assert payload["ok"] is False


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
async def test_cancelled_perception_snapshot_emits_once_and_restores_context() -> None:
    events: list[tuple[str, dict]] = []
    cancelled_context = MiddlewareContext(
        message=SimpleNamespace(name="skyvern_observe"),
        fastmcp_context=None,
    )

    async def cancelled_call(_context: MiddlewareContext[object]) -> object:
        async with track_perception_snapshot("stale_ref_refresh"):
            async with track_perception_probe(evaluate_page_scan=True):
                raise asyncio.CancelledError

    following_context = MiddlewareContext(message=SimpleNamespace(name="skyvern_click"), fastmcp_context=None)

    async def following_call(_context: MiddlewareContext[object]) -> object:
        return SimpleNamespace(is_error=False, data={"ok": True}, content=[])

    def capture(event: str, *, data: dict, **_kwargs: object) -> None:
        events.append((event, data))

    clock = iter([0.0, 1.0, 3.0, 4.0, 5.0, 6.0])
    with (
        patch("skyvern.cli.mcp_tools.telemetry.time.perf_counter", side_effect=lambda: next(clock)),
        patch.object(analytics, "capture", side_effect=capture),
    ):
        with pytest.raises(asyncio.CancelledError):
            await MCPTelemetryMiddleware().on_call_tool(cancelled_context, cancelled_call)
        await MCPTelemetryMiddleware().on_call_tool(following_context, following_call)

    assert len(events) == 2
    cancelled_event_name, cancelled_payload = events[0]
    assert cancelled_event_name == "mcp_tool_call"
    assert cancelled_payload["ok"] is False
    assert cancelled_payload["error_type"] == "CancelledError"
    assert cancelled_payload["top_level_mcp_calls"] == 1
    assert cancelled_payload["perception_snapshots"] == 1
    assert cancelled_payload["model_visible_observe_results"] == 0
    assert cancelled_payload["automatic_observe_snapshots"] == 0
    assert cancelled_payload["stale_ref_refresh_snapshots"] == 1
    assert (
        cancelled_payload["model_visible_observe_results"]
        + cancelled_payload["automatic_observe_snapshots"]
        + cancelled_payload["stale_ref_refresh_snapshots"]
        == cancelled_payload["perception_snapshots"]
    )
    assert cancelled_payload["failed_perception_probes"] == 1
    assert cancelled_payload["evaluate_page_scans"] == 1
    assert cancelled_payload["browser_perception_wall_ms"] == 2000
    assert isinstance(cancelled_payload["browser_perception_wall_ms"], int)
    assert "response_bytes" not in cancelled_payload

    following_event_name, following_payload = events[1]
    assert following_event_name == "mcp_tool_call"
    assert following_payload["ok"] is True
    assert following_payload["top_level_mcp_calls"] == 1
    assert following_payload["perception_snapshots"] == 0
    assert following_payload["failed_perception_probes"] == 0
    assert following_payload["evaluate_page_scans"] == 0
    assert following_payload["browser_perception_wall_ms"] == 0


@pytest.mark.asyncio
async def test_cancelled_tool_call_propagates_when_telemetry_fails() -> None:
    context = MiddlewareContext(message=SimpleNamespace(name="skyvern_observe"), fastmcp_context=None)
    cancellation = asyncio.CancelledError()

    async def call_next(_context: MiddlewareContext[object]) -> object:
        async with track_perception_snapshot("automatic"):
            raise cancellation

    with (
        patch.object(analytics, "capture", side_effect=RuntimeError("telemetry down")),
        pytest.raises(asyncio.CancelledError) as exc_info,
    ):
        await MCPTelemetryMiddleware().on_call_tool(context, call_next)

    assert exc_info.value is cancellation


@pytest.mark.asyncio
async def test_perception_counters_emit_on_exception() -> None:
    events: list[dict] = []
    context = MiddlewareContext(message=SimpleNamespace(name="skyvern_observe"), fastmcp_context=None)

    async def call_next(_context: MiddlewareContext[object]) -> object:
        async with track_perception_snapshot("model_visible"):
            raise RuntimeError("observe failed")

    with (
        patch.object(analytics, "capture", side_effect=lambda _event, *, data, **_kwargs: events.append(data)),
        pytest.raises(RuntimeError, match="observe failed"),
    ):
        await MCPTelemetryMiddleware().on_call_tool(context, call_next)

    assert events[0]["ok"] is False
    assert events[0]["perception_snapshots"] == 1
    assert events[0]["model_visible_observe_results"] == 1
    assert events[0]["automatic_observe_snapshots"] == 0
    assert events[0]["stale_ref_refresh_snapshots"] == 0
    assert events[0]["failed_perception_probes"] == 1
    assert isinstance(events[0]["browser_perception_wall_ms"], int)


@pytest.mark.asyncio
async def test_swallowed_evaluate_scan_failure_is_accounted() -> None:
    from skyvern.cli.core.browser_ops import _get_dom_observe_elements

    events: list[dict] = []
    context = MiddlewareContext(message=SimpleNamespace(name="skyvern_observe"), fastmcp_context=None)
    page = SimpleNamespace(evaluate=AsyncMock(side_effect=RuntimeError("scan failed")))

    async def call_next(_context: MiddlewareContext[object]) -> object:
        assert await _get_dom_observe_elements(page) == []
        return SimpleNamespace(is_error=False, data={"ok": True}, content=[])

    with patch.object(analytics, "capture", side_effect=lambda _event, *, data, **_kwargs: events.append(data)):
        await MCPTelemetryMiddleware().on_call_tool(context, call_next)

    assert events[0]["perception_snapshots"] == 0
    assert events[0]["evaluate_page_scans"] == 1
    assert events[0]["failed_perception_probes"] == 1
    assert isinstance(events[0]["browser_perception_wall_ms"], int)


@pytest.mark.asyncio
async def test_document_id_cdp_failures_and_fallback_are_accounted() -> None:
    from skyvern.cli.core.browser_ops import get_observe_document_id

    events: list[dict] = []
    context = MiddlewareContext(message=SimpleNamespace(name="skyvern_observe"), fastmcp_context=None)
    cached_cdp = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("stale CDP session")))
    new_cdp_session = AsyncMock(side_effect=RuntimeError("CDP attach failed"))
    raw_page = SimpleNamespace(
        context=SimpleNamespace(new_cdp_session=new_cdp_session),
        _skyvern_observe_cdp_session=cached_cdp,
    )
    page = SimpleNamespace(
        page=raw_page,
        _working_frame=None,
        evaluate=AsyncMock(return_value="doc-1"),
    )

    async def call_next(_context: MiddlewareContext[object]) -> object:
        assert await get_observe_document_id(page) == "page:doc-1"
        return SimpleNamespace(is_error=False, data={"ok": True}, content=[])

    clock = iter([0.0, 1.0, 2.0, 3.0, 5.0, 6.0, 9.0, 10.0])
    with (
        patch("skyvern.cli.mcp_tools.telemetry.time.perf_counter", side_effect=lambda: next(clock)),
        patch.object(analytics, "capture", side_effect=lambda _event, *, data, **_kwargs: events.append(data)),
    ):
        await MCPTelemetryMiddleware().on_call_tool(context, call_next)

    cached_cdp.send.assert_awaited_once_with("Page.getFrameTree")
    new_cdp_session.assert_awaited_once_with(raw_page)
    page.evaluate.assert_awaited_once()
    assert events[0]["perception_snapshots"] == 0
    assert events[0]["failed_perception_probes"] == 2
    assert events[0]["evaluate_page_scans"] == 0
    assert events[0]["browser_perception_wall_ms"] == 6000


@pytest.mark.asyncio
async def test_nested_evaluate_scan_does_not_double_count_snapshot_wall_time() -> None:
    events: list[dict] = []
    context = MiddlewareContext(message=SimpleNamespace(name="skyvern_observe"), fastmcp_context=None)

    async def call_next(_context: MiddlewareContext[object]) -> object:
        async with track_perception_snapshot("model_visible"):
            async with track_perception_probe(evaluate_page_scan=True):
                pass
        return SimpleNamespace(is_error=False, data={"ok": True}, content=[])

    clock = iter([0.0, 0.001, 0.008, 0.010])
    with (
        patch("skyvern.cli.mcp_tools.telemetry.time.perf_counter", side_effect=lambda: next(clock)),
        patch.object(analytics, "capture", side_effect=lambda _event, *, data, **_kwargs: events.append(data)),
    ):
        await MCPTelemetryMiddleware().on_call_tool(context, call_next)

    assert events[0]["perception_snapshots"] == 1
    assert events[0]["evaluate_page_scans"] == 1
    assert events[0]["browser_perception_wall_ms"] == 7


@pytest.mark.asyncio
async def test_perception_counters_are_isolated_across_concurrent_calls() -> None:
    events: list[dict] = []
    both_started = asyncio.Event()
    started = 0

    async def run(tool: str, category: PerceptionSnapshotCategory) -> None:
        context = MiddlewareContext(message=SimpleNamespace(name=tool), fastmcp_context=None)

        async def call_next(_context: MiddlewareContext[object]) -> object:
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await both_started.wait()
            async with track_perception_snapshot(category):
                if category == "automatic":
                    async with track_perception_probe(evaluate_page_scan=True):
                        pass
            return SimpleNamespace(is_error=False, data={"ok": True}, content=[])

        await MCPTelemetryMiddleware().on_call_tool(context, call_next)

    with patch.object(analytics, "capture", side_effect=lambda _event, *, data, **_kwargs: events.append(data)):
        await asyncio.gather(run("explicit", "model_visible"), run("bundled", "automatic"))

    by_tool = {event["tool"]: event for event in events}
    assert by_tool["explicit"]["top_level_mcp_calls"] == 1
    assert by_tool["explicit"]["perception_snapshots"] == 1
    assert by_tool["explicit"]["model_visible_observe_results"] == 1
    assert by_tool["explicit"]["automatic_observe_snapshots"] == 0
    assert by_tool["explicit"]["evaluate_page_scans"] == 0
    assert by_tool["bundled"]["top_level_mcp_calls"] == 1
    assert by_tool["bundled"]["perception_snapshots"] == 1
    assert by_tool["bundled"]["model_visible_observe_results"] == 0
    assert by_tool["bundled"]["automatic_observe_snapshots"] == 1
    assert by_tool["bundled"]["evaluate_page_scans"] == 1


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
    initialize_payloads = [
        payload
        for event, payload, _, _, _ in events
        if event == "mcp_request" and payload and payload["operation"] == "initialize"
    ]
    assert len(initialize_payloads) == 1
    # The in-memory fastmcp Client sends real clientInfo, so this exercises the full dispatch path.
    assert isinstance(initialize_payloads[0]["client_name"], str)
    assert initialize_payloads[0]["client_name"] not in ("", "unknown")
    assert isinstance(initialize_payloads[0]["client_version"], str)
    assert any(
        event == "mcp_request" and payload and payload["operation"] == "tools/list"
        for event, payload, _, _, _ in events
    )


def _initialize_context(params: object) -> MiddlewareContext:
    return MiddlewareContext(message=SimpleNamespace(params=params), fastmcp_context=None)


async def _run_initialize(context: MiddlewareContext) -> dict:
    payloads: list[dict] = []

    def fake_capture(event: str, data: dict | None = None, **_: object) -> None:
        payloads.append(data or {})

    async def call_next(_context: MiddlewareContext[object]) -> object:
        return SimpleNamespace()

    with patch.object(analytics, "capture", side_effect=fake_capture):
        await MCPTelemetryMiddleware().on_initialize(context, call_next)

    assert len(payloads) == 1
    return payloads[0]


@pytest.mark.asyncio
async def test_initialize_captures_client_info() -> None:
    context = _initialize_context(SimpleNamespace(clientInfo=SimpleNamespace(name="hermes-agent", version="1.2.3")))

    with patch("skyvern.cli.mcp_tools.telemetry.LOG") as log_mock:
        payload = await _run_initialize(context)

    assert payload["client_name"] == "hermes-agent"
    assert payload["client_version"] == "1.2.3"
    log_kwargs = log_mock.info.call_args.kwargs
    assert log_kwargs["mcp_client_name"] == "hermes-agent"
    assert log_kwargs["mcp_client_version"] == "1.2.3"


@pytest.mark.asyncio
async def test_initialize_runs_boot_ready_callback_after_success() -> None:
    events: list[str] = []
    context = _initialize_context(SimpleNamespace())

    async def call_next(_context: MiddlewareContext[object]) -> object:
        events.append("initialize")
        return SimpleNamespace()

    configure_mcp_telemetry_runtime(
        server_mode="local_cli",
        transport="stdio",
        boot_ready_callback=lambda: events.append("ready"),
    )

    await MCPTelemetryMiddleware().on_initialize(context, call_next)

    assert events == ["initialize", "ready"]


@pytest.mark.asyncio
async def test_initialize_does_not_run_boot_ready_callback_on_failure() -> None:
    ready = Mock()
    context = _initialize_context(SimpleNamespace())

    async def fail(_context: MiddlewareContext[object]) -> object:
        raise RuntimeError("initialize failed")

    configure_mcp_telemetry_runtime(
        server_mode="local_cli",
        transport="stdio",
        boot_ready_callback=ready,
    )

    with pytest.raises(RuntimeError, match="initialize failed"):
        await MCPTelemetryMiddleware().on_initialize(context, fail)

    ready.assert_not_called()


@pytest.mark.asyncio
async def test_initialize_missing_client_info_defaults_to_unknown() -> None:
    payload = await _run_initialize(_initialize_context(SimpleNamespace()))

    assert payload["client_name"] == "unknown"
    assert payload["client_version"] == "unknown"


@pytest.mark.asyncio
async def test_initialize_malformed_client_info_defaults_to_unknown() -> None:
    context = _initialize_context(SimpleNamespace(clientInfo=SimpleNamespace(name=123, version=["4.5"])))

    payload = await _run_initialize(context)

    assert payload["client_name"] == "unknown"
    assert payload["client_version"] == "unknown"


@pytest.mark.asyncio
async def test_initialize_truncates_oversized_client_info() -> None:
    context = _initialize_context(SimpleNamespace(clientInfo=SimpleNamespace(name="x" * 10_000, version="1.0")))

    payload = await _run_initialize(context)

    assert payload["client_name"].endswith("... [truncated]")
    assert len(payload["client_name"]) < 250
    assert payload["client_version"] == "1.0"


@pytest.mark.asyncio
async def test_initialize_escapes_newlines_in_client_info() -> None:
    context = _initialize_context(
        SimpleNamespace(clientInfo=SimpleNamespace(name="evil\nclient", version="1.0\r\n2.0"))
    )

    payload = await _run_initialize(context)

    assert payload["client_name"] == "evil\\nclient"
    assert payload["client_version"] == "1.0\\r\\n2.0"


@pytest.mark.asyncio
async def test_initialize_survives_log_failure() -> None:
    context = _initialize_context(SimpleNamespace(clientInfo=SimpleNamespace(name="hermes-agent", version="1.2.3")))
    payloads: list[dict] = []

    def fake_capture(event: str, data: dict | None = None, **_: object) -> None:
        payloads.append(data or {})

    async def call_next(_context: MiddlewareContext[object]) -> object:
        return SimpleNamespace()

    with (
        patch("skyvern.cli.mcp_tools.telemetry.LOG.info", side_effect=RuntimeError("logger down")),
        patch.object(analytics, "capture", side_effect=fake_capture),
    ):
        result = await MCPTelemetryMiddleware().on_initialize(context, call_next)

    assert result is not None
    assert len(payloads) == 1
    assert payloads[0]["ok"] is True
    assert payloads[0]["client_name"] == "hermes-agent"


@pytest.mark.asyncio
async def test_tool_call_resolves_client_info_from_session() -> None:
    payloads: list[dict] = []

    def fake_capture(event: str, data: dict | None = None, **_: object) -> None:
        payloads.append(data or {})

    fastmcp_context = SimpleNamespace(
        request_id="req-1",
        session_id="sess-1",
        client_id=None,
        session=SimpleNamespace(
            client_params=SimpleNamespace(clientInfo=SimpleNamespace(name="cursor", version="0.4"))
        ),
    )
    context = MiddlewareContext(message=SimpleNamespace(name="skyvern_block_schema"), fastmcp_context=fastmcp_context)
    response = SimpleNamespace(is_error=False, data={"ok": True}, content=[])

    async def call_next(_context: MiddlewareContext[object]) -> object:
        return response

    with patch.object(analytics, "capture", side_effect=fake_capture):
        await MCPTelemetryMiddleware().on_call_tool(context, call_next)

    assert len(payloads) == 1
    assert payloads[0]["client_name"] == "cursor"
    assert payloads[0]["client_version"] == "0.4"


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
