"""SDK-native MCP server with schema overlays for the Skyvern copilot."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import structlog
from agents.agent import AgentBase
from agents.mcp.server import MCPServer
from agents.run_context import RunContextWrapper
from fastmcp import Client
from mcp import Tool as MCPTool
from mcp.types import (
    CallToolResult,
    GetPromptResult,
    ListPromptsResult,
    TextContent,
)

from skyvern.forge.sdk.copilot.loop_detection import detect_tool_loop
from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    ensure_browser_session,
    mcp_browser_context,
    mcp_to_copilot,
)
from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result

PreHook = Callable[[dict[str, Any], AgentContext], Awaitable[dict[str, Any] | None]]
PostHook = Callable[[dict[str, Any], dict[str, Any], AgentContext], Awaitable[dict[str, Any]]]


@dataclass
class SchemaOverlay:
    """Schema overlay for MCP tools — hides params, renames args, injects forced values."""

    description: str | None = None
    hide_params: frozenset[str] = frozenset()
    required_overrides: list[str] | None = None
    arg_transforms: dict[str, str] = field(default_factory=dict)
    forced_args: dict[str, Any] = field(default_factory=dict)
    requires_browser: bool = False
    timeout: int | None = None
    pre_hook: PreHook | None = None
    post_hook: PostHook | None = None


LOG = structlog.get_logger()


def _apply_schema_overlay(
    input_schema: dict[str, Any],
    overlay: SchemaOverlay,
) -> dict[str, Any]:
    props = dict(input_schema.get("properties", {}))
    required = list(input_schema.get("required", []))

    for p in overlay.hide_params | frozenset(overlay.forced_args):
        props.pop(p, None)
        if p in required:
            required.remove(p)

    for copilot_param, mcp_param in overlay.arg_transforms.items():
        if mcp_param in props:
            props[copilot_param] = props.pop(mcp_param)
        if mcp_param in required:
            required.remove(mcp_param)
            required.append(copilot_param)

    if overlay.required_overrides is not None:
        required = overlay.required_overrides

    return {
        "type": input_schema.get("type", "object"),
        "properties": props,
        "required": required,
    }


def _transform_args(
    arguments: dict[str, Any],
    overlay: SchemaOverlay,
) -> dict[str, Any]:
    mcp_args = {k: v for k, v in arguments.items() if k not in overlay.hide_params}

    for copilot_param, mcp_param in overlay.arg_transforms.items():
        if copilot_param in mcp_args:
            mcp_args[mcp_param] = mcp_args.pop(copilot_param)

    mcp_args.update(overlay.forced_args)
    return mcp_args


def _copilot_to_call_tool_result(
    copilot_result: dict[str, Any],
) -> CallToolResult:
    sanitized = sanitize_tool_result_for_llm("", copilot_result)
    content: list[TextContent] = [TextContent(type="text", text=json.dumps(sanitized))]
    is_error = not copilot_result.get("ok", True)
    return CallToolResult(content=content, isError=is_error)


class SkyvernOverlayMCPServer(MCPServer):
    """MCP server that wraps a FastMCP transport with schema overlays and
    copilot-specific dispatch logic (loop detection, browser injection, hooks).
    """

    def __init__(
        self,
        transport: Any,
        overlays: dict[str, SchemaOverlay],
        alias_map: dict[str, str],
        allowlist: frozenset[str],
        context_provider: Callable[[], Any],
    ) -> None:
        super().__init__(use_structured_content=False)
        self._transport = transport
        self._overlays = overlays
        self._alias_map = alias_map  # copilot_name -> mcp_name
        self._reverse_alias: dict[str, str] = {v: k for k, v in alias_map.items()}
        self._allowlist = allowlist
        self._context_provider = context_provider
        self._client: Client | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._cached_tools: list[MCPTool] | None = None

    @property
    def name(self) -> str:
        return "skyvern"

    async def connect(self) -> None:
        stack = AsyncExitStack()
        await stack.__aenter__()
        client = Client(self._transport)
        await stack.enter_async_context(client)
        self._client = client
        self._exit_stack = stack

    async def cleanup(self) -> None:
        if self._exit_stack:
            await self._exit_stack.__aexit__(None, None, None)
        self._client = None
        self._exit_stack = None
        self._cached_tools = None

    async def list_tools(
        self,
        run_context: RunContextWrapper[Any] | None = None,
        agent: AgentBase | None = None,
    ) -> list[MCPTool]:
        if self._cached_tools is not None:
            return self._cached_tools

        if not self._client:
            raise RuntimeError("Not connected — call connect() first")

        raw_tools = await self._client.list_tools()
        result: list[MCPTool] = []

        for tool in raw_tools:
            if tool.name not in self._allowlist:
                continue

            copilot_name = self._reverse_alias.get(tool.name, tool.name)
            overlay = self._overlays.get(copilot_name, SchemaOverlay())

            schema = _apply_schema_overlay(tool.inputSchema, overlay)
            description = overlay.description or tool.description or ""

            result.append(
                MCPTool(
                    name=copilot_name,
                    description=description,
                    inputSchema=schema,
                )
            )
        self._cached_tools = result
        return result

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        if not self._client:
            raise RuntimeError("Not connected — call connect() first")

        arguments = arguments or {}
        copilot_ctx = self._context_provider()
        overlay = self._overlays.get(tool_name, SchemaOverlay())

        tracker = getattr(copilot_ctx, "consecutive_tool_tracker", None)
        loop_error = detect_tool_loop(tracker, tool_name) if isinstance(tracker, list) else None
        if loop_error:
            LOG.warning(
                "Tool loop detected, skipping execution",
                tool_name=tool_name,
            )
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "ok": False,
                                "error": loop_error,
                            }
                        ),
                    )
                ],
                isError=True,
            )

        if overlay.pre_hook:
            hook_result = await overlay.pre_hook(arguments, copilot_ctx)
            if hook_result is not None:
                return _copilot_to_call_tool_result(hook_result)

        mcp_name = self._alias_map.get(tool_name, tool_name)
        mcp_args = _transform_args(arguments, overlay)

        if overlay.requires_browser:
            err = await ensure_browser_session(copilot_ctx)
            if err:
                return _copilot_to_call_tool_result(err)
            mcp_args["session_id"] = copilot_ctx.browser_session_id

        try:
            call = self._client.call_tool(mcp_name, mcp_args, raise_on_error=False)
            if overlay.requires_browser:
                async with mcp_browser_context(copilot_ctx):
                    raw_result = await call
            else:
                raw_result = await call
        except Exception as e:
            LOG.warning(
                "MCP tool call failed",
                tool=tool_name,
                error=str(e),
                exc_info=True,
            )
            return _copilot_to_call_tool_result({"ok": False, "error": f"{tool_name} failed: {e}"})

        # Copy fastmcp's structured_content so mutations below stay local to
        # this call — the client may reuse or cache the response object.
        raw_mcp = dict(raw_result.structured_content or {})
        if raw_result.is_error:
            raw_mcp["ok"] = False
            if not raw_result.structured_content and raw_result.content:
                text_parts = [c.text for c in raw_result.content if hasattr(c, "text")]
                raw_mcp["error"] = " ".join(text_parts) if text_parts else "Unknown MCP error"
            else:
                raw_mcp["error"] = raw_mcp.get("error") or "Unknown MCP error"
        copilot_result = mcp_to_copilot(raw_mcp)

        if overlay.post_hook:
            copilot_result = await overlay.post_hook(copilot_result, raw_mcp, copilot_ctx)

        enqueue_screenshot_from_result(copilot_ctx, copilot_result)
        return _copilot_to_call_tool_result(copilot_result)

    async def list_prompts(self) -> ListPromptsResult:
        return ListPromptsResult(prompts=[])

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> GetPromptResult:
        raise ValueError(f"Prompts not supported: {name}")
