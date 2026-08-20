"""SDK-native MCP server with schema overlays for the Skyvern copilot."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal, cast
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

from skyvern.cli.core.session_manager import request_session_scope
from skyvern.forge import app
from skyvern.forge.agent_functions import CopilotCandidateNetworkHop
from skyvern.forge.sdk.copilot.blocker_signal import (
    BROWSER_SESSION_LOST_BLOCKER_REASON_CODE,
    CopilotToolBlockerSignal,
    stash_blocker_signal,
)
from skyvern.forge.sdk.copilot.enforcement import requested_output_paths_for_derivation
from skyvern.forge.sdk.copilot.hooks import _copilot_log_fields
from skyvern.forge.sdk.copilot.loop_detection import record_tool_step_result_for_ctx
from skyvern.forge.sdk.copilot.output_utils import mark_mcp_result_untrusted_for_llm, sanitize_tool_result_for_llm
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    CopilotBrowserSessionUnavailable,
    close_browser_session_quietly,
    ensure_browser_session,
    mcp_browser_context,
    mcp_to_copilot,
    resolve_browser_state_for_context,
    retire_browser_session_id,
)
from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin
from skyvern.webeye.browser_state import BrowserState

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.context import CopilotContext

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
    "pending_scout_selector_candidates",
    "pending_scout_input_value",
    "pending_scout_role_name",
    "pending_scout_role_name_match_count",
    "pending_scout_ambiguous",
    "pending_scout_selector_match_count",
    "pending_scout_reanchor",
    "post_run_page_observation_tool",
    "post_run_page_observation_url",
    "post_run_page_observation_workflow_run_id",
    "post_run_page_observation_after_failed_test",
    "post_run_page_observation_generation",
    "latest_recorded_build_test_outcome",
    "code_only_target_page_evidence_seen",
    "last_scout_observation_trajectory_index",
    "last_scout_observation_has_password_control",
    "scouted_output_covered_paths",
    "scout_observed_terminal_criterion_ids",
    "scout_observation_contract",
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
    # Params the copilot offers on top of the MCP tool's own schema. A hook consumes them; they are
    # stripped before the call, so the underlying tool never sees an argument it cannot accept.
    copilot_params: dict[str, Any] = field(default_factory=dict)
    requires_browser: bool = False
    timeout: int | None = None
    pre_hook: PreHook | None = None
    post_hook: PostHook | None = None


LOG = structlog.get_logger()
_INTERNAL_TOOL_ARG_KEYS = frozenset({"_summarized"})
_SESSION_EXPIRED_ERROR_CODE = "SESSION_EXPIRED"
_SESSION_REESTABLISH_TIMEOUT_SECONDS = 35.0
_CONTINUITY_COORDINATION_TTL = timedelta(minutes=45)
_SESSION_LOST_USER_FACING_REASON = (
    "The browser session was lost, and I couldn't re-establish it. Please retry this turn."
)
_FALLBACK_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _BrowserSessionContinuityOutcome:
    lost_session_id: str
    root_session_id: str
    disposition: Literal["reestablished", "failed"]
    replacement_session_id: str | None


@dataclass
class _LocalContinuityLock:
    lock: asyncio.Lock
    users: int = 0


_LOCAL_CONTINUITY_LOCKS: dict[tuple[asyncio.AbstractEventLoop, str], _LocalContinuityLock] = {}
_LOCAL_CONTINUITY_OUTCOMES: dict[tuple[str, str], _BrowserSessionContinuityOutcome] = {}
_LOCAL_CONTINUITY_ROOTS: dict[tuple[str, str], str] = {}


def _continuity_outcome_key(organization_id: str, lost_session_id: str) -> str:
    return f"copilot_browser_continuity:outcome:{organization_id}:{lost_session_id}"


def _continuity_lineage_key(organization_id: str, session_id: str) -> str:
    return f"copilot_browser_continuity:lineage:{organization_id}:{session_id}"


def _continuity_lock_key(organization_id: str, lost_session_id: str) -> str:
    return f"copilot_browser_continuity:lock:{organization_id}:{lost_session_id}"


def _cache_supports_continuity_state() -> bool:
    # Every real BaseCache advertises this as a bool. Some focused tests replace
    # app.CACHE with a loose mock; keep those isolated from accidental mock state.
    return isinstance(getattr(app.CACHE, "is_shared", None), bool)


@asynccontextmanager
async def _browser_session_continuity_lock(organization_id: str, lost_session_id: str) -> AsyncIterator[None]:
    lock_key = _continuity_lock_key(organization_id, lost_session_id)
    local_key = (asyncio.get_running_loop(), lock_key)
    entry = _LOCAL_CONTINUITY_LOCKS.setdefault(local_key, _LocalContinuityLock(lock=asyncio.Lock()))
    entry.users += 1
    try:
        async with entry.lock:
            if getattr(app.CACHE, "is_shared", False) is not True:
                yield
            else:
                async with app.CACHE.get_lock(
                    lock_key,
                    blocking_timeout=int(_SESSION_REESTABLISH_TIMEOUT_SECONDS + 5),
                    timeout=int(_SESSION_REESTABLISH_TIMEOUT_SECONDS + 10),
                ):
                    yield
    finally:
        entry.users -= 1
        if entry.users == 0 and _LOCAL_CONTINUITY_LOCKS.get(local_key) is entry:
            _LOCAL_CONTINUITY_LOCKS.pop(local_key, None)


@asynccontextmanager
async def _context_browser_session_recovery_lock(ctx: AgentContext) -> AsyncIterator[None]:
    lock = getattr(ctx, "browser_session_recovery_lock", None)
    if lock is None:
        yield
        return
    async with lock:
        yield


def _decode_continuity_outcome(raw: object) -> _BrowserSessionContinuityOutcome | None:
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    disposition = data.get("disposition")
    replacement = data.get("replacement_session_id")
    if (
        not isinstance(data.get("lost_session_id"), str)
        or not isinstance(data.get("root_session_id"), str)
        or disposition not in {"reestablished", "failed"}
        or (replacement is not None and not isinstance(replacement, str))
    ):
        return None
    return _BrowserSessionContinuityOutcome(
        lost_session_id=data["lost_session_id"],
        root_session_id=data["root_session_id"],
        disposition=disposition,
        replacement_session_id=replacement,
    )


async def _get_continuity_outcome(
    organization_id: str, lost_session_id: str
) -> _BrowserSessionContinuityOutcome | None:
    if not _cache_supports_continuity_state():
        return _LOCAL_CONTINUITY_OUTCOMES.get((organization_id, lost_session_id))
    return _decode_continuity_outcome(await app.CACHE.get(_continuity_outcome_key(organization_id, lost_session_id)))


async def _get_continuity_root(organization_id: str, session_id: str) -> str | None:
    if not _cache_supports_continuity_state():
        return _LOCAL_CONTINUITY_ROOTS.get((organization_id, session_id))
    raw = await app.CACHE.get(_continuity_lineage_key(organization_id, session_id))
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    return raw if isinstance(raw, str) and raw else None


async def _store_continuity_outcome(organization_id: str, outcome: _BrowserSessionContinuityOutcome) -> None:
    if not _cache_supports_continuity_state():
        _LOCAL_CONTINUITY_OUTCOMES[(organization_id, outcome.lost_session_id)] = outcome
        if outcome.replacement_session_id is not None:
            _LOCAL_CONTINUITY_ROOTS[(organization_id, outcome.replacement_session_id)] = outcome.root_session_id
        return
    await app.CACHE.set(
        _continuity_outcome_key(organization_id, outcome.lost_session_id),
        json.dumps(
            {
                "lost_session_id": outcome.lost_session_id,
                "root_session_id": outcome.root_session_id,
                "disposition": outcome.disposition,
                "replacement_session_id": outcome.replacement_session_id,
            }
        ),
        ex=_CONTINUITY_COORDINATION_TTL,
    )
    if outcome.replacement_session_id is not None:
        await app.CACHE.set(
            _continuity_lineage_key(organization_id, outcome.replacement_session_id),
            outcome.root_session_id,
            ex=_CONTINUITY_COORDINATION_TTL,
        )


def _mapping_keys_preserved(source: Any, scrubbed: Any) -> bool:
    if isinstance(source, dict):
        return (
            isinstance(scrubbed, dict)
            and source.keys() == scrubbed.keys()
            and all(_mapping_keys_preserved(value, scrubbed[key]) for key, value in source.items())
        )
    if isinstance(source, (list, tuple)):
        return (
            type(source) is type(scrubbed)
            and len(source) == len(scrubbed)
            and all(_mapping_keys_preserved(left, right) for left, right in zip(source, scrubbed, strict=True))
        )
    return True


def _scrub_tool_result(ctx: AgentContext, result: Any) -> dict[str, Any]:
    scrubbed_secrets = scrub_secrets_from_structure(ctx, result)
    if not isinstance(scrubbed_secrets, dict) or not _mapping_keys_preserved(result, scrubbed_secrets):
        return {}
    parameters = getattr(ctx, "codeblock_redaction_parameters", None)
    if not isinstance(parameters, dict) or not parameters:
        return scrubbed_secrets
    scrubbed = app.AGENT_FUNCTION.redact_codeblock_parameter_values(scrubbed_secrets, parameters)
    if not isinstance(scrubbed, dict) or not _mapping_keys_preserved(scrubbed_secrets, scrubbed):
        return {}
    if type(scrubbed_secrets.get("ok")) is bool:
        scrubbed["ok"] = scrubbed_secrets["ok"]
    return scrubbed


def _scrub_tool_exception(ctx: AgentContext, tool_name: str, exception: BaseException) -> Any:
    try:
        detail = str(exception)
    except BaseException:
        detail = ""
    error = f"{tool_name} failed: {detail}" if detail else f"{tool_name} failed"
    del exception, detail
    return _scrub_tool_result(ctx, {"ok": False, "error": error})


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _log_mcp_timing(
    ctx: CopilotContext,
    tool_name: str,
    mcp_tool_name: str,
    wall_clock_ms: int,
    raw_mcp: dict[str, Any],
    call_path: Literal["model", "internal"],
    call_status: Literal["ok", "timeout", "error", "session_error", "cancelled", "not_connected"] = "ok",
) -> None:
    timing = raw_mcp.get("timing_ms")
    total = timing.get("total") if isinstance(timing, dict) else None
    reported = total if isinstance(total, int) and not isinstance(total, bool) else None
    LOG.info(
        "MCP tool timing",
        tool_name=tool_name,
        mcp_tool_name=mcp_tool_name,
        wall_clock_ms=wall_clock_ms,
        server_timing_ms=reported,
        call_path=call_path,
        call_status=call_status,
        **_copilot_log_fields(ctx),
    )


def _browser_session_loss_result(
    result: dict[str, Any], *, disposition: Literal["reestablished", "failed"]
) -> dict[str, Any]:
    data = result.get("data")
    continuity = {
        "source": "direct_mcp",
        "disposition": disposition,
        "fresh_state_required": disposition == "reestablished",
        "prior_action_effect": "unknown",
    }
    result_data = dict(data) if isinstance(data, dict) else {}
    result_data["browser_session_continuity"] = continuity
    if disposition == "reestablished":
        error = (
            "The browser session was lost during this operation. A fresh browser session is ready; "
            "inspect the page before continuing because the prior operation's effect is unknown."
        )
    else:
        error = _SESSION_LOST_USER_FACING_REASON
    return {
        **result,
        "ok": False,
        "error_code": _SESSION_EXPIRED_ERROR_CODE,
        "error": error,
        "data": result_data,
    }


def _browser_session_loss_blocker_signal(
    *, tool_name: str, call_path: Literal["model", "internal"], lost_session_id: str
) -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=(
            "The browser session was confirmed lost and one replacement attempt failed. "
            "End the turn from the recorded session-loss evidence."
        ),
        user_facing_reason=_SESSION_LOST_USER_FACING_REASON,
        recovery_hint="report_blocker_to_user",
        preserves_workflow_draft=True,
        renders_final_reply=True,
        internal_reason_code=BROWSER_SESSION_LOST_BLOCKER_REASON_CODE,
        blocked_tool=tool_name,
        extra={
            "call_path": call_path,
            "continuity_source": "direct_mcp",
            "continuity_disposition": "failed",
            "lost_browser_session_id": lost_session_id,
        },
    )


async def _handle_browser_session_loss(
    ctx: AgentContext,
    *,
    tool_name: str,
    call_path: Literal["model", "internal"],
    lost_session_id: str,
) -> Literal["reestablished", "failed"]:
    local_replacements = getattr(ctx, "browser_session_replacements", {})
    if lost_session_id in local_replacements:
        return "reestablished" if local_replacements[lost_session_id] is not None else "failed"

    async with _browser_session_continuity_lock(ctx.organization_id, lost_session_id):
        recorded = await _get_continuity_outcome(ctx.organization_id, lost_session_id)
        if recorded is not None:
            _apply_continuity_outcome(ctx, recorded, tool_name=tool_name, call_path=call_path)
            return recorded.disposition

        root_session_id = await _get_continuity_root(ctx.organization_id, lost_session_id)
        _emit_continuity_event(
            ctx,
            tool_name=tool_name,
            call_path=call_path,
            lost_session_id=lost_session_id,
            replacement_session_id=None,
            disposition="detected",
        )
        await close_browser_session_quietly(ctx.organization_id, lost_session_id)
        retire_browser_session_id(ctx, lost_session_id)

        if root_session_id is not None:
            outcome = _BrowserSessionContinuityOutcome(
                lost_session_id=lost_session_id,
                root_session_id=root_session_id,
                disposition="failed",
                replacement_session_id=None,
            )
        else:
            try:
                async with asyncio.timeout(_SESSION_REESTABLISH_TIMEOUT_SECONDS):
                    recovery_error = await ensure_browser_session(ctx)
            except asyncio.CancelledError:
                raise
            except Exception:
                recovery_error = {"ok": False, "error": "Failed to create browser session"}
            replacement_session_id = ctx.browser_session_id if recovery_error is None else None
            if recovery_error is not None and ctx.browser_session_id is not None:
                await close_browser_session_quietly(ctx.organization_id, ctx.browser_session_id)
                retire_browser_session_id(ctx, ctx.browser_session_id)
            outcome = _BrowserSessionContinuityOutcome(
                lost_session_id=lost_session_id,
                root_session_id=lost_session_id,
                disposition="reestablished" if replacement_session_id is not None else "failed",
                replacement_session_id=replacement_session_id,
            )

        await _store_continuity_outcome(ctx.organization_id, outcome)
        _apply_continuity_outcome(ctx, outcome, tool_name=tool_name, call_path=call_path)
        _emit_continuity_event(
            ctx,
            tool_name=tool_name,
            call_path=call_path,
            lost_session_id=lost_session_id,
            replacement_session_id=outcome.replacement_session_id,
            disposition=outcome.disposition,
        )
        return outcome.disposition


def _emit_continuity_event(
    ctx: AgentContext,
    *,
    tool_name: str,
    call_path: Literal["model", "internal"],
    lost_session_id: str,
    replacement_session_id: str | None,
    disposition: Literal["detected", "reestablished", "failed"],
) -> None:
    workflow_run_id = (
        ctx.sdk_action_workflow_run_ids_by_browser_session.get((ctx.organization_id, lost_session_id))
        or ctx.last_run_blocks_workflow_run_id
    )
    fields: dict[str, Any] = {
        "error_code": _SESSION_EXPIRED_ERROR_CODE,
        "session_id": lost_session_id,
        "replacement_session_id": replacement_session_id,
        "workflow_run_id": workflow_run_id,
        "tool_name": tool_name,
        "call_path": call_path,
        "continuity_source": "direct_mcp",
        "continuity_disposition": disposition,
        **_copilot_log_fields(cast("CopilotContext", ctx)),
    }
    if disposition != "detected":
        fields["reestablish_budget_seconds"] = _SESSION_REESTABLISH_TIMEOUT_SECONDS
    log = LOG.info if disposition == "reestablished" else LOG.warning
    try:
        log("copilot_browser_session_continuity_loss", **fields)
    except Exception:
        try:
            _FALLBACK_LOGGER.warning(
                "copilot_browser_session_continuity_loss session_id=%s disposition=%s",
                lost_session_id,
                disposition,
            )
        except Exception:
            pass


def _apply_continuity_outcome(
    ctx: AgentContext,
    outcome: _BrowserSessionContinuityOutcome,
    *,
    tool_name: str,
    call_path: Literal["model", "internal"],
) -> None:
    ctx.browser_session_id = outcome.replacement_session_id
    replacements = getattr(ctx, "browser_session_replacements", None)
    if not isinstance(replacements, dict):
        replacements = {}
        ctx.browser_session_replacements = replacements
    replacements[outcome.lost_session_id] = outcome.replacement_session_id
    ctx.browser_session_continuity_generation = getattr(ctx, "browser_session_continuity_generation", 0) + 1
    ctx.browser_session_continuity_disposition = outcome.disposition
    if outcome.disposition == "failed":
        stash_blocker_signal(
            ctx,
            _browser_session_loss_blocker_signal(
                tool_name=tool_name,
                call_path=call_path,
                lost_session_id=outcome.lost_session_id,
            ),
        )


async def _prepare_browser_session_for_dispatch(
    ctx: AgentContext,
    *,
    tool_name: str,
    call_path: Literal["model", "internal"],
    observed_generation: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Verify the current session and surface any replacement before action dispatch."""
    async with _context_browser_session_recovery_lock(ctx):
        if getattr(ctx, "browser_session_continuity_generation", 0) != observed_generation:
            disposition: Literal["reestablished", "failed"] = (
                "reestablished"
                if getattr(ctx, "browser_session_continuity_disposition", None) == "reestablished"
                else "failed"
            )
            return None, _browser_session_loss_result({}, disposition=disposition)

        prior_session_id = ctx.browser_session_id
        if not prior_session_id:
            return await ensure_browser_session(ctx, require_verified_session=True), None

        async with _browser_session_continuity_lock(ctx.organization_id, prior_session_id):
            recorded = await _get_continuity_outcome(ctx.organization_id, prior_session_id)
            if recorded is not None:
                _apply_continuity_outcome(ctx, recorded, tool_name=tool_name, call_path=call_path)
                return None, _browser_session_loss_result({}, disposition=recorded.disposition)

            error = await ensure_browser_session(ctx, require_verified_session=True)
            if error is not None and ctx.browser_session_id == prior_session_id:
                # A bounded probe that could not determine liveness is not evidence of loss.
                return error, None
            if (
                getattr(ctx, "turn_origin", TurnOrigin.interactive) == TurnOrigin.runtime_self_heal
                or ctx.browser_session_id == prior_session_id
            ):
                return None, None

            _emit_continuity_event(
                ctx,
                tool_name=tool_name,
                call_path=call_path,
                lost_session_id=prior_session_id,
                replacement_session_id=None,
                disposition="detected",
            )
            replacement_session_id = ctx.browser_session_id if error is None else None
            if error is not None and ctx.browser_session_id is not None:
                await close_browser_session_quietly(ctx.organization_id, ctx.browser_session_id)
                retire_browser_session_id(ctx, ctx.browser_session_id)
            await close_browser_session_quietly(ctx.organization_id, prior_session_id)
            outcome = _BrowserSessionContinuityOutcome(
                lost_session_id=prior_session_id,
                root_session_id=prior_session_id,
                disposition="reestablished" if replacement_session_id is not None else "failed",
                replacement_session_id=replacement_session_id,
            )
            await _store_continuity_outcome(ctx.organization_id, outcome)
            _apply_continuity_outcome(ctx, outcome, tool_name=tool_name, call_path=call_path)
            _emit_continuity_event(
                ctx,
                tool_name=tool_name,
                call_path=call_path,
                lost_session_id=prior_session_id,
                replacement_session_id=outcome.replacement_session_id,
                disposition=outcome.disposition,
            )
            return None, _browser_session_loss_result({}, disposition=outcome.disposition)


def _requested_output_path_choices(schema: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    """Present the outputs this turn owes as the choices for the path a read claims.

    A free-form string left the model naming its own purpose, so the read that observed the requested
    quantity was filed as exploration and the value it saw never witnessed the path the binder owes
    (SKY-13226). The field stays optional: a read that is genuinely exploration omits it, and every
    requested path stays available so a later read can refine one already claimed.
    """
    properties = schema.get("properties")
    if not paths or not isinstance(properties, dict):
        return schema
    output_path = properties.get("output_path")
    if not isinstance(output_path, dict):
        return schema
    listed = ", ".join(paths)
    return {
        **schema,
        "properties": {
            **properties,
            "output_path": {
                **output_path,
                "enum": paths,
                "description": (
                    f"{output_path.get('description', '')} This turn owes: {listed}. Set it to the one "
                    "this read fills; omit it when the read is exploration."
                ).strip(),
            },
        },
    }


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

    props.update(overlay.copilot_params)

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
    dropped = overlay.hide_params | _INTERNAL_TOOL_ARG_KEYS | frozenset(overlay.copilot_params)
    mcp_args = {k: v for k, v in arguments.items() if k not in dropped}

    for copilot_param, mcp_param in overlay.arg_transforms.items():
        if copilot_param in mcp_args:
            mcp_args[mcp_param] = mcp_args.pop(copilot_param)

    mcp_args.update(overlay.forced_args)
    return mcp_args


def _copilot_to_call_tool_result(
    copilot_result: dict[str, Any],
) -> CallToolResult:
    if not copilot_result:
        return CallToolResult(content=[], isError=True)
    sanitized = sanitize_tool_result_for_llm("", copilot_result)
    marked = mark_mcp_result_untrusted_for_llm(sanitized)
    content: list[TextContent] = [TextContent(type="text", text=json.dumps(marked))]
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
        with request_session_scope(self._context_provider().organization_id):
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
        examined_session_id = ctx.browser_session_id
        try:
            browser_state = await resolve_browser_state_for_context(ctx)
            if browser_state is None:
                retire_browser_session_id(ctx, examined_session_id)
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
        try:
            requested_output_path_choices = sorted(requested_output_paths_for_derivation(self._context_provider()))
        except Exception:
            requested_output_path_choices = []

        for tool in raw_tools:
            if tool.name not in self._allowlist:
                continue

            copilot_name = self._reverse_alias.get(tool.name, tool.name)

            overlay = self._overlays.get(copilot_name, SchemaOverlay())

            schema = _apply_schema_overlay(tool.inputSchema, overlay)
            schema = _requested_output_path_choices(schema, requested_output_path_choices)
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
        propagated_error: BaseException
        try:
            return await self._call_tool(tool_name, arguments, meta)
        except BaseException as exc:
            if not app.AGENT_FUNCTION.prepare_codeblock_control_flow_exception(exc):
                LOG.warning("MCP tool dispatch failed")
                return CallToolResult(content=[], isError=True)
            propagated_error = exc.with_traceback(None)
            del self, tool_name, arguments, meta, exc
        raise propagated_error from None

    async def _call_tool(
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
        observed_continuity_generation = getattr(copilot_ctx, "browser_session_continuity_generation", 0)

        policy = copilot_ctx.request_policy
        if overlay.requires_browser and isinstance(policy, RequestPolicy) and policy.raw_secret_detected:
            result = {
                "ok": False,
                "error": "A raw-secret draft cannot use browser tools. Save only the redacted draft.",
            }
            result = _scrub_tool_result(copilot_ctx, result)
            LOG.info("Raw-secret safety blocked MCP browser tool", tool_name=tool_name)
            record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, result)
            return _copilot_to_call_tool_result(result)

        if overlay.pre_hook:
            hook_result = await overlay.pre_hook(arguments, copilot_ctx)
            if hook_result is not None:
                hook_result = _scrub_tool_result(copilot_ctx, hook_result)
                record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, hook_result)
                return _copilot_to_call_tool_result(hook_result)

        started = time.monotonic()
        mcp_name = self._alias_map.get(tool_name, tool_name)
        mcp_args = _transform_args(arguments, overlay)

        if overlay.requires_browser:
            try:
                err, continuity_result = await _prepare_browser_session_for_dispatch(
                    copilot_ctx,
                    tool_name=tool_name,
                    call_path="model",
                    observed_generation=observed_continuity_generation,
                )
            except asyncio.CancelledError:
                _log_mcp_timing(copilot_ctx, tool_name, mcp_name, _elapsed_ms(started), {}, "model", "cancelled")
                raise
            except Exception:
                _log_mcp_timing(copilot_ctx, tool_name, mcp_name, _elapsed_ms(started), {}, "model", "session_error")
                raise
            if err:
                _log_mcp_timing(copilot_ctx, tool_name, mcp_name, _elapsed_ms(started), {}, "model", "session_error")
                err = _scrub_tool_result(copilot_ctx, err)
                record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, err)
                return _copilot_to_call_tool_result(err)
            if continuity_result is not None:
                _log_mcp_timing(copilot_ctx, tool_name, mcp_name, _elapsed_ms(started), {}, "model", "session_error")
                continuity_result = _scrub_tool_result(copilot_ctx, continuity_result)
                record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, continuity_result)
                return _copilot_to_call_tool_result(continuity_result)
            mcp_args["session_id"] = copilot_ctx.browser_session_id
        call_browser_session_id = copilot_ctx.browser_session_id if overlay.requires_browser else None

        try:
            # wait_for(timeout=None) is a plain await, so only overlays that declare a ceiling get
            # one. The ceiling bounds every await under the call — a page evaluate, but also a stale
            # session handle whose CDP request never answers, which held a turn for 307s (SKY-13226).
            if overlay.requires_browser:
                async with mcp_browser_context(copilot_ctx):
                    raw_result = await asyncio.wait_for(
                        self._client.call_tool(mcp_name, mcp_args, raise_on_error=False),
                        timeout=overlay.timeout,
                    )
            else:
                raw_result = await asyncio.wait_for(
                    self._client.call_tool(mcp_name, mcp_args, raise_on_error=False),
                    timeout=overlay.timeout,
                )
        except TimeoutError:
            LOG.warning("MCP tool call timed out", tool=tool_name, ceiling_seconds=overlay.timeout)
            _log_mcp_timing(copilot_ctx, tool_name, mcp_name, _elapsed_ms(started), {}, "model", "timeout")
            # The call is cancelled where it stands, so a tool that changes the page may already have
            # changed it. Reporting a plain failure invites a retry that acts on the page twice.
            err = {
                "ok": False,
                "error": (
                    f"{tool_name} did not answer within {overlay.timeout}s and was cancelled. "
                    "Whether it took effect is unknown; read the page before trying it again."
                ),
            }
            err = _scrub_tool_result(copilot_ctx, err)
            record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, err)
            return _copilot_to_call_tool_result(err)
        except CopilotBrowserSessionUnavailable as exc:
            _log_mcp_timing(copilot_ctx, tool_name, mcp_name, _elapsed_ms(started), {}, "model", "session_error")
            disposition = await _handle_browser_session_loss(
                copilot_ctx,
                tool_name=tool_name,
                call_path="model",
                lost_session_id=exc.session_id,
            )
            err = _scrub_tool_result(
                copilot_ctx,
                _browser_session_loss_result({}, disposition=disposition),
            )
            record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, err)
            return _copilot_to_call_tool_result(err)
        except asyncio.CancelledError:
            _log_mcp_timing(copilot_ctx, tool_name, mcp_name, _elapsed_ms(started), {}, "model", "cancelled")
            raise
        except Exception as exc:
            LOG.warning("MCP tool call failed", tool=tool_name)
            _log_mcp_timing(copilot_ctx, tool_name, mcp_name, _elapsed_ms(started), {}, "model", "error")
            err = _scrub_tool_exception(copilot_ctx, tool_name, exc)
            record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, err)
            return _copilot_to_call_tool_result(err)
        wall_clock_ms = _elapsed_ms(started)

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
        failed = raw_result.is_error or raw_mcp.get("ok", True) is not True
        _log_mcp_timing(copilot_ctx, tool_name, mcp_name, wall_clock_ms, raw_mcp, "model", "error" if failed else "ok")
        # Scrub before the post hook so evidence the hooks record from raw_mcp
        # (flow evidence, scout observations) is scrubbed too.
        raw_mcp = _scrub_tool_result(copilot_ctx, raw_mcp)
        copilot_result = mcp_to_copilot(raw_mcp) if raw_mcp else {}
        session_lost = False
        if (
            overlay.requires_browser
            and copilot_ctx.turn_origin != TurnOrigin.runtime_self_heal
            and isinstance(call_browser_session_id, str)
            and call_browser_session_id
            and copilot_result.get("error_code") == _SESSION_EXPIRED_ERROR_CODE
        ):
            session_lost = True
            disposition = await _handle_browser_session_loss(
                copilot_ctx,
                tool_name=tool_name,
                call_path="model",
                lost_session_id=call_browser_session_id,
            )
            copilot_result = _browser_session_loss_result(copilot_result, disposition=disposition)

        if overlay.post_hook and not session_lost:
            base_copilot_result = deepcopy(copilot_result)
            ctx_snapshot = _snapshot_post_hook_context(copilot_ctx)
            try:
                copilot_result = await overlay.post_hook(copilot_result, raw_mcp, copilot_ctx)
            except Exception:
                # A post-hook enriches evidence only; a crash must not fail the browser action or keep partial credit.
                _restore_post_hook_context(copilot_ctx, ctx_snapshot)
                LOG.warning("MCP post-hook failed; returning base tool result", tool=tool_name)
                copilot_result = base_copilot_result

        copilot_result = _scrub_tool_result(copilot_ctx, copilot_result)
        record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, copilot_result)
        enqueue_screenshot_from_result(copilot_ctx, copilot_result)
        return _copilot_to_call_tool_result(copilot_result)

    async def call_internal_tool(
        self,
        mcp_tool_name: str,
        mcp_args: dict[str, Any],
    ) -> dict[str, Any]:
        propagated_error: BaseException
        try:
            return await self._call_internal_tool(mcp_tool_name, mcp_args)
        except BaseException as exc:
            if not app.AGENT_FUNCTION.prepare_codeblock_control_flow_exception(exc):
                LOG.warning("Internal MCP tool dispatch failed")
                return {}
            propagated_error = exc.with_traceback(None)
            del self, mcp_tool_name, mcp_args, exc
        raise propagated_error from None

    async def _call_internal_tool(
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
        started = time.monotonic()
        copilot_name = self._reverse_alias.get(mcp_tool_name, mcp_tool_name)
        ctx = self._context_provider()
        observed_continuity_generation = getattr(ctx, "browser_session_continuity_generation", 0)
        if not self._client:
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, _elapsed_ms(started), {}, "internal", "not_connected")
            return _scrub_tool_result(ctx, {"ok": False, "error": "MCP client not connected"})
        try:
            err, continuity_result = await _prepare_browser_session_for_dispatch(
                ctx,
                tool_name=copilot_name,
                call_path="internal",
                observed_generation=observed_continuity_generation,
            )
        except asyncio.CancelledError:
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, _elapsed_ms(started), {}, "internal", "cancelled")
            raise
        except Exception:
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, _elapsed_ms(started), {}, "internal", "session_error")
            raise
        if err:
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, _elapsed_ms(started), {}, "internal", "session_error")
            return _scrub_tool_result(ctx, err)
        if continuity_result is not None:
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, _elapsed_ms(started), {}, "internal", "session_error")
            return _scrub_tool_result(ctx, continuity_result)
        merged_args = {**mcp_args, "session_id": ctx.browser_session_id}
        call_browser_session_id = ctx.browser_session_id
        try:
            async with mcp_browser_context(ctx):
                raw = await self._client.call_tool(mcp_tool_name, merged_args, raise_on_error=False)
            wall_clock_ms = _elapsed_ms(started)
            if self._evidence_candidate_origin is not None:
                await asyncio.sleep(0)
                await self._drain_evidence_candidate_response_tasks()
        except asyncio.CancelledError:
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, _elapsed_ms(started), {}, "internal", "cancelled")
            raise
        except CopilotBrowserSessionUnavailable as exc:
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, _elapsed_ms(started), {}, "internal", "session_error")
            disposition = await _handle_browser_session_loss(
                ctx,
                tool_name=copilot_name,
                call_path="internal",
                lost_session_id=exc.session_id,
            )
            return _scrub_tool_result(ctx, _browser_session_loss_result({}, disposition=disposition))
        except Exception as exc:
            LOG.warning("Internal MCP tool call failed", tool=mcp_tool_name)
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, _elapsed_ms(started), {}, "internal", "error")
            return _scrub_tool_exception(ctx, mcp_tool_name, exc)
        raw_mcp = dict(raw.structured_content or {})
        if raw.is_error:
            raw_mcp["ok"] = False
            if not raw.structured_content and raw.content:
                text_parts = [c.text for c in raw.content if hasattr(c, "text")]
                raw_mcp["error"] = " ".join(text_parts) if text_parts else "Unknown MCP error"
            else:
                raw_mcp["error"] = raw_mcp.get("error") or "Unknown MCP error"
        failed = raw.is_error or raw_mcp.get("ok", True) is not True
        _log_mcp_timing(
            ctx, copilot_name, mcp_tool_name, wall_clock_ms, raw_mcp, "internal", "error" if failed else "ok"
        )
        scrubbed = _scrub_tool_result(ctx, raw_mcp)
        result = mcp_to_copilot(scrubbed) if scrubbed else {}
        if (
            ctx.turn_origin != TurnOrigin.runtime_self_heal
            and call_browser_session_id is not None
            and result.get("error_code") == _SESSION_EXPIRED_ERROR_CODE
        ):
            disposition = await _handle_browser_session_loss(
                ctx,
                tool_name=copilot_name,
                call_path="internal",
                lost_session_id=call_browser_session_id,
            )
            return _browser_session_loss_result(result, disposition=disposition)
        return result

    async def list_prompts(self) -> ListPromptsResult:
        return ListPromptsResult(prompts=[])

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> GetPromptResult:
        raise ValueError(f"Prompts not supported: {name}")
