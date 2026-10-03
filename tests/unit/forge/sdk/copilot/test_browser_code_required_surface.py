from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from typing import Any

import pytest
from agents import RunContextWrapper
from agents.tool import invoke_function_tool
from agents.tool_context import ToolContext
from mcp.types import Tool as MCPTool

from skyvern.forge.sdk.copilot.browser_ablation import (
    CopilotBrowserCodeMode,
    CopilotEvalMode,
    CopilotToolSurface,
    CopilotToolSurfaceIdentity,
    dispatch_allowlist_enforced,
    resolve_copilot_tool_surface,
)
from skyvern.forge.sdk.copilot.mcp_adapter import SkyvernOverlayMCPServer
from skyvern.forge.sdk.copilot.tools import (
    NATIVE_TOOLS,
    _build_skyvern_mcp_overlays,
    copilot_native_tools,
    get_skyvern_mcp_alias_map,
)
from skyvern.forge.sdk.copilot.tools._shared import _CURRENT_PAGE_INSPECTION_TARGETS
from skyvern.forge.sdk.copilot.tools.composition_capture import (
    CURRENT_PAGE_INSPECTION_TARGET,
    current_page_inspection_tool,
)
from skyvern.forge.sdk.copilot.unrecoverable_tool_error import _is_unrecoverable_browser_session_error
from tests.unit.copilot_test_helpers import make_copilot_ctx
from tests.unit.test_copilot_secret_scrub import _FakeClient

# The browser-bound tools the surface still advertises. Tools that need no browser are kept by
# rule rather than by name, so this is the whole browser-facing keep list.
_REQUIRED_BROWSER_READ_NAMES = {
    "get_browser_screenshot",
    "console_messages",
    "wait_for_either_state",
    "skyvern_frame_list",
    "skyvern_tab_list",
    "skyvern_tab_close",
}
_REQUIRED_WORKFLOW_KNOWLEDGE_NAMES = {"get_workflow_knowledge", "get_block_schema", "validate_block"}
_REMOVED_MODEL_FACING_NAMES = {
    "navigate_browser",
    "click",
    "type_text",
    "scroll",
    "select_option",
    "press_key",
    "skyvern_frame_switch",
    "skyvern_frame_main",
    "skyvern_tab_new",
    "skyvern_tab_switch",
    "evaluate",
    "discover_workflow_entrypoint",
}


def _surface(*, browser_code_available: bool) -> CopilotToolSurface:
    return resolve_copilot_tool_surface(
        mode=None,
        native_tools=copilot_native_tools(supports_question_tool=True, browser_code_available=browser_code_available),
        alias_map=get_skyvern_mcp_alias_map(),
        overlays=_build_skyvern_mcp_overlays(),
        browser_code_mode=CopilotBrowserCodeMode.REPLACE if browser_code_available else CopilotBrowserCodeMode.OFF,
    )


def test_the_required_surface_drops_the_direct_and_mixed_action_routes() -> None:
    optional = _surface(browser_code_available=False)
    required = _surface(browser_code_available=True)

    overlays = _build_skyvern_mcp_overlays()

    assert required.identity == CopilotToolSurfaceIdentity.REQUIRED_CODE
    assert {name for name in required.ordered_mcp_names if overlays[name].requires_browser} == (
        _REQUIRED_BROWSER_READ_NAMES
    )
    assert _REQUIRED_WORKFLOW_KNOWLEDGE_NAMES <= set(required.ordered_mcp_names)
    removed = (set(optional.ordered_mcp_names) | set(optional.ordered_native_names)) - (
        set(required.ordered_mcp_names) | set(required.ordered_native_names)
    )
    assert removed == _REMOVED_MODEL_FACING_NAMES
    assert {
        "run_browser_code",
        "fill_credential_field",
        "request_credential",
        "inspect_locator_matches",
        "inspect_page_for_composition",
        "ask_user",
        "update_workflow",
        "run_blocks_and_collect_debug",
        "get_run_results",
    } <= set(required.ordered_native_names)
    assert required.sha256 != optional.sha256
    assert set(required.alias_map) == set(required.overlays) == set(required.ordered_mcp_names)


def test_a_deployment_without_the_code_tool_keeps_the_browser_tools() -> None:
    """Where the code tool is not offered, the surface is the parent's, so a deployment the rollout
    does not reach is untouched by this change."""
    optional = _surface(browser_code_available=False)

    assert optional.identity == CopilotToolSurfaceIdentity.OPTIONAL
    assert "run_browser_code" not in optional.ordered_native_names
    assert {"click", "type_text", "evaluate", "navigate_browser"} <= set(optional.ordered_mcp_names)


def test_the_locator_probe_arms_follow_the_production_identity() -> None:
    surface = resolve_copilot_tool_surface(
        mode=CopilotEvalMode.REPAIR_PROBE_OFF,
        native_tools=copilot_native_tools(supports_question_tool=True, browser_code_available=True),
        alias_map=get_skyvern_mcp_alias_map(),
        overlays=_build_skyvern_mcp_overlays(),
        browser_code_mode=CopilotBrowserCodeMode.REPLACE,
    )

    assert surface.identity == CopilotToolSurfaceIdentity.REQUIRED_CODE
    assert "inspect_locator_matches" not in surface.ordered_native_names
    assert surface.ordered_mcp_names == _surface(browser_code_available=True).ordered_mcp_names


def test_only_a_narrowed_surface_enforces_the_dispatch_allowlist() -> None:
    assert dispatch_allowlist_enforced(_surface(browser_code_available=True).identity)
    assert dispatch_allowlist_enforced(CopilotToolSurfaceIdentity.BROWSER_ABLATION)
    assert not dispatch_allowlist_enforced(_surface(browser_code_available=False).identity)
    assert not dispatch_allowlist_enforced(None)


@pytest.mark.asyncio
async def test_the_optional_surface_dispatches_a_raw_transport_name_as_before() -> None:
    optional = _surface(browser_code_available=False)
    dispatched: list[str] = []
    server = SkyvernOverlayMCPServer(
        transport=SimpleNamespace(),
        overlays=optional.overlays,
        alias_map=optional.alias_map,
        allowlist=frozenset(optional.alias_map.values()),
        context_provider=lambda: make_copilot_ctx(browser_session_id="pbs_1"),
        enforce_dispatch_allowlist=dispatch_allowlist_enforced(optional.identity),
    )
    server._client = _FakeClient({"ok": True}, on_call=lambda: dispatched.append("called"))

    result = await server._call_tool("skyvern_workflow_knowledge", {})

    assert dispatched == ["called"]
    assert result.isError is False


def _required_surface_server(client: _FakeClient) -> SkyvernOverlayMCPServer:
    required = _surface(browser_code_available=True)
    server = SkyvernOverlayMCPServer(
        transport=SimpleNamespace(),
        overlays=required.overlays,
        alias_map=required.alias_map,
        allowlist=frozenset(required.alias_map.values()),
        context_provider=lambda: make_copilot_ctx(browser_session_id="pbs_1"),
        enforce_dispatch_allowlist=dispatch_allowlist_enforced(required.identity),
    )
    server._client = client
    return server


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name", ["click", "type_text", "evaluate", "navigate_browser", "skyvern_click", "skyvern_evaluate"]
)
async def test_a_removed_or_raw_name_is_refused_before_the_transport(tool_name: str) -> None:
    dispatched: list[str] = []
    server = _required_surface_server(_FakeClient({"ok": True}, on_call=lambda: dispatched.append(tool_name)))

    with pytest.raises(ValueError, match="not available"):
        await server._call_tool(tool_name, {})

    assert dispatched == []


@pytest.mark.asyncio
async def test_a_kept_tool_still_dispatches_on_the_required_surface() -> None:
    dispatched: list[str] = []
    server = _required_surface_server(_FakeClient({"ok": True}, on_call=lambda: dispatched.append("called")))

    result = await server._call_tool("get_workflow_knowledge", {})

    assert dispatched == ["called"]
    assert result.isError is False


@pytest.mark.asyncio
async def test_the_required_surface_advertises_only_its_own_catalog() -> None:
    required = _surface(browser_code_available=True)
    server = _required_surface_server(_FakeClient({"ok": True}))
    server._cached_raw_tools = [
        MCPTool(name=transport, description=name, inputSchema={"type": "object", "properties": {}})
        for name, transport in get_skyvern_mcp_alias_map().items()
    ]

    advertised = {tool.name for tool in await server.list_tools()}

    assert advertised == set(required.ordered_mcp_names)


@pytest.mark.asyncio
async def test_the_projected_inspection_reads_the_current_page_whatever_the_model_asks_for() -> None:
    source = next(tool for tool in NATIVE_TOOLS if tool.name == "inspect_page_for_composition")
    projected = current_page_inspection_tool(source)
    observed: list[Any] = []

    async def record(_ctx: object, arguments: str) -> str:
        observed.append(json.loads(arguments))
        return "{}"

    delegating = current_page_inspection_tool(dataclasses.replace(source, on_invoke_tool=record))

    await delegating.on_invoke_tool(SimpleNamespace(context=None), json.dumps({"target_url": "https://example.com/a"}))

    assert "target_url" not in projected.params_json_schema["properties"]
    assert "target_url" not in projected.params_json_schema["required"]
    assert "target_url" in source.params_json_schema["properties"]
    assert observed == [{"target_url": CURRENT_PAGE_INSPECTION_TARGET}]
    assert CURRENT_PAGE_INSPECTION_TARGET in _CURRENT_PAGE_INSPECTION_TARGETS


@pytest.mark.asyncio
async def test_the_projected_inspection_runs_under_the_runner_tool_context() -> None:
    """The runner forks a bare context for a wrapper that declares RunContextWrapper, and the real
    delegate cannot run on one; invoking through the runner's path proves the real body executed."""
    source = next(tool for tool in NATIVE_TOOLS if tool.name == "inspect_page_for_composition")
    projected = current_page_inspection_tool(source)
    arguments = json.dumps({"target_url": "https://example.com/a"})
    tool_ctx = ToolContext.from_agent_context(
        RunContextWrapper(context=make_copilot_ctx(browser_session_id="pbs_1")),
        tool_call_id="call_1",
        tool_name=projected.name,
        tool_arguments=arguments,
    )

    result = json.loads(await invoke_function_tool(function_tool=projected, context=tool_ctx, arguments=arguments))

    assert result["browser_target"] == "debug"
    assert "RunContextWrapper" not in str(result.get("error", ""))


def test_a_lost_browser_session_reported_through_code_ends_the_loop_like_a_direct_tool() -> None:
    lost = {"ok": False, "error": "browser session pbs_1 not found"}
    code_error = {"ok": False, "error": "locator timed out at line 2"}

    assert _is_unrecoverable_browser_session_error("run_browser_code", lost)
    assert not _is_unrecoverable_browser_session_error("run_browser_code", code_error)
