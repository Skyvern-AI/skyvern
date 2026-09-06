"""Tests for CopilotRunHooks.on_tool_end activity recording."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest
import yaml
from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.build_test_connect_failure import SUPERSEDED_BY_NEWER_TEST_REASON
from skyvern.forge.sdk.copilot.code_block_synthesis import synthesize_code_block
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import StructuredContext
from skyvern.forge.sdk.copilot.hooks import CopilotRunHooks
from skyvern.forge.sdk.copilot.output_utils import MCP_RESULT_PROVENANCE_KEY, MCP_RESULT_PROVENANCE_VALUE
from skyvern.forge.sdk.copilot.runtime import (
    SENSITIVE_ORIGIN_PAGE_ERROR,
    AgentContext,
    OriginRunRedactionRegistry,
    bound_call_browser_session,
)
from skyvern.forge.sdk.copilot.secret_scrub import (
    REDACTED_SECRET_PLACEHOLDER,
    clear_session_scrub_values,
    register_secret_scrub_value,
)
from skyvern.forge.sdk.copilot.tools import (
    _capture_scout_pre_action,
    _click_post_hook,
    _click_pre_hook,
    _prenav_ambiguity_for_selector,
    _verify_scout_type_landed,
    mcp_hooks,
)
from skyvern.forge.sdk.copilot.tools import scouting as scouting_module
from skyvern.forge.sdk.copilot.tools.credential_fill import _fill_observed_effects
from skyvern.forge.sdk.copilot.tools.mcp_hooks import (
    ScoutReadbackOutcome,
    _scout_readback_outcome,
    _scout_type_landing_failure,
)
from skyvern.forge.sdk.copilot.tools.run_execution import _halt_turn_if_superseded
from skyvern.forge.sdk.copilot.tools.scouting import (
    _build_scout_page_summary,
    _page_evidence_names_obstruction,
    _summary_disclosure_control,
    _summary_entry,
)
from skyvern.forge.sdk.copilot.turn_halt import CopilotTurnHalt, TurnHaltKind
from skyvern.webeye.persistent_sessions_manager import BrowserOperation, BrowserRetirement
from tests.unit.copilot_test_helpers import (
    SENSITIVE_DISCLOSURE_WITHHOLDING_ARMS,
    make_copilot_ctx,
    remove_sensitive_disclosure_prerequisite,
    taint_by_terminal_run,
)

READBACK_OUTCOME_CASES = yaml.safe_load((Path(__file__).parent / "credential_readback_outcome_cases.yaml").read_text())[
    "cases"
]


@dataclass
class _FakeContext:
    check_model_work_deadline: Callable[[], None] | None = None
    tool_activity: list[dict[str, Any]] = field(default_factory=list)
    tool_calls_this_turn: int = 0
    workflow_permanent_id: str = "wpid_example"
    turn_id: str = "turn_example"
    workflow_copilot_chat_id: str = "chat_example"
    total_tokens_used: int | None = None
    last_artifact_health_blocker_reason: str | None = None
    completion_verification_result: Any = None
    turn_ownership: Any = None
    blocker_signal_claimant: Any = None
    gate_precedence_conflict_events: list[Any] = field(default_factory=list)


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
async def test_on_tool_end_exact_credential_preserves_identity_for_structured_context() -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    output = _mcp_text_output(
        {
            "ok": True,
            "data": {
                "status": "resolved",
                "credential": {"credential_id": "cred_saved_login", "name": "Saved Login"},
            },
        }
    )
    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("list_credentials"), output)

    structured_context = StructuredContext()
    structured_context.merge_turn_summary(ctx.tool_activity)

    assert ctx.tool_activity[0]["credentials"] == [{"credential_id": "cred_saved_login", "name": "Saved Login"}]
    assert structured_context.credentials_checked[-1].model_dump() == {
        "credential_name": "Saved Login",
        "credential_id": "cred_saved_login",
        "found": True,
    }


@pytest.mark.asyncio
async def test_on_tool_end_list_credentials_empty_skips_field() -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)

    output = _mcp_text_output({"ok": True, "data": {"credentials": [], "count": 0}})
    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("list_credentials"), output)

    assert "credentials" not in ctx.tool_activity[0]


@pytest.mark.asyncio
async def test_on_tool_end_list_integrations_records_server_owned_binding_evidence() -> None:
    ctx = _FakeContext()
    hooks = CopilotRunHooks(ctx)
    output = _mcp_text_output(
        {
            "ok": True,
            "data": {
                "integrations": [
                    {
                        "connection_id": "goac_sheets",
                        "provider": "google",
                        "name": "Sheets account",
                        "state": "active",
                        "scopes_granted": ["https://www.googleapis.com/auth/spreadsheets"],
                    },
                    {
                        "connection_id": "msoac_mail",
                        "provider": "microsoft",
                        "name": "Mail account",
                        "state": "active",
                        "scopes_granted": [],
                    },
                ]
            },
        }
    )

    await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("list_integrations"), output)

    assert ctx.tool_activity[0]["integrations"] == [
        {
            "connection_id": "goac_sheets",
            "provider": "google",
            "state": "active",
            "scopes_granted": ["https://www.googleapis.com/auth/spreadsheets"],
        },
        {
            "connection_id": "msoac_mail",
            "provider": "microsoft",
            "state": "active",
            "scopes_granted": [],
        },
    ]


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
        await hooks.on_tool_start(_UNUSED, _UNUSED, _fake_tool("run_blocks_and_collect_debug"))
        await hooks.on_tool_end(_UNUSED, _UNUSED, _fake_tool("run_blocks_and_collect_debug"), payload)

    # The recording path raised inside json.dumps before append. The guard
    # swallowed it, so the invariant is "the run did not crash" -- and the
    # activity entry was dropped. That is the acceptable trade for observability.
    assert ctx.tool_activity == []
    assert ctx.tool_calls_this_turn == 1
    warning = next(
        log for log in logs if log["event"] == "CopilotRunHooks.on_tool_end recording failed, skipping entry"
    )
    assert warning["workflow_permanent_id"] == "wpid_example"
    assert warning["turn_id"] == "turn_example"
    assert warning["workflow_copilot_chat_id"] == "chat_example"


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
        assert parsed == {**data, MCP_RESULT_PROVENANCE_KEY: MCP_RESULT_PROVENANCE_VALUE}


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
            ctx.pending_scout_input_value = "partial"
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
            lambda _ctx, result, **_kwargs: screenshots.append(dict(result)),
        )

        initial_scouted_interactions = [{"tool_name": "click", "selector": "#existing"}]
        initial_scout_trajectory = [{"tool_name": "click", "selector": "#existing", "trajectory_index": 0}]
        initial_flow_evidence = [{"step": 1, "evidence": {"source_tool": "existing"}}]
        initial_pending_observation = SimpleNamespace(tool_name="click", url="https://existing")
        ctx = SimpleNamespace(
            browser_session_continuity_generation=0,
            consecutive_tool_tracker=[],
            failed_tool_step_tracker={},
            scouted_interactions=list(initial_scouted_interactions),
            scout_trajectory=list(initial_scout_trajectory),
            flow_evidence=list(initial_flow_evidence),
            pending_browser_interaction_observation=initial_pending_observation,
            pending_scout_source_url="https://source",
            pending_scout_input_value="typed",
            completion_criteria_turn_state=None,
            last_code_authoring_repair_context=None,
            scouted_output_covered_paths=set(),
            request_policy=None,
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
        preserved = {
            "ok": True,
            "data": {"selector": None, "resolved_selector": "xpath=//button[2]", "status": "clicked"},
        }
        assert parsed == {**preserved, MCP_RESULT_PROVENANCE_KEY: MCP_RESULT_PROVENANCE_VALUE}
        assert summarize_tool_result("click", parsed) == "Clicked 'xpath=//button[2]'"
        # The untrusted marker is model-facing only; the loop context records the result itself.
        assert recorded == [preserved]
        assert screenshots == [preserved]
        assert ctx.scouted_interactions == initial_scouted_interactions
        assert ctx.scout_trajectory == initial_scout_trajectory
        assert ctx.flow_evidence == initial_flow_evidence
        assert ctx.pending_browser_interaction_observation == initial_pending_observation
        assert ctx.pending_scout_source_url == "https://source"
        assert ctx.pending_scout_input_value == "typed"

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
        ctx.turn_ownership = None
        ctx.blocker_signal_claimant = None
        ctx.gate_precedence_conflict_events = []
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
        preserved = {"ok": False, "error": "element not found"}
        assert parsed == {**preserved, MCP_RESULT_PROVENANCE_KEY: MCP_RESULT_PROVENANCE_VALUE}
        assert recorded == [preserved]

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

        @asynccontextmanager
        async def fake_mcp_browser_context(ctx: Any, *, session_id_override: str | None = None) -> Any:
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
        ctx.turn_ownership = None
        ctx.blocker_signal_claimant = None
        ctx.gate_precedence_conflict_events = []
        ctx.browser_session_id = None
        ctx.browser_session_continuity_generation = 0
        ctx.browser_session_recovery_lock = None
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

        @asynccontextmanager
        async def _browser_operation(_session_id: str, state: Any) -> AsyncIterator[BrowserOperation]:
            yield BrowserOperation(state, BrowserRetirement())

        persistent_session_manager = SimpleNamespace(
            get_browser_state=AsyncMock(return_value=browser_state),
            browser_operation=_browser_operation,
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


class TestMCPToolOverlayCompleteness:
    """Verify alias map and overlay configs are in sync and complete."""

    def test_alias_map_covers_expected_tools(self) -> None:
        from skyvern.forge.sdk.copilot.tools import get_skyvern_mcp_alias_map

        alias_map = get_skyvern_mcp_alias_map()
        expected_aliases = {
            "get_workflow_knowledge",
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
            "wait_for_either_state",
            "skyvern_frame_list",
            "skyvern_frame_switch",
            "skyvern_frame_main",
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
            "wait_for_either_state",
        }
        for name in browser_tools:
            assert overlays[name].requires_browser, f"{name} should have requires_browser=True"

    def test_wait_for_either_state_exposes_the_two_selector_branch(self) -> None:
        """Without selector_or the copilot must guess one state and pay the timeout when it is wrong."""
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays, get_skyvern_mcp_alias_map

        assert get_skyvern_mcp_alias_map()["wait_for_either_state"] == "skyvern_wait_for_either_state"
        overlay = _build_skyvern_mcp_overlays()["wait_for_either_state"]

        # Both states are required by the tool itself, so a single-selector call — the shape that
        # spends the whole ceiling on a wrong guess — is not expressible here at all.
        assert not overlay.hide_params & {"selector_a", "selector_b"}
        assert overlay.pre_hook is mcp_hooks._sensitive_origin_page_action_pre_hook

    def test_intent_hidden_on_element_action_tools(self) -> None:
        """An `intent` runs a second LLM agent to pick the element, duplicating reasoning the
        copilot loop already did. The element-action tools take a selector only."""
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlays = _build_skyvern_mcp_overlays()
        for name in {"click", "type_text", "select_option", "press_key"}:
            hidden = overlays[name].hide_params or frozenset()
            assert "intent" in hidden, f"{name} should hide intent"

        # `scroll` deliberately keeps it: scrolling a named element into view is not the
        # element-picking this removes, and the agent prompt still offers it.
        assert "intent" not in (overlays["scroll"].hide_params or frozenset())


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
        assert overlay.pre_hook is mcp_hooks._sensitive_origin_page_pre_hook
        assert overlay.post_hook is mcp_hooks._sensitive_origin_page_post_hook

    def test_frame_control_overlays_refuse_sensitive_origin_pages(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlays = _build_skyvern_mcp_overlays()

        for name in ("skyvern_frame_list", "skyvern_frame_switch", "skyvern_frame_main"):
            overlay = overlays[name]
            assert overlay.pre_hook is mcp_hooks._sensitive_origin_page_pre_hook
            assert overlay.post_hook is mcp_hooks._sensitive_origin_page_post_hook

    def test_select_option_overlay(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlay = _build_skyvern_mcp_overlays()["select_option"]
        assert overlay.hide_params == frozenset({"session_id", "cdp_url", "timeout", "intent"})
        assert overlay.required_overrides == ["value"]
        assert overlay.requires_browser is True
        assert overlay.timeout == 15
        assert overlay.post_hook is not None

    def test_press_key_overlay(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlay = _build_skyvern_mcp_overlays()["press_key"]
        assert overlay.hide_params == frozenset({"session_id", "cdp_url", "intent"})
        assert overlay.required_overrides == ["key"]
        assert overlay.requires_browser is True
        assert overlay.post_hook is not None

    def test_click_and_type_overlays_are_selector_only(self) -> None:
        # Acting by selector is deterministic; an `intent` would spawn a second LLM agent to
        # choose the element. The parameter is off the schema, so the description must not
        # reference it and must point at re-observing when a selector fails (regression guard).
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlays = _build_skyvern_mcp_overlays()
        for name in ("click", "type_text"):
            desc = overlays[name].description or ""
            assert "CSS selector" in desc, f"{name} should name the selector contract"
            assert "intent" not in desc, f"{name} description must not reference intent"
            assert "inspect the page again" in desc, f"{name} should steer to re-observation on failure"
            assert "intent" in overlays[name].hide_params

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


class TestScoutReadbackOutcome:
    """The readback is reported as what was observed; nothing here decides where the value belongs."""

    @pytest.mark.parametrize("case", READBACK_OUTCOME_CASES, ids=[case["name"] for case in READBACK_OUTCOME_CASES])
    def test_the_shared_case_table_pins_the_outcome_the_failure_and_the_landing_record(
        self, case: dict[str, Any]
    ) -> None:
        outcome = _scout_readback_outcome(case["readback"], case["typed_value"])
        failure = _scout_type_landing_failure(outcome, tool_name="fill_credential_field", selector="#field")
        effects = _fill_observed_effects(outcome, landing_inferred_from_navigation=False)

        assert outcome.value == case["outcome"]
        assert (failure is not None) is case["fails"]
        assert ("value_landed" in effects) is case["records_value_landed"]

    def test_a_landing_inferred_from_navigation_records_the_inference_and_not_a_sighting(self) -> None:
        effects = _fill_observed_effects(ScoutReadbackOutcome.EMPTY, landing_inferred_from_navigation=True)

        assert effects == {"landing_inferred_from_navigation": True}

    @pytest.mark.parametrize(
        "verdict",
        [ScoutReadbackOutcome.EXACT_MATCH, ScoutReadbackOutcome.DIFFERENT, ScoutReadbackOutcome.UNAVAILABLE],
    )
    def test_only_an_empty_field_fails_the_fill(self, verdict: ScoutReadbackOutcome) -> None:
        assert _scout_type_landing_failure(verdict, tool_name="fill_credential_field", selector="#totp") is None

    def test_an_empty_field_fails_and_names_the_route_back(self) -> None:
        failure = _scout_type_landing_failure(
            ScoutReadbackOutcome.EMPTY, tool_name="fill_credential_field", selector="#totp"
        )

        assert failure is not None
        assert failure["ok"] is False
        assert "retry fill_credential_field" in failure["error"]


class TestVerifyScoutTypeLanded:
    """A scout type that an overlay silently consumed must surface as a failure."""

    def _ctx_with_value(self, value: Any) -> SimpleNamespace:
        server = SimpleNamespace()
        server.call_internal_tool = AsyncMock(return_value={"ok": True, "data": {"value": value}})
        return SimpleNamespace(discovery_mcp_server=server)

    @pytest.mark.asyncio
    async def test_empty_field_after_nonempty_type_returns_failure(self) -> None:
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
    async def test_no_selector_skips_readback(self) -> None:
        ctx = self._ctx_with_value("")
        result = await _verify_scout_type_landed(ctx, selector="", typed_length=12)

        assert result is None
        ctx.discovery_mcp_server.call_internal_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_auto_formatting_field_passes_despite_growing_past_the_typed_length(self) -> None:
        """A phone/card/date input inserts its own separators, so the readback is longer than what
        was typed with nothing having landed in the wrong field. Rejecting it fails every such form."""
        ctx = self._ctx_with_value("(555) 123-4567")
        result = await _verify_scout_type_landed(ctx, selector="#phone", typed_length=len("5551234567"))

        assert result is None

    @pytest.mark.asyncio
    async def test_a_field_holding_more_than_was_typed_passes(self) -> None:
        """type_text does not hold the value it typed, so a longer readback is not an observation
        that the text joined a value already in the field."""
        ctx = self._ctx_with_value("alreadyherenewvalue")
        result = await _verify_scout_type_landed(ctx, selector="#name", typed_length=len("newvalue"))

        assert result is None

    @pytest.mark.asyncio
    async def test_unreadable_field_passes(self) -> None:
        """An unread field is not an observation that the type was lost, so it must not fail the type."""
        ctx = self._ctx_with_value(None)
        result = await _verify_scout_type_landed(ctx, selector="#search-input", typed_length=12)

        assert result is None
        assert ctx.discovery_mcp_server.call_internal_tool.await_count == 1

    @pytest.mark.asyncio
    async def test_zero_typed_length_skips_readback(self) -> None:
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
            pending_scout_input_value=None,
            pending_scout_role_name=None,
            pending_scout_click_selector=None,
            pending_scout_ambiguous=None,
            pending_scout_reanchor=None,
            pending_scout_dynamic_row=None,
            pending_scout_download_snapshot=None,
            pending_scout_download=False,
            pending_scout_download_detachers=[],
            pending_scout_popup=None,
            pending_scout_popup_content_type=None,
            discovery_mcp_server=None,
            scouted_interactions=[],
            scout_trajectory=[],
            pending_scout_source_url=None,
            last_run_blocks_workflow_run_id=None,
            browser_session_id=None,
            request_policy=None,
            org_credentials_for_turn=None,
            organization_id="o_1",
        )
        result = await _click_post_hook(
            {"ok": True, "data": {"selector": "#add-to-cart"}},
            {"browser_context": {"url": "https://example.com/results", "title": "Results"}},
            ctx,
        )

        assert result["data"] == {
            "executed_selector": "#add-to-cart",
            "effective_target": "#add-to-cart",
            "url": "https://example.com/",
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
            pending_scout_input_value=None,
            pending_scout_role_name=None,
            pending_scout_click_selector=None,
            pending_scout_ambiguous=None,
            pending_scout_reanchor=None,
            pending_scout_dynamic_row=None,
            pending_scout_download_snapshot=None,
            pending_scout_download=False,
            pending_scout_download_detachers=[],
            pending_scout_popup=None,
            pending_scout_popup_content_type=None,
            discovery_mcp_server=None,
            scouted_interactions=[],
            scout_trajectory=[],
            pending_scout_source_url=None,
            last_run_blocks_workflow_run_id=None,
            browser_session_id=None,
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
            pending_scout_input_value=None,
            discovery_mcp_server=None,
            scouted_interactions=[],
            scout_trajectory=[],
            pending_scout_source_url=None,
            last_run_blocks_workflow_run_id=None,
            browser_session_id=None,
        )

        result = await tools_module._type_text_post_hook(
            {"ok": True, "data": {"selector": "#q", "text_length": 12}},
            {"browser_context": {"url": "https://example.com/search", "title": "Search"}},
            ctx,
        )

        assert result == {"ok": False, "error": "field is still empty"}
        assert ctx.pending_browser_interaction_observation is None


_ACTED_SELECTOR = "button[data-action='order']"


def _no_redaction_ctx() -> SimpleNamespace:
    """These cases assert summary shape; the redactor is a no-op without codeblock parameters."""
    return SimpleNamespace(codeblock_redaction_parameters={})


# Post-action browser calls each interaction issues, production-shaped (vision on). Only `click`
# is in _ACT_OBSERVE_TOOLS, so only it takes the structured packet and its retained fallback frame;
# The type_text path takes no frame but is the heaviest at four calls: two reads that verify the
# text landed plus two evaluates. A new probe on any path fails this.
_INTERACTION_HOOK_PAIRS = (
    ("_click_pre_hook", {}, "_click_post_hook", {}, {"skyvern_evaluate": 1, "skyvern_screenshot": 1}),
    (
        "_type_text_pre_hook",
        {"text": "socks"},
        "_type_text_post_hook",
        {"text_length": 5},
        {"skyvern_evaluate": 2, "skyvern_get_value": 2},
    ),
    ("_select_option_pre_hook", {"value": "price_asc"}, "_select_option_post_hook", {"value": "price_asc"}, {}),
    ("_press_key_pre_hook", {"key": "Enter"}, "_press_key_post_hook", {"key": "Enter"}, {}),
)

_INTERACTION_PRE_HOOKS = (
    ("_click_pre_hook", {}),
    ("_type_text_pre_hook", {"text": "socks"}),
    ("_select_option_pre_hook", {"value": "price_asc"}),
    ("_press_key_pre_hook", {"key": "Enter"}),
)


class TestScoutedInteractionCapture:
    """A scouted interaction with a concrete selector is captured and surfaced to
    code-only authoring; intent-only and failed-readback actions are not."""

    def _ctx(self, *, policy: object = None, source_url: str | None = None) -> SimpleNamespace:
        ns = SimpleNamespace(
            pending_browser_interaction_observation=None,
            pending_scout_input_value=None,
            pending_scout_role_name=None,
            pending_scout_click_selector=None,
            pending_scout_ambiguous=None,
            pending_scout_reanchor=None,
            pending_scout_dynamic_row=None,
            pending_scout_download_snapshot=None,
            pending_scout_download=False,
            pending_scout_download_detachers=[],
            pending_scout_popup=None,
            pending_scout_popup_content_type=None,
            discovery_mcp_server=None,
            browser_session_id=None,
            last_run_blocks_workflow_run_id=None,
            scouted_interactions=[],
            scout_trajectory=[],
            scout_observed_terminal_criterion_ids=set(),
            completion_criteria_turn_state=None,
            observed_browser_urls=[],
            pending_scout_source_url=source_url,
            prior_carried_trajectory=[],
            carried_trajectory_rebound_done=False,
            request_policy=None,
            org_credentials_for_turn=None,
            organization_id="o_1",
        )
        if policy is not None:
            ns.block_authoring_policy = policy
        return ns

    def test_browser_selector_candidates_are_preserved_in_response_order(self) -> None:
        from skyvern.forge.sdk.copilot.tools.mcp_hooks import _selector_candidates_from_tool_data

        assert _selector_candidates_from_tool_data(
            {
                "selector": "#submit",
                "resolved_selector": "button.primary",
                "selector_candidates": [
                    {"selector": "form#login button[type=submit]", "source": "structural_path", "match_count": 1},
                    {"selector": "button[aria-label=Continue]", "source": "aria_label", "match_count": 2},
                    {"selector": "#submit", "source": "id", "match_count": 1},
                ],
            }
        ) == [
            {"selector": "form#login button[type=submit]", "source": "structural_path", "match_count": 1},
            {"selector": "button[aria-label=Continue]", "source": "aria_label", "match_count": 2},
            {"selector": "#submit", "source": "id", "match_count": 1},
            {"selector": "button.primary", "source": "resolved", "match_count": None},
        ]

    def test_role_name_count_expression_counts_every_observed_match(self) -> None:
        from skyvern.forge.sdk.copilot.composition_browser_expressions import role_name_match_count_expression

        expression = role_name_match_count_expression("button", "Continue")

        assert "count++" in expression
        assert "if (count > 1) break" not in expression

    @pytest.mark.asyncio
    async def test_browser_candidate_capture_keeps_the_complete_packet(self) -> None:
        from skyvern.forge.sdk.copilot.tools.scouting import _capture_scout_selector_candidates

        server = SimpleNamespace(
            call_internal_tool=AsyncMock(
                return_value={
                    "ok": True,
                    "data": {
                        "result": [
                            {"selector": "#email", "source": "id", "match_count": 1},
                            {"selector": 'input[name="email"]', "source": "name", "match_count": 2},
                            {
                                "selector": "form#login input:nth-of-type(1)",
                                "source": "structural_path",
                                "match_count": 1,
                            },
                        ]
                    },
                }
            )
        )
        ctx = SimpleNamespace(discovery_mcp_server=server, pending_scout_selector_candidates=None)

        await _capture_scout_selector_candidates(ctx, "#email")

        assert ctx.pending_scout_selector_candidates == [
            {"selector": "#email", "source": "id", "match_count": 1},
            {"selector": 'input[name="email"]', "source": "name", "match_count": 2},
            {"selector": "form#login input:nth-of-type(1)", "source": "structural_path", "match_count": 1},
        ]

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
        recorded = ctx.scouted_interactions[0]
        assert (recorded["tool_name"], recorded["selector"], recorded["source_url"]) == (
            "click",
            "#add-to-cart",
            "https://example.com/product",
        )
        assert "result_url" not in recorded

    def test_record_preserves_browser_target_and_effect_facts_without_ranking(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _record_scouted_interaction

        ctx = self._ctx()
        _record_scouted_interaction(
            ctx,
            tool_name="click",
            selector="#submit",
            selector_candidates=[
                {"selector": "#submit", "source": "requested", "match_count": 1},
                {"selector": "xpath=//button[@type='submit']", "source": "resolved", "match_count": None},
            ],
            selector_match_count=1,
            role="button",
            accessible_name="Submit",
            role_name_match_count=1,
            source_url="https://example.com/form",
            result_url="https://example.com/thanks",
        )

        assert ctx.scout_trajectory == [
            {
                "tool_name": "click",
                "selector": "#submit",
                "executed_selector": "#submit",
                "selector_candidates": [
                    {"selector": "#submit", "source": "requested", "match_count": 1},
                    {"selector": "xpath=//button[@type='submit']", "source": "resolved", "match_count": None},
                ],
                "selector_match_count": 1,
                "role": "button",
                "accessible_name": "Submit",
                "role_name_match_count": 1,
                "source_url": "https://example.com/form",
                "result_url": "https://example.com/thanks",
                "observed_effects": {"url_changed": True},
                "trajectory_index": 0,
            }
        ]

    def test_record_preserves_selectorless_navigation_fact(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _record_scouted_interaction

        ctx = self._ctx()
        _record_scouted_interaction(
            ctx,
            tool_name="navigate_browser",
            source_url="https://example.com/two-factor",
            result_url="https://example.com/dashboard",
        )

        assert ctx.scout_trajectory == [
            {
                "tool_name": "navigate_browser",
                "source_url": "https://example.com/two-factor",
                "result_url": "https://example.com/dashboard",
                "observed_effects": {"url_changed": True},
                "trajectory_index": 0,
            }
        ]

    def test_record_preserves_selectorless_wait_effect_fact(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _record_scouted_interaction

        ctx = self._ctx()
        _record_scouted_interaction(
            ctx,
            tool_name="wait_for_either_state",
            selector_candidates=[
                {"selector": "#token", "source": "selector_a", "match_count": None},
                {"selector": "#dashboard", "source": "selector_b", "match_count": None},
            ],
            source_url="https://example.com/two-factor",
            result_url="https://example.com/two-factor",
            observed_wait_ms=121595,
        )

        assert ctx.scout_trajectory == [
            {
                "tool_name": "wait_for_either_state",
                "selector_candidates": [
                    {"selector": "#token", "source": "selector_a", "match_count": None},
                    {"selector": "#dashboard", "source": "selector_b", "match_count": None},
                ],
                "source_url": "https://example.com/two-factor",
                "result_url": "https://example.com/two-factor",
                "observed_effects": {"url_changed": False},
                "observed_wait_ms": 121595,
                "trajectory_index": 0,
            }
        ]

    @pytest.mark.asyncio
    async def test_wait_post_hook_records_the_ordered_effect_fact(self) -> None:
        from skyvern.forge.sdk.copilot.tools.mcp_hooks import _wait_for_either_state_post_hook

        ctx = self._ctx()
        result = {
            "ok": False,
            "data": {
                "selector_a": "#token",
                "selector_b": "#dashboard",
                "source_url": "https://example.com/two-factor",
                "result_url": "https://example.com/two-factor",
                "observed_wait_ms": 121595,
            },
            "error": "neither state appeared",
        }

        returned = await _wait_for_either_state_post_hook(result, {}, ctx)

        assert returned is result
        assert ctx.scout_trajectory[0]["tool_name"] == "wait_for_either_state"
        assert ctx.scout_trajectory[0]["observed_wait_ms"] == 121595

    @pytest.mark.asyncio
    async def test_navigate_post_hook_records_the_ordered_effect_fact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = self._ctx(source_url="https://example.com/two-factor")
        monkeypatch.setattr(mcp_hooks, "_bind_login_credential_for_observed_url", AsyncMock())
        monkeypatch.setattr(mcp_hooks, "_capture_post_interaction_screenshot", AsyncMock())

        result = await mcp_hooks._navigate_post_hook(
            {"ok": True, "data": {"url": "https://example.com/dashboard"}},
            {},
            ctx,
        )

        assert result["url"] == "https://example.com/dashboard"
        assert ctx.scout_trajectory[0]["tool_name"] == "navigate_browser"
        assert ctx.scout_trajectory[0]["observed_effects"] == {"url_changed": True}

    @pytest.mark.asyncio
    async def test_navigate_post_hook_next_step_claims_no_attached_screenshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = self._ctx(source_url="https://example.com/login")
        monkeypatch.setattr(mcp_hooks, "_bind_login_credential_for_observed_url", AsyncMock())
        monkeypatch.setattr(mcp_hooks, "_capture_post_interaction_screenshot", AsyncMock(return_value=False))

        result = await mcp_hooks._navigate_post_hook(
            {"ok": True, "data": {"url": "https://example.com/dashboard"}},
            {},
            ctx,
        )

        assert "screenshot" not in result["next_step"]
        assert "attached" not in result["next_step"]

    @pytest.mark.asyncio
    async def test_navigate_post_hook_claims_only_the_screenshot_it_staged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = self._ctx(source_url="https://example.com/login")
        monkeypatch.setattr(mcp_hooks, "_bind_login_credential_for_observed_url", AsyncMock())
        monkeypatch.setattr(mcp_hooks, "_capture_post_interaction_screenshot", AsyncMock(return_value=True))

        result = await mcp_hooks._navigate_post_hook(
            {"ok": True, "data": {"url": "https://example.com/dashboard"}},
            {},
            ctx,
        )

        assert "A screenshot is attached." in result["next_step"]

    @pytest.mark.asyncio
    async def test_failed_navigate_stages_a_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        capture = AsyncMock(return_value=True)
        monkeypatch.setattr(mcp_hooks, "_capture_post_interaction_screenshot", capture)

        ctx = self._ctx(source_url="https://example.com/login")
        result = await mcp_hooks._navigate_post_hook({"ok": False, "error": "navigation timed out"}, {}, ctx)

        capture.assert_awaited_once()
        assert "next_step" not in result

    @pytest.mark.asyncio
    async def test_sensitive_origin_page_refuses_screenshot_before_dispatch(self) -> None:
        ctx = self._ctx()
        ctx.browser_session_id = "pbs-debug"
        ctx.sensitive_origin_browser_session_ids = {"pbs-run"}
        ctx.codeblock_redaction_parameters = {}

        assert await mcp_hooks._screenshot_pre_hook({}, ctx) is None
        with bound_call_browser_session("pbs-run"):
            result = await mcp_hooks._screenshot_pre_hook({}, ctx)

        assert result is not None
        assert result["ok"] is False
        assert "specific named URL" in result["error"]

    @pytest.mark.asyncio
    async def test_sensitive_origin_page_refuses_evaluate_without_stashing_expression(self) -> None:
        ctx = self._ctx()
        ctx.browser_session_id = "pbs-debug"
        ctx.sensitive_origin_browser_session_ids = {"pbs-run"}
        ctx.pending_scout_read_expression = "stale"
        ctx.pending_scout_read_output_path = "output.stale"

        with bound_call_browser_session("pbs-run"):
            result = await mcp_hooks._evaluate_pre_hook(
                {"expression": "document.body.innerText", "output_path": "output.private"},
                ctx,
            )

        assert result is not None
        assert result["ok"] is False
        assert ctx.pending_scout_read_expression is None
        assert ctx.pending_scout_read_output_path is None

    @pytest.mark.asyncio
    async def test_sensitive_origin_page_suppresses_an_in_flight_evaluate_result(self) -> None:
        ctx = self._ctx()
        ctx.browser_session_id = "pbs-debug"
        ctx.sensitive_origin_browser_session_ids = {"pbs-run"}
        ctx.pending_scout_read_expression = "document.body.innerText"
        ctx.pending_scout_read_output_path = "output.private"
        ctx.scout_observation_contract = {"kind": "stale"}

        with bound_call_browser_session("pbs-run"):
            result = await mcp_hooks._evaluate_post_hook(
                {
                    "ok": True,
                    "data": {
                        "result": "private page contents",
                        "url": "https://private.example.test/account",
                    },
                },
                {},
                ctx,
            )

        assert result["ok"] is False
        assert "data" not in result
        assert ctx.pending_scout_read_expression is None
        assert ctx.pending_scout_read_output_path is None
        assert ctx.scout_observation_contract is None

    @pytest.mark.asyncio
    async def test_sensitive_origin_successful_navigation_clears_taint_and_permits_inspection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        capture = AsyncMock(return_value=True)
        monkeypatch.setattr(mcp_hooks, "_bind_login_credential_for_observed_url", AsyncMock())
        monkeypatch.setattr(mcp_hooks, "_capture_post_interaction_screenshot", capture)
        ctx = self._ctx(source_url="https://private.example.test/account")
        ctx.browser_session_id = "pbs-debug"
        ctx.sensitive_origin_browser_session_ids = {"pbs-debug", "pbs-run"}
        ctx.codeblock_redaction_parameters = {}

        with bound_call_browser_session("pbs-run"):
            result = await mcp_hooks._navigate_post_hook(
                {"ok": True, "data": {"url": "https://safe.example.test/start"}},
                {},
                ctx,
            )

        assert result["ok"] is True
        assert ctx.sensitive_origin_browser_session_ids == {"pbs-debug"}
        assert "source_url" not in ctx.scout_trajectory[0]
        capture.assert_awaited_once()
        assert await mcp_hooks._screenshot_pre_hook({}, ctx) is not None
        with bound_call_browser_session("pbs-run"):
            assert await mcp_hooks._evaluate_pre_hook({"expression": "document.title"}, ctx) is None

    @pytest.mark.asyncio
    async def _click_with_attached_evidence(
        self, monkeypatch: pytest.MonkeyPatch, evidence: dict[str, Any]
    ) -> AsyncMock:
        capture = AsyncMock(return_value=True)
        monkeypatch.setattr(mcp_hooks, "_capture_post_interaction_screenshot", capture)

        async def attach_observation(ctx: SimpleNamespace, **kwargs: Any) -> tuple[int | None, dict[str, Any] | None]:
            ctx.last_scout_act_observe_outcome = "attached"
            return None, evidence

        monkeypatch.setattr(mcp_hooks, "_register_scout_interaction_observation", attach_observation)
        monkeypatch.setattr(mcp_hooks, "_attach_scout_page_summary", lambda ctx, result, page_evidence: None)

        ctx = self._ctx(source_url="https://example.com/product")
        await mcp_hooks._click_post_hook(
            {"ok": True, "data": {"selector": "#add-to-cart"}},
            {"browser_context": {"url": "https://example.com/cart", "title": "Cart"}},
            ctx,
        )
        return capture

    @pytest.mark.asyncio
    async def test_click_whose_evidence_names_a_modal_stages_no_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        evidence = {
            "page_title": "Cart",
            "modal_overlays": [{"selector": "#promo", "dismiss_controls": [{"text": "No thanks"}]}],
            "challenge_state": {"detected": False},
        }
        capture = await self._click_with_attached_evidence(monkeypatch, evidence)
        capture.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_click_whose_evidence_names_no_obstruction_still_stages_a_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        evidence = {
            "page_title": "Cart",
            "modal_overlays": [],
            "challenge_controls": [],
            "challenge_state": {"detected": False},
            "visual_obstruction_candidates": [{"selector": ".sticky", "reason": "fixed_overlay"}],
        }
        capture = await self._click_with_attached_evidence(monkeypatch, evidence)
        capture.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failed_click_stages_a_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        capture = AsyncMock(return_value=True)
        monkeypatch.setattr(mcp_hooks, "_capture_post_interaction_screenshot", capture)

        ctx = self._ctx(source_url="https://example.com/product")
        await mcp_hooks._click_post_hook({"ok": False, "error": "click timed out"}, {}, ctx)

        capture.assert_awaited_once()

    def test_post_action_observation_step_is_attached_to_both_fact_views(self) -> None:
        from skyvern.forge.sdk.copilot.tools.scouting import _attach_scout_observation_step

        ctx = self._ctx()
        tools_module._record_scouted_interaction(ctx, tool_name="click", selector="#submit")

        _attach_scout_observation_step(
            ctx,
            tool_name="click",
            selector="#submit",
            observation_step=7,
        )

        assert ctx.scouted_interactions[-1]["observation_step"] == 7
        assert ctx.scout_trajectory[-1]["observation_step"] == 7

    def test_demonstrated_step_facts_are_ordered_and_exclude_secret_values(self) -> None:
        from skyvern.forge.sdk.copilot.tools.mcp_hooks import _demonstrated_step_facts

        ctx = self._ctx()
        ctx.secret_scrub_values = ["actual-password"]
        ctx.scout_trajectory = [
            {
                "tool_name": "fill_credential_field",
                "selector": "#password",
                "selector_candidates": [{"selector": "#password", "source": "requested", "match_count": 1}],
                "selector_match_count": 1,
                "role": "textbox",
                "accessible_name": "Password",
                "role_name_match_count": 1,
                "source_url": "https://example.com/login",
                "result_url": "https://example.com/login",
                "credential_id": "cred_1",
                "credential_name": "example-login",
                "credential_field": "password",
                "input_id": "input_opaque_1",
                "input_value": "actual-password",
                "typed_length": 15,
                "trajectory_index": 4,
            },
            {
                "tool_name": "click",
                "selector": "#submit",
                "source_url": "https://example.com/login",
                "result_url": "https://example.com/home",
                "trajectory_index": 5,
            },
        ]

        facts = _demonstrated_step_facts(ctx)

        assert [fact["trajectory_index"] for fact in facts] == [4, 5]
        assert facts[0]["credential_id"] == "cred_1"
        assert facts[0]["input_id"] == "input_opaque_1"
        assert "input_value" not in facts[0]
        assert "actual-password" not in str(facts)

    def test_demonstrated_step_facts_project_urls_to_safe_origins(self) -> None:
        from skyvern.forge.sdk.copilot.tools.mcp_hooks import _demonstrated_step_facts

        ctx = self._ctx()
        ctx.scout_trajectory = [
            {
                "tool_name": "click",
                "selector": "#submit",
                "source_url": "https://user:password@example.com/account/5db266e1?token=7cc8f30a#billing",
                "result_url": "https://example.com/dashboard/9f82016e?session=a857b755#overview",
            }
        ]

        assert _demonstrated_step_facts(ctx) == [
            {
                "tool_name": "click",
                "executed_selector": "#submit",
                "source_url": "https://example.com/",
                "result_url": "https://example.com/",
                "selector_candidates": None,
                "role": None,
                "accessible_name": None,
                "role_name_match_count": None,
                "observed_effects": None,
                "observation_step": None,
                "input_id": None,
            }
        ]

    @pytest.mark.asyncio
    async def test_code_schema_surfaces_ordered_facts_not_synthesized_source(self) -> None:
        from skyvern.forge.sdk.copilot.tools.mcp_hooks import _get_block_schema_post_hook

        ctx = self._ctx(policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER)
        ctx.code_only_code_schema_seen = False
        ctx.secret_scrub_values = []
        ctx.scout_trajectory = [
            {
                "tool_name": "click",
                "executed_selector": "#submit",
                "selector_candidates": None,
                "role": None,
                "accessible_name": None,
                "role_name_match_count": None,
                "source_url": "https://example.com/",
                "result_url": "https://example.com/",
                "observed_effects": None,
                "observation_step": None,
                "input_id": None,
                "trajectory_index": 0,
            }
        ]

        result = await _get_block_schema_post_hook({"data": {"block_type": "code"}}, {}, ctx)

        assert result["data"]["demonstrated_steps"] == [
            {
                "tool_name": "click",
                "executed_selector": "#submit",
                "selector_candidates": None,
                "role": None,
                "accessible_name": None,
                "role_name_match_count": None,
                "source_url": "https://example.com/",
                "result_url": "https://example.com/",
                "observed_effects": None,
                "observation_step": None,
                "input_id": None,
                "trajectory_index": 0,
            }
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
            {"tool_name": "click", "selector": "#x", "executed_selector": "#x", "source_url": "https://e.com/a"},
            {"tool_name": "click", "selector": "#y", "executed_selector": "#y", "source_url": "https://e.com/a"},
        ]

    def test_record_drops_zero_typed_length(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _record_scouted_interaction

        ctx = self._ctx()
        _record_scouted_interaction(ctx, tool_name="type_text", selector="#q", typed_length=0)
        assert ctx.scouted_interactions == [{"tool_name": "type_text", "selector": "#q", "executed_selector": "#q"}]

    def test_record_omits_empty_extras_and_caps_history(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _MAX_SCOUTED_INTERACTIONS, _record_scouted_interaction

        ctx = self._ctx()
        for index in range(_MAX_SCOUTED_INTERACTIONS + 5):
            _record_scouted_interaction(ctx, tool_name="click", selector=f"#item-{index}")
        assert len(ctx.scouted_interactions) == _MAX_SCOUTED_INTERACTIONS
        # oldest dropped, newest kept
        assert ctx.scouted_interactions[-1]["selector"] == f"#item-{_MAX_SCOUTED_INTERACTIONS + 4}"
        assert "source_url" not in ctx.scouted_interactions[-1]

    def test_recorded_reads_use_monotone_capped_trajectory_indices(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _MAX_SCOUTED_INTERACTIONS
        from skyvern.forge.sdk.copilot.tools.mcp_hooks import _record_scouted_read

        ctx = self._ctx()
        recorded = None
        for index in range(_MAX_SCOUTED_INTERACTIONS + 5):
            recorded = _record_scouted_read(
                ctx,
                expression=f"document.querySelector('#value-{index}').textContent",
                data={"result": str(index)},
                url="https://example.com/results",
            )

        assert len(ctx.scout_trajectory) == _MAX_SCOUTED_INTERACTIONS
        assert ctx.scout_trajectory[0]["trajectory_index"] == 5
        assert ctx.scout_trajectory[-1]["trajectory_index"] == _MAX_SCOUTED_INTERACTIONS + 4
        assert recorded is ctx.scout_trajectory[-1]

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
        recorded = ctx.scouted_interactions[0]
        assert (recorded["tool_name"], recorded["selector"], recorded["source_url"]) == (
            "click",
            "#add-to-cart",
            "https://example.com/product",
        )
        assert recorded["result_url"] == "https://example.com/cart"

    @pytest.mark.asyncio
    async def test_navigating_bare_click_records_prenav_role_name(self) -> None:
        ctx = self._ctx(source_url="https://example.com/billing")
        ctx.pending_scout_role_name = ("a", "link", "View Printable Statement")
        await _click_post_hook(
            {"ok": True, "data": {"selector": "a"}},
            {"browser_context": {"url": "https://example.com/statement.pdf", "title": "Statement"}},
            ctx,
        )
        recorded = ctx.scouted_interactions[0]
        assert (recorded["tool_name"], recorded["selector"], recorded["source_url"]) == (
            "click",
            "a",
            "https://example.com/billing",
        )
        assert (recorded["role"], recorded["accessible_name"]) == ("link", "View Printable Statement")
        assert ctx.pending_scout_role_name is None

    @pytest.mark.asyncio
    async def test_prenav_role_name_lets_strict_synthesis_emit_get_by_role(self) -> None:
        ctx = self._ctx(source_url="https://example.com/billing")
        ctx.pending_scout_role_name = ("a", "link", "View Printable Statement")
        ctx.scout_trajectory = [
            {
                "tool_name": "click",
                "selector": "#statement-row",
                "source_url": "https://example.com/billing",
                "trajectory_index": 0,
            }
        ]
        await _click_post_hook(
            {"ok": True, "data": {"selector": "a"}},
            {"browser_context": {"url": "https://example.com/statement.pdf", "title": "Statement"}},
            ctx,
        )
        result = synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
        assert result is not None
        assert 'await page.get_by_role("link", name="View Printable Statement", exact=True).click()' in result.code
        assert result.diagnostics.dropped_interactions == []

    @pytest.mark.asyncio
    async def test_prenav_role_name_stash_ignored_on_selector_mismatch(self) -> None:
        ctx = self._ctx(source_url="https://example.com/billing")
        ctx.pending_scout_role_name = ("a", "link", "View Printable Statement")
        await _click_post_hook(
            {"ok": True, "data": {"selector": "#concrete-row"}},
            {"browser_context": {"url": "https://example.com/statement.pdf", "title": "Statement"}},
            ctx,
        )
        recorded = ctx.scouted_interactions[-1]
        assert "role" not in recorded
        assert "accessible_name" not in recorded
        assert ctx.pending_scout_role_name is None

    @pytest.mark.asyncio
    async def test_click_pre_hook_stashes_role_name_from_role_selector(self) -> None:
        ctx = self._ctx()
        ctx.pending_scout_source_url = None
        await _click_pre_hook({"selector": 'role=link[name="Continue"]'}, ctx)
        assert ctx.pending_scout_role_name == ('role=link[name="Continue"]', "link", "Continue")

    @pytest.mark.asyncio
    async def test_click_pre_hook_stashes_role_name_from_bare_css_via_browser_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bare CSS selector cannot be parsed, so the role/name comes from the page read.
        ctx = self._ctx_with_scripted_reads(selector_count=1, role_name=("link", "Download"), match_count=1)
        ctx.pending_scout_source_url = None
        await _click_pre_hook({"selector": "a"}, ctx)
        assert ctx.pending_scout_role_name == ("a", "link", "Download")

    @pytest.mark.asyncio
    async def test_click_post_hook_omits_source_url_when_unavailable(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _click_post_hook

        ctx = self._ctx()  # no pre-hook source url
        await _click_post_hook(
            {"ok": True, "data": {"selector": "#add-to-cart"}},
            {"browser_context": {"url": "https://example.com/cart", "title": "Cart"}},
            ctx,
        )
        recorded = ctx.scouted_interactions[0]
        assert recorded["tool_name"] == "click"
        assert recorded["selector"] == "#add-to-cart"
        assert "source_url" not in recorded

    @pytest.mark.asyncio
    async def test_type_post_hook_assigns_opaque_identity_and_keeps_value_private(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.forge.sdk.copilot import tools as tools_module

        async def passes(*_a: object, **_k: object) -> None:
            return None

        monkeypatch.setattr(tools_module.mcp_hooks, "_verify_scout_type_landed", passes)
        ctx = self._ctx()
        ctx.pending_scout_input_value = "query123"
        await tools_module._type_text_post_hook(
            {"ok": True, "data": {"selector": "#q", "text_length": 8}},
            {"browser_context": {"url": "https://example.com/search", "title": "Search"}},
            ctx,
        )
        recorded = ctx.scouted_interactions[0]
        assert (recorded["tool_name"], recorded["selector"], recorded["typed_length"]) == ("type_text", "#q", 8)
        assert recorded["input_id"].startswith("input_")
        assert recorded["input_value"] == "query123"
        facts = tools_module.mcp_hooks._demonstrated_step_facts(ctx)
        assert facts[0]["input_id"] == recorded["input_id"]
        assert "input_value" not in facts[0]
        assert "query123" not in repr(facts)

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
        assert [item["tool_name"] for item in ctx.scouted_interactions] == ["select_option", "press_key"]
        assert (ctx.scouted_interactions[0]["selector"], ctx.scouted_interactions[0]["value"]) == (
            "#sort",
            "price_asc",
        )
        assert (ctx.scouted_interactions[1]["selector"], ctx.scouted_interactions[1]["key"]) == ("#q", "Enter")

    @pytest.mark.asyncio
    async def test_selector_press_key_records_complete_source_target_packet_and_clears_pending(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _press_key_post_hook
        from skyvern.forge.sdk.copilot.tools.mcp_hooks import _press_key_pre_hook

        ctx = self._ctx_with_scripted_reads(
            selector_count=2,
            role_name=("textbox", "Search"),
            match_count=2,
            candidates=[{"selector": "#q", "source": "id"}, {"selector": 'input[name="q"]', "source": "name"}],
        )
        ctx.pending_scout_source_url = "https://example.com/form"

        assert await _press_key_pre_hook({"selector": "#q", "key": "Enter"}, ctx) is None
        await _press_key_post_hook(
            {"ok": True, "data": {"selector": "#q", "key": "Enter"}},
            {"browser_context": {"url": "https://example.com/results", "title": "Results"}},
            ctx,
        )

        recorded = ctx.scout_trajectory[-1]
        assert recorded["selector_candidates"] == [
            {"selector": "#q", "source": "id", "match_count": None},
            {"selector": 'input[name="q"]', "source": "name", "match_count": None},
        ]
        assert recorded["selector_match_count"] == 2
        assert recorded["role"] == "textbox"
        assert recorded["accessible_name"] == "Search"
        assert recorded["role_name_match_count"] == 2
        assert recorded["ambiguous"] is True
        assert ctx.pending_scout_selector_candidates is None
        assert ctx.pending_scout_selector_match_count is None
        assert ctx.pending_scout_role_name is None
        assert ctx.pending_scout_role_name_match_count is None
        assert ctx.pending_scout_ambiguous is None
        assert ctx.pending_scout_reanchor is None

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

    def _ctx_with_count(self, count: int) -> SimpleNamespace:
        return self._ctx_with_scripted_reads(selector_count=count, role_name=("", ""), match_count=-1)

    @pytest.mark.asyncio
    async def test_capture_scout_ambiguity_marks_multi_match_selector(self) -> None:
        ctx = self._ctx_with_count(3)
        await _capture_scout_pre_action(ctx, "button[data-action='orderDocuments']")
        assert ctx.pending_scout_ambiguous == ("button[data-action='orderDocuments']", True)

    @pytest.mark.asyncio
    async def test_capture_scout_ambiguity_does_not_mark_unique_selector(self) -> None:
        ctx = self._ctx_with_count(1)
        await _capture_scout_pre_action(ctx, "#unique")
        assert ctx.pending_scout_ambiguous is None

    def test_live_scout_packet_has_no_dynamic_row_classifier(self) -> None:
        from skyvern.forge.sdk.copilot.tools import mcp_hooks as mcp_hooks_module

        assert "dynamic_row_evidence" not in mcp_hooks_module._MODEL_SCOUT_FACT_KEYS
        assert not hasattr(mcp_hooks_module, "_capture_scout_dynamic_row")

    @pytest.mark.asyncio
    async def test_click_cycle_records_scout_ambiguous_multi_match(self) -> None:
        ctx = self._ctx_with_count(2)
        await _click_pre_hook({"selector": "button[data-action='orderDocuments']"}, ctx)
        await _click_post_hook(
            {"ok": True, "data": {"selector": "button[data-action='orderDocuments']"}},
            {"browser_context": {"url": "https://example.com/portal", "title": "Portal"}},
            ctx,
        )
        assert ctx.scouted_interactions[-1].get("ambiguous") is True

    def _ctx_with_scripted_reads(
        self,
        *,
        selector_count: int,
        role_name: tuple[str, str],
        match_count: int,
        candidates: list[dict[str, str]] | None = None,
    ) -> SimpleNamespace:
        role, name = role_name

        async def call(_tool: str, args: dict[str, Any]) -> dict[str, Any]:
            return {
                "ok": True,
                "data": {
                    "result": {
                        "role_name": {"role": role, "accessible_name": name} if (role or name) else None,
                        "role_name_match_count": match_count,
                        "selector_match_count": selector_count,
                        "selector_candidates": candidates or [],
                    }
                },
            }

        server = SimpleNamespace()
        server.call_internal_tool = AsyncMock(side_effect=call)
        ctx = self._ctx(source_url="https://example.com/portal")
        ctx.discovery_mcp_server = server
        return ctx

    @pytest.mark.asyncio
    async def test_capture_scout_ambiguity_stashes_unique_reanchor(self) -> None:
        ctx = self._ctx_with_scripted_reads(
            selector_count=2, role_name=("button", "I AM A BUSINESS CUSTOMER"), match_count=1
        )
        await _capture_scout_pre_action(ctx, "button[data-action='businessToggle']")
        assert ctx.pending_scout_ambiguous == ("button[data-action='businessToggle']", True)
        assert ctx.pending_scout_reanchor == (
            "button[data-action='businessToggle']",
            "button",
            "I AM A BUSINESS CUSTOMER",
        )

    @pytest.mark.asyncio
    async def test_capture_scout_ambiguity_withholds_name_degenerate_reanchor(self) -> None:
        ctx = self._ctx_with_scripted_reads(selector_count=2, role_name=("button", "Toggle"), match_count=2)
        await _capture_scout_pre_action(ctx, "button[data-action='businessToggle']")
        assert ctx.pending_scout_ambiguous == ("button[data-action='businessToggle']", True)
        assert ctx.pending_scout_reanchor is None

    @pytest.mark.asyncio
    async def test_capture_scout_ambiguity_withholds_nameless_reanchor(self) -> None:
        ctx = self._ctx_with_scripted_reads(selector_count=2, role_name=("button", ""), match_count=1)
        await _capture_scout_pre_action(ctx, "button[data-action='businessToggle']")
        assert ctx.pending_scout_ambiguous == ("button[data-action='businessToggle']", True)
        assert ctx.pending_scout_reanchor is None

    @pytest.mark.asyncio
    async def test_ambiguous_click_records_validated_reanchor_role_name(self) -> None:
        ctx = self._ctx_with_scripted_reads(
            selector_count=2, role_name=("button", "I AM A BUSINESS CUSTOMER"), match_count=1
        )
        await _click_pre_hook({"selector": "button[data-action='businessToggle']"}, ctx)
        await _click_post_hook(
            {"ok": True, "data": {"selector": "button[data-action='businessToggle']"}},
            {"browser_context": {"url": "https://example.com/portal", "title": "Portal"}},
            ctx,
        )
        recorded = ctx.scouted_interactions[-1]
        assert recorded.get("ambiguous") is True
        assert recorded.get("role") == "button"
        assert recorded.get("accessible_name") == "I AM A BUSINESS CUSTOMER"

    @pytest.mark.asyncio
    async def test_ambiguous_click_preserves_observed_nonunique_role_name_and_count(self) -> None:
        ctx = self._ctx_with_scripted_reads(selector_count=2, role_name=("button", "Toggle"), match_count=2)
        await _click_pre_hook({"selector": "button[data-action='businessToggle']"}, ctx)
        await _click_post_hook(
            {"ok": True, "data": {"selector": "button[data-action='businessToggle']"}},
            {"browser_context": {"url": "https://example.com/portal", "title": "Portal"}},
            ctx,
        )
        recorded = ctx.scouted_interactions[-1]
        assert recorded.get("ambiguous") is True
        assert recorded["role"] == "button"
        assert recorded["accessible_name"] == "Toggle"
        assert recorded["role_name_match_count"] == 2

    def test_prenav_ambiguity_ignores_mismatched_selector(self) -> None:
        assert _prenav_ambiguity_for_selector(("button", True), "a") is False
        assert _prenav_ambiguity_for_selector(("button", True), "button") is True
        assert _prenav_ambiguity_for_selector(None, "button") is False

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
        assert result["data"]["executed_selector"] == "xpath=//button[2]"
        assert result["data"]["effective_target"] == "xpath=//button[2]"
        recorded = ctx.scouted_interactions[0]
        assert (recorded["tool_name"], recorded["selector"], recorded["source_url"]) == (
            "click",
            "xpath=//button[2]",
            "https://example.com/product",
        )
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
        recorded = ctx.scouted_interactions[0]
        assert (recorded["tool_name"], recorded["selector"]) == ("click", "#add-to-cart")

    @pytest.mark.asyncio
    async def test_click_post_hook_records_every_browser_selector_and_source_page_counts(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _click_post_hook

        ctx = self._ctx(source_url="https://example.com/form")
        ctx.pending_scout_selector_match_count = ("#submit", 1)
        ctx.pending_scout_role_name = ("#submit", "button", "Submit")
        ctx.pending_scout_role_name_match_count = ("#submit", "button", "Submit", 1)
        result = await _click_post_hook(
            {
                "ok": True,
                "data": {"selector": "#submit", "resolved_selector": "xpath=//button[@type='submit']"},
            },
            {"browser_context": {"url": "https://example.com/thanks", "title": "Thanks"}},
            ctx,
        )

        recorded = ctx.scout_trajectory[-1]
        assert recorded["selector_candidates"] == [
            {"selector": "#submit", "source": "requested", "match_count": None},
            {"selector": "xpath=//button[@type='submit']", "source": "resolved", "match_count": None},
        ]
        assert recorded["selector_match_count"] == 1
        assert recorded["role_name_match_count"] == 1
        assert recorded["source_url"] == "https://example.com/form"
        assert recorded["result_url"] == "https://example.com/thanks"
        assert recorded["observed_effects"] == {"url_changed": True}
        assert result["data"]["executed_selector"] == "#submit"

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

        assert result["data"]["executed_selector"] == "xpath=//button[2]"
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
        assert type_result["data"]["executed_selector"] == ""
        assert select_result["ok"] is True
        assert select_result["data"]["executed_selector"] == ""
        assert press_result["ok"] is True
        assert press_result["data"]["executed_selector"] == ""
        assert ctx.scouted_interactions[0]["tool_name"] == "press_key"
        assert ctx.scouted_interactions[0]["key"] == "Enter"

    @pytest.mark.parametrize(("hook_name", "extra_params"), _INTERACTION_PRE_HOOKS)
    @pytest.mark.parametrize("policy", [None, BlockAuthoringPolicy.CODE_ONLY_BROWSER])
    @pytest.mark.asyncio
    async def test_interaction_pre_hook_issues_one_internal_packet(
        self,
        hook_name: str,
        extra_params: dict[str, Any],
        policy: BlockAuthoringPolicy | None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(mcp_hooks, "_scout_session_download_names", AsyncMock(return_value=frozenset()))
        ctx = self._ctx_with_scripted_reads(selector_count=2, role_name=("button", "Order"), match_count=1)
        ctx.block_authoring_policy = policy

        assert await getattr(mcp_hooks, hook_name)({"selector": _ACTED_SELECTOR, **extra_params}, ctx) is None

        assert ctx.discovery_mcp_server.call_internal_tool.await_count == 1

    def _ctx_with_post_action_capture(
        self, *, hollow: bool = False, unsettled_challenge: bool = False
    ) -> SimpleNamespace:
        """Context whose discovery server answers the fused pre-action read and the
        post-action structured capture. ``hollow`` returns a packet with no controls,
        which is the only state that earns the bounded recapture."""
        page: dict[str, Any] = {"page_title": "Results", "forms": [], "navigation_targets": []}
        if not hollow:
            page = {
                "page_title": "Results",
                "forms": [
                    {
                        "fields": [{"name": "npi", "label": "NPI number", "type": "text", "selector": "#npi"}],
                        "submit_controls": [{"text": "Search", "type": "submit", "selector": "#go"}],
                    }
                ],
                "navigation_targets": [{"text": "Details", "href": "https://example.com/d", "selector": "a.d"}],
                "result_containers": [{"tag": "table", "id": "results", "selector": "#results"}],
            }
        if unsettled_challenge:
            # Keyword says challenge, no rendered carrier resolves it — the second recapture trigger.
            page = {**page, "anti_bot_indicators": ["captcha"], "challenge_controls": []}

        async def call(_tool: str, args: dict[str, Any]) -> dict[str, Any]:
            if "REQUESTED_TARGETS" in str(args.get("expression", "")):
                return {"ok": True, "data": {"result": page}}
            return {
                "ok": True,
                "data": {
                    "result": {
                        "role_name": {"role": "button", "accessible_name": "Order"},
                        "role_name_match_count": 1,
                        "selector_match_count": 1,
                        "selector_candidates": [],
                    }
                },
            }

        server = SimpleNamespace()
        server.call_internal_tool = AsyncMock(side_effect=call)
        ctx = self._ctx(source_url="https://example.com/portal")
        ctx.discovery_mcp_server = server
        ctx.pre_run_gated_output_warning_fingerprint = ()
        ctx.last_code_authoring_repair_context = None
        ctx.supports_vision = True
        return ctx

    @staticmethod
    def _census(ctx: SimpleNamespace) -> dict[str, int]:
        census: dict[str, int] = {}
        for await_call in ctx.discovery_mcp_server.call_internal_tool.await_args_list:
            census[await_call.args[0]] = census.get(await_call.args[0], 0) + 1
        return census

    @pytest.mark.parametrize(
        ("pre_hook", "pre_args", "post_hook", "post_data", "expected_census"), _INTERACTION_HOOK_PAIRS
    )
    @pytest.mark.asyncio
    async def test_every_interaction_post_hook_census(
        self,
        pre_hook: str,
        pre_args: dict[str, Any],
        post_hook: str,
        post_data: dict[str, Any],
        expected_census: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Production-shaped: vision on, so the retained fallback frame is counted."""
        monkeypatch.setattr(mcp_hooks, "_scout_session_download_names", AsyncMock(return_value=frozenset()))
        ctx = self._ctx_with_post_action_capture()
        assert await getattr(mcp_hooks, pre_hook)({"selector": _ACTED_SELECTOR, **pre_args}, ctx) is None
        ctx.discovery_mcp_server.call_internal_tool.reset_mock()

        await getattr(mcp_hooks, post_hook)(
            {"ok": True, "data": {"selector": _ACTED_SELECTOR, **post_data}},
            {"browser_context": {"url": "https://example.com/next", "title": "Next"}},
            ctx,
        )

        assert self._census(ctx) == expected_census

    @pytest.mark.parametrize("policy", [None, BlockAuthoringPolicy.CODE_ONLY_BROWSER])
    @pytest.mark.parametrize("landing_url", ["https://example.com/portal", "https://example.com/next"])
    @pytest.mark.asyncio
    async def test_click_post_hook_issues_one_structured_packet_plus_the_frame(
        self,
        policy: BlockAuthoringPolicy | None,
        landing_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Vision is on, as it is in production, so the fallback frame is counted here."""
        monkeypatch.setattr(mcp_hooks, "_scout_session_download_names", AsyncMock(return_value=frozenset()))
        ctx = self._ctx_with_post_action_capture()
        ctx.block_authoring_policy = policy
        assert await mcp_hooks._click_pre_hook({"selector": _ACTED_SELECTOR}, ctx) is None
        ctx.discovery_mcp_server.call_internal_tool.reset_mock()

        await mcp_hooks._click_post_hook(
            {"ok": True, "data": {"selector": _ACTED_SELECTOR}},
            {"browser_context": {"url": landing_url, "title": "Next"}},
            ctx,
        )

        assert self._census(ctx) == {"skyvern_evaluate": 1, "skyvern_screenshot": 1}

    @pytest.mark.asyncio
    async def test_click_post_hook_recaptures_once_when_the_first_packet_is_hollow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hollow is one of three recapture triggers; unsettled challenge evidence and an
        unchanged-after-interaction page are the others."""
        monkeypatch.setattr(mcp_hooks, "_scout_session_download_names", AsyncMock(return_value=frozenset()))
        ctx = self._ctx_with_post_action_capture(hollow=True)
        assert await mcp_hooks._click_pre_hook({"selector": _ACTED_SELECTOR}, ctx) is None
        ctx.discovery_mcp_server.call_internal_tool.reset_mock()

        await mcp_hooks._click_post_hook(
            {"ok": True, "data": {"selector": _ACTED_SELECTOR}},
            {"browser_context": {"url": "https://example.com/next", "title": "Next"}},
            ctx,
        )

        assert self._census(ctx) == {"skyvern_evaluate": 2, "skyvern_screenshot": 1}
        assert ctx.last_scout_act_observe_recapture_attempted is True

    @pytest.mark.asyncio
    async def test_click_post_hook_recaptures_once_when_challenge_evidence_is_unsettled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_hooks, "_scout_session_download_names", AsyncMock(return_value=frozenset()))
        ctx = self._ctx_with_post_action_capture(unsettled_challenge=True)
        assert await mcp_hooks._click_pre_hook({"selector": _ACTED_SELECTOR}, ctx) is None
        ctx.discovery_mcp_server.call_internal_tool.reset_mock()

        await mcp_hooks._click_post_hook(
            {"ok": True, "data": {"selector": _ACTED_SELECTOR}},
            {"browser_context": {"url": "https://example.com/next", "title": "Next"}},
            ctx,
        )

        # Two packets, no frame: the settled packet names the challenge, which is the one case the
        # ticket allows the fallback frame to be dropped.
        assert self._census(ctx) == {"skyvern_evaluate": 2}
        assert ctx.last_scout_act_observe_recapture_attempted is True

    def test_page_summary_never_reconstructs_a_singular_selector(self) -> None:
        long_selector = "div.wrapper > " + " > ".join(f"section.level-{n}" for n in range(12))

        entry = _summary_entry("Close", {"selector": long_selector})

        assert entry == {"text": "Close"}
        assert _summary_entry(
            "Close",
            {"selector_candidates": [{"selector": "#close", "source": "id", "match_count": 1}]},
        ) == {
            "text": "Close",
            "selector_candidates": [{"selector": "#close", "source": "id"}],
        }

        # The disclosure summary obeys the same no-singular-recommendation invariant.
        oversized = {"expanded": True, "text": "Show options", "selector": long_selector}
        assert "selector" not in _summary_disclosure_control(oversized)
        sized = {
            "expanded": True,
            "text": "Show options",
            "selector_candidates": [{"selector": "#opts", "source": "id", "match_count": 1}],
        }
        assert _summary_disclosure_control(sized)["selector_candidates"] == [{"selector": "#opts", "source": "id"}]

    @pytest.mark.parametrize(("hook_name", "extra_params"), _INTERACTION_PRE_HOOKS)
    @pytest.mark.asyncio
    async def test_interaction_pre_hooks_reach_the_same_stash(
        self, hook_name: str, extra_params: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_hooks, "_scout_session_download_names", AsyncMock(return_value=frozenset()))
        candidates = [{"selector": "#order-docs", "source": "id", "match_count": None}]
        ctx = self._ctx_with_scripted_reads(
            selector_count=2, role_name=("button", "Order"), match_count=1, candidates=candidates
        )

        assert await getattr(mcp_hooks, hook_name)({"selector": _ACTED_SELECTOR, **extra_params}, ctx) is None

        assert ctx.pending_scout_role_name == (_ACTED_SELECTOR, "button", "Order")
        assert ctx.pending_scout_role_name_match_count == (_ACTED_SELECTOR, "button", "Order", 1)
        assert ctx.pending_scout_selector_candidates == candidates
        assert ctx.pending_scout_selector_match_count == (_ACTED_SELECTOR, 2)
        assert ctx.pending_scout_ambiguous == (_ACTED_SELECTOR, True)
        assert ctx.pending_scout_reanchor == (_ACTED_SELECTOR, "button", "Order")

    @pytest.mark.parametrize(("hook_name", "extra_params"), _INTERACTION_PRE_HOOKS)
    @pytest.mark.asyncio
    async def test_interaction_pre_hook_without_a_selector_clears_the_prior_stash(
        self, hook_name: str, extra_params: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_hooks, "_scout_session_download_names", AsyncMock(return_value=frozenset()))
        ctx = self._ctx_with_scripted_reads(selector_count=2, role_name=("button", "Order"), match_count=1)
        ctx.pending_scout_role_name = ("#stale", "button", "Stale")
        ctx.pending_scout_selector_match_count = ("#stale", 1)

        assert await getattr(mcp_hooks, hook_name)(dict(extra_params), ctx) is None

        assert ctx.pending_scout_role_name is None
        assert ctx.pending_scout_selector_match_count is None
        assert ctx.discovery_mcp_server.call_internal_tool.await_count == 0

    @pytest.mark.asyncio
    async def test_pre_action_packet_keeps_every_candidate_and_the_ambiguity_flag(self) -> None:
        candidates = [
            {"selector": "button[data-action='order']", "source": "requested", "match_count": None},
            {"selector": "#order-docs", "source": "id", "match_count": None},
            {"selector": "button.order", "source": "class_list", "match_count": None},
        ]
        ctx = self._ctx_with_scripted_reads(
            selector_count=3, role_name=("button", "Order documents"), match_count=3, candidates=candidates
        )
        await _capture_scout_pre_action(ctx, "button[data-action='order']")

        assert ctx.pending_scout_selector_candidates == candidates
        assert ctx.pending_scout_ambiguous == ("button[data-action='order']", True)
        assert ctx.pending_scout_selector_match_count == ("button[data-action='order']", 3)
        assert ctx.pending_scout_reanchor is None

    @pytest.mark.asyncio
    async def test_pre_action_packet_leaves_unknown_cardinality_unset(self) -> None:
        ctx = self._ctx_with_scripted_reads(selector_count=-1, role_name=("", ""), match_count=-1)
        await _capture_scout_pre_action(ctx, "input[[bad")

        assert ctx.pending_scout_selector_match_count is None
        assert ctx.pending_scout_role_name_match_count is None
        assert ctx.pending_scout_ambiguous is None


class TestScoutPageSummary:
    @staticmethod
    def _evidence(field_count: int) -> dict[str, Any]:
        def element(selector: str, **facts: Any) -> dict[str, Any]:
            return {
                **facts,
                "selector_candidates": [{"selector": selector, "source": "test_fixture", "match_count": 1}],
            }

        return {
            "page_title": "Checkout",
            "forms": [
                {
                    "fields": [
                        element(f"#field-{index}", label=f"Field {index} with a fairly long visible label")
                        for index in range(field_count)
                    ],
                    "submit_controls": [element("#place-order", text="Place order")],
                }
            ],
            "navigation_targets": [
                element(f"a.nav-{index}", text=f"Section {index} of the site navigation") for index in range(8)
            ],
            "modal_overlays": [{"dismiss_controls": [element("#promo-close", text="No thanks")]}],
            "page_obstructions": [
                {
                    "kind": "interaction_blocking_layer",
                    **element("#checkpoint"),
                    "intercepts_outside_control": True,
                    "visible_controls": [element("#continue", text="Continue")],
                    "visible_controls_omitted": 2,
                }
            ],
            "challenge_state": {"detected": False},
        }

    def test_summary_carries_unranked_candidates_for_each_control(self) -> None:
        summary = _build_scout_page_summary(self._evidence(2))

        assert summary["forms"][0]["fields"][0]["selector_candidates"][0]["selector"] == "#field-0"
        assert summary["forms"][0]["submit_controls"][0]["selector_candidates"][0]["selector"] == "#place-order"
        assert summary["navigation_targets"][0]["selector_candidates"][0]["selector"] == "a.nav-0"
        assert summary["modal_dismiss_controls"][0]["selector_candidates"][0]["selector"] == "#promo-close"
        assert summary["interaction_blocking_layers"] == [
            {
                "selector_candidates": [{"selector": "#checkpoint", "source": "test_fixture"}],
                "intercepts_outside_control": True,
                "visible_controls": [
                    {
                        "text": "Continue",
                        "selector_candidates": [{"selector": "#continue", "source": "test_fixture"}],
                    }
                ],
                "visible_controls_omitted": 2,
            }
        ]
        assert "match_count" not in summary["forms"][0]["fields"][0]["selector_candidates"][0]

    def test_summary_names_every_section_it_sheds(self) -> None:
        result: dict[str, Any] = {"ok": True, "data": {"filler": "x" * (scouting_module._SCOUT_RESULT_CHAR_CAP - 800)}}
        scouting_module._attach_scout_page_summary(_no_redaction_ctx(), result, self._evidence(8))

        page = result["data"]["page"]
        assert page["shed"][:2] == ["control_selectors", "navigation_targets"]
        assert page["navigation_targets"] == []
        assert "page_summary" not in page["shed"]

    def test_selectors_are_shed_before_any_control_the_baseline_carried(self) -> None:
        evidence = self._evidence(8)
        result: dict[str, Any] = {"ok": True, "data": {"filler": "x" * 600}}
        scouting_module._attach_scout_page_summary(_no_redaction_ctx(), result, evidence)

        page = result["data"]["page"]
        assert page["shed"] == ["control_selectors"]
        assert page["navigation_targets"] == [target["text"] for target in evidence["navigation_targets"]]
        assert page["forms"][0]["fields"] == [field["label"] for field in evidence["forms"][0]["fields"]]
        assert page["interaction_blocking_layers"] == [
            {
                "intercepts_outside_control": True,
                "visible_controls": ["Continue"],
                "visible_controls_omitted": 2,
            }
        ]

    def test_a_summary_too_large_to_shed_still_leaves_its_shed_record(self) -> None:
        result: dict[str, Any] = {"ok": True, "data": {"filler": "x" * scouting_module._SCOUT_RESULT_CHAR_CAP}}
        scouting_module._attach_scout_page_summary(_no_redaction_ctx(), result, self._evidence(8))

        page = result["data"]["page"]
        assert set(page) == {"shed"}
        assert page["shed"][0] == "control_selectors"
        assert page["shed"][-1] == "page_summary"

    def test_summary_carries_no_filled_field_value(self) -> None:
        secret = "hunter2-not-a-real-secret"
        summary = _build_scout_page_summary(
            {
                "page_title": "Sign in",
                "forms": [
                    {
                        "fields": [
                            {
                                "label": "Password",
                                "type": "password",
                                "value": secret,
                                "selector_candidates": [{"selector": "#pw", "source": "id", "match_count": 1}],
                            },
                            {
                                "label": "Username",
                                "value": "operator",
                                "selector_candidates": [{"selector": "#user", "source": "id", "match_count": 1}],
                            },
                        ],
                        "submit_controls": [
                            {
                                "text": "Sign in",
                                "selector_candidates": [{"selector": "#signin", "source": "id", "match_count": 1}],
                            }
                        ],
                    }
                ],
            }
        )

        assert secret not in json.dumps(summary)
        assert summary["forms"][0]["fields"][0] == {
            "text": "Password",
            "selector_candidates": [{"selector": "#pw", "source": "id"}],
        }

    def test_obstruction_predicate_ignores_visual_candidates(self) -> None:
        assert not _page_evidence_names_obstruction(
            {
                "modal_overlays": [],
                "challenge_controls": [],
                "challenge_state": {"detected": False},
                "visual_obstruction_candidates": [{"selector": ".sticky"}],
            }
        )
        assert not _page_evidence_names_obstruction({"modal_overlays": [{"selector": "#promo"}]})
        assert not _page_evidence_names_obstruction(
            {
                "page_obstructions": [
                    {
                        "kind": "interaction_blocking_layer",
                        "intercepts_outside_control": True,
                        "visible_controls": [{"text": "Continue", "selector": "#continue"}],
                    }
                ]
            }
        )
        assert _page_evidence_names_obstruction(
            {"modal_overlays": [{"selector": "#promo", "dismiss_controls": [{"text": "Close", "selector": ".x"}]}]}
        )
        assert _page_evidence_names_obstruction({"challenge_state": {"detected": True}})


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

    def test_screenshot_and_nudge_ordering(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import NUDGE_SENTINEL, _assemble_enforcement_messages

        screenshot_msg = self._screenshot_msg()
        msgs = _assemble_enforcement_messages(screenshot_msg, "please finish the workflow")

        screenshot_indices = [i for i, m in enumerate(msgs) if m is screenshot_msg]
        assert screenshot_indices == [msgs.index(screenshot_msg)]
        assert len(screenshot_indices) == 1

        nudge_index = next(
            i
            for i, m in enumerate(msgs)
            if isinstance(m.get("content"), str) and m["content"].startswith(NUDGE_SENTINEL)
        )
        assert nudge_index == len(msgs) - 1

    def test_screenshot_without_nudge(self) -> None:
        from skyvern.forge.sdk.copilot.enforcement import NUDGE_SENTINEL, _assemble_enforcement_messages

        screenshot_msg = self._screenshot_msg()
        msgs = _assemble_enforcement_messages(screenshot_msg, None)

        assert msgs.count(screenshot_msg) == 1
        assert not any(isinstance(m.get("content"), str) and m["content"].startswith(NUDGE_SENTINEL) for m in msgs)


def test_browser_overlays_are_covered_by_session_classification() -> None:
    """Every browser overlay participates in dead-session failure classification."""
    from skyvern.forge.sdk.copilot.streaming_adapter import _OBSERVATION_TOOLS
    from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays
    from skyvern.forge.sdk.copilot.unrecoverable_tool_error import _BROWSER_SESSION_TOOL_NAMES

    browser_overlays = {name for name, o in _build_skyvern_mcp_overlays().items() if o.requires_browser}

    assert browser_overlays <= _BROWSER_SESSION_TOOL_NAMES, browser_overlays - _BROWSER_SESSION_TOOL_NAMES

    # navigate_browser is the call that creates the need for an observation, so it cannot satisfy it.
    # Every other browser tool touches the page it arrived on, which is what the post-navigate nudge
    # is asking the model to do — leaving one out re-asks for work it already did.
    observers = browser_overlays - {"navigate_browser"}
    assert observers <= _OBSERVATION_TOOLS, observers - _OBSERVATION_TOOLS


@pytest.mark.asyncio
async def test_lifecycle_hooks_count_model_calls_and_enforcement_passes() -> None:
    ctx = make_copilot_ctx()
    hooks = CopilotRunHooks(ctx)

    await hooks.on_agent_start(MagicMock(), MagicMock())
    await hooks.on_llm_start(MagicMock(), MagicMock(), None, [])
    await hooks.on_llm_start(MagicMock(), MagicMock(), None, [])

    assert ctx.enforcement_pass_count == 1
    assert ctx.model_calls_this_turn == 2


@pytest.mark.asyncio
async def test_lifecycle_hooks_count_on_a_bare_agent_context() -> None:
    ctx = AgentContext(
        organization_id="org-1",
        workflow_id="wf-1",
        workflow_permanent_id="wfp-1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(is_disconnected=AsyncMock(return_value=False)),
    )
    hooks = CopilotRunHooks(ctx)

    await hooks.on_agent_start(MagicMock(), MagicMock())
    await hooks.on_llm_start(MagicMock(), MagicMock(), None, [])

    assert ctx.enforcement_pass_count == 1
    assert ctx.model_calls_this_turn == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("called_tools", "expected_count"),
    [
        pytest.param(["edit_block"], 0, id="no-build-test-call"),
        pytest.param(["run_blocks_and_collect_debug"], 1, id="one-build-test-call"),
        pytest.param(["edit_block_and_run", "update_and_run_blocks"], 2, id="two-build-test-calls"),
        pytest.param(["run_blocks_and_collect_debug", "request_credential"], 1, id="build-test-beside-a-card"),
    ],
)
async def test_on_llm_end_counts_the_responses_build_test_calls_before_any_of_them_runs(
    called_tools: list[str], expected_count: int
) -> None:
    ctx = make_copilot_ctx()
    ctx.build_test_tool_calls_in_model_response = 99
    response = SimpleNamespace(output=[SimpleNamespace(name=name) for name in called_tools])

    await CopilotRunHooks(ctx).on_llm_end(MagicMock(), MagicMock(), response)

    assert ctx.build_test_tool_calls_in_model_response == expected_count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_reason", "dispatched_this_turn", "expected_halt"),
    [
        pytest.param(SUPERSEDED_BY_NEWER_TEST_REASON, True, True, id="superseded-this-turn"),
        pytest.param("The extraction block found no matching element.", True, False, id="ordinary-failure"),
        pytest.param(SUPERSEDED_BY_NEWER_TEST_REASON, False, False, id="superseded-in-an-earlier-turn"),
        pytest.param(SUPERSEDED_BY_NEWER_TEST_REASON, False, False, id="inherited-by-a-repair-turn"),
    ],
)
async def test_a_superseded_build_test_run_stops_the_turn_before_it_can_dispatch_another(
    failure_reason: str, dispatched_this_turn: bool, expected_halt: bool
) -> None:
    ctx = make_copilot_ctx()
    if dispatched_this_turn:
        ctx.dispatched_run_ids_this_turn.add("wr_this_turn")
    else:
        # What a repair turn inherits: seeded as the turn's last run, but never dispatched here.
        ctx.last_run_blocks_workflow_run_id = "wr_this_turn"
    await _halt_turn_if_superseded(
        ctx,
        SimpleNamespace(failure_reason=failure_reason),
        workflow_run_id="wr_this_turn",
    )
    output = _mcp_text_output({"ok": False, "data": {"workflow_run_id": "wr_this_turn"}})
    tool = _fake_tool("run_blocks_and_collect_debug")

    if not expected_halt:
        await CopilotRunHooks(ctx).on_tool_end(_UNUSED, _UNUSED, tool, output)
        assert ctx.turn_halt is None
        return

    with pytest.raises(CopilotTurnHalt) as raised:
        await CopilotRunHooks(ctx).on_tool_end(_UNUSED, _UNUSED, tool, output)
    assert raised.value.halt.kind is TurnHaltKind.BUILD_TEST_SUPERSEDED
    assert raised.value.halt.run_refs == {"workflow_run_id": "wr_this_turn"}


@pytest.mark.asyncio
async def test_a_superseded_run_still_drains_after_its_reason_is_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The superseded run's own finalizer can overwrite the reason on the run row, so the drain
    falls back to copilot's record of what it ended."""
    import skyvern.forge.sdk.copilot.tools.run_execution as run_execution_module

    was_superseded = AsyncMock(return_value=True)
    monkeypatch.setattr(
        run_execution_module,
        "app",
        SimpleNamespace(
            DATABASE=SimpleNamespace(workflow_params=SimpleNamespace(build_test_run_was_superseded=was_superseded))
        ),
    )
    ctx = make_copilot_ctx(workflow_copilot_chat_id="wcc_1")
    ctx.dispatched_run_ids_this_turn.add("wr_this_turn")

    await _halt_turn_if_superseded(
        ctx,
        SimpleNamespace(failure_reason="code_block block failed. failure reason: Target page closed"),
        workflow_run_id="wr_this_turn",
    )

    assert ctx.turn_halt is not None
    assert ctx.turn_halt.kind is TurnHaltKind.BUILD_TEST_SUPERSEDED
    was_superseded.assert_awaited_once()


ACTION_SEAM_PRE_HOOKS = [
    "_click_pre_hook",
    "_type_text_pre_hook",
    "_select_option_pre_hook",
    "_press_key_pre_hook",
    "_scroll_pre_hook",
    "_sensitive_origin_page_action_pre_hook",
]
ACTION_SEAM_POST_HOOKS = [
    "_click_post_hook",
    "_type_text_post_hook",
    "_select_option_post_hook",
    "_press_key_post_hook",
    "_scroll_post_hook",
    "_wait_for_either_state_post_hook",
]
CONTINUATION_RUN_OTP = "424242"
CONTINUATION_RUN_PASSWORD = "Sp1r!t-Level-2026"


def _terminal_credential_run_ctx() -> AgentContext:
    ctx = make_copilot_ctx(browser_session_id="pbs_run")
    clear_session_scrub_values("pbs_run")
    ctx.last_run_blocks_browser_session_id = "pbs_run"
    ctx.last_run_blocks_workflow_run_id = "wr_credential"
    taint_by_terminal_run(ctx, workflow_run_id="wr_credential", session_id="pbs_run")
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        workflow_run_id="wr_credential",
        parameters={"password": CONTINUATION_RUN_PASSWORD, "totp": CONTINUATION_RUN_OTP},
        contains_sensitive_values=True,
        contains_all_sensitive_values=True,
    )
    return ctx


class TestSensitiveOriginActionContinuation:
    @pytest.mark.asyncio
    async def test_click_continues_on_the_page_a_terminal_credential_run_left(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_hooks, "_scout_session_download_names", AsyncMock(return_value=frozenset()))
        ctx = _terminal_credential_run_ctx()
        landed_page = {
            "page_title": f"Web analytics {CONTINUATION_RUN_PASSWORD}",
            "forms": [
                {
                    "fields": [{"name": "range", "label": "Date range", "type": "select", "selector": "#range"}],
                    "submit_controls": [{"text": "Apply", "type": "submit", "selector": "#apply"}],
                }
            ],
            "navigation_targets": [],
            "result_containers": [{"tag": "table", "id": "trends", "selector": "#trends"}],
        }
        ctx.discovery_mcp_server = SimpleNamespace(
            call_internal_tool=AsyncMock(return_value={"ok": True, "data": {"result": landed_page}})
        )
        ctx.supports_vision = False

        assert await mcp_hooks._click_pre_hook({"selector": "a.nav--analytics"}, ctx) is None
        result = await mcp_hooks._click_post_hook(
            {
                "ok": True,
                "data": {"selector": "a.nav--analytics", "text_content": f"Web analytics {CONTINUATION_RUN_OTP}"},
            },
            {"browser_context": {"url": f"http://pathfold.test/app?code={CONTINUATION_RUN_OTP}", "title": "Pathfold"}},
            ctx,
        )

        assert result["ok"] is True
        assert ctx.scouted_interactions, "the continuation click was not recorded"
        # The page the click reached is observed and retained, not just the click itself: a
        # click-reached block can be authored against it without a separate inspection.
        assert ctx.last_scout_act_observe_packet is not None
        assert result["observation_step"] is not None
        assert result["data"]["observation_step"] == result["observation_step"]
        # Every retained store, the interaction-reached URLs included, is free of both run secrets.
        retained = json.dumps(
            {
                "scouted_interactions": ctx.scouted_interactions,
                "scout_trajectory": ctx.scout_trajectory,
                "flow_evidence": ctx.flow_evidence,
                "composition_page_evidence": ctx.composition_page_evidence,
                "act_observe_packet": ctx.last_scout_act_observe_packet,
            },
            default=str,
        )
        assert CONTINUATION_RUN_PASSWORD not in retained
        assert CONTINUATION_RUN_OTP not in retained
        assert ctx.pending_screenshots == []

    @pytest.mark.asyncio
    async def test_a_retained_selector_a_secret_rewrote_is_dropped_not_kept_dead(self) -> None:
        """A six-digit code can occur inside an id by chance; a placeholder there is a dead locator
        that would fail a later run silently, so the whole locator identity is dropped: the synthesizer
        must not fall back from the dropped selector to a role/name pair that carries the placeholder."""
        ctx = _terminal_credential_run_ctx()
        register_secret_scrub_value(ctx, CONTINUATION_RUN_OTP)

        scouting_module._record_scouted_interaction(
            ctx,
            tool_name="click",
            selector=f"#row-{CONTINUATION_RUN_OTP} button",
            source_url=f"http://pathfold.test/app?code={CONTINUATION_RUN_OTP}",
            accessible_name=f"Verify {CONTINUATION_RUN_OTP}",
            role="button",
        )

        recorded = ctx.scouted_interactions[-1]
        assert not set(recorded) & set(scouting_module._RETAINED_LOCATOR_IDENTITY_FIELDS)
        assert recorded["source_url"] == f"http://pathfold.test/app?code={REDACTED_SECRET_PLACEHOLDER}"
        assert CONTINUATION_RUN_OTP not in json.dumps(ctx.scout_trajectory)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arm", SENSITIVE_DISCLOSURE_WITHHOLDING_ARMS)
    async def test_evaluate_post_hook_stays_withheld_when_a_disclosure_prerequisite_is_absent(self, arm: str) -> None:
        ctx = _terminal_credential_run_ctx()
        remove_sensitive_disclosure_prerequisite(ctx, arm)

        result = await mcp_hooks._evaluate_post_hook(
            {"ok": True, "data": {"result": f"code {CONTINUATION_RUN_OTP}", "url": "http://pathfold.test/app"}},
            {},
            ctx,
        )

        assert result == {"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arm", SENSITIVE_DISCLOSURE_WITHHOLDING_ARMS)
    async def test_click_stays_withheld_when_a_disclosure_prerequisite_is_absent(self, arm: str) -> None:
        ctx = _terminal_credential_run_ctx()
        remove_sensitive_disclosure_prerequisite(ctx, arm)

        refusal = await mcp_hooks._click_pre_hook({"selector": "a.nav--analytics"}, ctx)

        assert refusal == {"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arm", SENSITIVE_DISCLOSURE_WITHHOLDING_ARMS)
    async def test_type_text_stays_withheld_when_a_disclosure_prerequisite_is_absent(self, arm: str) -> None:
        ctx = _terminal_credential_run_ctx()
        remove_sensitive_disclosure_prerequisite(ctx, arm)

        refusal = await mcp_hooks._type_text_pre_hook({"selector": "#token", "text": "hello"}, ctx)

        assert refusal == {"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hook_name", ACTION_SEAM_PRE_HOOKS)
    async def test_every_action_pre_hook_stays_withheld_without_a_matching_registry(self, hook_name: str) -> None:
        ctx = _terminal_credential_run_ctx()
        remove_sensitive_disclosure_prerequisite(ctx, "registry_missing")

        refusal = await getattr(mcp_hooks, hook_name)({"selector": "#token", "text": "hello", "key": "Enter"}, ctx)

        assert refusal == {"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hook_name", ACTION_SEAM_POST_HOOKS)
    async def test_every_action_post_hook_stays_withheld_without_a_matching_registry(self, hook_name: str) -> None:
        ctx = _terminal_credential_run_ctx()
        remove_sensitive_disclosure_prerequisite(ctx, "registry_missing")

        refusal = await getattr(mcp_hooks, hook_name)(
            {"ok": True, "data": {"selector": "#token", "text_content": CONTINUATION_RUN_OTP}},
            {"browser_context": {"url": "http://pathfold.test/app"}},
            ctx,
        )

        assert refusal == {"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR}
