import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import CallToolResult
from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot import mcp_adapter
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.mcp_adapter import (
    SchemaOverlay,
    SkyvernOverlayMCPServer,
    _requested_output_path_choices,
)
from skyvern.forge.sdk.copilot.runtime import AgentContext, mcp_to_copilot
from skyvern.forge.sdk.copilot.tools.mcp_hooks import _build_skyvern_mcp_overlays, get_skyvern_mcp_alias_map
from tests.unit.copilot_test_helpers import make_copilot_ctx
from tests.unit.test_copilot_secret_scrub import _make_server


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "expression": {"type": "string"},
            "output_path": {"type": "string", "description": "The requested output this read fills."},
        },
        "required": ["expression"],
    }


class TestRequestedOutputPathChoices:
    def test_the_turn_s_requested_paths_become_the_choices(self) -> None:
        # Live shape (SKY-13226): a free-form path let the model name its own purpose, so the read
        # that saw the requested quantity was filed as exploration and never witnessed the path.
        schema = _requested_output_path_choices(_schema(), ["output.azure_error_count"])

        assert schema["properties"]["output_path"]["enum"] == ["output.azure_error_count"]
        assert "output.azure_error_count" in schema["properties"]["output_path"]["description"]

    def test_exploration_still_passes_by_omitting_the_path(self) -> None:
        schema = _requested_output_path_choices(_schema(), ["output.azure_error_count"])

        assert "output_path" not in schema["required"]

    def test_every_requested_path_stays_available_for_a_reread(self) -> None:
        schema = _requested_output_path_choices(_schema(), ["output.a", "output.b"])

        assert schema["properties"]["output_path"]["enum"] == ["output.a", "output.b"]

    def test_a_turn_owing_no_output_leaves_the_schema_alone(self) -> None:
        assert _requested_output_path_choices(_schema(), []) == _schema()


def test_the_declared_path_says_the_expression_is_that_value() -> None:
    overlays = _build_skyvern_mcp_overlays(BlockAuthoringPolicy.CODE_ONLY_BROWSER)
    description = overlays["evaluate"].copilot_params["output_path"]["description"]

    assert "evaluates to that one value" in description
    assert "exploration" in description


_TIMING_EVENT = "MCP tool timing"
_PAYLOAD_KEYS_THAT_WOULD_LEAK = {"args", "merged_args", "arguments", "mcp_args", "data", "raw_mcp", "result"}
_BROWSER_BOOT_SECONDS = 2.0
_MCP_CALL_SECONDS = 3.0
_AFTER_CALL_SECONDS = 5.0
_WALL_MS = 5000
_SESSION_ONLY_MS = 2000


@pytest.fixture
def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    now = [100.0]
    monkeypatch.setattr(mcp_adapter, "time", SimpleNamespace(monotonic=lambda: now[0]))
    yield now


@pytest.fixture
def _stub_browser_session(monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]) -> Iterator[None]:
    async def _no_error(_ctx: AgentContext) -> None:
        _fake_clock[0] += _BROWSER_BOOT_SECONDS
        return None

    @asynccontextmanager
    async def _no_session_scope(_ctx: AgentContext) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _no_error)
    monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _no_session_scope)
    yield


def _timing_records(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in captured if record.get("event") == _TIMING_EVENT]


async def _call(server: SkyvernOverlayMCPServer, call_path: str) -> CallToolResult | dict[str, Any]:
    if call_path == "model":
        return await server.call_tool("evaluate", {"expression": "scan()"})
    return await server.call_internal_tool("skyvern_evaluate", {"expression": "scan()"})


def _surfaced_error(result: CallToolResult | dict[str, Any], call_path: str) -> str:
    if call_path == "model":
        assert isinstance(result, CallToolResult)
        return result.content[0].text
    assert isinstance(result, dict)
    return str(result.get("error"))


def _server_whose_call_takes_time(
    payload: dict[str, Any] | Exception,
    overlay: SchemaOverlay,
    clock: list[float],
    alias_map: dict[str, str] | None = None,
    is_error: bool = False,
) -> SkyvernOverlayMCPServer:
    def _advance() -> None:
        clock[0] += _MCP_CALL_SECONDS

    return _make_server(
        make_copilot_ctx(browser_session_id="pbs_1"),
        payload,
        overlay,
        alias_map=alias_map,
        on_call=_advance,
        is_error=is_error,
    )


@pytest.mark.usefixtures("_stub_browser_session")
class TestMCPToolTiming:
    @pytest.mark.asyncio
    async def test_internal_call_logs_the_server_total_without_changing_the_result(
        self, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        payload = {"ok": True, "data": {"x": 1}, "timing_ms": {"sdk": 812, "total": 815}}
        server = _server_whose_call_takes_time(payload, SchemaOverlay(), _fake_clock)
        server._evidence_candidate_origin = "https://public.test"

        async def _slow_drain() -> None:
            _fake_clock[0] += _AFTER_CALL_SECONDS

        monkeypatch.setattr(server, "_drain_evidence_candidate_response_tasks", _slow_drain)

        with capture_logs() as captured:
            result = await server.call_internal_tool("skyvern_evaluate", {"expression": "scan()"})

        records = _timing_records(captured)
        assert len(records) == 1
        record = records[0]
        assert record["tool_name"] == "skyvern_evaluate"
        assert record["mcp_tool_name"] == "skyvern_evaluate"
        assert record["call_path"] == "internal"
        assert record["server_timing_ms"] == 815
        assert record["wall_clock_ms"] == _WALL_MS
        assert set(record) & _PAYLOAD_KEYS_THAT_WOULD_LEAK == set()
        assert record["workflow_permanent_id"] == "wfp-1"
        assert "turn_id" in record
        assert "workflow_copilot_chat_id" in record

        assert result == mcp_to_copilot(payload)
        assert "timing_ms" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("timing_ms", [None, {}, "815ms", {"total": "815"}, {"total": True}])
    async def test_a_result_without_a_server_timing_dict_still_logs(
        self, timing_ms: str | dict[str, str | bool] | None, _fake_clock: list[float]
    ) -> None:
        payload: dict[str, Any] = {"ok": True, "data": {"x": 1}}
        if timing_ms is not None:
            payload["timing_ms"] = timing_ms
        server = _server_whose_call_takes_time(payload, SchemaOverlay(), _fake_clock)

        with capture_logs() as captured:
            await server.call_internal_tool("skyvern_evaluate", {"expression": "scan()"})

        records = _timing_records(captured)
        assert len(records) == 1
        assert records[0]["server_timing_ms"] is None
        assert records[0]["tool_name"] == "skyvern_evaluate"
        assert records[0]["mcp_tool_name"] == "skyvern_evaluate"
        assert records[0]["call_path"] == "internal"
        assert records[0]["wall_clock_ms"] == _WALL_MS

    @pytest.mark.asyncio
    async def test_model_facing_call_logs_both_names_and_excludes_the_post_hook(self, _fake_clock: list[float]) -> None:
        payload = {"ok": True, "data": {"result": 1}, "timing_ms": {"sdk": 812, "total": 815}}

        async def _slow_post_hook(
            copilot_result: dict[str, Any], raw_mcp: dict[str, Any], ctx: AgentContext
        ) -> dict[str, Any]:
            _fake_clock[0] += _AFTER_CALL_SECONDS
            return copilot_result

        overlay = SchemaOverlay(requires_browser=True, post_hook=_slow_post_hook)
        server = _server_whose_call_takes_time(payload, overlay, _fake_clock, alias_map=get_skyvern_mcp_alias_map())

        with capture_logs() as captured:
            result = await server.call_tool("evaluate", {"expression": "scan()"})

        records = _timing_records(captured)
        assert len(records) == 1
        assert records[0]["tool_name"] == "evaluate"
        assert records[0]["mcp_tool_name"] == "skyvern_evaluate"
        assert records[0]["call_path"] == "model"
        assert records[0]["server_timing_ms"] == 815
        assert records[0]["wall_clock_ms"] == _WALL_MS
        assert records[0]["call_status"] == "ok"
        assert "timing_ms" not in result.content[0].text

    @pytest.mark.asyncio
    async def test_a_call_that_exceeds_its_ceiling_reports_its_wall_time(self, _fake_clock: list[float]) -> None:
        overlay = SchemaOverlay(requires_browser=True, timeout=30)
        server = _server_whose_call_takes_time(TimeoutError(), overlay, _fake_clock)

        with capture_logs() as captured:
            await server.call_tool("evaluate", {"expression": "scan()"})

        records = _timing_records(captured)
        assert len(records) == 1
        assert records[0]["call_status"] == "timeout"
        assert records[0]["server_timing_ms"] is None
        assert records[0]["wall_clock_ms"] == _WALL_MS

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            {"ok": True, "data": {"x": 1}, "timing_ms": {"sdk": 812, "total": 815}},
            RuntimeError("mcp exploded"),
        ],
        ids=["ok", "error"],
    )
    async def test_an_internal_call_reports_the_model_facing_name_so_one_tool_is_one_facet(
        self, payload: dict[str, Any] | Exception, _fake_clock: list[float]
    ) -> None:
        server = _server_whose_call_takes_time(
            payload, SchemaOverlay(), _fake_clock, alias_map={"evaluate": "skyvern_evaluate"}
        )

        with capture_logs() as captured:
            await server.call_internal_tool("skyvern_evaluate", {"expression": "scan()"})

        records = _timing_records(captured)
        assert len(records) == 1
        assert records[0]["tool_name"] == "evaluate"
        assert records[0]["mcp_tool_name"] == "skyvern_evaluate"
        assert records[0]["call_path"] == "internal"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_path", ["model", "internal"])
    async def test_a_call_cancelled_while_resolving_its_session_still_reports_the_budget_it_spent(
        self, call_path: str, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        async def _cancelled(_ctx: AgentContext) -> None:
            _fake_clock[0] += _BROWSER_BOOT_SECONDS
            raise asyncio.CancelledError

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _cancelled)
        server = _server_whose_call_takes_time({"ok": True}, SchemaOverlay(requires_browser=True), _fake_clock)

        with capture_logs() as captured:
            with pytest.raises(asyncio.CancelledError):
                await _call(server, call_path)

        records = _timing_records(captured)
        assert len(records) == 1
        assert records[0]["call_status"] == "cancelled"
        assert records[0]["call_path"] == call_path
        assert records[0]["wall_clock_ms"] == _SESSION_ONLY_MS
        assert records[0]["server_timing_ms"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_path", ["model", "internal"])
    async def test_a_session_that_fails_to_resolve_still_reports_the_budget_it_spent(
        self, call_path: str, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        async def _session_error(_ctx: AgentContext) -> dict[str, Any]:
            _fake_clock[0] += _BROWSER_BOOT_SECONDS
            return {"ok": False, "error": "no browser session"}

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _session_error)
        server = _server_whose_call_takes_time({"ok": True}, SchemaOverlay(requires_browser=True), _fake_clock)

        with capture_logs() as captured:
            result = await _call(server, call_path)

        records = _timing_records(captured)
        assert len(records) == 1
        assert records[0]["call_status"] == "session_error"
        assert records[0]["call_path"] == call_path
        assert records[0]["wall_clock_ms"] == _SESSION_ONLY_MS
        assert records[0]["server_timing_ms"] is None
        assert "no browser session" in _surfaced_error(result, call_path)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_path", ["model", "internal"])
    async def test_a_session_that_raises_instead_of_returning_an_error_still_reports_its_budget(
        self, call_path: str, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        async def _session_raises(_ctx: AgentContext) -> None:
            _fake_clock[0] += _BROWSER_BOOT_SECONDS
            raise RuntimeError("adoption failed")

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _session_raises)
        server = _server_whose_call_takes_time({"ok": True}, SchemaOverlay(requires_browser=True), _fake_clock)

        with capture_logs() as captured:
            await _call(server, call_path)

        records = _timing_records(captured)
        assert len(records) == 1
        assert records[0]["call_status"] == "session_error"
        assert records[0]["call_path"] == call_path
        assert records[0]["wall_clock_ms"] == _SESSION_ONLY_MS

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_path", ["model", "internal"])
    async def test_a_tool_that_reports_failure_without_raising_is_not_recorded_as_ok(
        self, call_path: str, _fake_clock: list[float]
    ) -> None:
        payload = {"ok": False, "error": "selector not found", "timing_ms": {"total": 815}}
        server = _server_whose_call_takes_time(
            payload, SchemaOverlay(requires_browser=True), _fake_clock, alias_map=get_skyvern_mcp_alias_map()
        )

        with capture_logs() as captured:
            await _call(server, call_path)

        records = _timing_records(captured)
        assert len(records) == 1
        assert records[0]["call_status"] == "error"
        assert records[0]["server_timing_ms"] == 815

    @pytest.mark.asyncio
    async def test_an_internal_call_without_a_connected_client_still_leaves_a_record(
        self, _fake_clock: list[float]
    ) -> None:
        server = _server_whose_call_takes_time({"ok": True}, SchemaOverlay(), _fake_clock)
        server._client = None

        with capture_logs() as captured:
            await server.call_internal_tool("skyvern_evaluate", {"expression": "scan()"})

        records = _timing_records(captured)
        assert len(records) == 1
        assert records[0]["call_status"] == "not_connected"
        assert records[0]["call_path"] == "internal"
        assert records[0]["server_timing_ms"] is None
