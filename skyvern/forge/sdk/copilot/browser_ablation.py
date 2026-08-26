"""Typed capability projection for the internal Copilot browser ablation."""

from __future__ import annotations

import hashlib
import json
from copy import copy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypedDict, TypeVar

from skyvern.forge.sdk.copilot.config import CopilotConfig


class CopilotEvalMode(StrEnum):
    BROWSER_ABLATION = "browser_ablation"


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


BROWSER_ABLATION_PROMPT_TEMPLATE = "workflow-copilot-browser-ablation.j2"
BROWSER_ABLATION_NATIVE_TOOLS = (
    "list_credentials",
    "list_integrations",
    "discover_workflow_entrypoint",
    "inspect_page_for_composition",
    "fill_credential_field",
)
BROWSER_ABLATION_MCP_TOOLS = (
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
)


@dataclass(frozen=True, slots=True)
class CopilotToolSurface:
    native_tools: tuple[Any, ...]
    alias_map: dict[str, str]
    overlays: dict[str, Any]
    ordered_native_names: tuple[str, ...]
    ordered_mcp_names: tuple[str, ...]

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
                    "transport_name": self.alias_map[name],
                    "overlay": _overlay_fingerprint(self.overlays[name]),
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


def resolve_copilot_tool_surface(
    *,
    mode: CopilotEvalMode | None,
    native_tools: list[Any],
    alias_map: dict[str, str],
    overlays: dict[str, Any],
) -> CopilotToolSurface:
    if mode is None:
        return CopilotToolSurface(
            native_tools=tuple(native_tools),
            alias_map=alias_map,
            overlays=overlays,
            ordered_native_names=tuple(tool.name for tool in native_tools),
            ordered_mcp_names=tuple(alias_map),
        )

    native_by_name = {tool.name: tool for tool in native_tools}
    if len(native_by_name) != len(native_tools):
        raise ValueError("duplicate native tool names in source catalog")
    selected_native = _resolve_exact_ordered_names(BROWSER_ABLATION_NATIVE_TOOLS, native_by_name, channel="native")
    selected_alias_values = _resolve_exact_ordered_names(BROWSER_ABLATION_MCP_TOOLS, alias_map, channel="MCP")
    if len(set(selected_alias_values)) != len(selected_alias_values):
        raise ValueError("duplicate MCP tool names in source catalog")
    _resolve_exact_ordered_names(BROWSER_ABLATION_MCP_TOOLS, overlays, channel="MCP overlay")
    return CopilotToolSurface(
        native_tools=selected_native,
        alias_map=dict(zip(BROWSER_ABLATION_MCP_TOOLS, selected_alias_values, strict=True)),
        overlays={name: overlays[name] for name in BROWSER_ABLATION_MCP_TOOLS},
        ordered_native_names=BROWSER_ABLATION_NATIVE_TOOLS,
        ordered_mcp_names=BROWSER_ABLATION_MCP_TOOLS,
    )
