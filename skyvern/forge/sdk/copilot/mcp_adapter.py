"""SDK-native MCP server with schema overlays for the Skyvern copilot."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

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
from playwright.async_api import Browser, BrowserContext

from skyvern.forge import app
from skyvern.forge.agent_functions import CopilotCandidateNetworkHop
from skyvern.forge.sdk.copilot.blocker_signal import (
    build_loop_blocker_signal,
    loop_blocker_evidence_from_ctx,
    refresh_held_loop_blocker_evidence,
    stash_blocker_signal,
)
from skyvern.forge.sdk.copilot.build_phase import _phase_blocker_signal
from skyvern.forge.sdk.copilot.enforcement import (
    post_run_page_path_interaction_allowed,
    register_no_progress_interaction_click,
    synthesized_block_persistence_signal,
    terminal_challenge_blocker_signal_from_current_page_evidence,
    try_admit_post_run_page_path_interaction,
)
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
    resolve_browser_state_for_context,
)
from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure
from skyvern.forge.sdk.copilot.turn_halt import stash_turn_halt_from_blocker_signal
from skyvern.forge.sdk.copilot.turn_ownership import emit_blocker_signal_payload
from skyvern.webeye.browser_state import BrowserState

PreHook = Callable[[dict[str, Any], AgentContext], Awaitable[dict[str, Any] | None]]
PostHook = Callable[[dict[str, Any], dict[str, Any], AgentContext], Awaitable[dict[str, Any]]]

_POST_HOOK_CONTEXT_ROLLBACK_FIELDS = (
    "flow_evidence",
    "composition_page_evidence",
    "workflow_verification_evidence",
    "pending_browser_interaction_observation",
    "scouted_interactions",
    "scout_trajectory",
    "never_captured_obligation",
    "synthesized_block_reopened_for_capture_obligation",
    "pending_scout_source_url",
    "pending_scout_typed_value",
    "pending_scout_role_name",
    "pending_scout_ambiguous",
    "pending_scout_reanchor",
    "post_budget_page_inspection_required",
    "post_budget_page_inspection_url",
    "post_budget_page_inspection_run_id",
    "post_run_page_observation_tool",
    "post_run_page_observation_url",
    "post_run_page_observation_workflow_run_id",
    "post_run_page_observation_after_failed_test",
    "post_run_page_observation_generation",
    "latest_recorded_build_test_outcome",
    "code_only_target_page_evidence_seen",
    "last_evaluate_actionable_signature",
    "last_evaluate_actionable_url",
    "last_scout_observation_trajectory_index",
    "last_scout_observation_has_password_control",
    "latest_evaluate_result_composition_steer",
    "latest_evaluate_result_composition_signature",
    "last_auto_acted_signature",
    "reached_download_target",
    "synthesized_block_offered",
    "synthesized_block_offered_trajectory_len",
    "synthesized_block_offered_goal_complete",
    "synthesized_business_required_parameter_keys",
    "scouted_output_covered_paths",
    "scout_observed_terminal_criterion_ids",
    "scout_observation_contract",
    "requested_output_extraction_candidate",
    "synthesized_block_reopened_for_output_coverage",
    "uncovered_output_rescout_context_key",
    "uncovered_output_rescout_steer_key",
    "consecutive_no_progress_interaction_count",
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
    payload = emit_blocker_signal_payload(ctx, signal)
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
    payload = emit_blocker_signal_payload(ctx, signal)
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


def _evidence_candidate_url_origin(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return None
    return f"https://{parsed.hostname.lower()}{port}"


@asynccontextmanager
async def _service_worker_blocked_context(
    browser_state: BrowserState,
    *,
    organization_id: str,
) -> AsyncIterator[BrowserContext]:
    original_context = browser_state.browser_context
    if original_context is None:
        raise RuntimeError("Evidence-candidate browser does not support an isolated context")
    original_page = await browser_state.get_working_page()
    browser = original_context.browser
    fallback_browser: Browser | None = None
    if browser is None:
        fallback_browser = await browser_state.pw.chromium.launch()
        browser = fallback_browser
    candidate_context: BrowserContext | None = None
    try:
        candidate_context = await browser.new_context(service_workers="block")
        await app.AGENT_FUNCTION.setup_browser_context_extensions(
            candidate_context,
            organization_id=organization_id,
            copilot_candidate_network_guard=True,
        )
        candidate_page = await candidate_context.new_page()
        browser_state.browser_context = candidate_context
        await browser_state.set_active_page(candidate_page)
        yield candidate_context
    finally:
        try:
            if candidate_context is not None:
                browser_state.browser_context = original_context
                if original_page is None:
                    await browser_state.set_working_page(None)
                else:
                    await browser_state.set_active_page(original_page)
                await candidate_context.close()
        finally:
            if fallback_browser is not None:
                await fallback_browser.close()


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
        self._evidence_candidate_origin: str | None = None
        self._evidence_candidate_guarded_hops: list[CopilotCandidateNetworkHop] | None = None

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

    @asynccontextmanager
    async def evidence_candidate_navigation_guard(
        self,
        expected_origin: str,
    ) -> AsyncIterator[list[CopilotCandidateNetworkHop]]:
        if self._evidence_candidate_origin is not None:
            raise RuntimeError("Evidence-candidate navigation guard is already active")
        normalized_origin = _evidence_candidate_url_origin(expected_origin)
        if normalized_origin != expected_origin:
            raise ValueError("Evidence-candidate origin must be an exact HTTPS origin")
        ctx = self._context_provider()
        session_error = await ensure_browser_session(ctx)
        if session_error is not None:
            raise RuntimeError(str(session_error.get("error", "Evidence-candidate browser session unavailable")))
        try:
            browser_state = await resolve_browser_state_for_context(ctx)
            if browser_state is None:
                raise RuntimeError("Evidence-candidate navigation guard requires a browser context")
            async with _service_worker_blocked_context(
                browser_state,
                organization_id=ctx.organization_id,
            ) as browser_context:
                cookies = await browser_context.cookies()
                if (
                    cookies
                    or browser_context.service_workers
                    or any(page.url not in {"", "about:blank"} for page in browser_context.pages)
                ):
                    raise RuntimeError("Evidence-candidate navigation guard requires a pristine browser context")
                async with app.AGENT_FUNCTION.copilot_candidate_network_guard(
                    browser_context, expected_origin=normalized_origin
                ) as guarded_hops:
                    self._evidence_candidate_origin = normalized_origin
                    self._evidence_candidate_guarded_hops = guarded_hops
                    try:
                        yield guarded_hops
                    finally:
                        await app.AGENT_FUNCTION.wait_for_copilot_candidate_network_idle(browser_context)
        finally:
            self._evidence_candidate_origin = None
            self._evidence_candidate_guarded_hops = None

    async def _drain_evidence_candidate_response_tasks(self) -> None:
        if self._evidence_candidate_origin is None:
            return
        browser_state = await resolve_browser_state_for_context(self._context_provider())
        browser_context = browser_state.browser_context if browser_state is not None else None
        if browser_context is None:
            raise RuntimeError("Evidence-candidate browser context became unavailable")
        await app.AGENT_FUNCTION.wait_for_copilot_candidate_network_idle(browser_context)

    async def evidence_candidate_browser_url(self) -> str:
        if self._evidence_candidate_origin is None:
            raise RuntimeError("Evidence-candidate navigation guard is not active")
        browser_state = await resolve_browser_state_for_context(self._context_provider())
        page = await browser_state.get_working_page() if browser_state is not None else None
        if page is None:
            raise RuntimeError("Evidence-candidate working page is unavailable")
        browser_url = page.url
        last_enforced_url = next(
            (
                hop["url"]
                for hop in reversed(self._evidence_candidate_guarded_hops or [])
                if hop["resource_type"] == "document"
            ),
            None,
        )
        if (
            _evidence_candidate_url_origin(browser_url) != self._evidence_candidate_origin
            or browser_url != last_enforced_url
        ):
            raise RuntimeError("candidate_browser_url_not_peer_verified")
        return browser_url

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
        post_run_page_path_allowed = post_run_page_path_interaction_allowed(
            copilot_ctx,
            tool_name,
            arguments,
        )
        if not post_run_page_path_allowed and tool_name in _CURRENT_PAGE_TERMINAL_CHALLENGE_MCP_TOOLS:
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

        persistence_signal = (
            None
            if post_run_page_path_allowed
            else synthesized_block_persistence_signal(copilot_ctx, tool_name, arguments)
        )
        if persistence_signal is not None:
            LOG.warning(
                "Synthesized block persistence required before MCP tool",
                tool_name=tool_name,
                synthesized_block_offered_trajectory_len=getattr(
                    copilot_ctx,
                    "synthesized_block_offered_trajectory_len",
                    None,
                ),
            )
            payload = emit_blocker_signal_payload(copilot_ctx, persistence_signal)
            result = {"ok": False, "error": payload}
            record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, result)
            return _copilot_to_call_tool_result(result)

        loop_error = detect_failed_tool_step_loop_for_ctx(copilot_ctx, tool_name, arguments)
        if loop_error:
            LOG.warning(
                "Failed tool step loop detected, skipping execution",
                tool_name=tool_name,
            )
            payload = _stash_and_emit_loop_blocker(copilot_ctx, loop_error, tool_name)
            return _copilot_to_call_tool_result({"ok": False, "error": payload})

        tracker = getattr(copilot_ctx, "consecutive_tool_tracker", None)
        loop_error = detect_tool_loop(tracker, tool_name, arguments) if isinstance(tracker, list) else None
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

        if post_run_page_path_allowed and not try_admit_post_run_page_path_interaction(
            copilot_ctx,
            tool_name,
            arguments,
        ):
            persistence_signal = synthesized_block_persistence_signal(copilot_ctx, tool_name, arguments)
            if persistence_signal is not None:
                payload = emit_blocker_signal_payload(copilot_ctx, persistence_signal)
                result = {"ok": False, "error": payload}
                record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, result)
                return _copilot_to_call_tool_result(result)

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
            if tool_name == "click":
                register_no_progress_interaction_click(copilot_ctx, outcome="click_failed")
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
            if self._evidence_candidate_origin is not None:
                await asyncio.sleep(0)
                await self._drain_evidence_candidate_response_tasks()
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
