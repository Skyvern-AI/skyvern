"""SDK-native MCP server with schema overlays for the Skyvern copilot."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from copy import deepcopy
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

from skyvern.forge.sdk.copilot.blocker_signal import (
    build_loop_blocker_signal,
    loop_blocker_evidence_from_ctx,
    refresh_held_loop_blocker_evidence,
    stash_blocker_signal,
)
from skyvern.forge.sdk.copilot.build_phase import _phase_blocker_signal
from skyvern.forge.sdk.copilot.enforcement import terminal_challenge_blocker_signal_from_current_page_evidence
from skyvern.forge.sdk.copilot.loop_detection import (
    detect_failed_tool_step_loop_for_ctx,
    detect_tool_loop,
    record_tool_step_result_for_ctx,
)
from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    ensure_browser_session,
    mcp_browser_context,
    mcp_to_copilot,
)
from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure
from skyvern.forge.sdk.copilot.turn_halt import stash_turn_halt_from_blocker_signal

PreHook = Callable[[dict[str, Any], AgentContext], Awaitable[dict[str, Any] | None]]
PostHook = Callable[[dict[str, Any], dict[str, Any], AgentContext], Awaitable[dict[str, Any]]]

_POST_HOOK_CONTEXT_ROLLBACK_FIELDS = (
    "flow_evidence",
    "composition_page_evidence",
    "workflow_verification_evidence",
    "pending_browser_interaction_observation",
    "scouted_interactions",
    "scout_trajectory",
    "pending_scout_source_url",
    "pending_scout_typed_value",
    "pending_scout_role_name",
    "post_budget_page_inspection_required",
    "post_budget_page_inspection_url",
    "post_budget_page_inspection_run_id",
    "post_run_page_observation_tool",
    "post_run_page_observation_url",
    "post_run_page_observation_workflow_run_id",
    "code_only_target_page_evidence_seen",
    "last_evaluate_actionable_signature",
    "last_evaluate_actionable_url",
    "latest_evaluate_result_composition_steer",
    "last_auto_acted_signature",
    "reached_download_target",
    "synthesized_block_offered",
    "synthesized_block_offered_trajectory_len",
)


@dataclass(frozen=True)
class _PostHookContextSnapshot:
    values: dict[str, Any]


def _snapshot_post_hook_context(ctx: AgentContext) -> _PostHookContextSnapshot:
    ctx_vars = vars(ctx)
    return _PostHookContextSnapshot(
        {field: deepcopy(ctx_vars[field]) for field in _POST_HOOK_CONTEXT_ROLLBACK_FIELDS if field in ctx_vars}
    )


def _restore_post_hook_context(ctx: AgentContext, snapshot: _PostHookContextSnapshot) -> None:
    for field_name, value in snapshot.values.items():
        setattr(ctx, field_name, deepcopy(value))


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
_INTERNAL_TOOL_ARG_KEYS = frozenset({"_summarized"})
_CURRENT_PAGE_TERMINAL_CHALLENGE_MCP_TOOLS = frozenset(
    {
        "click",
        "evaluate",
        "get_browser_screenshot",
        "navigate_browser",
        "press_key",
        "scroll",
        "select_option",
        "type_text",
    }
)


def _stash_and_emit_loop_blocker(ctx: Any, loop_message: str, tool_name: str) -> str:
    signal = build_loop_blocker_signal(loop_message, tool_name=tool_name, evidence=loop_blocker_evidence_from_ctx(ctx))
    payload = stash_blocker_signal(ctx, signal)
    stash_turn_halt_from_blocker_signal(ctx, signal, source="mcp_loop_blocker")
    return payload


def _stash_and_emit_current_page_terminal_challenge_blocker(ctx: Any, tool_name: str) -> str | None:
    signal = terminal_challenge_blocker_signal_from_current_page_evidence(
        ctx,
        blocked_tool=tool_name,
        evidence_source="mcp_page_evidence",
    )
    if signal is None:
        return None
    payload = stash_blocker_signal(ctx, signal)
    stash_turn_halt_from_blocker_signal(ctx, signal, source="mcp_current_page_terminal_challenge")
    return payload


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
    mcp_args = {k: v for k, v in arguments.items() if k not in overlay.hide_params | _INTERNAL_TOOL_ARG_KEYS}

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
    is_error = copilot_result.get("ok", True) is not True
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
        self._cached_raw_tools: list[MCPTool] | None = None

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
        self._cached_raw_tools = None

    async def list_tools(
        self,
        run_context: RunContextWrapper[Any] | None = None,
        agent: AgentBase | None = None,
    ) -> list[MCPTool]:
        if not self._client:
            raise RuntimeError("Not connected — call connect() first")

        if self._cached_raw_tools is None:
            self._cached_raw_tools = await self._client.list_tools()
        raw_tools = self._cached_raw_tools
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
        arguments = {k: v for k, v in arguments.items() if k not in _INTERNAL_TOOL_ARG_KEYS}
        copilot_ctx = self._context_provider()
        overlay = self._overlays.get(tool_name, SchemaOverlay())

        # MCP-side phase gate; mirror of `_authority_tool_error` in tools.py for MCP-only tools.
        phase_signal = _phase_blocker_signal(copilot_ctx, tool_name)
        if phase_signal is not None:
            LOG.warning(
                "Phase-gated MCP tool call rejected",
                tool_name=tool_name,
                build_phase=getattr(getattr(copilot_ctx, "build_phase", None), "value", None),
            )
            payload = stash_blocker_signal(copilot_ctx, phase_signal)
            record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, {"ok": False, "error": payload})
            return _copilot_to_call_tool_result({"ok": False, "error": payload})

        refresh_held_loop_blocker_evidence(copilot_ctx)
        if tool_name in _CURRENT_PAGE_TERMINAL_CHALLENGE_MCP_TOOLS:
            terminal_challenge_payload = _stash_and_emit_current_page_terminal_challenge_blocker(
                copilot_ctx,
                tool_name,
            )
            if terminal_challenge_payload is not None:
                LOG.warning(
                    "Current page terminal challenge detected, skipping MCP browser tool",
                    tool_name=tool_name,
                )
                return _copilot_to_call_tool_result({"ok": False, "error": terminal_challenge_payload})

        loop_error = detect_failed_tool_step_loop_for_ctx(copilot_ctx, tool_name, arguments)
        if loop_error:
            LOG.warning(
                "Failed tool step loop detected, skipping execution",
                tool_name=tool_name,
            )
            payload = _stash_and_emit_loop_blocker(copilot_ctx, loop_error, tool_name)
            return _copilot_to_call_tool_result({"ok": False, "error": payload})

        tracker = getattr(copilot_ctx, "consecutive_tool_tracker", None)
        loop_error = detect_tool_loop(tracker, tool_name) if isinstance(tracker, list) else None
        if loop_error:
            LOG.warning(
                "Tool loop detected, skipping execution",
                tool_name=tool_name,
            )
            payload = _stash_and_emit_loop_blocker(copilot_ctx, loop_error, tool_name)
            return _copilot_to_call_tool_result({"ok": False, "error": payload})

        if overlay.pre_hook:
            hook_result = await overlay.pre_hook(arguments, copilot_ctx)
            if hook_result is not None:
                record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, hook_result)
                return _copilot_to_call_tool_result(hook_result)

        mcp_name = self._alias_map.get(tool_name, tool_name)
        mcp_args = _transform_args(arguments, overlay)

        if overlay.requires_browser:
            err = await ensure_browser_session(copilot_ctx)
            if err:
                record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, err)
                return _copilot_to_call_tool_result(err)
            mcp_args["session_id"] = copilot_ctx.browser_session_id

        try:
            if overlay.requires_browser:
                async with mcp_browser_context(copilot_ctx):
                    raw_result = await self._client.call_tool(mcp_name, mcp_args, raise_on_error=False)
            else:
                raw_result = await self._client.call_tool(mcp_name, mcp_args, raise_on_error=False)
        except Exception as e:
            LOG.warning(
                "MCP tool call failed",
                tool=tool_name,
                error=str(e),
                exc_info=True,
            )
            err = scrub_secrets_from_structure(copilot_ctx, {"ok": False, "error": f"{tool_name} failed: {e}"})
            record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, err)
            return _copilot_to_call_tool_result(err)

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
        # Scrub before the post hook so evidence the hooks record from raw_mcp
        # (flow evidence, scout observations) is scrubbed too.
        raw_mcp = scrub_secrets_from_structure(copilot_ctx, raw_mcp)
        copilot_result = mcp_to_copilot(raw_mcp)

        if overlay.post_hook:
            base_copilot_result = deepcopy(copilot_result)
            ctx_snapshot = _snapshot_post_hook_context(copilot_ctx)
            try:
                copilot_result = await overlay.post_hook(copilot_result, raw_mcp, copilot_ctx)
            except Exception as e:
                # A post-hook enriches evidence only; a crash must not fail the browser action or keep partial credit.
                _restore_post_hook_context(copilot_ctx, ctx_snapshot)
                LOG.warning(
                    "MCP post-hook failed; returning base tool result",
                    tool=tool_name,
                    error=str(e),
                    exc_info=True,
                )
                copilot_result = base_copilot_result

        record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, copilot_result)
        enqueue_screenshot_from_result(copilot_ctx, copilot_result)
        return _copilot_to_call_tool_result(copilot_result)

    async def call_internal_tool(
        self,
        mcp_tool_name: str,
        mcp_args: dict[str, Any],
    ) -> dict[str, Any]:
        """Raw FastMCP call for internal copilot subsystems (discovery walker).

        Bypasses overlay hooks, loop detection, and screenshot recording —
        those are model-facing concerns. Still routes through
        ``ensure_browser_session`` and ``mcp_browser_context`` for session/auth
        scoping. Mirrors the error-handling block from ``call_tool`` so MCP-
        side validation or tool errors surface as ``ok=False`` with an
        extracted error string rather than silently defaulting to
        ``ok=True``.
        """
        if not self._client:
            return {"ok": False, "error": "MCP client not connected"}
        ctx = self._context_provider()
        err = await ensure_browser_session(ctx)
        if err:
            return err
        merged_args = {**mcp_args, "session_id": ctx.browser_session_id}
        try:
            async with mcp_browser_context(ctx):
                raw = await self._client.call_tool(mcp_tool_name, merged_args, raise_on_error=False)
        except Exception as exc:
            LOG.warning(
                "Internal MCP tool call failed",
                tool=mcp_tool_name,
                error=str(exc),
                exc_info=True,
            )
            return {"ok": False, "error": f"{mcp_tool_name} failed: {exc}"}
        raw_mcp = dict(raw.structured_content or {})
        if raw.is_error:
            raw_mcp["ok"] = False
            if not raw.structured_content and raw.content:
                text_parts = [c.text for c in raw.content if hasattr(c, "text")]
                raw_mcp["error"] = " ".join(text_parts) if text_parts else "Unknown MCP error"
            else:
                raw_mcp["error"] = raw_mcp.get("error") or "Unknown MCP error"
        return mcp_to_copilot(raw_mcp)

    async def list_prompts(self) -> ListPromptsResult:
        return ListPromptsResult(prompts=[])

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> GetPromptResult:
        raise ValueError(f"Prompts not supported: {name}")
