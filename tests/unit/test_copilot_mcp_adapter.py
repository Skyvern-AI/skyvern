import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from mcp.types import CallToolResult
from structlog.testing import capture_logs

from skyvern.forge.sdk.cache.base import NoopLock
from skyvern.forge.sdk.cache.local import LocalCache
from skyvern.forge.sdk.copilot import mcp_adapter
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.mcp_adapter import (
    SchemaOverlay,
    SkyvernOverlayMCPServer,
    _BrowserCallOutcome,
    _handle_browser_session_loss,
    _requested_output_path_choices,
)
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    CopilotBrowserLivenessUndetermined,
    CopilotBrowserSessionUnavailable,
    mcp_to_copilot,
)
from skyvern.forge.sdk.copilot.tools.mcp_hooks import _build_skyvern_mcp_overlays, get_skyvern_mcp_alias_map
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin
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
_AFTER_CALL_MS = 5000
_WALL_MS = 5000
_SESSION_ONLY_MS = 2000
_MCP_CALL_MS = 3000
_CONTEXT_ENTER_SECONDS = 4.0
_CONTEXT_EXIT_SECONDS = 1.0
_CONTEXT_ENTER_MS = 4000
_CONTEXT_EXIT_MS = 1000
_GAP_WALL_MS = 10000
_AN_HOUR_SECONDS = 3600.0
_AN_HOUR_MS = 3_600_000
_PHASE_KEYS = (
    "phase_session_prepare_ms",
    "phase_context_enter_ms",
    "phase_dispatch_ms",
    "phase_context_exit_ms",
    "phase_residual_ms",
)


def _assert_every_millisecond_is_attributed(record: dict[str, Any]) -> None:
    """The residual is the remainder, so its bound is what proves the segments were charged correctly.

    Summing the segments and the residual is an identity that holds for any values whatsoever.
    """
    assert sum(record[key] for key in _PHASE_KEYS) == record["wall_clock_ms"]
    assert 0 <= record["phase_residual_ms"] < len(mcp_adapter._MCP_CALL_SEGMENTS)


class _SharedLocalCache(LocalCache):
    is_shared = True

    def __init__(self) -> None:
        super().__init__()
        self.lock_names: list[str] = []

    def get_lock(self, lock_name: str, blocking_timeout: int = 5, timeout: int = 10) -> NoopLock:
        self.lock_names.append(lock_name)
        return NoopLock(lock_name, blocking_timeout, timeout)


@pytest.fixture
def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    now = [100.0]
    monkeypatch.setattr(mcp_adapter, "time", SimpleNamespace(monotonic=lambda: now[0]))
    yield now


@pytest.fixture
def _stub_browser_session(monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]) -> Iterator[None]:
    async def _no_error(_ctx: AgentContext, **_kwargs: Any) -> None:
        return None

    @asynccontextmanager
    async def _no_session_scope(_ctx: AgentContext) -> AsyncIterator[None]:
        _fake_clock[0] += _BROWSER_BOOT_SECONDS
        yield

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _no_error)
    monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _no_session_scope)
    yield


def _install_timed_context(monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> None:
    @asynccontextmanager
    async def _scope(_ctx: AgentContext) -> AsyncIterator[None]:
        clock[0] += _BROWSER_BOOT_SECONDS + _CONTEXT_ENTER_SECONDS
        try:
            yield
        finally:
            clock[0] += _CONTEXT_EXIT_SECONDS

    monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _scope)


def _browser_server(on_call: Callable[[], None]) -> SkyvernOverlayMCPServer:
    return _make_server(
        make_copilot_ctx(browser_session_id="pbs_1"),
        {"ok": True},
        SchemaOverlay(requires_browser=True),
        alias_map=get_skyvern_mcp_alias_map(),
        on_call=on_call,
    )


def _timing_records(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in captured if record.get("event") == _TIMING_EVENT]


def _browser_outcome_records(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in captured if record.get("event") == "copilot_browser_call_outcome"]


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
    browser_session_id: str | None = "pbs_1",
) -> SkyvernOverlayMCPServer:
    def _advance() -> None:
        clock[0] += _MCP_CALL_SECONDS

    return _make_server(
        make_copilot_ctx(browser_session_id=browser_session_id),
        payload,
        overlay,
        alias_map=alias_map,
        on_call=_advance,
        is_error=is_error,
    )


@pytest.mark.asyncio
async def test_internal_call_preserves_explicit_session_across_session_prepare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_snapshot")
    server = _make_server(
        ctx,
        {"ok": True},
        SchemaOverlay(requires_browser=True),
        alias_map=get_skyvern_mcp_alias_map(),
    )
    dispatched: list[dict[str, Any]] = []

    class _CapturingClient:
        async def call_tool(self, name: str, args: dict[str, Any], raise_on_error: bool = False) -> Any:
            dispatched.append(dict(args))
            return SimpleNamespace(
                structured_content={"ok": True},
                is_error=False,
                content=[],
            )

    async def _replace_ambient_session(_ctx: AgentContext, **_kwargs: Any) -> tuple[None, None, None]:
        _ctx.browser_session_id = "pbs_replacement"
        return None, None, None

    @asynccontextmanager
    async def _scope(_ctx: AgentContext) -> AsyncIterator[None]:
        yield

    server._client = _CapturingClient()
    monkeypatch.setattr(mcp_adapter, "_prepare_browser_session_for_dispatch", _replace_ambient_session)
    monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _scope)

    result = await server.call_internal_tool(
        "skyvern_evaluate",
        {"expression": "scan()", "session_id": "pbs_snapshot"},
    )

    assert result["ok"] is True
    assert dispatched == [{"expression": "scan()", "session_id": "pbs_snapshot"}]
    assert ctx.browser_session_id == "pbs_replacement"


@pytest.mark.usefixtures("_stub_browser_session")
class TestSharedBrowserCallOutcome:
    @staticmethod
    def _server(
        *,
        model_tool_name: str,
        raw_tool_name: str,
        payload: dict[str, Any] | Exception,
        is_error: bool = False,
    ) -> SkyvernOverlayMCPServer:
        server = _make_server(
            make_copilot_ctx(browser_session_id="pbs_1"),
            payload,
            SchemaOverlay(requires_browser=True),
            alias_map={model_tool_name: raw_tool_name},
            is_error=is_error,
        )
        server._overlays[model_tool_name] = SchemaOverlay(requires_browser=True)
        return server

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model_tool_name", "raw_tool_name", "payload", "is_error", "expected"),
        [
            pytest.param(
                "evaluate",
                "skyvern_evaluate",
                {"ok": True, "data": {"result": {"count": 3}}},
                False,
                {
                    "dispatched": True,
                    "ok": True,
                    "error_kind": None,
                    "screenshot_present": False,
                    "screenshot_reference": None,
                    "response_truncated": False,
                    "payload_omitted": False,
                },
                id="evaluate-success",
            ),
            pytest.param(
                "evaluate",
                "skyvern_evaluate",
                {"ok": False, "error": {"code": "ACTION_FAILED", "message": "evaluation failed"}},
                True,
                {
                    "dispatched": True,
                    "ok": False,
                    "error_kind": "tool",
                    "screenshot_present": False,
                    "screenshot_reference": None,
                    "response_truncated": False,
                    "payload_omitted": False,
                },
                id="evaluate-tool-error",
            ),
            pytest.param(
                "get_browser_screenshot",
                "skyvern_screenshot",
                {
                    "dispatched": True,
                    "ok": True,
                    "data": {"path": "/tmp/frame.png", "data": "encoded-frame", "mime": "image/png"},
                    "artifacts": [{"kind": "screenshot", "path": "/tmp/frame.png", "mime": "image/png"}],
                },
                False,
                {
                    "dispatched": True,
                    "ok": True,
                    "error_kind": None,
                    "screenshot_present": True,
                    "screenshot_reference": "/tmp/frame.png",
                    "response_truncated": False,
                    "payload_omitted": False,
                },
                id="screenshot-success",
            ),
            pytest.param(
                "get_browser_screenshot",
                "skyvern_screenshot",
                {
                    "ok": False,
                    "error": {"code": "RESPONSE_TOO_LARGE", "message": "inline screenshot omitted"},
                    "_truncated": True,
                    "_original_bytes": 200_000,
                    "_max_bytes": 100_000,
                    "artifacts": [{"kind": "screenshot", "path": "/tmp/oversized.png", "mime": "image/png"}],
                },
                False,
                {
                    "ok": False,
                    "error_kind": "tool",
                    "screenshot_present": True,
                    "screenshot_reference": "/tmp/oversized.png",
                    "response_truncated": True,
                    "payload_omitted": True,
                },
                id="screenshot-oversized",
            ),
        ],
    )
    async def test_both_adapters_project_identical_common_facts_three_times(
        self,
        monkeypatch: pytest.MonkeyPatch,
        model_tool_name: str,
        raw_tool_name: str,
        payload: dict[str, Any],
        is_error: bool,
        expected: dict[str, str | bool | None],
    ) -> None:
        observed: list[_BrowserCallOutcome] = []
        real_project = mcp_adapter._project_browser_call_outcome

        def _capture(outcome: _BrowserCallOutcome, *, display_tool_name: str) -> dict[str, Any]:
            observed.append(outcome)
            return real_project(outcome, display_tool_name=display_tool_name)

        monkeypatch.setattr(mcp_adapter, "_project_browser_call_outcome", _capture)
        for _ in range(3):
            server = self._server(
                model_tool_name=model_tool_name,
                raw_tool_name=raw_tool_name,
                payload=payload,
                is_error=is_error,
            )
            await server.call_tool(model_tool_name, {})
            await server.call_internal_tool(raw_tool_name, {})

        assert len(observed) == 6
        for model_outcome, internal_outcome in zip(observed[::2], observed[1::2], strict=True):
            assert model_outcome == internal_outcome
            assert model_outcome.raw_tool_name == raw_tool_name
            assert model_outcome.source_browser_session_id == "pbs_1"
            for field_name, value in expected.items():
                assert getattr(model_outcome, field_name) == value

    @pytest.mark.asyncio
    async def test_client_exception_identity_is_shared_beneath_adapter_wording(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observed: list[_BrowserCallOutcome] = []
        real_project = mcp_adapter._project_browser_call_outcome

        def _capture(outcome: _BrowserCallOutcome, *, display_tool_name: str) -> dict[str, Any]:
            observed.append(outcome)
            return real_project(outcome, display_tool_name=display_tool_name)

        monkeypatch.setattr(mcp_adapter, "_project_browser_call_outcome", _capture)
        server = self._server(
            model_tool_name="evaluate",
            raw_tool_name="skyvern_evaluate",
            payload=RuntimeError("transport unavailable"),
        )

        await server.call_tool("evaluate", {})
        await server.call_internal_tool("skyvern_evaluate", {})

        assert observed[0] == observed[1]
        assert observed[0].error_kind == "protocol"
        assert observed[0].protocol_error_detail == "transport unavailable"

    def test_payload_custody_copies_containers_without_copying_inline_screenshot_bytes(self) -> None:
        screenshot_base64 = "frame-" + "x" * 200_000
        payload = {"ok": True, "data": {"data": screenshot_base64, "metadata": {"width": 1280}}}

        outcome = mcp_adapter._browser_call_outcome_from_mapping(
            raw_tool_name="skyvern_screenshot",
            source_browser_session_id="pbs_1",
            raw_result=payload,
        )
        first = outcome.raw_result()
        second = outcome.raw_result()

        assert first is not payload
        assert first["data"] is not payload["data"]
        assert first["data"]["data"] is screenshot_base64
        assert second["data"]["data"] is screenshot_base64
        first["data"]["metadata"]["width"] = 1
        assert second["data"]["metadata"]["width"] == 1280

    @pytest.mark.asyncio
    async def test_typed_internal_accessor_preserves_legacy_dict_and_drain_incomplete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {"ok": True, "data": {"result": {"count": 3}}}
        server = self._server(
            model_tool_name="evaluate",
            raw_tool_name="skyvern_evaluate",
            payload=payload,
        )
        server._evidence_candidate_origin = "https://public.test"

        async def _failed_drain() -> None:
            raise RuntimeError("drain failed")

        monkeypatch.setattr(server, "_drain_evidence_candidate_response_tasks", _failed_drain)

        typed = await server.call_internal_browser_tool("skyvern_evaluate", {"expression": "scan()"})
        legacy = await server.call_internal_tool("skyvern_evaluate", {"expression": "scan()"})

        assert typed.result == legacy == mcp_to_copilot(payload)
        assert typed.outcome.dispatched is True
        assert typed.outcome.evidence_drain_complete is False
        assert typed.outcome.source_browser_session_generation == 0

    @pytest.mark.asyncio
    async def test_internal_not_connected_is_a_not_dispatched_outcome(self) -> None:
        server = self._server(
            model_tool_name="evaluate",
            raw_tool_name="skyvern_evaluate",
            payload={"ok": True},
        )
        server._client = None

        with capture_logs() as captured:
            call = await server.call_internal_browser_tool("skyvern_evaluate", {})

        assert call.result["ok"] is False
        assert call.outcome.dispatched is False
        assert _browser_outcome_records(captured)[0]["dispatched"] is False

    @pytest.mark.asyncio
    async def test_model_connect_precondition_remains_before_attempt_admission(self) -> None:
        server = self._server(
            model_tool_name="evaluate",
            raw_tool_name="skyvern_evaluate",
            payload={"ok": True},
        )
        server._client = None

        with capture_logs() as captured, pytest.raises(RuntimeError, match="Not connected"):
            await server._call_tool("evaluate", {})

        assert _browser_outcome_records(captured) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_path", ["model", "internal"])
    async def test_session_preparation_error_is_not_dispatched(
        self, call_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _session_error(_ctx: AgentContext, **_kwargs: Any) -> dict[str, Any]:
            return {"ok": False, "error": "session setup failed"}

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _session_error)
        server = self._server(
            model_tool_name="evaluate",
            raw_tool_name="skyvern_evaluate",
            payload={"ok": True},
        )
        server._context_provider().browser_session_id = None

        with capture_logs() as captured:
            await _call(server, call_path)

        records = _browser_outcome_records(captured)
        assert len(records) == 1
        assert records[0]["dispatched"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_path", ["model", "internal"])
    async def test_pre_dispatch_replacement_retains_both_session_identities(
        self, call_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = make_copilot_ctx(browser_session_id="pbs_original")

        async def _replacement(
            replacement_ctx: AgentContext,
            **_kwargs: Any,
        ) -> tuple[None, dict[str, Any], mcp_adapter._BrowserSessionLossDisposition]:
            replacement_ctx.browser_session_id = "pbs_replacement"
            replacement_ctx.browser_session_continuity_generation = 1
            replacement_ctx.browser_session_continuity_disposition = "reestablished"
            return (
                None,
                mcp_adapter._browser_session_loss_result({}, disposition="reestablished"),
                "reestablished",
            )

        monkeypatch.setattr(mcp_adapter, "_prepare_browser_session_for_dispatch", _replacement)
        server = _make_server(
            ctx,
            {"ok": True},
            SchemaOverlay(requires_browser=True),
            alias_map={"evaluate": "skyvern_evaluate"},
        )

        with capture_logs() as captured:
            await _call(server, call_path)

        record = _browser_outcome_records(captured)[0]
        assert record["dispatched"] is False
        assert record["source_browser_session_id"] == "pbs_original"
        assert record["replacement_browser_session_id"] == "pbs_replacement"
        assert record["source_browser_session_generation"] == 0
        assert record["completion_browser_session_generation"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_path", ["model", "internal"])
    async def test_context_entry_failure_is_not_dispatched(
        self, call_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        @asynccontextmanager
        async def _failed_context(_ctx: AgentContext) -> AsyncIterator[None]:
            raise RuntimeError("context unavailable")
            yield

        monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _failed_context)
        server = self._server(
            model_tool_name="evaluate",
            raw_tool_name="skyvern_evaluate",
            payload={"ok": True},
        )

        with capture_logs() as captured:
            await _call(server, call_path)

        records = _browser_outcome_records(captured)
        assert len(records) == 1
        assert records[0]["dispatched"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_path", ["model", "internal"])
    async def test_cancellation_records_dispatch_then_reraises_same_exception(
        self, call_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cancellation = asyncio.CancelledError("caller deadline")
        server = self._server(
            model_tool_name="evaluate",
            raw_tool_name="skyvern_evaluate",
            payload={"ok": True},
        )

        async def _cancelled_call(*_args: Any, **_kwargs: Any) -> None:
            raise cancellation

        monkeypatch.setattr(server._client, "call_tool", _cancelled_call)

        with capture_logs() as captured, pytest.raises(asyncio.CancelledError) as caught:
            await _call(server, call_path)

        assert caught.value is cancellation
        records = _browser_outcome_records(captured)
        assert len(records) == 1
        assert records[0]["dispatched"] is True
        assert records[0]["cancelled"] is True

    @pytest.mark.asyncio
    async def test_raw_secret_and_pre_hook_rejections_are_not_dispatched(self) -> None:
        raw_secret_ctx = make_copilot_ctx(
            browser_session_id="pbs_1",
            request_policy=RequestPolicy(raw_secret_detected=True),
        )
        raw_secret_ctx.browser_session_continuity_disposition = "reestablished"
        raw_secret_server = _make_server(
            raw_secret_ctx,
            {"ok": True},
            SchemaOverlay(requires_browser=True),
            alias_map={"evaluate": "skyvern_evaluate"},
        )

        async def _reject(_args: dict[str, Any], _ctx: AgentContext) -> dict[str, Any]:
            return {"ok": False, "error": "screenshot unavailable"}

        screenshot_server = _make_server(
            make_copilot_ctx(browser_session_id="pbs_1"),
            {"ok": True},
            SchemaOverlay(requires_browser=True, pre_hook=_reject),
            alias_map={"get_browser_screenshot": "skyvern_screenshot"},
        )
        screenshot_server._overlays["get_browser_screenshot"] = SchemaOverlay(
            requires_browser=True,
            pre_hook=_reject,
        )

        with capture_logs() as captured:
            raw_secret_result = await raw_secret_server.call_tool("evaluate", {})
            await screenshot_server.call_tool("get_browser_screenshot", {})

        assert "A raw-secret draft cannot use browser tools" in raw_secret_result.content[0].text
        records = _browser_outcome_records(captured)
        assert len(records) == 2
        assert all(record["dispatched"] is False for record in records)


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
        assert records[0]["server_attach_ms"] is None
        assert records[0]["tool_name"] == "skyvern_evaluate"
        assert records[0]["mcp_tool_name"] == "skyvern_evaluate"
        assert records[0]["call_path"] == "internal"
        assert records[0]["wall_clock_ms"] == _WALL_MS

    @pytest.mark.asyncio
    async def test_the_browser_attach_is_named_and_folded_back_into_the_server_span_once(
        self, _fake_clock: list[float]
    ) -> None:
        payload = {"ok": True, "data": {"x": 1}, "timing_ms": {"attach": 300, "total": 515}}
        server = _server_whose_call_takes_time(payload, SchemaOverlay(), _fake_clock)

        with capture_logs() as captured:
            await server.call_internal_tool("skyvern_evaluate", {"expression": "scan()"})

        record = _timing_records(captured)[0]
        assert record["server_attach_ms"] == 300
        assert record["server_timing_ms"] == 815
        assert record["phase_dispatch_untimed_ms"] == _MCP_CALL_MS - 815

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
        assert records[0]["timing_server_overrun"] is None
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
    async def test_a_call_cancelled_while_attaching_still_reports_the_budget_it_spent(
        self, call_path: str, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        @asynccontextmanager
        async def _cancelled(_ctx: AgentContext) -> AsyncIterator[None]:
            _fake_clock[0] += _BROWSER_BOOT_SECONDS
            raise asyncio.CancelledError
            yield

        monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _cancelled)
        server = _server_whose_call_takes_time({"ok": True}, SchemaOverlay(requires_browser=True), _fake_clock)

        with capture_logs() as captured, pytest.raises(asyncio.CancelledError):
            await _call(server, call_path)

        records = _timing_records(captured)
        assert len(records) == 1
        assert records[0]["call_status"] == "cancelled"
        assert records[0]["call_path"] == call_path
        assert records[0]["wall_clock_ms"] == _SESSION_ONLY_MS
        assert records[0]["server_timing_ms"] is None
        assert records[0]["timing_phase"] == "context_enter"
        assert records[0]["phase_context_enter_ms"] == _SESSION_ONLY_MS
        assert records[0]["phase_dispatch_ms"] == 0
        assert records[0]["phase_residual_ms"] >= 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_path", ["model", "internal"])
    async def test_a_session_that_fails_to_resolve_still_reports_the_budget_it_spent(
        self, call_path: str, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        async def _session_error(_ctx: AgentContext, **_kwargs: Any) -> dict[str, Any]:
            _fake_clock[0] += _BROWSER_BOOT_SECONDS
            return {"ok": False, "error": "no browser session"}

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _session_error)
        server = _server_whose_call_takes_time(
            {"ok": True}, SchemaOverlay(requires_browser=True), _fake_clock, browser_session_id=None
        )

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
    async def test_an_attach_that_raises_still_reports_its_budget(
        self, call_path: str, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        @asynccontextmanager
        async def _attach_raises(_ctx: AgentContext) -> AsyncIterator[None]:
            _fake_clock[0] += _BROWSER_BOOT_SECONDS
            raise RuntimeError("adoption failed")
            yield

        monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _attach_raises)
        server = _server_whose_call_takes_time({"ok": True}, SchemaOverlay(requires_browser=True), _fake_clock)

        with capture_logs() as captured:
            await _call(server, call_path)

        records = _timing_records(captured)
        assert len(records) == 1
        assert records[0]["call_status"] == "error"
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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_path", ["model", "internal"])
    async def test_a_call_whose_wall_clock_dwarfs_the_server_total_names_where_the_time_went(
        self, call_path: str, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        _install_timed_context(monkeypatch, _fake_clock)
        payload = {"ok": True, "data": {"x": 1}, "timing_ms": {"total": 815}}
        server = _server_whose_call_takes_time(
            payload, SchemaOverlay(requires_browser=True), _fake_clock, alias_map=get_skyvern_mcp_alias_map()
        )

        with capture_logs() as captured:
            await _call(server, call_path)

        record = _timing_records(captured)[0]
        assert record["wall_clock_ms"] == _GAP_WALL_MS
        assert record["server_timing_ms"] == 815
        assert record["phase_session_prepare_ms"] == 0
        assert record["phase_context_enter_ms"] == _SESSION_ONLY_MS + _CONTEXT_ENTER_MS
        assert record["phase_dispatch_ms"] == _MCP_CALL_MS
        assert record["phase_context_exit_ms"] == _CONTEXT_EXIT_MS
        assert record["phase_residual_ms"] == 0
        assert record["timing_phase"] is None
        _assert_every_millisecond_is_attributed(record)
        assert record["phase_dispatch_untimed_ms"] == _MCP_CALL_MS - 815
        assert record["timing_server_overrun"] is False
        outside_the_server = sum(record[key] for key in _PHASE_KEYS if key != "phase_dispatch_ms")
        assert outside_the_server + record["phase_dispatch_untimed_ms"] == _GAP_WALL_MS - 815

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_path", ["model", "internal"])
    async def test_a_tool_that_answered_with_an_error_names_no_segment_as_the_one_it_died_in(
        self, call_path: str, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        _install_timed_context(monkeypatch, _fake_clock)
        payload = {"ok": False, "error": "the page said no", "timing_ms": {"total": 815}}
        server = _server_whose_call_takes_time(
            payload, SchemaOverlay(requires_browser=True), _fake_clock, alias_map=get_skyvern_mcp_alias_map()
        )

        with capture_logs() as captured:
            await _call(server, call_path)

        record = _timing_records(captured)[0]
        assert record["call_status"] == "error"
        assert record["timing_phase"] is None
        assert record["post_call_evidence_drain_ms"] is None
        _assert_every_millisecond_is_attributed(record)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("requires_browser", [False, True], ids=["no_browser", "browser"])
    async def test_a_call_far_longer_than_any_plausible_ceiling_runs_to_completion(
        self, requires_browser: bool, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        overlay = SchemaOverlay(requires_browser=requires_browser)
        assert overlay.timeout is None
        if requires_browser:
            _install_timed_context(monkeypatch, _fake_clock)

        dispatches = []

        def _advance_an_hour() -> None:
            dispatches.append(_fake_clock[0])
            _fake_clock[0] += _AN_HOUR_SECONDS

        server = _make_server(
            make_copilot_ctx(browser_session_id="pbs_1"),
            {"ok": True, "data": {"x": 1}},
            overlay,
            on_call=_advance_an_hour,
        )

        with capture_logs() as captured:
            result = await server.call_tool("evaluate", {"expression": "scan()"})

        assert isinstance(result, CallToolResult)
        assert result.isError is not True
        assert len(dispatches) == 1
        assert json.loads(result.content[0].text)["data"] == {"x": 1}
        record = _timing_records(captured)[0]
        assert record["call_status"] == "ok"
        assert record["phase_dispatch_ms"] == _AN_HOUR_MS
        around_the_dispatch = _SESSION_ONLY_MS + _CONTEXT_ENTER_MS + _CONTEXT_EXIT_MS if requires_browser else 0
        assert record["wall_clock_ms"] == _AN_HOUR_MS + around_the_dispatch
        assert record["phase_residual_ms"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_path", ["model", "internal"])
    @pytest.mark.parametrize("cancel_at", ["context_enter", "dispatch", "context_exit"])
    async def test_a_cancelled_call_names_the_segment_that_was_in_flight(
        self, cancel_at: str, call_path: str, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        @asynccontextmanager
        async def _scope(_ctx: AgentContext) -> AsyncIterator[None]:
            _fake_clock[0] += _CONTEXT_ENTER_SECONDS
            if cancel_at == "context_enter":
                raise asyncio.CancelledError
            try:
                yield
            finally:
                _fake_clock[0] += _CONTEXT_EXIT_SECONDS
                if cancel_at == "context_exit":
                    raise asyncio.CancelledError

        monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _scope)

        def _advance() -> None:
            _fake_clock[0] += _MCP_CALL_SECONDS
            if cancel_at == "dispatch":
                raise asyncio.CancelledError

        server = _browser_server(_advance)

        with capture_logs() as captured:
            with pytest.raises(asyncio.CancelledError):
                await _call(server, call_path)

        record = _timing_records(captured)[0]
        assert record["call_status"] == "cancelled"
        assert record["call_path"] == call_path
        assert record["timing_phase"] == cancel_at
        assert record["server_timing_ms"] is None
        assert record["phase_residual_ms"] >= 0
        _assert_every_millisecond_is_attributed(record)
        if cancel_at == "context_enter":
            assert record["phase_dispatch_ms"] == 0
            assert record["phase_context_exit_ms"] == 0
        else:
            assert record["phase_dispatch_ms"] == _MCP_CALL_MS
            assert record["phase_context_exit_ms"] == _CONTEXT_EXIT_MS

    @pytest.mark.asyncio
    async def test_a_server_total_larger_than_the_dispatch_segment_is_not_subtracted(
        self, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        _install_timed_context(monkeypatch, _fake_clock)
        payload = {"ok": True, "data": {"x": 1}, "timing_ms": {"total": _MCP_CALL_MS + 1}}
        server = _server_whose_call_takes_time(
            payload, SchemaOverlay(requires_browser=True), _fake_clock, alias_map=get_skyvern_mcp_alias_map()
        )

        with capture_logs() as captured:
            await server.call_tool("evaluate", {"expression": "scan()"})

        record = _timing_records(captured)[0]
        assert record["phase_dispatch_untimed_ms"] is None
        assert record["timing_server_overrun"] is True
        _assert_every_millisecond_is_attributed(record)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("failure", "call_status"),
        [
            (asyncio.CancelledError(), "cancelled"),
            (CopilotBrowserSessionUnavailable("pbs_1"), "session_error"),
            (RuntimeError("drain exploded"), "error"),
        ],
        ids=["cancelled", "session_error", "error"],
    )
    async def test_an_evidence_drain_that_fails_still_reports_the_budget_it_spent(
        self,
        failure: BaseException,
        call_status: str,
        monkeypatch: pytest.MonkeyPatch,
        _fake_clock: list[float],
    ) -> None:
        payload = {"ok": True, "data": {"x": 1}, "timing_ms": {"total": 815}}
        server = _server_whose_call_takes_time(payload, SchemaOverlay(), _fake_clock)
        server._evidence_candidate_origin = "https://public.test"
        server._context_provider().browser_session_replacements = {"pbs_1": "pbs_replacement"}

        async def _failing_drain() -> None:
            _fake_clock[0] += _AFTER_CALL_SECONDS
            raise failure

        monkeypatch.setattr(server, "_drain_evidence_candidate_response_tasks", _failing_drain)

        with capture_logs() as captured:
            if isinstance(failure, asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await server.call_internal_tool("skyvern_evaluate", {"expression": "scan()"})
            else:
                await server.call_internal_tool("skyvern_evaluate", {"expression": "scan()"})

        record = _timing_records(captured)[0]
        assert record["call_status"] == call_status
        assert record["wall_clock_ms"] == _WALL_MS
        assert record["post_call_evidence_drain_ms"] == _AFTER_CALL_MS
        assert record["phase_residual_ms"] == 0
        assert record["timing_phase"] == "evidence_drain"
        _assert_every_millisecond_is_attributed(record)

    @pytest.mark.asyncio
    async def test_an_evidence_drain_that_succeeds_reports_what_the_caller_waited_for_it(
        self, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        payload = {"ok": True, "data": {"x": 1}, "timing_ms": {"total": 815}}
        server = _server_whose_call_takes_time(payload, SchemaOverlay(), _fake_clock)
        server._evidence_candidate_origin = "https://public.test"

        async def _slow_drain() -> None:
            _fake_clock[0] += _AFTER_CALL_SECONDS

        monkeypatch.setattr(server, "_drain_evidence_candidate_response_tasks", _slow_drain)

        with capture_logs() as captured:
            await server.call_internal_tool("skyvern_evaluate", {"expression": "scan()"})

        record = _timing_records(captured)[0]
        assert record["call_status"] == "ok"
        assert record["post_call_evidence_drain_ms"] == _AFTER_CALL_MS
        assert record["wall_clock_ms"] == _WALL_MS
        _assert_every_millisecond_is_attributed(record)

    @pytest.mark.asyncio
    async def test_a_timing_sink_that_fails_costs_the_record_and_not_the_tool_result(
        self, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        payload = {"ok": True, "data": {"x": 1}, "timing_ms": {"total": 815}}
        server = _server_whose_call_takes_time(payload, SchemaOverlay(), _fake_clock)
        real_info = mcp_adapter.LOG.info

        def _sink_down(event: str, **fields: Any) -> Any:
            if event == "MCP tool timing":
                raise RuntimeError("structlog sink unavailable")
            return real_info(event, **fields)

        monkeypatch.setattr(mcp_adapter.LOG, "info", _sink_down)

        result = await server.call_internal_tool("skyvern_evaluate", {"expression": "scan()"})

        assert result.get("data") == {"x": 1}

    @pytest.mark.asyncio
    async def test_a_tool_that_needs_no_browser_spends_its_whole_wall_clock_dispatching(
        self, _fake_clock: list[float]
    ) -> None:
        payload = {"ok": True, "data": {"x": 1}, "timing_ms": {"total": 815}}
        server = _server_whose_call_takes_time(payload, SchemaOverlay(), _fake_clock)

        with capture_logs() as captured:
            await server.call_tool("evaluate", {"expression": "scan()"})

        record = _timing_records(captured)[0]
        assert record["wall_clock_ms"] == _MCP_CALL_MS
        assert record["phase_dispatch_ms"] == _MCP_CALL_MS
        assert record["phase_session_prepare_ms"] == 0
        assert record["phase_context_enter_ms"] == 0
        assert record["phase_context_exit_ms"] == 0
        assert record["phase_residual_ms"] == 0
        assert record["timing_phase"] is None


class TestBrowserSessionContinuity:
    @pytest.fixture(autouse=True)
    def _isolated_coordination(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_adapter.app, "CACHE", LocalCache())
        mcp_adapter._LOCAL_CONTINUITY_OUTCOMES.clear()
        mcp_adapter._LOCAL_CONTINUITY_ROOTS.clear()

        async def _close(_organization_id: str, _session_id: str) -> None:
            return None

        monkeypatch.setattr(mcp_adapter, "close_browser_session_quietly", _close)

    @pytest.mark.asyncio
    async def test_session_expiry_logs_and_reestablishes_once(
        self, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        ctx = make_copilot_ctx(browser_session_id="pbs_lost", turn_id="turn_1", workflow_copilot_chat_id="chat_1")
        calls: list[str | None] = []

        async def _ensure(recovery_ctx: AgentContext, **_kwargs: Any) -> None:
            calls.append(recovery_ctx.browser_session_id)
            recovery_ctx.browser_session_id = "pbs_replacement"

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _ensure)

        with capture_logs() as captured:
            first = await _handle_browser_session_loss(
                ctx,
                tool_name="evaluate",
                call_path="model",
                lost_session_id="pbs_lost",
            )
            second = await _handle_browser_session_loss(
                ctx,
                tool_name="evaluate",
                call_path="model",
                lost_session_id="pbs_lost",
            )

        assert first == second == "reestablished"
        assert calls == [None]
        assert ctx.browser_session_id == "pbs_replacement"
        records = [record for record in captured if record.get("event") == "copilot_browser_session_continuity_loss"]
        assert records[0]["session_id"] == "pbs_lost"
        assert all(record["error_code"] == "SESSION_EXPIRED" for record in records)
        assert [record["continuity_disposition"] for record in records] == ["detected", "reestablished"]
        assert all(record["turn_id"] == "turn_1" for record in records)
        assert all(record["workflow_copilot_chat_id"] == "chat_1" for record in records)

    @pytest.mark.asyncio
    async def test_two_contexts_share_one_replacement_and_close_the_lost_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shared_cache = _SharedLocalCache()
        monkeypatch.setattr(mcp_adapter.app, "CACHE", shared_cache)
        manager = MagicMock()
        manager.get_browser_session_startup_timeout_seconds.return_value = 55.0
        monkeypatch.setattr(mcp_adapter.app, "PERSISTENT_SESSIONS_MANAGER", manager)
        first_ctx = make_copilot_ctx(browser_session_id="pbs_shared_lost")
        second_ctx = make_copilot_ctx(browser_session_id="pbs_shared_lost")
        allocations = 0
        closed: list[str] = []

        async def _ensure(recovery_ctx: AgentContext, **_kwargs: Any) -> None:
            nonlocal allocations
            allocations += 1
            recovery_ctx.browser_session_id = f"pbs_shared_replacement_{allocations}"

        async def _close(_organization_id: str, session_id: str) -> None:
            closed.append(session_id)

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _ensure)
        monkeypatch.setattr(mcp_adapter, "close_browser_session_quietly", _close)

        dispositions = await asyncio.gather(
            _handle_browser_session_loss(
                first_ctx, tool_name="evaluate", call_path="model", lost_session_id="pbs_shared_lost"
            ),
            _handle_browser_session_loss(
                second_ctx, tool_name="evaluate", call_path="model", lost_session_id="pbs_shared_lost"
            ),
        )

        assert dispositions == ["reestablished", "reestablished"]
        assert allocations == 1
        assert first_ctx.browser_session_id == second_ctx.browser_session_id == "pbs_shared_replacement_1"
        assert closed == ["pbs_shared_lost"]
        assert shared_cache.lock_names
        assert await shared_cache.get(mcp_adapter._continuity_outcome_key(first_ctx.organization_id, "pbs_shared_lost"))

    @pytest.mark.asyncio
    async def test_a_replacement_loss_does_not_allocate_a_second_replacement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = make_copilot_ctx(browser_session_id="pbs_generation_0")
        allocations = 0

        async def _ensure(recovery_ctx: AgentContext, **_kwargs: Any) -> None:
            nonlocal allocations
            allocations += 1
            recovery_ctx.browser_session_id = f"pbs_generation_{allocations}"

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _ensure)

        first = await _handle_browser_session_loss(
            ctx, tool_name="evaluate", call_path="model", lost_session_id="pbs_generation_0"
        )
        second = await _handle_browser_session_loss(
            ctx, tool_name="evaluate", call_path="model", lost_session_id="pbs_generation_1"
        )

        assert first == "reestablished"
        assert second == "failed"
        assert allocations == 1
        assert ctx.browser_session_id is None
        assert ctx.blocker_signal is not None

    @pytest.mark.asyncio
    async def test_observability_failure_does_not_abort_recovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = make_copilot_ctx(browser_session_id="pbs_log_failure")

        async def _ensure(recovery_ctx: AgentContext, **_kwargs: Any) -> None:
            recovery_ctx.browser_session_id = "pbs_after_log_failure"

        def _raise(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("emitter unavailable")

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _ensure)
        monkeypatch.setattr(mcp_adapter, "LOG", SimpleNamespace(warning=_raise, info=_raise))

        disposition = await _handle_browser_session_loss(
            ctx, tool_name="evaluate", call_path="model", lost_session_id="pbs_log_failure"
        )

        assert disposition == "reestablished"
        assert ctx.browser_session_id == "pbs_after_log_failure"

    @pytest.mark.asyncio
    async def test_session_loss_adopts_concurrent_replacement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = make_copilot_ctx(browser_session_id="pbs_replacement")
        seen_session_ids: list[str | None] = []

        async def _ensure(recovery_ctx: AgentContext, **_kwargs: Any) -> None:
            seen_session_ids.append(recovery_ctx.browser_session_id)

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _ensure)

        disposition = await _handle_browser_session_loss(
            ctx,
            tool_name="evaluate",
            call_path="model",
            lost_session_id="pbs_stale",
        )

        assert disposition == "reestablished"
        assert seen_session_ids == ["pbs_replacement"]
        assert ctx.browser_session_id == "pbs_replacement"
        assert ctx.browser_session_replacements == {"pbs_stale": "pbs_replacement"}

    @pytest.mark.asyncio
    async def test_failed_reestablish_sets_honest_terminal_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = make_copilot_ctx(browser_session_id="pbs_lost")

        async def _ensure(_ctx: AgentContext, **_kwargs: Any) -> dict[str, Any]:
            return {"ok": False, "error": "Failed to create browser session"}

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _ensure)

        disposition = await _handle_browser_session_loss(
            ctx,
            tool_name="evaluate",
            call_path="model",
            lost_session_id="pbs_lost",
        )

        assert disposition == "failed"
        assert ctx.blocker_signal is not None
        assert ctx.blocker_signal.user_facing_reason == (
            "The browser session was lost, and I couldn't re-establish it. Please retry this turn."
        )
        assert ctx.blocker_signal.internal_reason_code == "tool_error_browser_session_lost"

    @pytest.mark.asyncio
    async def test_session_lost_during_attach_reestablishes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = make_copilot_ctx(browser_session_id="pbs_lost")

        async def _ensure(recovery_ctx: AgentContext, **_kwargs: Any) -> None:
            if recovery_ctx.browser_session_id is None:
                recovery_ctx.browser_session_id = "pbs_replacement"

        @asynccontextmanager
        async def _lost_scope(_ctx: AgentContext) -> AsyncIterator[None]:
            raise CopilotBrowserSessionUnavailable("pbs_lost")
            yield

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _ensure)
        monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _lost_scope)
        server = _make_server(ctx, {"ok": True}, SchemaOverlay(requires_browser=True))

        result = await server.call_tool("evaluate", {"expression": "scan()"})

        assert "fresh browser session" in result.content[0].text
        assert ctx.browser_session_id == "pbs_replacement"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_path", ["model", "internal"])
    async def test_attach_uncertainty_returns_a_typed_error_without_claiming_loss(
        self, call_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = make_copilot_ctx(browser_session_id="pbs_lost")

        async def _ensure(_ctx: AgentContext) -> None:
            await asyncio.sleep(0)

        @asynccontextmanager
        async def _undetermined(_ctx: AgentContext) -> AsyncIterator[None]:
            raise CopilotBrowserLivenessUndetermined()
            yield

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _ensure)
        monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _undetermined)
        server = _make_server(ctx, {"ok": True}, SchemaOverlay(requires_browser=True))

        with capture_logs() as logs:
            result = await _call(server, call_path)

        assert ctx.blocker_signal is None
        assert ctx.browser_session_id == "pbs_lost"
        assert ctx.browser_session_replacements == {}
        assert "could not be determined" in _surfaced_error(result, call_path)
        continuity = [record for record in logs if record.get("event") == "copilot_browser_session_continuity_loss"]
        assert continuity == []
        timing = _timing_records(logs)
        assert len(timing) == 1
        assert timing[0]["call_status"] == "error"

    @pytest.mark.asyncio
    async def test_manager_reestablish_failure_terminalizes_session_loss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = make_copilot_ctx(browser_session_id="pbs_lost")
        closed: list[str] = []

        async def _manager_times_out(recovery_ctx: AgentContext, **_kwargs: Any) -> None:
            recovery_ctx.browser_session_id = "pbs_partial"
            raise TimeoutError

        async def _close(_organization_id: str, session_id: str) -> None:
            closed.append(session_id)

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _manager_times_out)
        monkeypatch.setattr(mcp_adapter, "close_browser_session_quietly", _close)

        disposition = await _handle_browser_session_loss(
            ctx,
            tool_name="evaluate",
            call_path="model",
            lost_session_id="pbs_lost",
        )

        assert disposition == "failed"
        assert closed == ["pbs_lost", "pbs_partial"]
        assert ctx.browser_session_id is None
        assert ctx.blocker_signal is not None
        assert ctx.blocker_signal.internal_reason_code == "tool_error_browser_session_lost"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_path", ["model", "internal"])
    async def test_tool_result_requires_fresh_perception_after_reestablish(
        self, call_path: str, monkeypatch: pytest.MonkeyPatch, _fake_clock: list[float]
    ) -> None:
        ctx = make_copilot_ctx(browser_session_id="pbs_lost")
        observed: list[_BrowserCallOutcome] = []
        real_project = mcp_adapter._project_browser_call_outcome

        def _capture(outcome: _BrowserCallOutcome, *, display_tool_name: str) -> dict[str, Any]:
            observed.append(outcome)
            return real_project(outcome, display_tool_name=display_tool_name)

        async def _ensure(recovery_ctx: AgentContext, **_kwargs: Any) -> None:
            if recovery_ctx.browser_session_id is None:
                recovery_ctx.browser_session_id = "pbs_replacement"

        @asynccontextmanager
        async def _scope(_ctx: AgentContext) -> AsyncIterator[None]:
            yield

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _ensure)
        monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _scope)
        monkeypatch.setattr(mcp_adapter, "_project_browser_call_outcome", _capture)
        payload = {
            "ok": False,
            "error": {
                "code": "SESSION_EXPIRED",
                "message": "Browser session expired or closed.",
                "hint": "Create a new browser session and retry this operation.",
            },
        }
        server = _make_server(
            ctx,
            payload,
            SchemaOverlay(requires_browser=True),
            alias_map=get_skyvern_mcp_alias_map(),
        )

        result = await _call(server, call_path)
        result_text = result.content[0].text if isinstance(result, CallToolResult) else json.dumps(result)

        assert "SESSION_EXPIRED" in result_text
        assert "fresh browser session" in result_text
        assert "inspect the page" in result_text
        assert ctx.browser_session_id == "pbs_replacement"
        assert len(observed) == 1
        assert observed[0].source_browser_session_id == "pbs_lost"
        assert observed[0].session_loss_disposition == "reestablished"
        assert observed[0].replacement_browser_session_id == "pbs_replacement"

    @pytest.mark.asyncio
    async def test_runtime_self_heal_does_not_enter_interactive_reestablish(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = make_copilot_ctx(browser_session_id="self-heal:wr_test", turn_origin=TurnOrigin.runtime_self_heal)

        async def _ensure(_ctx: AgentContext, **_kwargs: Any) -> None:
            return None

        @asynccontextmanager
        async def _scope(_ctx: AgentContext) -> AsyncIterator[None]:
            yield

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _ensure)
        monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _scope)
        server = _make_server(
            ctx,
            {
                "ok": False,
                "error": {"code": "SESSION_EXPIRED", "message": "Browser session expired or closed."},
            },
            SchemaOverlay(requires_browser=True),
        )

        result = await server.call_tool("evaluate", {"expression": "scan()"})

        assert "fresh browser session" not in result.content[0].text
        assert ctx.browser_session_replacements == {}
        assert ctx.blocker_signal is None


def test_reestablish_lock_budget_is_derived_from_the_managers_startup_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MagicMock()
    manager.get_browser_session_startup_timeout_seconds.return_value = 55.0
    monkeypatch.setattr(mcp_adapter.app, "PERSISTENT_SESSIONS_MANAGER", manager)

    assert mcp_adapter._reestablish_lock_seconds() == 85
    assert not hasattr(mcp_adapter, "_SESSION_REESTABLISH_TIMEOUT_SECONDS")
