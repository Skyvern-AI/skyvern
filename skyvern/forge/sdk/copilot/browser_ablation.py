"""Typed capability projection for the internal Copilot browser ablation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Sequence
from copy import copy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, TypedDict, TypeVar

from skyvern.forge.sdk.copilot.config import CopilotConfig


class CopilotEvalMode(StrEnum):
    BROWSER_ABLATION = "browser_ablation"
    # The two arms of the locator-inspection contrast. They keep the production prompt template,
    # the production MCP surface and ordinary dispatch; only the one native tool differs.
    REPAIR_PROBE_OFF = "repair_probe_off"
    REPAIR_PROBE_ON = "repair_probe_on"


class CopilotBrowserCodeMode(StrEnum):
    OFF = "off"
    # run_browser_code is advertised beside the browser tools.
    ADD = "add"
    # run_browser_code is the only way to act on the browser; the direct and mixed tools are withdrawn.
    REPLACE = "replace"


class CopilotToolSurfaceIdentity(StrEnum):
    REQUIRED_CODE = "required_code"
    OPTIONAL = "optional"
    BROWSER_ABLATION = "browser_ablation"


def dispatch_allowlist_enforced(identity: CopilotToolSurfaceIdentity | None) -> bool:
    """Only a narrowed surface refuses names at dispatch; the optional surface keeps the parent's
    open dispatch, so a deployment without the code tool behaves exactly as before."""
    return identity in (CopilotToolSurfaceIdentity.REQUIRED_CODE, CopilotToolSurfaceIdentity.BROWSER_ABLATION)


REPAIR_PROBE_TOOL = "inspect_locator_matches"
REPAIR_PROBE_MODES = (CopilotEvalMode.REPAIR_PROBE_OFF, CopilotEvalMode.REPAIR_PROBE_ON)


class BrowserAblationMetadata(TypedDict):
    eval_mode: str
    browser_session_id: str | None
    prompt_sha256: str | None
    tool_surface_sha256: str | None
    input_tokens: int | None
    output_tokens: int | None
    tool_activity: list[dict[str, Any]]
    screenshot_frames: list[dict[str, Any]]


_CopilotConfigT = TypeVar("_CopilotConfigT", bound=CopilotConfig)


class _RegisteredMCPTool(Protocol):
    """The registry fields browser-ablation projects into Copilot's tool surface."""

    name: str
    tags: Collection[str] | None
    description: str | None


BROWSER_ABLATION_PROMPT_TEMPLATE = "workflow-copilot-browser-ablation.j2"
BROWSER_ABLATION_NATIVE_TOOLS = (
    "list_credentials",
    "list_integrations",
    "discover_workflow_entrypoint",
    "inspect_page_for_composition",
    "inspect_locator_matches",
    "fill_credential_field",
    "request_credential",
)
# Browser ablation keeps the production Copilot browser aliases, then projects the missing
# multi-page capabilities from the app registry. This avoids both a second handwritten browser
# list and the overlapping 43-tool surface produced by advertising every raw browser endpoint.
BROWSER_ABLATION_COPILOT_TOOL_TAGS = frozenset({"browser_primitive", "inspection"})
BROWSER_ABLATION_REQUIRED_EXTENSION_TAGS = frozenset({"tab_management", "page_read"})
# Opening several URLs at once overlaps skyvern_tab_new while hiding the intermediate evidence
# these comparison tasks need. Keep the single-tab action and let the model decide each next page.
BROWSER_ABLATION_MCP_TOOL_EXCLUSIONS = frozenset({"skyvern_open_tabs"})


# The browser-bound aliases that survive when mutations must go through code: page and console reads, observe-only
# waits, frame and tab enumeration, and tab close (no page fact; the multi-tab hold's route out). Every alias that
# needs no browser survives too, so a later tool is withdrawn only if it acts on the page.
REQUIRED_CODE_BROWSER_ALIASES = frozenset(
    {
        "get_browser_screenshot",
        "console_messages",
        "wait_for_either_state",
        "skyvern_frame_list",
        "skyvern_tab_list",
        "skyvern_tab_close",
    }
)
REQUIRED_CODE_REMOVED_NATIVE_TOOLS = frozenset({"discover_workflow_entrypoint"})


@dataclass(frozen=True, slots=True)
class CopilotToolSurface:
    native_tools: tuple[Any, ...]
    alias_map: dict[str, str]
    overlays: dict[str, Any]
    ordered_native_names: tuple[str, ...]
    ordered_mcp_names: tuple[str, ...]
    # Kept out of the hash payload below: the identity names which projection ran, while the hash
    # must stay comparable across surfaces that advertise the same tools. OPTIONAL never narrows
    # dispatch, so a surface that does not name a projection keeps the parent's behavior.
    identity: CopilotToolSurfaceIdentity = CopilotToolSurfaceIdentity.OPTIONAL

    @property
    def sha256(self) -> str:
        return self._sha256(advertised_mcp_tools=None)

    def advertised_sha256(self, advertised_mcp_tools: list[Any]) -> str:
        return self._sha256(advertised_mcp_tools=advertised_mcp_tools)

    def _sha256(self, *, advertised_mcp_tools: list[Any] | None) -> str:
        advertised_by_name = (
            {tool.name: tool for tool in advertised_mcp_tools} if advertised_mcp_tools is not None else {}
        )
        if (
            advertised_mcp_tools is not None
            and tuple(tool.name for tool in advertised_mcp_tools) != self.ordered_mcp_names
        ):
            raise ValueError("advertised MCP tool order does not match the configured Copilot surface")
        payload = {
            "version": "copilot-tool-surface-v3" if advertised_mcp_tools is not None else "copilot-tool-surface-v2",
            "native": [
                {
                    "name": tool.name,
                    "description": getattr(tool, "description", None),
                    "input_schema": getattr(tool, "params_json_schema", None),
                }
                for tool in self.native_tools
            ],
            "mcp": [
                {
                    "name": name,
                    "transport_name": self.alias_map.get(name),
                    "overlay": _overlay_fingerprint(self.overlays.get(name)),
                    "description": getattr(advertised_by_name.get(name), "description", None),
                    "input_schema": getattr(advertised_by_name.get(name), "inputSchema", None),
                }
                for name in self.ordered_mcp_names
            ],
        }
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()


def _overlay_fingerprint(overlay: Any) -> dict[str, Any]:
    """Return the stable configured overlay contract advertised by this surface."""

    return {
        "description": getattr(overlay, "description", None),
        "description_suffix": getattr(overlay, "description_suffix", None),
        "hide_params": sorted(getattr(overlay, "hide_params", ())),
        "required_overrides": getattr(overlay, "required_overrides", None),
        "arg_transforms": getattr(overlay, "arg_transforms", {}),
        "forced_args": getattr(overlay, "forced_args", {}),
        "copilot_params": getattr(overlay, "copilot_params", {}),
        "requires_browser": getattr(overlay, "requires_browser", False),
        "timeout": getattr(overlay, "timeout", None),
        "pre_hook": _callable_identity(getattr(overlay, "pre_hook", None)),
        "post_hook": _callable_identity(getattr(overlay, "post_hook", None)),
    }


def _callable_identity(value: Any) -> str | None:
    if value is None:
        return None
    module = getattr(value, "__module__", None)
    qualname = getattr(value, "__qualname__", None)
    return f"{module}.{qualname}" if isinstance(module, str) and isinstance(qualname, str) else type(value).__name__


def prompt_sha256(rendered_instructions: str) -> str:
    return hashlib.sha256(rendered_instructions.encode()).hexdigest()


def prompt_template_for_mode(mode: CopilotEvalMode | None, default_template: str) -> str:
    if mode == CopilotEvalMode.BROWSER_ABLATION:
        return BROWSER_ABLATION_PROMPT_TEMPLATE
    return default_template


def config_for_eval_mode(config: _CopilotConfigT, mode: CopilotEvalMode | None) -> _CopilotConfigT:
    selected_template = prompt_template_for_mode(mode, config.prompt_template)
    if selected_template == config.prompt_template:
        return config
    selected_config = copy(config)
    selected_config.prompt_template = selected_template
    return selected_config


def _resolve_exact_ordered_names(
    requested: tuple[str, ...], available: dict[str, Any], *, channel: str
) -> tuple[Any, ...]:
    duplicate_names = sorted(name for name in set(requested) if requested.count(name) > 1)
    if duplicate_names:
        raise ValueError(f"duplicate {channel} tool names: {', '.join(duplicate_names)}")
    missing_names = [name for name in requested if name not in available]
    if missing_names:
        raise ValueError(f"missing {channel} tool names: {', '.join(missing_names)}")
    return tuple(available[name] for name in requested)


def _registered_browser_mcp_surface(
    *,
    registered_mcp_tools: Sequence[_RegisteredMCPTool],
    alias_map: dict[str, str],
    overlays: dict[str, Any],
) -> tuple[dict[str, str], dict[str, Any], tuple[str, ...]]:
    # Local import keeps this leaf usable by CopilotContext while mcp_adapter's runtime graph is
    # still importing. Surface resolution runs only after application initialization completes.
    from skyvern.forge.sdk.copilot.browser_target import BROWSER_TARGET_PARAM, BROWSER_TARGET_PARAM_NAME
    from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay

    registered_by_name = {tool.name: tool for tool in registered_mcp_tools}
    if len(registered_by_name) != len(registered_mcp_tools):
        raise ValueError("duplicate MCP tool names in registered catalog")

    reverse_alias = {transport_name: copilot_name for copilot_name, transport_name in alias_map.items()}
    if len(reverse_alias) != len(alias_map):
        raise ValueError("duplicate MCP tool names in source catalog")

    selected_aliases: dict[str, str] = {}
    selected_overlays: dict[str, Any] = {}

    # Preserve the production Copilot ACI names, descriptions, hooks, and order. Registry tags
    # distinguish its browser tools from the workflow-authoring aliases in the same canonical map.
    for copilot_name, transport_name in alias_map.items():
        tool = registered_by_name.get(transport_name)
        if tool is None:
            continue
        tags = frozenset(tool.tags or ())
        if (
            not tags.intersection(BROWSER_ABLATION_COPILOT_TOOL_TAGS)
            or transport_name in BROWSER_ABLATION_MCP_TOOL_EXCLUSIONS
        ):
            continue
        selected_aliases[copilot_name] = transport_name
        selected_overlays[copilot_name] = overlays[copilot_name]

    # Tab controls and bounded page reading are capabilities the benchmark needs but normal
    # workflow authoring does not. Their registry tags are the single source of membership.
    for tool in registered_mcp_tools:
        tags = frozenset(tool.tags or ())
        if (
            not tags.intersection(BROWSER_ABLATION_REQUIRED_EXTENSION_TAGS)
            or tool.name in BROWSER_ABLATION_MCP_TOOL_EXCLUSIONS
            or tool.name in selected_aliases.values()
        ):
            continue
        copilot_name = reverse_alias.get(tool.name, tool.name)
        if copilot_name in selected_aliases:
            raise ValueError(f"duplicate projected MCP tool name: {copilot_name}")
        selected_aliases[copilot_name] = tool.name
        selected_overlays[copilot_name] = overlays.get(
            copilot_name,
            SchemaOverlay(
                description=tool.description,
                hide_params=frozenset({"session_id", "cdp_url"}),
                copilot_params={BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM},
                requires_browser=True,
            ),
        )

    return selected_aliases, selected_overlays, tuple(selected_aliases)


def _optional_surface(
    selected_native: list[Any],
    alias_map: dict[str, str],
    overlays: dict[str, Any],
) -> CopilotToolSurface:
    return CopilotToolSurface(
        native_tools=tuple(selected_native),
        alias_map=alias_map,
        overlays=overlays,
        ordered_native_names=tuple(tool.name for tool in selected_native),
        ordered_mcp_names=tuple(alias_map),
        identity=CopilotToolSurfaceIdentity.OPTIONAL,
    )


def _required_code_surface(
    selected_native: list[Any],
    alias_map: dict[str, str],
    overlays: dict[str, Any],
) -> CopilotToolSurface:
    from skyvern.forge.sdk.copilot.tools.composition_capture import (
        COMPOSITION_INSPECTION_TOOL_NAME,
        current_page_inspection_tool,
    )

    missing_aliases = sorted(REQUIRED_CODE_BROWSER_ALIASES.difference(alias_map.keys() & overlays.keys()))
    if missing_aliases:
        raise ValueError(f"missing MCP tool names: {', '.join(missing_aliases)}")
    projected_native = [
        current_page_inspection_tool(tool) if tool.name == COMPOSITION_INSPECTION_TOOL_NAME else tool
        for tool in selected_native
        if tool.name not in REQUIRED_CODE_REMOVED_NATIVE_TOOLS
    ]
    selected_aliases = {
        name: transport
        for name, transport in alias_map.items()
        if name in REQUIRED_CODE_BROWSER_ALIASES or not overlays[name].requires_browser
    }
    return CopilotToolSurface(
        native_tools=tuple(projected_native),
        alias_map=selected_aliases,
        overlays={name: overlays[name] for name in selected_aliases},
        ordered_native_names=tuple(tool.name for tool in projected_native),
        ordered_mcp_names=tuple(selected_aliases),
        identity=CopilotToolSurfaceIdentity.REQUIRED_CODE,
    )


def resolve_copilot_tool_surface(
    *,
    mode: CopilotEvalMode | None,
    native_tools: list[Any],
    alias_map: dict[str, str],
    overlays: dict[str, Any],
    registered_mcp_tools: Sequence[Any] | None = None,
    browser_tools_available: bool = True,
    browser_code_mode: CopilotBrowserCodeMode = CopilotBrowserCodeMode.ADD,
) -> CopilotToolSurface:
    if not browser_tools_available:
        # Local import: tools/__init__ reaches this leaf back through CopilotContext.
        from skyvern.forge.sdk.copilot.tools import BROWSER_BOUND_TOOL_NAMES

        # A turn without browser authority advertises no tool that would need one, rather than
        # advertising them and refusing at dispatch.
        selected = [tool for tool in native_tools if tool.name not in BROWSER_BOUND_TOOL_NAMES]
        selected_aliases = {
            name: transport_name
            for name, transport_name in alias_map.items()
            if not getattr(overlays[name], "requires_browser", False)
            and not getattr(overlays[name], "requires_run_authority", False)
        }
        selected_overlays = {name: overlays[name] for name in selected_aliases}
        return CopilotToolSurface(
            native_tools=tuple(selected),
            alias_map=selected_aliases,
            overlays=selected_overlays,
            ordered_native_names=tuple(tool.name for tool in selected),
            ordered_mcp_names=tuple(selected_aliases),
        )

    if mode is None or mode in REPAIR_PROBE_MODES:
        from skyvern.forge.sdk.copilot.tools.browser_code import TOOL_NAME as BROWSER_CODE_TOOL_NAME

        selected = [
            tool
            for tool in native_tools
            if not (mode == CopilotEvalMode.REPAIR_PROBE_OFF and tool.name == REPAIR_PROBE_TOOL)
        ]
        # Ask whether the surface carries the tool, not whether a count moved: on the ON arm the
        # comprehension drops nothing, so a length comparison can only ever fire on OFF and an ON
        # run against a surface without the probe would report an OFF-vs-OFF contrast as real.
        if mode in REPAIR_PROBE_MODES and REPAIR_PROBE_TOOL not in {tool.name for tool in native_tools}:
            raise ValueError(f"{REPAIR_PROBE_TOOL} is not on the production native surface")
        if browser_code_mode == CopilotBrowserCodeMode.REPLACE and BROWSER_CODE_TOOL_NAME in {
            tool.name for tool in selected
        }:
            return _required_code_surface(selected, alias_map, overlays)
        return _optional_surface(selected, alias_map, overlays)

    native_by_name = {tool.name: tool for tool in native_tools}
    if len(native_by_name) != len(native_tools):
        raise ValueError("duplicate native tool names in source catalog")
    selected_native = _resolve_exact_ordered_names(BROWSER_ABLATION_NATIVE_TOOLS, native_by_name, channel="native")
    if registered_mcp_tools is None:
        raise ValueError("browser ablation requires the registered MCP tool catalog")
    selected_aliases, selected_overlays, selected_names = _registered_browser_mcp_surface(
        registered_mcp_tools=registered_mcp_tools,
        alias_map=alias_map,
        overlays=overlays,
    )
    return CopilotToolSurface(
        native_tools=selected_native,
        alias_map=selected_aliases,
        overlays=selected_overlays,
        ordered_native_names=BROWSER_ABLATION_NATIVE_TOOLS,
        ordered_mcp_names=selected_names,
        identity=CopilotToolSurfaceIdentity.BROWSER_ABLATION,
    )
