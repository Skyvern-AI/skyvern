from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import Tool as MCPTool

from skyvern.forge.sdk.copilot import screenshot_utils
from skyvern.forge.sdk.copilot.browser_ablation import (
    BROWSER_ABLATION_MCP_TOOLS,
    BROWSER_ABLATION_NATIVE_TOOLS,
    BROWSER_ABLATION_PROMPT_TEMPLATE,
    CopilotEvalMode,
    config_for_eval_mode,
    prompt_sha256,
    prompt_template_for_mode,
    resolve_copilot_tool_surface,
)
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay, SkyvernOverlayMCPServer
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotBrowserAblationResponseUpdate,
    WorkflowCopilotStreamResponseUpdate,
)


def _catalogs() -> tuple[list[SimpleNamespace], dict[str, str], dict[str, SchemaOverlay]]:
    native_names = ("update_workflow", *BROWSER_ABLATION_NATIVE_TOOLS, "run_blocks_and_collect_debug")
    native = [SimpleNamespace(name=name) for name in native_names]
    aliases = {
        "get_workflow_knowledge": "raw_knowledge",
        **{name: f"raw_{name}" for name in BROWSER_ABLATION_MCP_TOOLS},
        "validate_block": "raw_validate",
    }
    overlays = {name: SchemaOverlay(description=name) for name in aliases}
    return native, aliases, overlays


def test_browser_ablation_resolves_the_exact_ordered_surface() -> None:
    native, aliases, overlays = _catalogs()

    surface = resolve_copilot_tool_surface(
        mode=CopilotEvalMode.BROWSER_ABLATION,
        native_tools=native,
        alias_map=aliases,
        overlays=overlays,
    )

    assert tuple(tool.name for tool in surface.native_tools) == BROWSER_ABLATION_NATIVE_TOOLS
    assert tuple(surface.alias_map) == BROWSER_ABLATION_MCP_TOOLS
    assert tuple(surface.overlays) == BROWSER_ABLATION_MCP_TOOLS
    assert "update_workflow" not in surface.ordered_native_names
    assert "validate_block" not in surface.ordered_mcp_names


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
        )
    with pytest.raises(ValueError, match="duplicate native tool names"):
        resolve_copilot_tool_surface(
            mode=CopilotEvalMode.BROWSER_ABLATION,
            native_tools=[*native, native[0]],
            alias_map=aliases,
            overlays=overlays,
        )
    missing_aliases = dict(aliases)
    missing_aliases.pop("evaluate")
    with pytest.raises(ValueError, match="missing MCP tool names"):
        resolve_copilot_tool_surface(
            mode=CopilotEvalMode.BROWSER_ABLATION,
            native_tools=native,
            alias_map=missing_aliases,
            overlays=overlays,
        )
    duplicate_aliases = dict(aliases)
    duplicate_aliases["evaluate"] = duplicate_aliases["click"]
    with pytest.raises(ValueError, match="duplicate MCP tool names"):
        resolve_copilot_tool_surface(
            mode=CopilotEvalMode.BROWSER_ABLATION,
            native_tools=native,
            alias_map=duplicate_aliases,
            overlays=overlays,
        )


def test_hashes_are_stable_and_order_sensitive() -> None:
    native, aliases, overlays = _catalogs()
    surface = resolve_copilot_tool_surface(
        mode=CopilotEvalMode.BROWSER_ABLATION,
        native_tools=native,
        alias_map=aliases,
        overlays=overlays,
    )
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
    surface = resolve_copilot_tool_surface(
        mode=CopilotEvalMode.BROWSER_ABLATION,
        native_tools=native,
        alias_map=aliases,
        overlays=overlays,
    )
    changed_overlays = dict(overlays)
    changed_overlays["click"] = SchemaOverlay(description="changed click contract")
    changed = resolve_copilot_tool_surface(
        mode=CopilotEvalMode.BROWSER_ABLATION,
        native_tools=native,
        alias_map=aliases,
        overlays=changed_overlays,
    )

    assert surface.sha256 != changed.sha256


def test_advertised_surface_hash_changes_with_mcp_schema() -> None:
    native, aliases, overlays = _catalogs()
    surface = resolve_copilot_tool_surface(
        mode=CopilotEvalMode.BROWSER_ABLATION,
        native_tools=native,
        alias_map=aliases,
        overlays=overlays,
    )
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
    _, aliases, overlays = _catalogs()
    selected_aliases = {name: aliases[name] for name in BROWSER_ABLATION_MCP_TOOLS}
    server = SkyvernOverlayMCPServer(
        transport=object(),
        overlays={name: overlays[name] for name in BROWSER_ABLATION_MCP_TOOLS},
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

    assert tuple(tool.name for tool in advertised) == BROWSER_ABLATION_MCP_TOOLS
    with pytest.raises(ValueError, match="not available"):
        await server._call_tool("validate_block", {})
    with pytest.raises(ValueError, match="not available"):
        await server._call_tool("raw_validate", {})
    with pytest.raises(ValueError, match="not available"):
        await server._call_tool(selected_aliases["click"], {})


@pytest.mark.asyncio
async def test_selected_mcp_catalog_fails_when_transport_is_missing_or_duplicate() -> None:
    _, aliases, overlays = _catalogs()
    selected_aliases = {name: aliases[name] for name in BROWSER_ABLATION_MCP_TOOLS}
    server = SkyvernOverlayMCPServer(
        transport=object(),
        overlays={name: overlays[name] for name in BROWSER_ABLATION_MCP_TOOLS},
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
