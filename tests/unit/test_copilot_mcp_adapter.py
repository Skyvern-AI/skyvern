import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest
from mcp.types import CallToolResult
from structlog.testing import capture_logs

from skyvern.cli.mcp_tools import mcp
from skyvern.forge.sdk.cache.base import NoopLock
from skyvern.forge.sdk.cache.local import LocalCache
from skyvern.forge.sdk.copilot import mcp_adapter
from skyvern.forge.sdk.copilot import runtime as copilot_runtime
from skyvern.forge.sdk.copilot.browser_ablation import CopilotEvalMode, resolve_copilot_tool_surface
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.mcp_adapter import (
    BROWSER_TARGET_PARAM_NAME,
    SchemaOverlay,
    SkyvernOverlayMCPServer,
    _apply_schema_overlay,
    _BrowserCallOutcome,
    _handle_browser_session_loss,
    _requested_output_path_choices,
    _transform_args,
    resolve_browser_session_binding,
)
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    CopilotBrowserLivenessUndetermined,
    CopilotBrowserSessionUnavailable,
    OriginRunRedactionRegistry,
    bound_call_browser_session,
    browser_page_custody_lock,
    mcp_to_copilot,
)
from skyvern.forge.sdk.copilot.secret_scrub import (
    clear_session_scrub_values,
)
from skyvern.forge.sdk.copilot.tools import NATIVE_TOOLS
from skyvern.forge.sdk.copilot.tools.mcp_hooks import _build_skyvern_mcp_overlays, get_skyvern_mcp_alias_map
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin
from skyvern.webeye.persistent_sessions_manager import (
    BrowserOperation,
    BrowserRetirement,
    BrowserRetirementReason,
)
from tests.unit.copilot_test_helpers import (
    SENSITIVE_DISCLOSURE_WITHHOLDING_ARMS,
    make_copilot_ctx,
    remove_sensitive_disclosure_prerequisite,
    taint_by_terminal_run,
)
from tests.unit.test_copilot_secret_scrub import _FakeClient, _make_server


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "expression": {"type": "string"},
            "output_path": {"type": "string", "description": "The requested output this read fills."},
        },
        "required": ["expression"],
    }


def test_scrub_tool_result_redacts_encoded_matching_origin_values(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "magic+link@example.test?token=one&next=/done"
    encoded = quote(secret, safe="")
    seen_parameters: list[dict[str, Any]] = []

    def redact(value: Any, parameters: dict[str, Any]) -> Any:
        seen_parameters.append(parameters)

        def walk(node: Any) -> Any:
            if isinstance(node, str):
                return node.replace(encoded, "[redacted]")
            if isinstance(node, dict):
                return {key: walk(item) for key, item in node.items()}
            return node

        return walk(value)

    ctx = make_copilot_ctx()
    ctx.last_run_blocks_workflow_run_id = "wr_sensitive"
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        "wr_sensitive",
        {"magic_link": secret},
        contains_sensitive_values=True,
        contains_all_sensitive_values=True,
    )
    monkeypatch.setattr(
        mcp_adapter.app,
        "AGENT_FUNCTION",
        SimpleNamespace(redact_codeblock_parameter_values=redact),
    )

    scrubbed = mcp_adapter.scrub_model_facing_tool_result(
        ctx,
        {"ok": True, "data": {"url": f"https://example.test/callback?payload={encoded}"}},
    )

    assert scrubbed["ok"] is True
    assert secret not in str(scrubbed)
    assert encoded not in str(scrubbed)
    assert "[redacted]" in str(scrubbed)
    assert seen_parameters == [{"magic_link": secret}]


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

    def test_narrowing_the_path_choices_leaves_the_rest_of_the_contract_intact(self) -> None:
        source = {**_schema(), "additionalProperties": False}

        schema = _requested_output_path_choices(source, ["output.a"])

        assert schema["additionalProperties"] is False
        assert schema["properties"]["expression"] == source["properties"]["expression"]
        assert schema["required"] == source["required"]


def _closed_source_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "description": "Open a page.",
        "$defs": {"Where": {"type": "string", "enum": ["chat", "last_run"]}},
        "properties": {
            "session_id": {"type": "string", "description": "Browser session id."},
            "url": {"type": "string", "description": "URL to open."},
            "where": {"$ref": "#/$defs/Where"},
        },
        "required": ["url"],
    }


async def _tab_new_overlay() -> SchemaOverlay:
    surface = resolve_copilot_tool_surface(
        mode=CopilotEvalMode.BROWSER_ABLATION,
        native_tools=list(NATIVE_TOOLS),
        alias_map=get_skyvern_mcp_alias_map(),
        overlays=_build_skyvern_mcp_overlays(),
        registered_mcp_tools=await mcp.list_tools(run_middleware=False),
    )
    return surface.overlays["skyvern_tab_new"]


class TestSchemaOverlayPreservesTheSourceContract:
    def test_an_empty_overlay_leaves_the_accepted_argument_contract_untouched(self) -> None:
        source = _closed_source_schema()

        assert _apply_schema_overlay(source, SchemaOverlay()) == source

    @pytest.mark.parametrize(
        ("overlay", "expected_properties", "expected_required"),
        [
            (SchemaOverlay(hide_params=frozenset({"session_id"})), {"url", "where"}, ["url"]),
            (SchemaOverlay(forced_args={"session_id": "pbs_1"}), {"url", "where"}, ["url"]),
            (SchemaOverlay(arg_transforms={"target": "where"}), {"session_id", "url", "target"}, ["url"]),
            (
                SchemaOverlay(copilot_params={"target": {"type": "string"}}),
                {"session_id", "url", "where", "target"},
                ["url"],
            ),
        ],
    )
    def test_an_overlay_edits_the_fields_it_names_and_nothing_else(
        self,
        overlay: SchemaOverlay,
        expected_properties: set[str],
        expected_required: list[str],
    ) -> None:
        source = _closed_source_schema()

        schema = _apply_schema_overlay(source, overlay)

        assert set(schema["properties"]) == expected_properties
        assert schema["required"] == expected_required
        assert schema["additionalProperties"] is False
        assert schema["$defs"] == source["$defs"]
        assert schema["description"] == source["description"]

    def test_a_renamed_field_keeps_its_definition_reachable(self) -> None:
        schema = _apply_schema_overlay(_closed_source_schema(), SchemaOverlay(arg_transforms={"target": "where"}))

        assert schema["properties"]["target"] == {"$ref": "#/$defs/Where"}
        assert schema["$defs"]["Where"]["enum"] == ["chat", "last_run"]


@pytest.mark.asyncio
async def test_a_valid_tab_creation_dispatches_only_the_tools_own_arguments() -> None:
    overlay = await _tab_new_overlay()

    mcp_args = _transform_args({"url": "https://example.com", BROWSER_TARGET_PARAM_NAME: "last_run"}, overlay)

    assert mcp_args == {"url": "https://example.com"}


@pytest.mark.asyncio
async def test_an_argument_outside_the_contract_is_refused_rather_than_swallowed() -> None:
    overlay = await _tab_new_overlay()

    mcp_args = _transform_args({"url": "https://example.com", "settle_seconds": 30}, overlay)
    result = await mcp.call_tool("skyvern_tab_new", mcp_args)
    payload = result.structured_content

    assert mcp_args["settle_seconds"] == 30
    assert payload is not None
    assert payload["ok"] is False
    assert payload["error"]["message"] == "skyvern_tab_new was called with unsupported argument(s): settle_seconds."
    assert payload["error"]["details"]["unsupported_arguments"] == ["settle_seconds"]


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
    async def _no_session_scope(_ctx: AgentContext, *, session_id_override: str | None = None) -> AsyncIterator[None]:
        _fake_clock[0] += _BROWSER_BOOT_SECONDS
        yield

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _no_error)
    monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _no_session_scope)
    yield


def _install_timed_context(monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> None:
    @asynccontextmanager
    async def _scope(_ctx: AgentContext, *, session_id_override: str | None = None) -> AsyncIterator[None]:
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


@pytest.mark.asyncio
async def test_generation_retirement_reports_replacement_without_expiring_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_replaced")

    async def _prepared(_ctx: AgentContext, **_kwargs: Any) -> tuple[None, None, None]:
        return None, None, None

    @asynccontextmanager
    async def _retired(_ctx: AgentContext, **_kwargs: Any) -> AsyncIterator[None]:
        raise mcp_adapter.CopilotBrowserGenerationRetired("pbs_replaced")
        yield

    monkeypatch.setattr(mcp_adapter, "_prepare_browser_session_for_dispatch", _prepared)
    monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _retired)
    server = _make_server(ctx, {"ok": True}, SchemaOverlay(requires_browser=True))

    result = await server.call_tool("evaluate", {})
    surfaced = json.loads(result.content[0].text)

    assert surfaced["error_code"] == "BROWSER_GENERATION_RETIRED"
    assert surfaced["data"]["browser_session_continuity"] == {
        "source": "direct_mcp",
        "disposition": "generation_retired",
        "cause": "connection_generation_retired",
        "fresh_state_required": True,
        "prior_action_effect": "unknown",
    }


@pytest.mark.asyncio
async def test_terminal_retirement_uses_session_loss_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_closed")
    retired_error = mcp_adapter.CopilotBrowserGenerationRetired(
        "pbs_closed",
        BrowserRetirementReason.session_ending,
    )

    async def _prepared(_ctx: AgentContext, **_kwargs: Any) -> tuple[None, None, None]:
        return None, None, None

    @asynccontextmanager
    async def _retired(_ctx: AgentContext, **_kwargs: Any) -> AsyncIterator[None]:
        raise retired_error
        yield

    handle_loss = AsyncMock(return_value="failed")
    monkeypatch.setattr(mcp_adapter, "_prepare_browser_session_for_dispatch", _prepared)
    monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _retired)
    monkeypatch.setattr(mcp_adapter, "_handle_browser_session_loss", handle_loss)
    server = _make_server(ctx, {"ok": True}, SchemaOverlay(requires_browser=True))

    result = await server.call_tool("evaluate", {})
    surfaced = json.loads(result.content[0].text)

    assert surfaced["error_code"] == "SESSION_EXPIRED"
    handle_loss.assert_awaited_once_with(
        ctx,
        tool_name="evaluate",
        call_path="model",
        lost_session_id="pbs_closed",
    )


@pytest.mark.asyncio
async def test_browser_post_hook_finishes_inside_the_exact_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_post_hook")
    scope_active = False
    post_hook_saw_scope = False

    async def _prepared(_ctx: AgentContext, **_kwargs: Any) -> tuple[None, None, None]:
        return None, None, None

    @asynccontextmanager
    async def _scope(_ctx: AgentContext, **_kwargs: Any) -> AsyncIterator[None]:
        nonlocal scope_active
        scope_active = True
        try:
            yield
        finally:
            scope_active = False

    async def _post_hook(
        copilot_result: dict[str, Any], _raw_mcp: dict[str, Any], _ctx: AgentContext
    ) -> dict[str, Any]:
        nonlocal post_hook_saw_scope
        post_hook_saw_scope = scope_active
        return copilot_result

    monkeypatch.setattr(mcp_adapter, "_prepare_browser_session_for_dispatch", _prepared)
    monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _scope)
    server = _make_server(
        ctx,
        {"ok": True},
        SchemaOverlay(requires_browser=True, post_hook=_post_hook),
    )

    result = await server.call_tool("evaluate", {})

    assert result.isError is False
    assert post_hook_saw_scope is True
    assert scope_active is False


@pytest.mark.asyncio
async def test_retirement_after_post_hook_rolls_back_context_and_defers_success_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_retired_post_hook")
    original_evidence = {"step": 1, "evidence": {"source_tool": "before"}}
    ctx.flow_evidence = [original_evidence]

    async def _prepared(_ctx: AgentContext, **_kwargs: Any) -> tuple[None, None, None]:
        return None, None, None

    @asynccontextmanager
    async def _scope(_ctx: AgentContext, **_kwargs: Any) -> AsyncIterator[None]:
        yield
        raise mcp_adapter.CopilotBrowserGenerationRetired("pbs_retired_post_hook")

    async def _post_hook(
        copilot_result: dict[str, Any], _raw_mcp: dict[str, Any], hook_ctx: AgentContext
    ) -> dict[str, Any]:
        hook_ctx.flow_evidence.append({"step": 2, "evidence": {"source_tool": "evaluate"}})
        await asyncio.sleep(0)
        return copilot_result

    record_outcome = MagicMock()
    monkeypatch.setattr(mcp_adapter, "_prepare_browser_session_for_dispatch", _prepared)
    monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _scope)
    monkeypatch.setattr(mcp_adapter, "_record_browser_call_outcome", record_outcome)
    server = _make_server(
        ctx,
        {"ok": True},
        SchemaOverlay(requires_browser=True, post_hook=_post_hook),
        alias_map={"evaluate": "skyvern_evaluate"},
    )

    result = await server.call_tool("evaluate", {})

    assert result.isError is True
    assert ctx.flow_evidence == [original_evidence]
    assert record_outcome.call_count == 1
    assert record_outcome.call_args.args[1].ok is False


@pytest.mark.asyncio
async def test_retirement_rollback_preserves_concurrent_sibling_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_shared_context")
    ctx.flow_evidence = []
    allow_retiring_dispatch = asyncio.Event()
    retiring_dispatch_started = asyncio.Event()

    async def _prepared(_ctx: AgentContext, **_kwargs: Any) -> tuple[None, None, None]:
        return None, None, None

    @asynccontextmanager
    async def _scope(_ctx: AgentContext, **_kwargs: Any) -> AsyncIterator[None]:
        yield
        if asyncio.current_task() is retiring_task:
            raise mcp_adapter.CopilotBrowserGenerationRetired("pbs_shared_context")

    class _ConcurrentClient:
        async def call_tool(
            self,
            _name: str,
            _args: dict[str, Any],
            raise_on_error: bool = False,
        ) -> Any:
            if asyncio.current_task() is retiring_task:
                retiring_dispatch_started.set()
                await allow_retiring_dispatch.wait()
                tag = "retiring"
            else:
                tag = "sibling"
            return SimpleNamespace(structured_content={"ok": True, "tag": tag}, is_error=False, content=[])

    async def _post_hook(
        copilot_result: dict[str, Any], raw_mcp: dict[str, Any], hook_ctx: AgentContext
    ) -> dict[str, Any]:
        hook_ctx.flow_evidence.append({"source": raw_mcp["tag"]})
        await asyncio.sleep(0)
        return copilot_result

    monkeypatch.setattr(mcp_adapter, "_prepare_browser_session_for_dispatch", _prepared)
    monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _scope)
    server = _make_server(
        ctx,
        {"ok": True},
        SchemaOverlay(requires_browser=True, post_hook=_post_hook),
    )
    server._client = _ConcurrentClient()

    retiring_task = asyncio.create_task(server._call_tool("evaluate", {}))
    await retiring_dispatch_started.wait()
    sibling_task = asyncio.create_task(server._call_tool("evaluate", {}))
    await asyncio.sleep(0)
    allow_retiring_dispatch.set()

    results = await asyncio.gather(retiring_task, sibling_task, return_exceptions=True)

    assert isinstance(results[0], CallToolResult)
    assert results[0].isError is True
    assert not isinstance(results[1], BaseException)
    assert ctx.flow_evidence == [{"source": "sibling"}]


@pytest.mark.asyncio
async def test_browser_pre_hook_internal_read_reuses_the_outer_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_nested_hook")
    ctx.api_key = "test-api-key"
    browser = MagicMock()
    browser.is_connected.return_value = True
    browser_context = MagicMock()
    browser_context.browser = browser
    browser_context._impl_obj = SimpleNamespace(_close_was_called=False, _closed=False)
    browser_state = MagicMock(browser_context=browser_context)
    operation_entries = 0

    @asynccontextmanager
    async def _operation(_session_id: str, resolved_state: Any) -> AsyncIterator[BrowserOperation]:
        nonlocal operation_entries
        operation_entries += 1
        yield BrowserOperation(resolved_state, BrowserRetirement())

    manager = MagicMock()
    manager.get_browser_state = AsyncMock(return_value=browser_state)
    manager.browser_operation = _operation
    monkeypatch.setattr(copilot_runtime.app, "PERSISTENT_SESSIONS_MANAGER", manager)
    monkeypatch.setattr(copilot_runtime, "get_skyvern", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(copilot_runtime, "SkyvernBrowser", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(copilot_runtime, "get_active_api_key", MagicMock(return_value="test-api-key"))
    monkeypatch.setattr(copilot_runtime, "set_api_key_override", MagicMock(return_value=object()))
    monkeypatch.setattr(copilot_runtime, "reset_api_key_override", MagicMock())
    register = MagicMock()
    unregister = MagicMock()
    monkeypatch.setattr(copilot_runtime, "register_copilot_session", register)
    monkeypatch.setattr(copilot_runtime, "unregister_copilot_session", unregister)

    async def _prepared(_ctx: AgentContext, **_kwargs: Any) -> tuple[None, None, None]:
        return None, None, None

    monkeypatch.setattr(mcp_adapter, "_prepare_browser_session_for_dispatch", _prepared)
    server: SkyvernOverlayMCPServer

    async def _pre_hook(_args: dict[str, Any], _ctx: AgentContext) -> None:
        internal = await server.call_internal_tool("skyvern_evaluate", {"expression": "source()"})
        assert internal["ok"] is True

    server = _make_server(
        ctx,
        {"ok": True, "data": {"result": "done"}},
        SchemaOverlay(requires_browser=True, pre_hook=_pre_hook),
        alias_map={"evaluate": "skyvern_evaluate"},
    )

    result = await server.call_tool("evaluate", {"expression": "action()"})

    assert result.isError is False
    assert operation_entries == 1
    manager.get_browser_state.assert_awaited_once()
    register.assert_called_once()
    unregister.assert_called_once()


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


@pytest.mark.asyncio
async def test_sensitive_run_custody_does_not_block_an_unrelated_browser_session() -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs-debug")
    run_lock = browser_page_custody_lock(ctx, session_id="pbs-run")
    debug_lock = browser_page_custody_lock(ctx, session_id="pbs-debug")
    await run_lock.acquire()
    try:
        await asyncio.wait_for(debug_lock.acquire(), timeout=0.1)
        debug_lock.release()
        assert browser_page_custody_lock(ctx, session_id="pbs-run") is run_lock
        assert browser_page_custody_lock(ctx, session_id="pbs-debug") is debug_lock
    finally:
        run_lock.release()


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
    async def _scope(_ctx: AgentContext, *, session_id_override: str | None = None) -> AsyncIterator[None]:
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
        async def _failed_context(_ctx: AgentContext, *, session_id_override: str | None = None) -> AsyncIterator[None]:
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
    async def test_adapter_rolls_back_post_hook_facts_when_session_becomes_sensitive_in_flight(
        self, _stub_browser_session: None
    ) -> None:
        ctx = make_copilot_ctx(browser_session_id="pbs_1")
        existing_flow = {"step": 1, "evidence": {"source_tool": "existing"}}
        ctx.flow_evidence = [existing_flow]
        ctx.scouted_output_covered_paths = {"output.existing"}

        async def _tainted_post_hook(
            copilot_result: dict[str, Any], raw_mcp: dict[str, Any], hook_ctx: AgentContext
        ) -> dict[str, Any]:
            await asyncio.sleep(0)
            hook_ctx.flow_evidence.append({"step": 2, "evidence": {"source_tool": "evaluate"}})
            hook_ctx.scouted_output_covered_paths.add("output.private")
            hook_ctx.sensitive_origin_browser_session_ids.add("pbs_1")
            return copilot_result

        server = _make_server(
            ctx,
            {"ok": True, "data": {"result": "private page contents", "url": "https://private.test"}},
            SchemaOverlay(requires_browser=True, post_hook=_tainted_post_hook),
            alias_map=get_skyvern_mcp_alias_map(),
        )

        result = await server.call_tool("evaluate", {"expression": "scan()"})

        surfaced = json.loads(result.content[0].text)
        assert surfaced["ok"] is False
        assert "specific named URL" in surfaced["error"]
        assert "data" not in surfaced
        assert ctx.flow_evidence == [existing_flow]
        assert ctx.scouted_output_covered_paths == {"output.existing"}

    @pytest.mark.asyncio
    async def test_evaluate_on_sensitive_origin_redacts_matching_run_registry_before_disclosure(
        self, _stub_browser_session: None
    ) -> None:
        ctx = make_copilot_ctx(browser_session_id="pbs_1")
        ctx.last_run_blocks_workflow_run_id = "wr_sensitive"
        ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
            "wr_sensitive",
            {
                "credential": {"username": "private-user", "password": "private-pass"},
                "copilot_run_runtime_secret_values": ("654321",),
            },
            contains_sensitive_values=True,
            contains_all_sensitive_values=True,
        )
        ctx.sensitive_origin_browser_session_ids.add("pbs_1")
        overlay = _build_skyvern_mcp_overlays()["evaluate"]
        server = _make_server(
            ctx,
            {
                "ok": True,
                "data": {
                    "result": {
                        "form": {"selector": "#otp-form", "field": "#totp"},
                        "echo": "private-user:private-pass:654321",
                    },
                    "url": "https://example.test/otp",
                    "title": "Verification for private-user",
                },
            },
            overlay,
            alias_map=get_skyvern_mcp_alias_map(),
        )

        result = await server.call_tool("evaluate", {"expression": "scan()"})

        surfaced = json.loads(result.content[0].text)
        assert surfaced["ok"] is True
        assert surfaced["data"]["result"]["form"] == {"selector": "#otp-form", "field": "#totp"}
        assert "private-user" not in result.content[0].text
        assert "private-pass" not in result.content[0].text
        assert "654321" not in result.content[0].text
        assert "[REDACTED_SECRET]" in result.content[0].text
        retained = json.dumps(
            {
                "composition_page_evidence": ctx.composition_page_evidence,
                "flow_evidence": ctx.flow_evidence,
                "scout_trajectory": ctx.scout_trajectory,
            },
            default=str,
        )
        assert "private-user" not in retained
        assert "private-pass" not in retained
        assert "654321" not in retained

    @pytest.mark.asyncio
    async def test_evaluate_on_sensitive_origin_stays_withheld_when_matching_registry_is_incomplete(
        self, _stub_browser_session: None
    ) -> None:
        ctx = make_copilot_ctx(browser_session_id="pbs_1")
        ctx.last_run_blocks_workflow_run_id = "wr_sensitive"
        ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
            "wr_sensitive",
            {"credential": {"username": "private-user"}},
            contains_sensitive_values=True,
            contains_all_sensitive_values=False,
        )
        ctx.sensitive_origin_browser_session_ids.add("pbs_1")
        server = _make_server(
            ctx,
            {
                "ok": True,
                "data": {
                    "result": "private-user:unregistered-pass",
                    "url": "https://example.test/otp",
                },
            },
            _build_skyvern_mcp_overlays()["evaluate"],
            alias_map=get_skyvern_mcp_alias_map(),
        )

        result = await server.call_tool("evaluate", {"expression": "scan()"})

        surfaced = json.loads(result.content[0].text)
        assert surfaced["ok"] is False
        assert "specific named URL" in surfaced["error"]
        assert "private-user" not in result.content[0].text
        assert "unregistered-pass" not in result.content[0].text

    @pytest.mark.asyncio
    async def test_evaluate_stays_withheld_while_the_sensitive_origin_run_is_active(
        self, _stub_browser_session: None
    ) -> None:
        ctx = make_copilot_ctx(browser_session_id="pbs_1")
        ctx.last_run_blocks_workflow_run_id = "wr_sensitive"
        ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
            "wr_sensitive",
            {"password": "private-pass"},
            contains_sensitive_values=True,
            contains_all_sensitive_values=True,
        )
        ctx.sensitive_origin_browser_session_ids.add("pbs_1")
        ctx.active_sensitive_origin_browser_session_ids.add("pbs_1")
        server = _make_server(
            ctx,
            {"ok": True, "data": {"result": "private-pass"}},
            _build_skyvern_mcp_overlays()["evaluate"],
            alias_map=get_skyvern_mcp_alias_map(),
        )

        result = await server.call_tool("evaluate", {"expression": "scan()"})

        surfaced = json.loads(result.content[0].text)
        assert surfaced["ok"] is False
        assert "specific named URL" in surfaced["error"]
        assert "private-pass" not in result.content[0].text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool_name", "args"),
        [
            ("skyvern_frame_list", {}),
            ("skyvern_frame_switch", {"selector": "#payment-frame"}),
            ("skyvern_frame_main", {}),
        ],
    )
    async def test_frame_controls_refuse_tainted_browser_sessions_before_dispatch(
        self,
        _stub_browser_session: None,
        tool_name: str,
        args: dict[str, str],
    ) -> None:
        ctx = make_copilot_ctx(browser_session_id="pbs_1")
        ctx.sensitive_origin_browser_session_ids.add("pbs_1")
        dispatched: list[str] = []

        class _CapturingClient:
            async def call_tool(self, name: str, args: dict[str, Any], raise_on_error: bool = False) -> Any:
                dispatched.append(name)
                return SimpleNamespace(structured_content={"ok": True}, is_error=False, content=[])

        aliases = get_skyvern_mcp_alias_map()
        server = SkyvernOverlayMCPServer(
            transport=MagicMock(),
            overlays={tool_name: _build_skyvern_mcp_overlays()[tool_name]},
            alias_map={tool_name: aliases[tool_name]},
            allowlist=frozenset({aliases[tool_name]}),
            context_provider=lambda: ctx,
        )
        server._client = _CapturingClient()

        result = await server.call_tool(tool_name, args)

        surfaced = json.loads(result.content[0].text)
        assert surfaced["ok"] is False
        assert "specific named URL" in surfaced["error"]
        assert dispatched == []

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
        async def _cancelled(_ctx: AgentContext, *, session_id_override: str | None = None) -> AsyncIterator[None]:
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
        async def _attach_raises(_ctx: AgentContext, *, session_id_override: str | None = None) -> AsyncIterator[None]:
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
        async def _scope(_ctx: AgentContext, *, session_id_override: str | None = None) -> AsyncIterator[None]:
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


def _async_return(value: Any) -> Callable[..., Any]:
    async def _call(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return _call


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
    async def test_a_deadline_expiry_is_named_rather_than_reported_as_a_browser_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SKY-15044: a vendor session killed on its fixed deadline mid-turn reached the user as a
        generic lost-session message, so a periodic, expected expiry read as a browser fault. The
        finalized row says which happened, and the surfaced error has to say so too."""
        ctx = make_copilot_ctx(browser_session_id="pbs_expired")

        async def _ensure(recovery_ctx: AgentContext, **_kwargs: Any) -> None:
            recovery_ctx.browser_session_id = "pbs_replacement"

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _ensure)
        monkeypatch.setattr(
            mcp_adapter,
            "app",
            SimpleNamespace(
                PERSISTENT_SESSIONS_MANAGER=SimpleNamespace(seconds_until_fixed_deadline=_async_return(-3.0)),
                CACHE=mcp_adapter.app.CACHE,
            ),
        )

        disposition = await _handle_browser_session_loss(
            ctx, tool_name="evaluate", call_path="model", lost_session_id="pbs_expired"
        )

        assert disposition == "reestablished"
        assert ctx.browser_session_continuity_deadline_expired is True
        # The primary browser tools return only the projection of a shared outcome, so the cause
        # has to survive that hop or evaluate and screenshot still report a generic failure.
        projected = mcp_adapter._project_browser_call_outcome(
            mcp_adapter._not_dispatched_browser_call_outcome(
                raw_tool_name="skyvern_evaluate",
                source_browser_session_id="pbs_expired",
                source_browser_session_generation=0,
                raw_result={},
                ctx=ctx,
                session_loss_disposition=disposition,
            ),
            display_tool_name="evaluate",
        )
        assert "time limit" in projected["error"]
        assert projected["data"]["browser_session_continuity"]["cause"] == "fixed_deadline_expiry"

    @pytest.mark.asyncio
    async def test_an_unexplained_loss_is_not_named_a_deadline_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The cause has to discriminate: a session that died well inside its deadline died for some
        other reason, and reporting that as an expected expiry makes the signal meaningless."""
        ctx = make_copilot_ctx(browser_session_id="pbs_dead")

        async def _ensure(recovery_ctx: AgentContext, **_kwargs: Any) -> None:
            recovery_ctx.browser_session_id = "pbs_replacement"

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", _ensure)
        monkeypatch.setattr(
            mcp_adapter,
            "app",
            SimpleNamespace(
                PERSISTENT_SESSIONS_MANAGER=SimpleNamespace(seconds_until_fixed_deadline=_async_return(742.0)),
                CACHE=mcp_adapter.app.CACHE,
            ),
        )

        await _handle_browser_session_loss(ctx, tool_name="evaluate", call_path="model", lost_session_id="pbs_dead")

        assert ctx.browser_session_continuity_deadline_expired is False

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
        async def _lost_scope(_ctx: AgentContext, *, session_id_override: str | None = None) -> AsyncIterator[None]:
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
        async def _undetermined(_ctx: AgentContext, *, session_id_override: str | None = None) -> AsyncIterator[None]:
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
        async def _scope(_ctx: AgentContext, *, session_id_override: str | None = None) -> AsyncIterator[None]:
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
        async def _scope(_ctx: AgentContext, *, session_id_override: str | None = None) -> AsyncIterator[None]:
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


def _binding_ctx(*, debug: str | None, run: str | None, run_id: str | None = "wr_1") -> SimpleNamespace:
    return SimpleNamespace(
        browser_session_id=debug,
        last_run_blocks_browser_session_id=run,
        last_run_blocks_workflow_run_id=run_id,
    )


def test_default_target_binds_the_debug_browser() -> None:
    binding = resolve_browser_session_binding(_binding_ctx(debug="pbs_debug", run="pbs_run"), {})

    assert (binding.target, binding.session_id_override) == ("debug", None)
    assert binding.provenance() == {"browser_target": "debug", "source_matches_target": True}


def test_last_run_target_binds_the_run_browser() -> None:
    binding = resolve_browser_session_binding(_binding_ctx(debug="pbs_debug", run="pbs_run"), {"target": "last_run"})

    assert binding.session_id_override == "pbs_run"
    assert binding.provenance()["browser_target_workflow_run_id"] == "wr_1"
    assert binding.source_matches_target is True


def test_last_run_without_a_recorded_run_session_is_disclosed_not_silently_served() -> None:
    # last_run is a promise about which browser answered, so an unknown run session has to be
    # reported rather than quietly satisfied from the debug browser.
    binding = resolve_browser_session_binding(_binding_ctx(debug="pbs_debug", run=None), {"target": "last_run"})

    assert binding.session_id_override is None
    assert binding.source_matches_target is False
    assert "No test run" in binding.provenance()["browser_target_unavailable"]


def test_an_unknown_target_value_is_refused_rather_than_served_from_the_debug_browser() -> None:
    # Coercing an off-enum target to the chat's browser would run a click or a fill in a browser the
    # model did not name, and stamp the result as if it had landed where it was aimed.
    binding = resolve_browser_session_binding(_binding_ctx(debug="pbs_debug", run="pbs_run"), {"target": "whatever"})

    assert binding.session_id_override is None
    assert binding.source_matches_target is False
    assert binding.unavailable_reason is not None
    assert "whatever" in binding.unavailable_reason


def test_a_run_that_shared_the_chats_browser_binds_no_override() -> None:
    # There is nothing to redirect, and an override would pin the call to a recorded id that a later
    # session re-establishment leaves behind.
    binding = resolve_browser_session_binding(_binding_ctx(debug="pbs_same", run="pbs_same"), {"target": "last_run"})

    assert (binding.target, binding.session_id_override) == ("last_run", None)
    assert binding.unavailable_reason is None


def test_every_browser_overlay_offers_the_target_param() -> None:
    overlays = _build_skyvern_mcp_overlays(BlockAuthoringPolicy.CODE_ONLY_BROWSER)
    browser_overlays = {name: overlay for name, overlay in overlays.items() if overlay.requires_browser}

    assert {
        "skyvern_frame_list",
        "skyvern_frame_switch",
        "skyvern_frame_main",
    } <= set(browser_overlays)
    assert len(browser_overlays) == 13
    assert all(BROWSER_TARGET_PARAM_NAME in overlay.copilot_params for overlay in browser_overlays.values())
    # inspect_page_for_composition owns target_url semantics; a second target would overload it.
    assert "inspect_page_for_composition" not in browser_overlays


def test_the_target_param_never_reaches_the_underlying_tool() -> None:
    overlay = _build_skyvern_mcp_overlays(BlockAuthoringPolicy.CODE_ONLY_BROWSER)["evaluate"]

    mcp_args = _transform_args({"expression": "1+1", BROWSER_TARGET_PARAM_NAME: "last_run"}, overlay)

    assert mcp_args == {"expression": "1+1"}


def test_a_dead_targeted_browser_does_not_tear_down_the_chats_session() -> None:
    # Continuity recovery closes the lost browser and rebinds the chat to a replacement. Routing a
    # targeted browser's death through it would destroy the page the model asked to look at and
    # swap the chat's browser because a browser it does not own died.
    ctx = SimpleNamespace(
        organization_id="org",
        browser_session_id="pbs_chat",
        browser_session_replacements={},
    )

    async def _run() -> str:
        with bound_call_browser_session("pbs_run"):
            return await _handle_browser_session_loss(
                ctx,  # type: ignore[arg-type]
                tool_name="evaluate",
                call_path="model",
                lost_session_id="pbs_run",
            )

    assert asyncio.run(_run()) == "failed"
    assert ctx.browser_session_id == "pbs_chat"
    assert ctx.browser_session_replacements == {}


@pytest.mark.asyncio
async def test_internal_probes_inside_a_targeted_call_read_the_targeted_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A tool call is a dispatch wrapped in pre/post-hook probes. Sending the dispatch to the run's
    # browser while the probes read the chat's would verify the action against a different page and
    # stamp the result with the browser it did not observe.
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    server = _make_server(
        ctx,
        {"ok": True},
        SchemaOverlay(requires_browser=True),
        alias_map=get_skyvern_mcp_alias_map(),
    )
    dispatched: list[dict[str, Any]] = []
    scoped: list[str | None] = []

    class _CapturingClient:
        async def call_tool(self, name: str, args: dict[str, Any], raise_on_error: bool = False) -> Any:
            dispatched.append(dict(args))
            return SimpleNamespace(structured_content={"ok": True}, is_error=False, content=[])

    @asynccontextmanager
    async def _scope(_ctx: AgentContext, *, session_id_override: str | None = None) -> AsyncIterator[None]:
        scoped.append(session_id_override)
        yield

    server._client = _CapturingClient()
    monkeypatch.setattr(mcp_adapter, "mcp_browser_context", _scope)

    with bound_call_browser_session("pbs_run"):
        result = await server.call_internal_tool("skyvern_evaluate", {"expression": "scan()"})

    assert result["ok"] is True
    assert dispatched == [{"expression": "scan()", "session_id": "pbs_run"}]
    assert scoped == ["pbs_run"]
    assert ctx.browser_session_id == "pbs_chat"


@pytest.mark.asyncio
async def test_a_targeted_call_is_not_gated_on_the_chats_own_browser() -> None:
    # The chat's continuity is not a precondition for a call acting in a browser the chat does not
    # own; a dead debug browser must not refuse a look at the run's page.
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")

    with bound_call_browser_session("pbs_run"):
        prepared = await mcp_adapter._prepare_browser_session_for_dispatch(
            ctx,
            tool_name="evaluate",
            call_path="model",
            observed_generation=ctx.browser_session_continuity_generation,
        )

    assert prepared == (None, None, None)
    assert ctx.browser_session_id == "pbs_chat"


@pytest.mark.asyncio
async def test_a_call_aimed_at_an_unavailable_browser_never_dispatches() -> None:
    """The no-silent-fallback property: a click or a fill aimed at the run's browser must not land
    in the chat's, so the refusal has to happen before the underlying tool is reached."""
    dispatched: list[bool] = []

    # The api key matters: without one the call cannot dispatch anyway, and the dispatch assertion
    # below would hold whether or not the refusal exists.
    ctx = make_copilot_ctx(browser_session_id="pbs_chat", api_key="sk-test")
    ctx.last_run_blocks_browser_session_id = None
    ctx.last_run_blocks_workflow_run_id = None

    server = _make_server(
        ctx,
        {"ok": True},
        SchemaOverlay(requires_browser=True),
        on_call=lambda: dispatched.append(True),
    )

    result = await server.call_tool("evaluate", {"expression": "1+1", BROWSER_TARGET_PARAM_NAME: "last_run"})

    assert dispatched == []
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is False
    # The refusal names the target it could not honour; a later failure would not.
    assert payload.get("browser_target") == "last_run"
    assert "recorded a browser" in payload["error"]


def _click_overlay_server(ctx: AgentContext, payload: dict[str, Any]) -> SkyvernOverlayMCPServer:
    aliases = get_skyvern_mcp_alias_map()
    server = SkyvernOverlayMCPServer(
        transport=MagicMock(),
        overlays={"click": _build_skyvern_mcp_overlays()["click"]},
        alias_map={"click": aliases["click"]},
        allowlist=frozenset({aliases["click"]}),
        context_provider=lambda: ctx,
    )
    server._client = _FakeClient(payload)
    return server


def _sensitive_click_ctx(*, registry_complete: bool) -> AgentContext:
    ctx = make_copilot_ctx(browser_session_id="pbs_1")
    clear_session_scrub_values("pbs_1")
    ctx.last_run_blocks_workflow_run_id = "wr_sensitive"
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        "wr_sensitive",
        {"copilot_run_runtime_secret_values": ("654321",)},
        contains_sensitive_values=True,
        contains_all_sensitive_values=registry_complete,
    )
    taint_by_terminal_run(ctx, workflow_run_id="wr_sensitive", session_id="pbs_1")
    return ctx


class TestSensitiveOriginActionContinuation:
    @pytest.mark.asyncio
    async def test_click_discloses_a_scrubbed_result_after_a_terminal_matching_run(
        self, _stub_browser_session: None
    ) -> None:
        ctx = _sensitive_click_ctx(registry_complete=True)
        server = _click_overlay_server(
            ctx,
            {
                "ok": True,
                "data": {
                    "selector": "a.nav--analytics",
                    "text_content": "Web analytics 654321",
                    "url": "https://example.test/app",
                },
            },
        )

        result = await server.call_tool("click", {"selector": "a.nav--analytics"})

        surfaced = json.loads(result.content[0].text)
        assert surfaced["ok"] is True
        assert "654321" not in result.content[0].text
        assert "[REDACTED_SECRET]" in result.content[0].text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arm", SENSITIVE_DISCLOSURE_WITHHOLDING_ARMS)
    async def test_click_stays_withheld_when_a_disclosure_prerequisite_is_absent(
        self, _stub_browser_session: None, arm: str
    ) -> None:
        # The adapter composes the predicate differently from the hooks (it also reads bare taint
        # and the overlay flag), so every arm is pinned here too, the active-run arm included.
        ctx = _sensitive_click_ctx(registry_complete=True)
        remove_sensitive_disclosure_prerequisite(ctx, arm)
        server = _click_overlay_server(
            ctx,
            {"ok": True, "data": {"selector": "a.nav--analytics", "text_content": "Web analytics 654321"}},
        )

        result = await server.call_tool("click", {"selector": "a.nav--analytics"})

        surfaced = json.loads(result.content[0].text)
        assert surfaced["ok"] is False
        assert "specific named URL" in surfaced["error"]
        assert "654321" not in result.content[0].text
