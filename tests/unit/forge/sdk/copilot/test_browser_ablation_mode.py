from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.tools.tool import Tool as RegisteredTool
from jsonschema import Draft202012Validator
from mcp.types import Tool as MCPTool

from skyvern.cli.mcp_tools import mcp
from skyvern.forge.sdk.copilot import screenshot_utils
from skyvern.forge.sdk.copilot.browser_ablation import (
    BROWSER_ABLATION_MCP_TOOL_EXCLUSIONS,
    BROWSER_ABLATION_NATIVE_TOOLS,
    BROWSER_ABLATION_PROMPT_TEMPLATE,
    REPAIR_PROBE_TOOL,
    CopilotEvalMode,
    CopilotToolSurface,
    config_for_eval_mode,
    prompt_sha256,
    prompt_template_for_mode,
    resolve_copilot_tool_surface,
)
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.mcp_adapter import BROWSER_TARGET_PARAM_NAME, SchemaOverlay, SkyvernOverlayMCPServer
from skyvern.forge.sdk.copilot.tools import NATIVE_TOOLS, _build_skyvern_mcp_overlays, get_skyvern_mcp_alias_map
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotBrowserAblationResponseUpdate,
    WorkflowCopilotStreamResponseUpdate,
)

_EXISTING_BROWSER_ALIASES = (
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
)
_EXPECTED_BROWSER_ABLATION_MCP_TOOLS = (
    *_EXISTING_BROWSER_ALIASES,
    "skyvern_tab_list",
    "skyvern_tab_new",
    "skyvern_tab_switch",
    "skyvern_tab_close",
    "skyvern_tab_wait_for_new",
    "skyvern_page",
)
_FRAME_TOOLS = ("skyvern_frame_list", "skyvern_frame_switch", "skyvern_frame_main")


def _catalogs() -> tuple[list[SimpleNamespace], dict[str, str], dict[str, SchemaOverlay]]:
    native_names = ("update_workflow", *BROWSER_ABLATION_NATIVE_TOOLS, "run_blocks_and_collect_debug")
    native = [SimpleNamespace(name=name) for name in native_names]
    aliases = {
        "get_workflow_knowledge": "raw_knowledge",
        **{name: f"raw_{name}" for name in _EXISTING_BROWSER_ALIASES},
        "validate_block": "raw_validate",
    }
    overlays = {name: SchemaOverlay(description=name) for name in aliases}
    return native, aliases, overlays


def _registered_browser_tools(aliases: dict[str, str]) -> list[SimpleNamespace]:
    def registered(name: str, tags: set[str]) -> SimpleNamespace:
        return SimpleNamespace(name=name, tags=tags, description=f"{name} description")

    return [
        *(
            registered(aliases[name], {"inspection"} if name == "console_messages" else {"browser_primitive"})
            for name in _EXISTING_BROWSER_ALIASES
        ),
        registered(aliases["get_workflow_knowledge"], {"block_discovery"}),
        registered(aliases["validate_block"], {"block_discovery"}),
        registered("skyvern_tab_list", {"tab_management"}),
        registered("skyvern_tab_new", {"tab_management"}),
        registered("skyvern_open_tabs", {"tab_management", "browser_primitive"}),
        registered("skyvern_tab_switch", {"tab_management"}),
        registered("skyvern_tab_close", {"tab_management"}),
        registered("skyvern_tab_wait_for_new", {"tab_management"}),
        registered("skyvern_page", {"page_read"}),
        registered("skyvern_act", {"browser_primitive", "ai_powered"}),
        registered("skyvern_workflow_run", {"workflow"}),
    ]


def _browser_surface(
    native: list[SimpleNamespace],
    aliases: dict[str, str],
    overlays: dict[str, SchemaOverlay],
) -> CopilotToolSurface:
    return resolve_copilot_tool_surface(
        mode=CopilotEvalMode.BROWSER_ABLATION,
        native_tools=native,
        alias_map=aliases,
        overlays=overlays,
        registered_mcp_tools=_registered_browser_tools(aliases),
    )


def test_browser_ablation_projects_registered_tab_and_page_tools_without_workflow_tools() -> None:
    native, aliases, overlays = _catalogs()

    surface = _browser_surface(native, aliases, overlays)

    assert {
        "skyvern_tab_list",
        "skyvern_tab_new",
        "skyvern_tab_switch",
        "skyvern_tab_wait_for_new",
        "skyvern_tab_close",
        "skyvern_page",
    }.issubset(surface.ordered_mcp_names)
    assert surface.ordered_mcp_names == _EXPECTED_BROWSER_ABLATION_MCP_TOOLS
    assert "skyvern_open_tabs" not in surface.ordered_mcp_names
    assert "skyvern_workflow_run" not in surface.ordered_mcp_names


def test_normal_copilot_frame_contracts_are_shared_with_browser_ablation_and_hashed() -> None:
    aliases = get_skyvern_mcp_alias_map()
    overlays = _build_skyvern_mcp_overlays()
    normal = resolve_copilot_tool_surface(
        mode=None,
        native_tools=[],
        alias_map=aliases,
        overlays=overlays,
    )
    aliases_without_frames = {name: transport for name, transport in aliases.items() if name not in _FRAME_TOOLS}
    overlays_without_frames = {name: overlay for name, overlay in overlays.items() if name not in _FRAME_TOOLS}
    old_surface = resolve_copilot_tool_surface(
        mode=None,
        native_tools=[],
        alias_map=aliases_without_frames,
        overlays=overlays_without_frames,
    )

    assert all(aliases[name] == name for name in _FRAME_TOOLS)
    assert set(_FRAME_TOOLS) <= set(normal.ordered_mcp_names)
    assert all(overlays[name].requires_browser for name in _FRAME_TOOLS)
    assert all(overlays[name].hide_params == {"session_id", "cdp_url"} for name in _FRAME_TOOLS)
    assert normal.sha256 != old_surface.sha256


@pytest.mark.asyncio
async def test_browser_ablation_projects_the_app_registry_contract() -> None:
    registered_tools = await mcp.list_tools(run_middleware=False)
    surface = resolve_copilot_tool_surface(
        mode=CopilotEvalMode.BROWSER_ABLATION,
        native_tools=list(NATIVE_TOOLS),
        alias_map=get_skyvern_mcp_alias_map(),
        overlays=_build_skyvern_mcp_overlays(),
        registered_mcp_tools=registered_tools,
    )

    assert {
        "skyvern_tab_list",
        "skyvern_tab_new",
        "skyvern_tab_switch",
        "skyvern_tab_wait_for_new",
        "skyvern_tab_close",
        "skyvern_page",
        *_FRAME_TOOLS,
    }.issubset(surface.ordered_mcp_names)
    assert surface.ordered_mcp_names == _EXPECTED_BROWSER_ABLATION_MCP_TOOLS
    assert BROWSER_ABLATION_MCP_TOOL_EXCLUSIONS.isdisjoint(surface.alias_map.values())
    assert {"get_workflow_knowledge", "get_block_schema", "validate_block"}.isdisjoint(surface.ordered_mcp_names)
    registered_workflow_tools = {tool.name for tool in registered_tools if "workflow" in set(tool.tags or ())}
    assert registered_workflow_tools.isdisjoint(surface.alias_map.values())


async def _advertised_schemas(
    surface: CopilotToolSurface, registered: list[RegisteredTool]
) -> dict[str, dict[str, Any]]:
    server = SkyvernOverlayMCPServer(
        transport=MagicMock(),
        overlays=surface.overlays,
        alias_map=surface.alias_map,
        allowlist=frozenset(surface.alias_map.values()),
        context_provider=lambda: SimpleNamespace(organization_id="o_test"),
    )
    server._client = MagicMock()
    server._cached_raw_tools = [
        MCPTool(name=tool.name, description=tool.description, inputSchema=tool.parameters) for tool in registered
    ]
    return {tool.name: tool.inputSchema for tool in await server.list_tools()}


def _production_surfaces(registered: list[RegisteredTool]) -> tuple[CopilotToolSurface, CopilotToolSurface]:
    aliases = get_skyvern_mcp_alias_map()
    overlays = _build_skyvern_mcp_overlays()
    normal = resolve_copilot_tool_surface(
        mode=None,
        native_tools=list(NATIVE_TOOLS),
        alias_map=aliases,
        overlays=overlays,
    )
    ablation = resolve_copilot_tool_surface(
        mode=CopilotEvalMode.BROWSER_ABLATION,
        native_tools=list(NATIVE_TOOLS),
        alias_map=aliases,
        overlays=overlays,
        registered_mcp_tools=registered,
    )
    return normal, ablation


@pytest.mark.asyncio
async def test_the_advertised_tab_contract_is_closed_around_the_fields_it_offers() -> None:
    registered = await mcp.list_tools(run_middleware=False)
    _, ablation = _production_surfaces(registered)

    tab_new = (await _advertised_schemas(ablation, registered))["skyvern_tab_new"]

    assert set(tab_new["properties"]) == {"url", BROWSER_TARGET_PARAM_NAME}
    assert tab_new["additionalProperties"] is False
    validator = Draft202012Validator(tab_new)
    assert validator.is_valid({"url": "https://example.com"})
    assert not validator.is_valid({"url": "https://example.com", "settle_seconds": 30})


@pytest.mark.asyncio
async def test_shared_browser_tools_advertise_one_contract_across_both_modes() -> None:
    registered = await mcp.list_tools(run_middleware=False)
    normal, ablation = _production_surfaces(registered)

    normal_schemas = await _advertised_schemas(normal, registered)
    ablation_schemas = await _advertised_schemas(ablation, registered)

    shared = set(normal_schemas) & set(ablation_schemas)
    assert set(_EXISTING_BROWSER_ALIASES) <= shared
    assert {name: normal_schemas[name] for name in shared} == {name: ablation_schemas[name] for name in shared}
    assert all(normal_schemas[name].get("additionalProperties") is False for name in shared)


def test_browser_ablation_preserves_existing_aliases_then_adds_required_registry_capabilities() -> None:
    native, aliases, overlays = _catalogs()

    surface = _browser_surface(native, aliases, overlays)

    assert tuple(tool.name for tool in surface.native_tools) == BROWSER_ABLATION_NATIVE_TOOLS
    assert surface.ordered_mcp_names == _EXPECTED_BROWSER_ABLATION_MCP_TOOLS
    assert tuple(surface.alias_map) == surface.ordered_mcp_names
    assert tuple(surface.overlays) == surface.ordered_mcp_names
    assert surface.overlays["skyvern_page"].hide_params == {"session_id", "cdp_url"}
    assert surface.overlays["skyvern_page"].requires_browser is True
    assert "update_workflow" not in surface.ordered_native_names
    assert "validate_block" not in surface.ordered_mcp_names


def test_new_copilot_compatible_browser_tool_is_included_without_an_ablation_allowlist_change() -> None:
    native, aliases, overlays = _catalogs()
    aliases["future_browser"] = "skyvern_future_browser"
    overlays["future_browser"] = SchemaOverlay(description="future browser")
    registered = [
        *_registered_browser_tools(aliases),
        SimpleNamespace(name="skyvern_future_browser", tags={"browser_primitive"}, description="future browser"),
        SimpleNamespace(name="skyvern_unadapted_browser", tags={"browser_primitive"}, description="unadapted browser"),
    ]

    surface = resolve_copilot_tool_surface(
        mode=CopilotEvalMode.BROWSER_ABLATION,
        native_tools=native,
        alias_map=aliases,
        overlays=overlays,
        registered_mcp_tools=registered,
    )

    assert surface.ordered_mcp_names[len(_EXISTING_BROWSER_ALIASES)] == "future_browser"
    assert surface.alias_map["future_browser"] == "skyvern_future_browser"
    assert "skyvern_unadapted_browser" not in surface.ordered_mcp_names


def test_default_surface_preserves_source_objects_and_order() -> None:
    native, aliases, overlays = _catalogs()

    surface = resolve_copilot_tool_surface(
        mode=None,
        native_tools=native,
        alias_map=aliases,
        overlays=overlays,
    )

    assert surface.native_tools == tuple(native)
    assert surface.alias_map is aliases
    assert surface.overlays is overlays


def test_surface_resolution_fails_on_missing_or_duplicate_names() -> None:
    native, aliases, overlays = _catalogs()
    with pytest.raises(ValueError, match="missing native tool names"):
        resolve_copilot_tool_surface(
            mode=CopilotEvalMode.BROWSER_ABLATION,
            native_tools=[tool for tool in native if tool.name != "fill_credential_field"],
            alias_map=aliases,
            overlays=overlays,
            registered_mcp_tools=_registered_browser_tools(aliases),
        )
    with pytest.raises(ValueError, match="duplicate native tool names"):
        resolve_copilot_tool_surface(
            mode=CopilotEvalMode.BROWSER_ABLATION,
            native_tools=[*native, native[0]],
            alias_map=aliases,
            overlays=overlays,
            registered_mcp_tools=_registered_browser_tools(aliases),
        )
    duplicate_registered = _registered_browser_tools(aliases)
    with pytest.raises(ValueError, match="duplicate MCP tool names in registered catalog"):
        resolve_copilot_tool_surface(
            mode=CopilotEvalMode.BROWSER_ABLATION,
            native_tools=native,
            alias_map=aliases,
            overlays=overlays,
            registered_mcp_tools=[*duplicate_registered, duplicate_registered[0]],
        )
    duplicate_aliases = dict(aliases)
    duplicate_aliases["evaluate"] = duplicate_aliases["click"]
    with pytest.raises(ValueError, match="duplicate MCP tool names"):
        resolve_copilot_tool_surface(
            mode=CopilotEvalMode.BROWSER_ABLATION,
            native_tools=native,
            alias_map=duplicate_aliases,
            overlays=overlays,
            registered_mcp_tools=_registered_browser_tools(aliases),
        )


def test_hashes_are_stable_and_order_sensitive() -> None:
    native, aliases, overlays = _catalogs()
    surface = _browser_surface(native, aliases, overlays)
    reordered_aliases = dict(reversed(surface.alias_map.items()))
    reordered = resolve_copilot_tool_surface(
        mode=None,
        native_tools=list(surface.native_tools),
        alias_map=reordered_aliases,
        overlays=surface.overlays,
    )

    assert surface.sha256 == surface.sha256
    assert surface.sha256 != reordered.sha256
    assert prompt_sha256("prompt") == prompt_sha256("prompt")
    assert prompt_sha256("prompt") != prompt_sha256("prompt changed")


def test_surface_hash_changes_with_a_model_visible_tool_contract() -> None:
    native, aliases, overlays = _catalogs()
    surface = _browser_surface(native, aliases, overlays)
    changed_overlays = dict(overlays)
    changed_overlays["click"] = SchemaOverlay(description="changed click contract")
    changed = resolve_copilot_tool_surface(
        mode=CopilotEvalMode.BROWSER_ABLATION,
        native_tools=native,
        alias_map=aliases,
        overlays=changed_overlays,
        registered_mcp_tools=_registered_browser_tools(aliases),
    )

    assert surface.sha256 != changed.sha256


def test_advertised_surface_hash_changes_with_mcp_schema() -> None:
    native, aliases, overlays = _catalogs()
    surface = _browser_surface(native, aliases, overlays)
    advertised = [
        MCPTool(name=name, description=name, inputSchema={"type": "object"}) for name in surface.ordered_mcp_names
    ]
    changed = list(advertised)
    changed[-1] = MCPTool(
        name=changed[-1].name,
        description=changed[-1].description,
        inputSchema={"type": "object", "properties": {"seconds": {"type": "integer"}}},
    )

    assert surface.advertised_sha256(advertised) != surface.advertised_sha256(changed)


def test_prompt_selection_changes_only_for_browser_ablation() -> None:
    default = "workflow-copilot-agent.j2"

    assert prompt_template_for_mode(None, default) == default
    assert prompt_template_for_mode(CopilotEvalMode.BROWSER_ABLATION, default) == BROWSER_ABLATION_PROMPT_TEMPLATE


def test_prompt_selection_preserves_custom_config_subclasses_without_reconstructing_them() -> None:
    class CustomInitConfig(CopilotConfig):
        def __init__(self) -> None:
            super().__init__(security_rules="cloud rules")

    original = CustomInitConfig()

    selected = config_for_eval_mode(original, CopilotEvalMode.BROWSER_ABLATION)

    assert isinstance(selected, CustomInitConfig)
    assert selected is not original
    assert selected.prompt_template == BROWSER_ABLATION_PROMPT_TEMPLATE
    assert selected.security_rules == "cloud rules"
    assert original.prompt_template != BROWSER_ABLATION_PROMPT_TEMPLATE


def test_default_response_schema_does_not_gain_ablation_fields() -> None:
    selected_fields = {
        "eval_mode",
        "browser_session_id",
        "prompt_sha256",
        "tool_surface_sha256",
        "input_tokens",
        "output_tokens",
        "tool_activity",
        "screenshot_frames",
    }

    assert selected_fields.isdisjoint(WorkflowCopilotStreamResponseUpdate.model_fields)
    assert selected_fields.issubset(WorkflowCopilotBrowserAblationResponseUpdate.model_fields)


def test_browser_ablation_screenshot_evidence_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resize(value: str, *, provenance: SimpleNamespace, captured_at: float | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            capture_id=f"capture-{value}",
            mime="image/jpeg",
            b64=value,
            provenance=provenance,
            captured_at=captured_at or 0.0,
        )

    monkeypatch.setattr(screenshot_utils, "resize_screenshot_b64", fake_resize)
    provenance = SimpleNamespace(
        source_tool="screenshot", captured_url="https://example.com", browser_session_id="pbs_1"
    )
    ctx = SimpleNamespace(
        supports_vision=True,
        pending_screenshots=[],
        eval_mode="browser_ablation",
        eval_screenshot_frames=[],
    )

    for index in range(30):
        assert screenshot_utils.enqueue_screenshot(ctx, str(index), provenance=provenance)

    assert len(ctx.eval_screenshot_frames) == 24
    assert [frame["capture_id"] for frame in ctx.eval_screenshot_frames[:12]] == [
        f"capture-{index}" for index in range(12)
    ]
    assert ctx.eval_screenshot_frames[-1]["capture_id"] == "capture-29"


@pytest.mark.asyncio
async def test_mcp_advertisement_order_and_dispatch_share_the_same_boundary() -> None:
    native, aliases, overlays = _catalogs()
    surface = _browser_surface(native, aliases, overlays)
    selected_aliases = surface.alias_map
    server = SkyvernOverlayMCPServer(
        transport=object(),
        overlays=surface.overlays,
        alias_map=selected_aliases,
        allowlist=frozenset(selected_aliases.values()),
        context_provider=lambda: SimpleNamespace(),
        ordered_allowlist=tuple(selected_aliases.values()),
        enforce_dispatch_allowlist=True,
    )
    raw_tools = [
        MCPTool(name=raw_name, description=name, inputSchema={"type": "object"})
        for name, raw_name in reversed(tuple(selected_aliases.items()))
    ]
    server._client = SimpleNamespace(list_tools=AsyncMock(return_value=raw_tools))

    advertised = await server.list_tools()

    assert tuple(tool.name for tool in advertised) == surface.ordered_mcp_names
    with pytest.raises(ValueError, match="not available"):
        await server._call_tool("validate_block", {})
    with pytest.raises(ValueError, match="not available"):
        await server._call_tool("raw_validate", {})
    with pytest.raises(ValueError, match="not available"):
        await server._call_tool(selected_aliases["click"], {})


@pytest.mark.asyncio
async def test_selected_mcp_catalog_fails_when_transport_is_missing_or_duplicate() -> None:
    native, aliases, overlays = _catalogs()
    surface = _browser_surface(native, aliases, overlays)
    selected_aliases = surface.alias_map
    server = SkyvernOverlayMCPServer(
        transport=object(),
        overlays=surface.overlays,
        alias_map=selected_aliases,
        allowlist=frozenset(selected_aliases.values()),
        context_provider=lambda: SimpleNamespace(),
        ordered_allowlist=tuple(selected_aliases.values()),
        enforce_dispatch_allowlist=True,
    )
    raw_tools = [
        MCPTool(name=raw_name, description=name, inputSchema={"type": "object"})
        for name, raw_name in selected_aliases.items()
    ]
    server._client = SimpleNamespace(list_tools=AsyncMock(return_value=raw_tools[:-1]))
    with pytest.raises(RuntimeError, match="omitted allowed tools"):
        await server.list_tools()

    server._client.list_tools.return_value = [*raw_tools, raw_tools[0]]
    server._cached_raw_tools = None
    with pytest.raises(RuntimeError, match="duplicate tool names"):
        await server.list_tools()


def _repair_probe_tools(*names: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(name=name) for name in names]


@pytest.mark.parametrize("mode", [CopilotEvalMode.REPAIR_PROBE_ON, CopilotEvalMode.REPAIR_PROBE_OFF])
def test_a_surface_without_the_probe_tool_refuses_both_arms(mode: CopilotEvalMode) -> None:
    """A length comparison can only move on the arm that removes the tool, so it would let the ON
    arm run against a surface that never carried it and report OFF against OFF as a contrast."""
    with pytest.raises(ValueError, match=REPAIR_PROBE_TOOL):
        resolve_copilot_tool_surface(
            mode=mode,
            native_tools=_repair_probe_tools("run_blocks", "update_workflow"),
            alias_map={},
            overlays={},
        )


def test_the_off_arm_drops_only_the_probe_tool() -> None:
    surface = resolve_copilot_tool_surface(
        mode=CopilotEvalMode.REPAIR_PROBE_OFF,
        native_tools=_repair_probe_tools("run_blocks", REPAIR_PROBE_TOOL, "update_workflow"),
        alias_map={},
        overlays={},
    )

    assert surface.ordered_native_names == ("run_blocks", "update_workflow")


def test_the_on_arm_keeps_the_probe_tool() -> None:
    surface = resolve_copilot_tool_surface(
        mode=CopilotEvalMode.REPAIR_PROBE_ON,
        native_tools=_repair_probe_tools("run_blocks", REPAIR_PROBE_TOOL, "update_workflow"),
        alias_map={},
        overlays={},
    )

    assert surface.ordered_native_names == ("run_blocks", REPAIR_PROBE_TOOL, "update_workflow")
