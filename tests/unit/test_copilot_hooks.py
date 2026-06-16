"""Tests for CopilotRunHooks.on_tool_end activity recording."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot import hooks as hooks_module
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.enforcement import CopilotGoalSatisfied
from skyvern.forge.sdk.copilot.hooks import CopilotRunHooks
from skyvern.forge.sdk.copilot.turn_halt import CopilotTurnHalt, turn_halt_from_blocker_signal


@dataclass
class _FakeContext:
    tool_activity: list[dict[str, Any]] = field(default_factory=list)
    workflow_permanent_id: str = "wpid_example"
    turn_id: str = "turn_example"
    workflow_copilot_chat_id: str = "chat_example"
    total_tokens_used: int | None = None


# `on_tool_end(context, agent, tool, result)` only reads `tool` and `result`;
# `context` and `agent` are unused by CopilotRunHooks, so a single sentinel
# mock stands in for both across every test.
_UNUSED = MagicMock()


def _fake_tool(name: str) -> MagicMock:
    tool = MagicMock()
    tool.name = name
    return tool


def _mcp_text_output(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Shape that `parse_tool_output` expects from FastMCP tool calls."""
    return [{"type": "text", "text": json.dumps(payload)}]


def _terminal_loop_signal() -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind="loop_detected",
        agent_steering_text="LOOP DETECTED: 'update_workflow' has already failed 3 times.",
        user_facing_reason="I retried without making progress. Tell me what to change and I'll try again.",
        recovery_hint="report_blocker_to_user",
        internal_reason_code="loop_detected_repeated_failed_step",
        blocked_tool="update_workflow",
    )


@pytest.mark.asyncio
async def test_on_tool_end_appends_generic_tool_entry() -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    output = _mcp_text_output({"ok": True, "data": {"url": "https://example.com"}})
    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("navigate_browser"), output)

    assert len(ctx.tool_activity) == 1
    entry = ctx.tool_activity[0]
    assert entry["tool"] == "navigate_browser"
    assert "summary" in entry
    assert "output_preview" not in entry  # non-whitelisted tool


@pytest.mark.asyncio
async def test_on_tool_end_logs_copilot_turn_identifiers() -> None:
    ctx = _FakeContext(total_tokens_used=123)
    hooks = CopilotRunHooks(ctx)

    output = _mcp_text_output({"ok": True, "data": {"url": "https://example.com"}})
    with capture_logs() as logs:
        await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("navigate_browser"), output)

    completed = next(log for log in logs if log["event"] == "copilot tool completed")
    assert completed["workflow_permanent_id"] == "wpid_example"
    assert completed["turn_id"] == "turn_example"
    assert completed["workflow_copilot_chat_id"] == "chat_example"


@pytest.mark.asyncio
async def test_on_tool_end_raises_turn_halt_after_activity_recording() -> None:
    ctx = _FakeContext()
    ctx.turn_halt = turn_halt_from_blocker_signal(_terminal_loop_signal(), source="test")  # type: ignore[attr-defined]
    hooks = CopilotRunHooks(ctx)

    output = _mcp_text_output({"ok": False, "error": "terminal blocker"})
    with pytest.raises(CopilotTurnHalt) as exc_info:
        await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("update_workflow"), output)

    assert exc_info.value.halt is ctx.turn_halt
    assert ctx.tool_activity[0]["tool"] == "update_workflow"


@pytest.mark.asyncio
async def test_on_tool_end_whitelisted_tool_produces_output_preview() -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    output = _mcp_text_output(
        {
            "ok": True,
            "data": {
                "blocks": [
                    {"label": "extract_prices", "output": {"prices": [10, 20]}},
                    {"label": "extract_names", "extracted_data": ["alice"]},
                ]
            },
        }
    )
    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("run_blocks_and_collect_debug"), output)

    entry = ctx.tool_activity[0]
    assert entry["tool"] == "run_blocks_and_collect_debug"
    assert "output_preview" in entry
    assert "extract_prices" in entry["output_preview"]
    assert "extract_names" in entry["output_preview"]


@pytest.mark.asyncio
async def test_on_tool_end_truncates_output_preview_at_500_chars() -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    huge_output = {"values": ["x" * 1000]}
    output = _mcp_text_output({"ok": True, "data": {"blocks": [{"label": "big", "output": huge_output}]}})
    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("get_run_results"), output)

    entry = ctx.tool_activity[0]
    preview = entry["output_preview"]
    _prefix, _sep, value = preview.partition(": ")
    assert value.endswith("...")
    assert len(value) <= 503


@pytest.mark.asyncio
async def test_on_tool_end_whitelisted_tool_without_block_outputs_skips_preview() -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    output = _mcp_text_output({"ok": True, "data": {"blocks": [{"label": "noop"}]}})
    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("update_and_run_blocks"), output)

    entry = ctx.tool_activity[0]
    assert entry["tool"] == "update_and_run_blocks"
    assert "output_preview" not in entry


@pytest.mark.asyncio
async def test_on_tool_end_failed_whitelisted_tool_skips_preview() -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    output = _mcp_text_output({"ok": False, "error": "workflow exploded"})
    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("run_blocks_and_collect_debug"), output)

    entry = ctx.tool_activity[0]
    assert "output_preview" not in entry


@pytest.mark.asyncio
async def test_on_tool_end_list_credentials_records_resolved_ids() -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    output = _mcp_text_output(
        {
            "ok": True,
            "data": {
                "credentials": [
                    {"credential_id": "cred_amazon", "name": "Amazon", "username": "shopper@example.test"},
                    {"credential_id": "cred_quicken", "name": "Quicken Classic"},
                ],
                "count": 2,
            },
        }
    )
    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("list_credentials"), output)

    entry = ctx.tool_activity[0]
    assert entry["credentials"] == [
        {"credential_id": "cred_amazon", "name": "Amazon"},
        {"credential_id": "cred_quicken", "name": "Quicken Classic"},
    ]


@pytest.mark.asyncio
async def test_on_tool_end_list_credentials_empty_skips_field() -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    output = _mcp_text_output({"ok": True, "data": {"credentials": [], "count": 0}})
    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("list_credentials"), output)

    assert "credentials" not in ctx.tool_activity[0]


@pytest.mark.asyncio
async def test_on_tool_end_swallows_unserializable_output() -> None:
    # json.dumps(default=str) can still raise if str() on the value raises --
    # on_tool_end must never propagate that into the agent loop.
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    class _Unserializable:
        def __str__(self) -> str:
            raise RuntimeError("str boom")

    payload = {"ok": True, "data": {"blocks": [{"label": "bad", "output": _Unserializable()}]}}
    with capture_logs() as logs:
        await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("run_blocks_and_collect_debug"), payload)

    # The recording path raised inside json.dumps before append. The guard
    # swallowed it, so the invariant is "the run did not crash" -- and the
    # activity entry was dropped. That is the acceptable trade for observability.
    assert ctx.tool_activity == []
    warning = next(
        log for log in logs if log["event"] == "CopilotRunHooks.on_tool_end recording failed, skipping entry"
    )
    assert warning["workflow_permanent_id"] == "wpid_example"
    assert warning["turn_id"] == "turn_example"
    assert warning["workflow_copilot_chat_id"] == "chat_example"


@pytest.mark.asyncio
async def test_on_tool_end_goal_satisfied_log_includes_copilot_turn_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)
    monkeypatch.setattr(hooks_module, "_tool_completion_satisfies_turn", lambda *_args: True)

    output = _mcp_text_output({"ok": True, "data": {"workflow_run_id": "wrid_example"}})
    with capture_logs() as logs:
        with pytest.raises(CopilotGoalSatisfied):
            await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("update_and_run_blocks"), output)

    satisfied = next(log for log in logs if log["event"] == "copilot tool satisfied goal; stopping agent loop")
    assert satisfied["workflow_permanent_id"] == "wpid_example"
    assert satisfied["turn_id"] == "turn_example"
    assert satisfied["workflow_copilot_chat_id"] == "chat_example"


class TestCopilotToCallToolResult:
    @staticmethod
    def _build(d: dict) -> Any:
        from skyvern.forge.sdk.copilot.mcp_adapter import _copilot_to_call_tool_result

        return _copilot_to_call_tool_result(d)

    def test_text_only_result(self) -> None:
        result = self._build({"ok": True, "data": "done"})
        assert len(result.content) == 1
        assert result.content[0].type == "text"
        assert result.isError is False

    def test_screenshot_payload_always_text_only(self) -> None:
        """Tool results never include images — screenshots are injected
        as synthetic user messages by the enforcement loop instead."""
        result = self._build({"ok": True, "data": {"screenshot_base64": "iVBOR"}})
        assert len(result.content) == 1
        assert result.content[0].type == "text"
        parsed = json.loads(result.content[0].text)
        assert parsed["data"]["screenshot_base64"].startswith("[base64 image omitted")

    def test_error_result(self) -> None:
        result = self._build({"ok": False, "error": "fail"})
        assert result.isError is True
        parsed = json.loads(result.content[0].text)
        assert parsed["ok"] is False
        assert parsed["error"] == "fail"

    def test_text_content_is_json(self) -> None:
        data = {"ok": True, "data": {"count": 5}}
        result = self._build(data)
        parsed = json.loads(result.content[0].text)
        assert parsed == data


class TestSchemaOverlay:
    def test_apply_schema_overlay_hides_params(self) -> None:
        from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, _apply_schema_overlay

        schema = {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "session_id": {"type": "string"},
                "cdp_url": {"type": "string"},
            },
            "required": ["url", "session_id"],
        }
        overlay = SchemaOverlay(
            hide_params=frozenset({"session_id", "cdp_url"}),
        )
        result = _apply_schema_overlay(schema, overlay)
        assert "session_id" not in result["properties"]
        assert "cdp_url" not in result["properties"]
        assert "url" in result["properties"]
        assert "session_id" not in result["required"]

    def test_apply_schema_overlay_renames_args(self) -> None:
        from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, _apply_schema_overlay

        schema = {
            "type": "object",
            "properties": {
                "clear": {"type": "boolean"},
                "text": {"type": "string"},
            },
            "required": ["clear", "text"],
        }
        overlay = SchemaOverlay(
            arg_transforms={"clear_first": "clear"},
        )
        result = _apply_schema_overlay(schema, overlay)
        assert "clear_first" in result["properties"]
        assert "clear" not in result["properties"]
        assert "clear_first" in result["required"]

    def test_transform_args_reverses_and_injects(self) -> None:
        from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, _transform_args

        overlay = SchemaOverlay(
            arg_transforms={"clear_first": "clear"},
            forced_args={"inline": True},
        )
        args = {"clear_first": True, "text": "hello", "_summarized": "older tool call"}
        result = _transform_args(args, overlay)
        assert result == {"clear": True, "text": "hello", "inline": True}
        assert "clear_first" not in result


class TestMCPFailedStepLoopDetection:
    @pytest.mark.asyncio
    async def test_post_hook_exception_preserves_successful_tool_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot import mcp_adapter
        from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, SkyvernOverlayMCPServer
        from skyvern.forge.sdk.copilot.output_utils import summarize_tool_result

        class FakeRawResult:
            structured_content = {
                "ok": True,
                "data": {"selector": None, "resolved_selector": "xpath=//button[2]", "status": "clicked"},
            }
            is_error = False
            content: list[Any] = []

        class FakeClient:
            async def call_tool(
                self,
                name: str,
                args: dict[str, Any],
                raise_on_error: bool = False,
            ) -> FakeRawResult:
                return FakeRawResult()

        async def raising_post_hook(
            result: dict[str, Any],
            raw: dict[str, Any],
            ctx: Any,
        ) -> dict[str, Any]:
            ctx.scouted_interactions.append({"tool_name": "click", "selector": "#partial"})
            ctx.scout_trajectory.append({"tool_name": "click", "selector": "#partial", "trajectory_index": 1})
            ctx.flow_evidence.append({"step": 2, "evidence": {"source_tool": "partial"}})
            ctx.pending_browser_interaction_observation = SimpleNamespace(tool_name="click", url="https://partial")
            ctx.pending_scout_source_url = None
            ctx.pending_scout_typed_value = "partial"
            raise AttributeError("'NoneType' object has no attribute 'strip'")

        recorded: list[dict[str, Any]] = []
        screenshots: list[dict[str, Any]] = []
        monkeypatch.setattr(
            mcp_adapter,
            "record_tool_step_result_for_ctx",
            lambda _ctx, _tool, _args, result: recorded.append(dict(result)),
        )
        monkeypatch.setattr(
            mcp_adapter,
            "enqueue_screenshot_from_result",
            lambda _ctx, result: screenshots.append(dict(result)),
        )

        initial_scouted_interactions = [{"tool_name": "click", "selector": "#existing"}]
        initial_scout_trajectory = [{"tool_name": "click", "selector": "#existing", "trajectory_index": 0}]
        initial_flow_evidence = [{"step": 1, "evidence": {"source_tool": "existing"}}]
        initial_pending_observation = SimpleNamespace(tool_name="click", url="https://existing")
        ctx = SimpleNamespace(
            consecutive_tool_tracker=[],
            failed_tool_step_tracker={},
            scouted_interactions=list(initial_scouted_interactions),
            scout_trajectory=list(initial_scout_trajectory),
            flow_evidence=list(initial_flow_evidence),
            pending_browser_interaction_observation=initial_pending_observation,
            pending_scout_source_url="https://source",
            pending_scout_typed_value="typed",
        )
        server = SkyvernOverlayMCPServer(
            transport=MagicMock(),
            overlays={"click": SchemaOverlay(post_hook=raising_post_hook)},
            alias_map={},
            allowlist=frozenset(),
            context_provider=lambda: ctx,
        )
        server._client = FakeClient()

        result = await server.call_tool("click", {"intent": "click the add button"})

        parsed = json.loads(result.content[0].text)
        assert result.isError is False
        assert parsed == {
            "ok": True,
            "data": {"selector": None, "resolved_selector": "xpath=//button[2]", "status": "clicked"},
        }
        assert summarize_tool_result("click", parsed) == "Clicked 'xpath=//button[2]'"
        assert recorded == [parsed]
        assert screenshots == [parsed]
        assert ctx.scouted_interactions == initial_scouted_interactions
        assert ctx.scout_trajectory == initial_scout_trajectory
        assert ctx.flow_evidence == initial_flow_evidence
        assert ctx.pending_browser_interaction_observation == initial_pending_observation
        assert ctx.pending_scout_source_url == "https://source"
        assert ctx.pending_scout_typed_value == "typed"

    @pytest.mark.asyncio
    async def test_post_hook_exception_preserves_failing_tool_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot import mcp_adapter
        from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, SkyvernOverlayMCPServer

        class FakeRawResult:
            structured_content = {"ok": False, "error": "element not found"}
            is_error = True
            content: list[Any] = []

        class FakeClient:
            async def call_tool(
                self,
                name: str,
                args: dict[str, Any],
                raise_on_error: bool = False,
            ) -> FakeRawResult:
                return FakeRawResult()

        async def raising_post_hook(
            result: dict[str, Any],
            raw: dict[str, Any],
            ctx: Any,
        ) -> dict[str, Any]:
            raise RuntimeError("post-hook failed")

        recorded: list[dict[str, Any]] = []
        monkeypatch.setattr(
            mcp_adapter,
            "record_tool_step_result_for_ctx",
            lambda _ctx, _tool, _args, result: recorded.append(dict(result)),
        )

        ctx = MagicMock()
        ctx.consecutive_tool_tracker = []
        ctx.failed_tool_step_tracker = {}
        server = SkyvernOverlayMCPServer(
            transport=MagicMock(),
            overlays={"click": SchemaOverlay(post_hook=raising_post_hook)},
            alias_map={},
            allowlist=frozenset(),
            context_provider=lambda: ctx,
        )
        server._client = FakeClient()

        result = await server.call_tool("click", {"selector": "#missing"})

        parsed = json.loads(result.content[0].text)
        assert result.isError is True
        assert parsed == {"ok": False, "error": "element not found"}
        assert recorded == [parsed]

    @pytest.mark.asyncio
    async def test_browser_tool_call_is_created_inside_copilot_browser_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.forge.sdk.copilot import mcp_adapter
        from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, SkyvernOverlayMCPServer

        class FakeRawResult:
            structured_content = {"ok": True, "data": {}}
            is_error = False
            content: list[Any] = []

        in_context = False
        calls: list[tuple[str, dict[str, Any], bool]] = []

        class FakeClient:
            async def call_tool(
                self,
                name: str,
                args: dict[str, Any],
                raise_on_error: bool = False,
            ) -> FakeRawResult:
                calls.append((name, args, in_context))
                return FakeRawResult()

        async def fake_ensure_browser_session(ctx: Any) -> None:
            ctx.browser_session_id = "pbs_copilot"
            return None

        @asynccontextmanager
        async def fake_mcp_browser_context(ctx: Any) -> Any:
            nonlocal in_context
            in_context = True
            try:
                yield
            finally:
                in_context = False

        monkeypatch.setattr(mcp_adapter, "ensure_browser_session", fake_ensure_browser_session)
        monkeypatch.setattr(mcp_adapter, "mcp_browser_context", fake_mcp_browser_context)

        ctx = MagicMock()
        ctx.consecutive_tool_tracker = []
        ctx.failed_tool_step_tracker = {}
        server = SkyvernOverlayMCPServer(
            transport=MagicMock(),
            overlays={"get_browser_screenshot": SchemaOverlay(requires_browser=True)},
            alias_map={},
            allowlist=frozenset(),
            context_provider=lambda: ctx,
        )
        server._client = FakeClient()

        result = await server.call_tool("get_browser_screenshot", {})

        assert result.isError is False
        assert calls == [("get_browser_screenshot", {"session_id": "pbs_copilot"}, True)]

    @pytest.mark.asyncio
    async def test_browser_overlay_reaches_fastmcp_tool_with_registered_copilot_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.cli.core import client as client_mod
        from skyvern.cli.core import session_manager
        from skyvern.cli.mcp_tools import browser as browser_tools
        from skyvern.cli.mcp_tools import mcp
        from skyvern.forge.sdk.copilot import runtime
        from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, SkyvernOverlayMCPServer
        from skyvern.forge.sdk.copilot.runtime import AgentContext
        from skyvern.forge.sdk.copilot.tools import _screenshot_post_hook

        client_mod._skyvern_instance.set(None)
        client_mod._api_key_override.set(None)
        client_mod._global_skyvern_instance = None
        client_mod._api_key_clients.clear()
        session_manager._current_session.set(None)
        session_manager._global_session = None
        session_manager._copilot_sessions.clear()
        session_manager.set_stateless_http_mode(False)

        raw_page = MagicMock()
        raw_page.is_closed.return_value = False
        raw_page.on = MagicMock()
        raw_page.url = "https://example.com"
        browser_context = SimpleNamespace(
            pages=[raw_page],
            on=MagicMock(),
            _impl_obj=SimpleNamespace(_close_was_called=False, _closed=False),
            browser=SimpleNamespace(is_connected=lambda: True),
        )
        browser_state = SimpleNamespace(browser_context=browser_context)
        persistent_session_manager = SimpleNamespace(
            get_browser_state=AsyncMock(return_value=browser_state),
        )
        monkeypatch.setattr(runtime.app, "PERSISTENT_SESSIONS_MANAGER", persistent_session_manager)
        monkeypatch.setattr(runtime.settings, "ENV", "local")

        runtime_skyvern = MagicMock()
        monkeypatch.setattr(runtime, "get_skyvern", lambda: runtime_skyvern)

        class FakeSkyvernBrowser:
            def __init__(
                self,
                skyvern: Any,
                browser_context: Any,
                *,
                browser_session_id: str | None = None,
                browser_address: str | None = None,
            ) -> None:
                del skyvern, browser_address
                self._browser_context = browser_context
                self._browser_session_id = browser_session_id

            async def get_working_page(self) -> Any:
                return SimpleNamespace(is_closed=lambda: False)

        monkeypatch.setattr(runtime, "SkyvernBrowser", FakeSkyvernBrowser)

        fallback_skyvern = MagicMock()
        fallback_skyvern.connect_to_cloud_browser_session = AsyncMock(
            side_effect=AssertionError("unexpected SDK reconnect")
        )
        monkeypatch.setattr(session_manager, "get_skyvern", lambda: fallback_skyvern)

        observed_session_ids: list[str | None] = []
        observed_localhost_access: list[bool | None] = []

        async def fake_do_screenshot(page: Any, full_page: bool = False, selector: str | None = None) -> Any:
            del page, full_page, selector
            current = session_manager.get_current_session()
            observed_session_ids.append(current.context.session_id if current.context else None)
            observed_localhost_access.append(current.context.can_access_localhost if current.context else None)
            assert current.api_key_hash == session_manager._api_key_hash("sk-copilot-org")
            return SimpleNamespace(data=b"fake-png")

        monkeypatch.setattr(browser_tools, "do_screenshot", fake_do_screenshot)

        ctx = AgentContext(
            organization_id="org-1",
            workflow_id="wf-1",
            workflow_permanent_id="wfp-1",
            workflow_yaml="",
            browser_session_id="pbs_copilot",
            stream=MagicMock(is_disconnected=AsyncMock(return_value=False)),
            api_key="sk-copilot-org",
        )

        server = SkyvernOverlayMCPServer(
            transport=mcp,
            overlays={
                "get_browser_screenshot": SchemaOverlay(
                    requires_browser=True,
                    forced_args={"inline": True},
                    post_hook=_screenshot_post_hook,
                )
            },
            alias_map={"get_browser_screenshot": "skyvern_screenshot"},
            allowlist=frozenset({"skyvern_screenshot"}),
            context_provider=lambda: ctx,
        )

        await server.connect()
        try:
            result = await server.call_tool("get_browser_screenshot", {})
        finally:
            await server.cleanup()
            session_manager._copilot_sessions.clear()

        parsed = json.loads(result.content[0].text)
        assert result.isError is False
        assert parsed["ok"] is True
        assert parsed["data"]["screenshot_base64"]
        assert observed_session_ids == ["pbs_copilot"]
        assert observed_localhost_access == [True]
        fallback_skyvern.connect_to_cloud_browser_session.assert_not_awaited()
        persistent_session_manager.get_browser_state.assert_any_await(
            session_id="pbs_copilot",
            organization_id="org-1",
        )
        assert persistent_session_manager.get_browser_state.await_args_list[0] == call(
            session_id="pbs_copilot",
            organization_id="org-1",
        )

    @pytest.mark.asyncio
    async def test_interleaved_same_step_failures_short_circuit_third_dispatch(self) -> None:
        from skyvern.forge.sdk.copilot.mcp_adapter import SkyvernOverlayMCPServer

        class FakeRawResult:
            def __init__(self, payload: dict[str, Any], is_error: bool = False) -> None:
                self.structured_content = payload
                self.is_error = is_error
                self.content: list[Any] = []

        class FakeClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, Any]]] = []

            async def call_tool(
                self,
                name: str,
                args: dict[str, Any],
                raise_on_error: bool = False,
            ) -> FakeRawResult:
                self.calls.append((name, args))
                if name == "get_browser_screenshot":
                    return FakeRawResult({"ok": False, "error": "screenshot failed"}, is_error=True)
                return FakeRawResult({"ok": True, "data": {"status": "failed"}})

        ctx = MagicMock()
        ctx.consecutive_tool_tracker = []
        ctx.failed_tool_step_tracker = {}
        client = FakeClient()
        server = SkyvernOverlayMCPServer(
            transport=MagicMock(),
            overlays={},
            alias_map={},
            allowlist=frozenset(),
            context_provider=lambda: ctx,
        )
        server._client = client

        await server.call_tool("get_browser_screenshot", {})
        await server.call_tool("get_run_results", {})
        await server.call_tool("get_browser_screenshot", {})
        await server.call_tool("get_run_results", {})
        blocked = await server.call_tool("get_browser_screenshot", {})

        parsed = json.loads(blocked.content[0].text)
        assert blocked.isError is True
        assert "LOOP DETECTED" in parsed["error"]
        assert client.calls == [
            ("get_browser_screenshot", {}),
            ("get_run_results", {}),
            ("get_browser_screenshot", {}),
            ("get_run_results", {}),
        ]


class TestMCPToolOverlayCompleteness:
    """Verify alias map and overlay configs are in sync and complete."""

    def test_alias_map_covers_expected_tools(self) -> None:
        from skyvern.forge.sdk.copilot.tools import get_skyvern_mcp_alias_map

        alias_map = get_skyvern_mcp_alias_map()
        expected_aliases = {
            "get_block_schema",
            "validate_block",
            "navigate_browser",
            "get_browser_screenshot",
            "evaluate",
            "click",
            "type_text",
            "scroll",
            "console_messages",
            "select_option",
            "press_key",
        }
        assert set(alias_map.keys()) == expected_aliases
        assert all(v.startswith("skyvern_") for v in alias_map.values())

    def test_every_alias_has_overlay(self) -> None:
        from skyvern.forge.sdk.copilot.tools import (
            _build_skyvern_mcp_overlays,
            get_skyvern_mcp_alias_map,
        )

        alias_map = get_skyvern_mcp_alias_map()
        overlays = _build_skyvern_mcp_overlays()
        missing = set(alias_map.keys()) - set(overlays.keys())
        assert not missing, f"Alias map keys missing overlay configs: {missing}"

    def test_browser_tools_require_browser(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlays = _build_skyvern_mcp_overlays()
        browser_tools = {
            "navigate_browser",
            "get_browser_screenshot",
            "evaluate",
            "click",
            "type_text",
            "scroll",
            "console_messages",
            "select_option",
            "press_key",
        }
        for name in browser_tools:
            assert overlays[name].requires_browser, f"{name} should have requires_browser=True"

    def test_intent_not_hidden_on_browser_tools(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlays = _build_skyvern_mcp_overlays()
        tools_with_intent = {"click", "type_text", "scroll", "select_option", "press_key"}
        for name in tools_with_intent:
            hidden = overlays[name].hide_params or frozenset()
            assert "intent" not in hidden, f"{name} should NOT hide intent"


class TestNewToolOverlayConfigs:
    """Verify the 4 new tool overlay configs are correct."""

    def test_scroll_overlay(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlay = _build_skyvern_mcp_overlays()["scroll"]
        assert overlay.hide_params == frozenset({"session_id", "cdp_url"})
        assert overlay.requires_browser is True
        assert overlay.post_hook is not None

    def test_console_messages_overlay(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlay = _build_skyvern_mcp_overlays()["console_messages"]
        assert overlay.hide_params == frozenset({"session_id", "cdp_url"})
        assert overlay.requires_browser is True
        assert overlay.post_hook is None

    def test_select_option_overlay(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlay = _build_skyvern_mcp_overlays()["select_option"]
        assert overlay.hide_params == frozenset({"session_id", "cdp_url", "timeout"})
        assert overlay.required_overrides == ["value"]
        assert overlay.requires_browser is True
        assert overlay.timeout == 15
        assert overlay.post_hook is not None

    def test_press_key_overlay(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlay = _build_skyvern_mcp_overlays()["press_key"]
        assert overlay.hide_params == frozenset({"session_id", "cdp_url"})
        assert overlay.required_overrides == ["key"]
        assert overlay.requires_browser is True
        assert overlay.post_hook is not None

    def test_click_and_type_overlays_steer_selector_first(self) -> None:
        # The tool contract must steer toward selector-only (deterministic) acting;
        # an `intent` routes the action through a slow full-page AI scan, so the
        # description must not invite "both" as the default (regression guard).
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlays = _build_skyvern_mcp_overlays()
        for name in ("click", "type_text"):
            desc = overlays[name].description or ""
            assert "selector ALONE" in desc, f"{name} should steer toward selector-only"
            assert "slower full-page AI scan" in desc, f"{name} should name the intent cost"
            assert "or both for resilient targeting" not in desc, f"{name} must not invite both by default"
            # intent must remain available for the genuine no-selector case
            assert "intent" not in overlays[name].hide_params

    def test_browser_action_overlays_force_direct_selector_mode(self) -> None:
        # The copilot keeps deterministic selector actions by binding selector_mode="direct"
        # via forced_args, even though the shared MCP default is resilient (SKY-10562).
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlays = _build_skyvern_mcp_overlays()
        for name in ("click", "type_text", "select_option"):
            assert overlays[name].forced_args.get("selector_mode") == "direct", (
                f"{name} overlay must force selector_mode=direct"
            )

    @pytest.mark.asyncio
    async def test_discovery_click_anchor_forces_direct_selector_mode(self) -> None:
        # call_internal_tool bypasses overlays, so the discovery path must pass selector_mode
        # explicitly; without it the anchor click would silently regress to the resilient default.
        from skyvern.forge.sdk.copilot.tools import _discovery_click_anchor

        call_internal_tool = AsyncMock(return_value={"ok": True})
        ctx = SimpleNamespace(discovery_mcp_server=SimpleNamespace(call_internal_tool=call_internal_tool))

        await _discovery_click_anchor(ctx, {"href": "https://example.com/cart"})

        call_internal_tool.assert_awaited_once()
        tool_name, tool_args = call_internal_tool.await_args.args
        assert tool_name == "skyvern_click"
        assert tool_args.get("selector_mode") == "direct"


class TestNewToolSummaries:
    """Verify summarize_tool_result handles the 4 new tools."""

    @staticmethod
    def _summarize(name: str, result: dict[str, Any]) -> str:
        from skyvern.forge.sdk.copilot.output_utils import summarize_tool_result

        return summarize_tool_result(name, result)

    def test_scroll_summary(self) -> None:
        result = {"ok": True, "data": {"direction": "down", "amount": 500}}
        assert "down" in self._summarize("scroll", result)

    def test_console_messages_summary(self) -> None:
        result = {"ok": True, "data": {"count": 3}}
        summary = self._summarize("console_messages", result)
        assert "3" in summary
        assert "console" in summary.lower()

    def test_select_option_summary(self) -> None:
        result = {"ok": True, "data": {"value": "USD", "selector": "#currency"}}
        assert "USD" in self._summarize("select_option", result)

    def test_press_key_summary(self) -> None:
        result = {"ok": True, "data": {"key": "Enter"}}
        assert "Enter" in self._summarize("press_key", result)


class TestObservationToolsSet:
    """Verify _OBSERVATION_TOOLS includes all browser interaction tools."""

    def test_contains_new_tools(self) -> None:
        from skyvern.forge.sdk.copilot.streaming_adapter import _OBSERVATION_TOOLS

        expected = {"scroll", "console_messages", "select_option", "press_key"}
        assert expected.issubset(_OBSERVATION_TOOLS)


class TestVerifyScoutTypeLanded:
    """A scout type that an overlay silently consumed must surface as a failure."""

    def _ctx_with_value(self, value: Any) -> SimpleNamespace:
        server = SimpleNamespace()
        server.call_internal_tool = AsyncMock(return_value={"ok": True, "data": {"value": value}})
        return SimpleNamespace(discovery_mcp_server=server)

    @pytest.mark.asyncio
    async def test_empty_field_after_nonempty_type_returns_failure(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _verify_scout_type_landed

        ctx = self._ctx_with_value("")
        result = await _verify_scout_type_landed(ctx, selector="#search-input", typed_length=12)

        assert result is not None
        assert result["ok"] is False
        assert "still empty" in result["error"]
        # an empty read settles and re-reads once before declaring the type lost
        assert ctx.discovery_mcp_server.call_internal_tool.await_count == 2
        ctx.discovery_mcp_server.call_internal_tool.assert_awaited_with(
            "skyvern_get_value", {"selector": "#search-input"}
        )

    @pytest.mark.asyncio
    async def test_empty_then_filled_on_reread_passes(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _verify_scout_type_landed

        server = SimpleNamespace()
        server.call_internal_tool = AsyncMock(
            side_effect=[
                {"ok": True, "data": {"value": ""}},
                {"ok": True, "data": {"value": "hello world"}},
            ]
        )
        ctx = SimpleNamespace(discovery_mcp_server=server)

        result = await _verify_scout_type_landed(ctx, selector="#search-input", typed_length=12)

        assert result is None
        assert server.call_internal_tool.await_count == 2

    @pytest.mark.asyncio
    async def test_nonempty_field_passes(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _verify_scout_type_landed

        ctx = self._ctx_with_value("hello world")
        result = await _verify_scout_type_landed(ctx, selector="#search-input", typed_length=12)

        assert result is None

    @pytest.mark.asyncio
    async def test_no_selector_skips_readback(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _verify_scout_type_landed

        ctx = self._ctx_with_value("")
        result = await _verify_scout_type_landed(ctx, selector="", typed_length=12)

        assert result is None
        ctx.discovery_mcp_server.call_internal_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zero_typed_length_skips_readback(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _verify_scout_type_landed

        ctx = self._ctx_with_value("")
        result = await _verify_scout_type_landed(ctx, selector="#search-input", typed_length=0)

        assert result is None
        ctx.discovery_mcp_server.call_internal_tool.assert_not_awaited()


class TestBrowserInteractionObservationHooks:
    @pytest.mark.asyncio
    async def test_click_hook_marks_pending_interaction_observation(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _click_post_hook

        ctx = SimpleNamespace(
            pending_browser_interaction_observation=None,
            pending_scout_typed_value=None,
            discovery_mcp_server=None,
            scouted_interactions=[],
            scout_trajectory=[],
            pending_scout_source_url=None,
        )
        result = await _click_post_hook(
            {"ok": True, "data": {"selector": "#add-to-cart"}},
            {"browser_context": {"url": "https://example.com/results", "title": "Results"}},
            ctx,
        )

        assert result["data"] == {
            "selector": "#add-to-cart",
            "effective_target": "#add-to-cart",
            "url": "https://example.com/results",
            "title": "Results",
        }
        assert ctx.pending_browser_interaction_observation is not None
        assert ctx.pending_browser_interaction_observation.tool_name == "click"
        assert ctx.pending_browser_interaction_observation.url == "https://example.com/results"

    @pytest.mark.asyncio
    async def test_failed_click_hook_clears_stale_pending_interaction_observation(self) -> None:
        from skyvern.forge.sdk.copilot.runtime import PendingBrowserInteractionObservation
        from skyvern.forge.sdk.copilot.tools import _click_post_hook

        ctx = SimpleNamespace(
            pending_browser_interaction_observation=PendingBrowserInteractionObservation(
                tool_name="click",
                url="https://example.com/results",
            ),
            pending_scout_typed_value=None,
            discovery_mcp_server=None,
            scouted_interactions=[],
            scout_trajectory=[],
            pending_scout_source_url=None,
        )

        result = await _click_post_hook(
            {"ok": False, "error": "element not found"},
            {"browser_context": {"url": "https://example.com/results", "title": "Results"}},
            ctx,
        )

        assert result == {"ok": False, "error": "element not found"}
        assert ctx.pending_browser_interaction_observation is None

    @pytest.mark.asyncio
    async def test_type_hook_does_not_mark_pending_interaction_when_readback_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from skyvern.forge.sdk.copilot import tools as tools_module

        async def fake_verify(*_args: object, **_kwargs: object) -> dict[str, object]:
            return {"ok": False, "error": "field is still empty"}

        monkeypatch.setattr(tools_module.mcp_hooks, "_verify_scout_type_landed", fake_verify)
        ctx = SimpleNamespace(
            pending_browser_interaction_observation=None,
            pending_scout_typed_value=None,
            discovery_mcp_server=None,
            scouted_interactions=[],
            scout_trajectory=[],
            pending_scout_source_url=None,
        )

        result = await tools_module._type_text_post_hook(
            {"ok": True, "data": {"selector": "#q", "text_length": 12}},
            {"browser_context": {"url": "https://example.com/search", "title": "Search"}},
            ctx,
        )

        assert result == {"ok": False, "error": "field is still empty"}
        assert ctx.pending_browser_interaction_observation is None


class TestScoutedInteractionCapture:
    """A scouted interaction with a concrete selector is captured and surfaced to
    code-only authoring; intent-only and failed-readback actions are not."""

    def _ctx(self, *, policy: object = None, source_url: str | None = None) -> SimpleNamespace:
        ns = SimpleNamespace(
            pending_browser_interaction_observation=None,
            pending_scout_typed_value=None,
            discovery_mcp_server=None,
            scouted_interactions=[],
            scout_trajectory=[],
            observed_browser_urls=[],
            pending_scout_source_url=source_url,
        )
        if policy is not None:
            ns.block_authoring_policy = policy
        return ns

    def test_record_requires_concrete_selector(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _record_scouted_interaction

        ctx = self._ctx()
        _record_scouted_interaction(ctx, tool_name="click", selector="")
        _record_scouted_interaction(ctx, tool_name="type_text", selector="   ")
        assert ctx.scouted_interactions == []

    def test_record_press_key_without_selector_is_kept(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _record_scouted_interaction

        ctx = self._ctx()
        _record_scouted_interaction(ctx, tool_name="press_key", selector="", key="Enter")
        assert ctx.scouted_interactions == [{"tool_name": "press_key", "key": "Enter"}]

    def test_record_takes_source_url_param(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _record_scouted_interaction

        ctx = self._ctx()
        _record_scouted_interaction(
            ctx, tool_name="click", selector="#add-to-cart", source_url="https://example.com/product"
        )
        assert ctx.scouted_interactions == [
            {"tool_name": "click", "selector": "#add-to-cart", "source_url": "https://example.com/product"}
        ]

    def test_consume_scout_source_url_reads_and_clears(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _consume_scout_source_url

        ctx = self._ctx(source_url="https://example.com/product")
        assert _consume_scout_source_url(ctx) == "https://example.com/product"
        # cleared so a failed/non-recording action cannot bleed into a later interaction
        assert ctx.pending_scout_source_url is None
        assert _consume_scout_source_url(ctx) is None

    def test_record_dedups_identical_interaction(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _record_scouted_interaction

        ctx = self._ctx()
        _record_scouted_interaction(ctx, tool_name="click", selector="#x", source_url="https://e.com/a")
        _record_scouted_interaction(ctx, tool_name="click", selector="#x", source_url="https://e.com/a")
        _record_scouted_interaction(ctx, tool_name="click", selector="#y", source_url="https://e.com/a")
        assert ctx.scouted_interactions == [
            {"tool_name": "click", "selector": "#x", "source_url": "https://e.com/a"},
            {"tool_name": "click", "selector": "#y", "source_url": "https://e.com/a"},
        ]

    def test_record_drops_zero_typed_length(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _record_scouted_interaction

        ctx = self._ctx()
        _record_scouted_interaction(ctx, tool_name="type_text", selector="#q", typed_length=0)
        assert ctx.scouted_interactions == [{"tool_name": "type_text", "selector": "#q"}]

    def test_record_omits_empty_extras_and_caps_history(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _MAX_SCOUTED_INTERACTIONS, _record_scouted_interaction

        ctx = self._ctx()
        for index in range(_MAX_SCOUTED_INTERACTIONS + 5):
            _record_scouted_interaction(ctx, tool_name="click", selector=f"#item-{index}")
        assert len(ctx.scouted_interactions) == _MAX_SCOUTED_INTERACTIONS
        # oldest dropped, newest kept
        assert ctx.scouted_interactions[-1]["selector"] == f"#item-{_MAX_SCOUTED_INTERACTIONS + 4}"
        assert "source_url" not in ctx.scouted_interactions[-1]

    @pytest.mark.asyncio
    async def test_click_post_hook_registers_interaction_reached_observation(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _click_post_hook

        ctx = self._ctx(source_url="https://example.com/product")
        ctx.flow_evidence = []
        result = await _click_post_hook(
            {"ok": True, "data": {"selector": "#add-to-cart"}},
            {"browser_context": {"url": "https://example.com/cart", "title": "Cart"}},
            ctx,
        )
        assert len(ctx.flow_evidence) == 1
        entry = ctx.flow_evidence[0]
        assert entry["reached_via"] == "interaction"
        assert entry["evidence"]["source_tool"] == "scout_interaction"
        assert entry["evidence"]["current_url"] == "https://example.com/cart"
        assert entry["evidence"]["interaction_selector"] == "#add-to-cart"
        assert entry["evidence"]["interaction_source_url"] == "https://example.com/product"
        assert result["observation_step"] == entry["step"]
        assert result["data"]["observation_step"] == entry["step"]

    @pytest.mark.asyncio
    async def test_post_hook_skips_observation_without_flow_evidence(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _click_post_hook

        ctx = self._ctx()  # no flow_evidence on this context
        result = await _click_post_hook(
            {"ok": True, "data": {"selector": "#add-to-cart"}},
            {"browser_context": {"url": "https://example.com/cart", "title": "Cart"}},
            ctx,
        )
        assert "observation_step" not in result

    @pytest.mark.asyncio
    async def test_click_post_hook_records_source_page_not_destination(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _click_post_hook

        # pre-hook stashed the product page; the click navigates to the cart
        ctx = self._ctx(source_url="https://example.com/product")
        await _click_post_hook(
            {"ok": True, "data": {"selector": "#add-to-cart"}},
            {"browser_context": {"url": "https://example.com/cart", "title": "Cart"}},
            ctx,
        )
        assert ctx.scouted_interactions == [
            {"tool_name": "click", "selector": "#add-to-cart", "source_url": "https://example.com/product"}
        ]

    @pytest.mark.asyncio
    async def test_click_post_hook_omits_source_url_when_unavailable(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _click_post_hook

        ctx = self._ctx()  # no pre-hook source url
        await _click_post_hook(
            {"ok": True, "data": {"selector": "#add-to-cart"}},
            {"browser_context": {"url": "https://example.com/cart", "title": "Cart"}},
            ctx,
        )
        assert ctx.scouted_interactions == [{"tool_name": "click", "selector": "#add-to-cart"}]

    @pytest.mark.asyncio
    async def test_type_post_hook_records_selector_and_length_not_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot import tools as tools_module

        async def passes(*_a: object, **_k: object) -> None:
            return None

        monkeypatch.setattr(tools_module.mcp_hooks, "_verify_scout_type_landed", passes)
        ctx = self._ctx()
        await tools_module._type_text_post_hook(
            {"ok": True, "data": {"selector": "#q", "text_length": 8}},
            {"browser_context": {"url": "https://example.com/search", "title": "Search"}},
            ctx,
        )
        assert ctx.scouted_interactions == [{"tool_name": "type_text", "selector": "#q", "typed_length": 8}]
        # the raw typed text is never captured (PII)
        assert all("text" not in item for item in ctx.scouted_interactions)

    @pytest.mark.asyncio
    async def test_type_post_hook_records_nothing_when_readback_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot import tools as tools_module

        async def fails(*_a: object, **_k: object) -> dict[str, object]:
            return {"ok": False, "error": "field is still empty"}

        monkeypatch.setattr(tools_module.mcp_hooks, "_verify_scout_type_landed", fails)
        ctx = self._ctx()
        await tools_module._type_text_post_hook(
            {"ok": True, "data": {"selector": "#q", "text_length": 8}},
            {"browser_context": {"url": "https://example.com/search", "title": "Search"}},
            ctx,
        )
        assert ctx.scouted_interactions == []

    @pytest.mark.asyncio
    async def test_select_and_press_key_capture_value_and_key(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _press_key_post_hook, _select_option_post_hook

        ctx = self._ctx()
        await _select_option_post_hook(
            {"ok": True, "data": {"selector": "#sort", "value": "price_asc"}},
            {"browser_context": {"url": "https://example.com/results", "title": "Results"}},
            ctx,
        )
        await _press_key_post_hook(
            {"ok": True, "data": {"selector": "#q", "key": "Enter"}},
            {"browser_context": {"url": "https://example.com/results", "title": "Results"}},
            ctx,
        )
        assert ctx.scouted_interactions == [
            {"tool_name": "select_option", "selector": "#sort", "value": "price_asc"},
            {"tool_name": "press_key", "selector": "#q", "key": "Enter"},
        ]

    @pytest.mark.asyncio
    async def test_multi_action_sequence_preserves_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot import tools as tools_module

        async def passes(*_a: object, **_k: object) -> None:
            return None

        monkeypatch.setattr(tools_module.mcp_hooks, "_verify_scout_type_landed", passes)
        ctx = self._ctx()
        await tools_module._type_text_post_hook(
            {"ok": True, "data": {"selector": "#q", "text_length": 8}},
            {"browser_context": {"url": "https://example.com/search", "title": "Search"}},
            ctx,
        )
        await tools_module._press_key_post_hook(
            {"ok": True, "data": {"selector": "#q", "key": "Enter"}},
            {"browser_context": {"url": "https://example.com/results", "title": "Results"}},
            ctx,
        )
        assert [item["tool_name"] for item in ctx.scouted_interactions] == ["type_text", "press_key"]

    @pytest.mark.asyncio
    async def test_post_hook_clears_source_url_even_when_action_fails(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _click_post_hook

        # a failed click must not leave its source page to bleed into a later interaction
        ctx = self._ctx(source_url="https://example.com/product")
        await _click_post_hook({"ok": False, "error": "not found"}, {}, ctx)
        assert ctx.pending_scout_source_url is None
        assert ctx.scouted_interactions == []

    @pytest.mark.asyncio
    async def test_capture_scout_source_url_reads_live_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.copilot import tools as tools_module
        from skyvern.forge.sdk.copilot.tools import scouting as scouting_module

        async def fake_url(_ctx: object) -> str:
            return "https://example.com/product"

        monkeypatch.setattr(scouting_module, "_live_working_page_url", fake_url)
        ctx = self._ctx()
        await tools_module._capture_scout_source_url(ctx)
        assert ctx.pending_scout_source_url == "https://example.com/product"

    @pytest.mark.asyncio
    async def test_select_option_post_hook_surfaces_observation_step(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _select_option_post_hook

        ctx = self._ctx()
        ctx.flow_evidence = []
        result = await _select_option_post_hook(
            {"ok": True, "data": {"selector": "#sort", "value": "price_asc"}},
            {"browser_context": {"url": "https://example.com/results", "title": "Results"}},
            ctx,
        )
        entry = ctx.flow_evidence[0]
        assert entry["reached_via"] == "interaction"
        assert entry["evidence"]["interaction_selector"] == "#sort"
        assert result["data"]["observation_step"] == entry["step"]

    @pytest.mark.asyncio
    async def test_intent_click_uses_resolved_selector_when_raw_selector_is_none(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _click_post_hook

        ctx = self._ctx(source_url="https://example.com/product")
        ctx.flow_evidence = []
        result = await _click_post_hook(
            {
                "ok": True,
                "data": {
                    "selector": None,
                    "intent": "click the add button",
                    "resolved_selector": "xpath=//button[2]",
                },
            },
            {"browser_context": {"url": "https://example.com/cart", "title": "Cart"}},
            ctx,
        )

        assert result["ok"] is True
        assert result["data"]["selector"] == "xpath=//button[2]"
        assert result["data"]["effective_target"] == "xpath=//button[2]"
        assert ctx.scouted_interactions == [
            {
                "tool_name": "click",
                "selector": "xpath=//button[2]",
                "source_url": "https://example.com/product",
            }
        ]
        assert ctx.flow_evidence[0]["evidence"]["interaction_selector"] == "xpath=//button[2]"

    @pytest.mark.asyncio
    async def test_click_post_hook_preserves_raw_selector_over_resolved_selector(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _click_post_hook

        ctx = self._ctx()
        result = await _click_post_hook(
            {"ok": True, "data": {"selector": "#add-to-cart", "resolved_selector": "xpath=//button[2]"}},
            {"browser_context": {"url": "https://example.com/product", "title": "Product"}},
            ctx,
        )

        assert result["data"]["effective_target"] == "#add-to-cart"
        assert ctx.scouted_interactions == [{"tool_name": "click", "selector": "#add-to-cart"}]

    @pytest.mark.asyncio
    async def test_click_post_hook_prefers_accessible_label_for_effective_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.forge.sdk.copilot import tools as tools_module

        async def resolved_label(*_a: object, **_k: object) -> tuple[str, str]:
            return "button", "Accept"

        monkeypatch.setattr(tools_module.mcp_hooks, "_resolve_scout_role_name", resolved_label)
        ctx = self._ctx()

        result = await tools_module._click_post_hook(
            {"ok": True, "data": {"selector": None, "resolved_selector": "xpath=//button[2]"}},
            {"browser_context": {"url": "https://example.com/product", "title": "Product"}},
            ctx,
        )

        assert result["data"]["selector"] == "xpath=//button[2]"
        assert result["data"]["effective_target"] == "button Accept"

    @pytest.mark.asyncio
    async def test_scout_helpers_tolerate_selector_none(self) -> None:
        from skyvern.forge.sdk.copilot.tools import (
            _record_scouted_interaction,
            _register_scout_interaction_observation,
            _resolve_scout_role_name,
        )

        ctx = self._ctx()
        ctx.flow_evidence = []

        assert await _resolve_scout_role_name(ctx, None) == ("", "")
        _record_scouted_interaction(ctx, tool_name="click", selector=None)
        observation_step, page_evidence = await _register_scout_interaction_observation(
            ctx,
            tool_name="click",
            selector=None,
            source_url="https://example.com/product",
            url="https://example.com/cart",
        )

        assert ctx.scouted_interactions == []
        assert ctx.flow_evidence == []
        assert observation_step is None
        assert page_evidence is None

    @pytest.mark.asyncio
    async def test_selector_none_interaction_hooks_degrade_without_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.forge.sdk.copilot import tools as tools_module

        async def passes(*_a: object, **_k: object) -> None:
            return None

        monkeypatch.setattr(tools_module.mcp_hooks, "_verify_scout_type_landed", passes)
        ctx = self._ctx()

        type_result = await tools_module._type_text_post_hook(
            {"ok": True, "data": {"selector": None, "text_length": 8}},
            {"browser_context": {"url": "https://example.com/form", "title": "Form"}},
            ctx,
        )
        select_result = await tools_module._select_option_post_hook(
            {"ok": True, "data": {"selector": None, "value": "large"}},
            {"browser_context": {"url": "https://example.com/form", "title": "Form"}},
            ctx,
        )
        press_result = await tools_module._press_key_post_hook(
            {"ok": True, "data": {"selector": None, "key": "Enter"}},
            {"browser_context": {"url": "https://example.com/results", "title": "Results"}},
            ctx,
        )

        assert type_result["ok"] is True
        assert type_result["data"]["selector"] == ""
        assert select_result["ok"] is True
        assert select_result["data"]["selector"] == ""
        assert press_result["ok"] is True
        assert press_result["data"]["selector"] == ""
        assert ctx.scouted_interactions == [{"tool_name": "press_key", "key": "Enter"}]


class TestAssembleEnforcementMessages:
    @staticmethod
    def _screenshot_msg() -> dict[str, Any]:
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": "screenshot"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            ],
        }

    @staticmethod
    def _offer_msg() -> dict[str, Any]:
        return {"role": "user", "content": "Here is a code block you can add."}

    def test_screenshot_nudge_and_offer_ordering(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import NUDGE_SENTINEL, _assemble_enforcement_messages

        screenshot_msg = self._screenshot_msg()
        offer_msg = self._offer_msg()
        msgs = _assemble_enforcement_messages(screenshot_msg, "please finish the workflow", offer_msg)

        screenshot_indices = [i for i, m in enumerate(msgs) if m is screenshot_msg]
        assert screenshot_indices == [msgs.index(screenshot_msg)]
        assert len(screenshot_indices) == 1

        nudge_index = next(
            i
            for i, m in enumerate(msgs)
            if isinstance(m.get("content"), str) and m["content"].startswith(NUDGE_SENTINEL)
        )
        assert nudge_index == len(msgs) - 1

        offer_index = msgs.index(offer_msg)
        assert offer_index < nudge_index

    def test_offer_and_screenshot_without_nudge(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import NUDGE_SENTINEL, _assemble_enforcement_messages

        screenshot_msg = self._screenshot_msg()
        offer_msg = self._offer_msg()
        msgs = _assemble_enforcement_messages(screenshot_msg, None, offer_msg)

        assert offer_msg in msgs
        assert msgs.count(screenshot_msg) == 1
        assert not any(isinstance(m.get("content"), str) and m["content"].startswith(NUDGE_SENTINEL) for m in msgs)


class TestClickPostHookReachedDownloadTarget:
    """SKY-11081: a scout-CLICK of a single same-host download affordance populates the typed
    reached_download_target from the click post-hook (not only the evaluate path), so the
    synthesizer fires off the actual scout-act the model performs."""

    @staticmethod
    def _patch_scouting(monkeypatch: pytest.MonkeyPatch, *, page_evidence: dict[str, Any] | None) -> None:
        from skyvern.forge.sdk.copilot.tools import mcp_hooks as mh

        monkeypatch.setattr(mh, "_clear_pending_browser_interaction_observation", lambda *_a, **_k: None)
        monkeypatch.setattr(mh, "_consume_scout_source_url", lambda *_a, **_k: "http://localhost:8901/x/")
        monkeypatch.setattr(mh, "_mark_pending_browser_interaction_observation", lambda *_a, **_k: None)
        monkeypatch.setattr(mh, "_record_scouted_interaction", lambda *_a, **_k: None)
        monkeypatch.setattr(mh, "_attach_scout_page_summary", lambda *_a, **_k: None)

        async def fake_resolve_url_title(_raw: Any, _ctx: Any) -> tuple[str, str]:
            return "http://localhost:8901/x/statement", "Statement"

        async def fake_resolve_role_name(*_a: Any, **_k: Any) -> tuple[str | None, str | None]:
            return "link", "View Printable Statement"

        async def fake_register(*_a: Any, **_k: Any) -> tuple[int | None, dict[str, Any] | None]:
            return (1, page_evidence)

        monkeypatch.setattr(mh, "_resolve_url_title", fake_resolve_url_title)
        monkeypatch.setattr(mh, "_resolve_scout_role_name", fake_resolve_role_name)
        monkeypatch.setattr(mh, "_register_scout_interaction_observation", fake_register)

    @staticmethod
    def _ctx() -> Any:
        from skyvern.forge.sdk.copilot.runtime import AgentContext

        return AgentContext(
            organization_id="org-1",
            workflow_id="wf-1",
            workflow_permanent_id="wfp-1",
            workflow_yaml="",
            browser_session_id="pbs_copilot",
            stream=MagicMock(is_disconnected=AsyncMock(return_value=False)),
        )

    _SINGLE_DOWNLOAD_EVIDENCE = {
        "navigation_targets": [
            {
                "selector": 'a[href="/x/statement.pdf"]',
                "text": "View Printable Statement",
                "download_kind": "extension",
            }
        ],
    }

    @pytest.mark.asyncio
    async def test_click_on_download_affordance_populates_reached_download_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.config import settings
        from skyvern.forge.sdk.copilot.tools.mcp_hooks import _click_post_hook

        self._patch_scouting(monkeypatch, page_evidence=self._SINGLE_DOWNLOAD_EVIDENCE)
        monkeypatch.setattr(settings, "COPILOT_DOWNLOAD_SCOUT_ACT_REQUIRED_ENABLED", True)
        monkeypatch.setattr(settings, "COPILOT_REACHED_DOWNLOAD_TARGET_AUTHOR_STEER_ENABLED", True)
        monkeypatch.setattr(settings, "COPILOT_DOWNLOAD_RUNG_SYNTHESIS_ENABLED", True)

        ctx = self._ctx()
        result = {"ok": True, "data": {"selector": 'a[href="/x/statement.pdf"]'}}
        await _click_post_hook(result, {}, ctx)

        assert ctx.reached_download_target is not None
        assert ctx.reached_download_target.download_kind == "extension"

    @pytest.mark.asyncio
    async def test_click_post_hook_is_noop_when_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.config import settings
        from skyvern.forge.sdk.copilot.tools.mcp_hooks import _click_post_hook

        self._patch_scouting(monkeypatch, page_evidence=self._SINGLE_DOWNLOAD_EVIDENCE)
        monkeypatch.setattr(settings, "COPILOT_DOWNLOAD_SCOUT_ACT_REQUIRED_ENABLED", False)
        monkeypatch.setattr(settings, "COPILOT_REACHED_DOWNLOAD_TARGET_AUTHOR_STEER_ENABLED", True)
        monkeypatch.setattr(settings, "COPILOT_DOWNLOAD_RUNG_SYNTHESIS_ENABLED", True)

        ctx = self._ctx()
        result = {"ok": True, "data": {"selector": 'a[href="/x/statement.pdf"]'}}
        await _click_post_hook(result, {}, ctx)

        assert ctx.reached_download_target is None

    @pytest.mark.asyncio
    async def test_click_on_two_affordances_leaves_target_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.config import settings
        from skyvern.forge.sdk.copilot.tools.mcp_hooks import _click_post_hook

        two = {
            "navigation_targets": [
                {"selector": 'a[href="/x/a.pdf"]', "text": "A", "download_kind": "extension"},
                {"selector": 'a[href="/x/b.pdf"]', "text": "B", "download_kind": "extension"},
            ]
        }
        self._patch_scouting(monkeypatch, page_evidence=two)
        monkeypatch.setattr(settings, "COPILOT_DOWNLOAD_SCOUT_ACT_REQUIRED_ENABLED", True)
        monkeypatch.setattr(settings, "COPILOT_REACHED_DOWNLOAD_TARGET_AUTHOR_STEER_ENABLED", True)
        monkeypatch.setattr(settings, "COPILOT_DOWNLOAD_RUNG_SYNTHESIS_ENABLED", True)

        ctx = self._ctx()
        result = {"ok": True, "data": {"selector": 'a[href="/x/a.pdf"]'}}
        await _click_post_hook(result, {}, ctx)

        assert ctx.reached_download_target is None
