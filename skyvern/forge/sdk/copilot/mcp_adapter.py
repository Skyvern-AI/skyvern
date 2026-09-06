"""SDK-native MCP server with schema overlays for the Skyvern copilot."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlparse

import structlog
from agents.agent import AgentBase
from agents.mcp.server import MCPServer
from agents.run_context import RunContextWrapper
from fastmcp import Client
from fastmcp.client.client import CallToolResult as FastMCPCallToolResult
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
from skyvern.forge.sdk.copilot.pending_operation import pending_operation
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.runtime import (
    _BROWSER_BOOT_WAIT_SECONDS,
    SENSITIVE_ORIGIN_PAGE_ERROR,
    AgentContext,
    CopilotBrowserGenerationRetired,
    CopilotBrowserSessionUnavailable,
    bound_call_browser_session,
    browser_evidence_commit_lock,
    browser_page_custody_lock,
    close_browser_session_quietly,
    current_call_browser_session_override,
    ensure_browser_session,
    mcp_browser_context,
    mcp_to_copilot,
    resolve_browser_state_for_context,
    retire_browser_session_id,
    sensitive_origin_page_facts_withheld,
    sensitive_origin_page_is_tainted,
)
from skyvern.forge.sdk.copilot.screenshot_utils import (
    ScreenshotActionRelation,
    ScreenshotProvenance,
    enqueue_screenshot_from_result,
)
from skyvern.forge.sdk.copilot.secret_scrub import (
    matching_origin_run_redaction_parameters,
    scrub_secrets_from_structure,
)
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin
from skyvern.utils.contained_effects import contained_effect
from skyvern.webeye.browser_retirement import BrowserRetirementReason
from skyvern.webeye.browser_state import BrowserState

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.context import CopilotContext

from skyvern.forge.sdk.copilot.browser_target import (
    BROWSER_TARGET_PARAM,
    BROWSER_TARGET_PARAM_NAME,
    BrowserSessionBinding,
    resolve_browser_session_binding,
)

__all__ = [
    "BROWSER_TARGET_PARAM",
    "BROWSER_TARGET_PARAM_NAME",
    "BrowserSessionBinding",
    "resolve_browser_session_binding",
]

PreHook = Callable[[dict[str, Any], AgentContext], Awaitable[dict[str, Any] | None]]
PostHook = Callable[[dict[str, Any], dict[str, Any], AgentContext], Awaitable[dict[str, Any]]]

_SHARED_BROWSER_OUTCOME_TOOLS = frozenset({"skyvern_evaluate", "skyvern_screenshot"})
_BrowserCallErrorKind = Literal["tool", "protocol"]
_BrowserSessionLossDisposition = Literal["reestablished", "generation_retired", "failed"]


@dataclass(frozen=True)
class _BrowserCallOutcome:
    """Immutable browser facts captured before either Copilot adapter shapes them.

    The payload is copied structurally at the protocol boundary and for each
    projection. Immutable leaves (including screenshot base64 strings) are shared,
    so adapter isolation does not duplicate the largest response body.
    """

    raw_tool_name: str
    source_browser_session_id: str | None
    source_browser_session_generation: int
    dispatched: bool
    ok: bool
    error_kind: _BrowserCallErrorKind | None
    error_code: str | None
    screenshot_present: bool
    screenshot_reference: str | None
    response_truncated: bool
    payload_omitted: bool
    session_loss_disposition: _BrowserSessionLossDisposition | None = None
    # Carried beside the disposition because the projection is the only thing the primary browser
    # tools return, and it has no context to read the cause from.
    session_loss_deadline_expired: bool = False
    replacement_browser_session_id: str | None = None
    completion_browser_session_id: str | None = None
    completion_browser_session_generation: int | None = None
    evidence_drain_complete: bool | None = None
    cancelled: bool = False
    protocol_error_detail: str | None = None
    _raw_result_payload: dict[str, Any] = field(default_factory=dict, repr=False)

    def raw_result(self) -> dict[str, Any]:
        return deepcopy(self._raw_result_payload)

    def with_raw_result(self, raw_result: dict[str, Any]) -> _BrowserCallOutcome:
        return replace(self, _raw_result_payload=_copy_browser_result(raw_result))


@dataclass(frozen=True)
class _InternalBrowserCallResult:
    result: dict[str, Any]
    outcome: _BrowserCallOutcome


@dataclass(frozen=True)
class _InternalToolCallResult:
    result: dict[str, Any]
    browser_outcome: _BrowserCallOutcome | None = None


def _copy_browser_result(raw_result: dict[str, Any]) -> dict[str, Any]:
    # deepcopy copies mutable containers but returns immutable strings unchanged.
    # Inline screenshot bytes therefore have one string allocation in custody.
    return deepcopy(raw_result)


def _browser_error_code(raw_result: dict[str, Any]) -> str | None:
    error = raw_result.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) and code else None


def _screenshot_reference(raw_tool_name: str, raw_result: dict[str, Any]) -> tuple[bool, str | None]:
    if raw_tool_name != "skyvern_screenshot":
        return False, None
    data = raw_result.get("data")
    reference = data.get("path") if isinstance(data, dict) else None
    inline_present = isinstance(data, dict) and any(
        isinstance(data.get(key), str) and bool(data[key]) for key in ("data", "screenshot_base64", "image_base64")
    )
    artifacts = raw_result.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, dict) or artifact.get("kind") != "screenshot":
                continue
            artifact_path = artifact.get("path")
            if isinstance(artifact_path, str) and artifact_path:
                reference = artifact_path
                break
    safe_reference = reference if isinstance(reference, str) and reference else None
    return inline_present or safe_reference is not None, safe_reference


def _browser_call_outcome_from_mapping(
    *,
    raw_tool_name: str,
    source_browser_session_id: str | None,
    source_browser_session_generation: int = 0,
    dispatched: bool = True,
    raw_result: dict[str, Any],
    error_kind: _BrowserCallErrorKind | None = None,
    protocol_error_detail: str | None = None,
) -> _BrowserCallOutcome:
    ok = raw_result.get("ok", raw_result.get("error") is None) is True and error_kind is None
    normalized_error_kind = error_kind or ("tool" if not ok else None)
    screenshot_present, screenshot_reference = _screenshot_reference(raw_tool_name, raw_result)
    response_truncated = raw_result.get("_truncated") is True
    return _BrowserCallOutcome(
        raw_tool_name=raw_tool_name,
        source_browser_session_id=source_browser_session_id,
        source_browser_session_generation=source_browser_session_generation,
        dispatched=dispatched,
        ok=ok,
        error_kind=normalized_error_kind,
        error_code=_browser_error_code(raw_result),
        screenshot_present=screenshot_present,
        screenshot_reference=screenshot_reference,
        response_truncated=response_truncated,
        payload_omitted=response_truncated,
        protocol_error_detail=protocol_error_detail,
        _raw_result_payload=_copy_browser_result(raw_result),
    )


def _normalize_browser_call_outcome(
    *,
    raw_tool_name: str,
    source_browser_session_id: str | None,
    source_browser_session_generation: int = 0,
    raw_result: FastMCPCallToolResult,
) -> _BrowserCallOutcome:
    raw_mcp = dict(raw_result.structured_content or {})
    if raw_result.is_error:
        raw_mcp["ok"] = False
        if not raw_result.structured_content and raw_result.content:
            text_parts = [content.text for content in raw_result.content if isinstance(content, TextContent)]
            raw_mcp["error"] = " ".join(text_parts) if text_parts else "Unknown MCP error"
        else:
            raw_mcp["error"] = raw_mcp.get("error") or "Unknown MCP error"
    return _browser_call_outcome_from_mapping(
        raw_tool_name=raw_tool_name,
        source_browser_session_id=source_browser_session_id,
        source_browser_session_generation=source_browser_session_generation,
        raw_result=raw_mcp,
        error_kind="tool" if raw_result.is_error else None,
    )


def _browser_protocol_exception_outcome(
    *,
    raw_tool_name: str,
    source_browser_session_id: str | None,
    source_browser_session_generation: int = 0,
    dispatched: bool = True,
    exception: BaseException,
) -> _BrowserCallOutcome:
    try:
        detail = str(exception)
    except BaseException:  # noqa: BLE001 - exception text is an untrusted protocol boundary
        detail = ""
    return _browser_call_outcome_from_mapping(
        raw_tool_name=raw_tool_name,
        source_browser_session_id=source_browser_session_id,
        source_browser_session_generation=source_browser_session_generation,
        dispatched=dispatched,
        raw_result={},
        error_kind="protocol",
        protocol_error_detail=detail,
    )


def _not_dispatched_browser_call_outcome(
    *,
    raw_tool_name: str,
    source_browser_session_id: str | None,
    source_browser_session_generation: int,
    raw_result: dict[str, Any],
    ctx: AgentContext,
    session_loss_disposition: _BrowserSessionLossDisposition | None = None,
) -> _BrowserCallOutcome:
    return replace(
        _browser_call_outcome_from_mapping(
            raw_tool_name=raw_tool_name,
            source_browser_session_id=source_browser_session_id,
            source_browser_session_generation=source_browser_session_generation,
            dispatched=False,
            raw_result=raw_result,
            error_kind="protocol",
        ),
        session_loss_disposition=session_loss_disposition,
        session_loss_deadline_expired=ctx.browser_session_continuity_deadline_expired,
        replacement_browser_session_id=(
            ctx.browser_session_id if session_loss_disposition == "reestablished" else None
        ),
        completion_browser_session_id=ctx.browser_session_id,
        completion_browser_session_generation=ctx.browser_session_continuity_generation,
    )


def _cancelled_browser_call_outcome(
    *,
    raw_tool_name: str,
    source_browser_session_id: str | None,
    source_browser_session_generation: int,
    dispatch_started: bool,
    ctx: AgentContext,
) -> _BrowserCallOutcome:
    return replace(
        _browser_call_outcome_from_mapping(
            raw_tool_name=raw_tool_name,
            source_browser_session_id=source_browser_session_id,
            source_browser_session_generation=source_browser_session_generation,
            dispatched=dispatch_started,
            raw_result={},
            error_kind="protocol",
        ),
        cancelled=True,
        completion_browser_session_id=ctx.browser_session_id,
        completion_browser_session_generation=ctx.browser_session_continuity_generation,
    )


def _record_browser_call_outcome(
    ctx: AgentContext,
    outcome: _BrowserCallOutcome,
    *,
    call_path: Literal["model", "internal"],
) -> None:
    with contained_effect("record browser call outcome", tool_name=outcome.raw_tool_name):
        LOG.info(
            "copilot_browser_call_outcome",
            tool_name=outcome.raw_tool_name,
            call_path=call_path,
            dispatched=outcome.dispatched,
            ok=outcome.ok,
            error_kind=outcome.error_kind,
            error_code=outcome.error_code,
            cancelled=outcome.cancelled,
            source_browser_session_id=outcome.source_browser_session_id,
            source_browser_session_generation=outcome.source_browser_session_generation,
            replacement_browser_session_id=outcome.replacement_browser_session_id,
            completion_browser_session_id=outcome.completion_browser_session_id,
            completion_browser_session_generation=outcome.completion_browser_session_generation,
            evidence_drain_complete=outcome.evidence_drain_complete,
            response_truncated=outcome.response_truncated,
            payload_omitted=outcome.payload_omitted,
            **_copilot_log_fields(cast("CopilotContext", ctx)),
        )


def _scrub_browser_call_outcome(ctx: AgentContext, outcome: _BrowserCallOutcome) -> _BrowserCallOutcome:
    scrubbed = outcome.with_raw_result(scrub_model_facing_tool_result(ctx, outcome.raw_result()))
    if outcome.protocol_error_detail is None:
        return scrubbed
    detail_result = scrub_model_facing_tool_result(ctx, {"ok": False, "error": outcome.protocol_error_detail})
    detail = detail_result.get("error")
    return replace(scrubbed, protocol_error_detail=detail if isinstance(detail, str) else "")


def _project_browser_call_outcome(
    outcome: _BrowserCallOutcome,
    *,
    display_tool_name: str,
) -> dict[str, Any]:
    if outcome.protocol_error_detail is not None:
        detail = outcome.protocol_error_detail
        error = f"{display_tool_name} failed: {detail}" if detail else f"{display_tool_name} failed"
        result: dict[str, Any] = {"ok": False, "error": error}
    else:
        raw_result = outcome.raw_result()
        result = mcp_to_copilot(raw_result) if raw_result else {}
    if outcome.session_loss_disposition is not None:
        result = _browser_session_loss_result(
            result,
            disposition=outcome.session_loss_disposition,
            deadline_expired=outcome.session_loss_deadline_expired,
        )
    return result


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
    "post_run_current_page_inspection_workflow_run_id",
    "observed_browser_urls",
    "latest_recorded_build_test_outcome",
    "recorded_build_test_outcome_history",
    "recorded_persisted_block_run_workflow_run_id",
    "code_only_target_page_evidence_seen",
    "last_scout_observation_trajectory_index",
    "last_scout_observation_has_password_control",
    "last_scout_act_observe_outcome",
    "last_scout_act_observe_packet",
    "scouted_output_covered_paths",
    "scout_observed_terminal_criterion_ids",
    "scout_observation_contract",
    "pending_scout_read_expression",
    "pending_scout_read_output_path",
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
    # Appended to the tool's own description instead of replacing it, for a fact the copilot
    # owns (a policy-dependent restriction) on a tool whose documentation it does not own.
    description_suffix: str | None = None
    hide_params: frozenset[str] = frozenset()
    required_overrides: list[str] | None = None
    arg_transforms: dict[str, str] = field(default_factory=dict)
    forced_args: dict[str, Any] = field(default_factory=dict)
    # Params the copilot offers on top of the MCP tool's own schema. A hook consumes them; they are
    # stripped before the call, so the underlying tool never sees an argument it cannot accept.
    copilot_params: dict[str, Any] = field(default_factory=dict)
    requires_browser: bool = False
    redacts_sensitive_origin_structured_result: bool = False
    timeout: int | None = None
    pre_hook: PreHook | None = None
    post_hook: PostHook | None = None


LOG = structlog.get_logger()
_INTERNAL_TOOL_ARG_KEYS = frozenset({"_summarized"})
_SESSION_EXPIRED_ERROR_CODE = "SESSION_EXPIRED"
_BROWSER_GENERATION_RETIRED_ERROR_CODE = "BROWSER_GENERATION_RETIRED"
_CONTINUITY_COORDINATION_TTL = timedelta(minutes=45)
_SESSION_LOST_USER_FACING_REASON = (
    "The browser session was lost, and I couldn't re-establish it. Please retry this turn."
)
_SESSION_DEADLINE_EXPIRED_USER_FACING_REASON = (
    "The browser session reached the time limit fixed for it and was ended, and I couldn't "
    "re-establish it. Please retry this turn."
)
_FALLBACK_LOGGER = logging.getLogger(__name__)


def _reestablish_lock_seconds() -> int:
    """The re-create is bounded by the manager's own startup timeout plus the boot poll; the
    coordination lock only has to outlive that."""
    startup = app.PERSISTENT_SESSIONS_MANAGER.get_browser_session_startup_timeout_seconds()
    return int(startup + _BROWSER_BOOT_WAIT_SECONDS)


@dataclass(frozen=True)
class _BrowserSessionContinuityOutcome:
    lost_session_id: str
    root_session_id: str
    disposition: Literal["reestablished", "failed"]
    replacement_session_id: str | None
    # The session reached the deadline its infrastructure fixes rather than failing unexplained.
    deadline_expired: bool = False


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
                    blocking_timeout=_reestablish_lock_seconds() + 5,
                    timeout=_reestablish_lock_seconds() + 10,
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
        deadline_expired=data.get("deadline_expired") is True,
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
                "deadline_expired": outcome.deadline_expired,
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


def scrub_model_facing_tool_result(ctx: AgentContext, result: Any) -> dict[str, Any]:
    scrubbed_secrets = scrub_secrets_from_structure(ctx, result)
    if not isinstance(scrubbed_secrets, dict) or not _mapping_keys_preserved(result, scrubbed_secrets):
        return {}
    parameter_sets: list[dict[str, Any]] = []
    parameters = getattr(ctx, "codeblock_redaction_parameters", None)
    if isinstance(parameters, dict) and parameters:
        parameter_sets.append(parameters)
    origin_parameters = matching_origin_run_redaction_parameters(ctx)
    if origin_parameters:
        parameter_sets.append(origin_parameters)

    scrubbed = scrubbed_secrets
    for parameter_set in parameter_sets:
        candidate = app.AGENT_FUNCTION.redact_codeblock_parameter_values(scrubbed, parameter_set)
        if not isinstance(candidate, dict) or not _mapping_keys_preserved(scrubbed, candidate):
            return {}
        if type(scrubbed.get("ok")) is bool:
            candidate["ok"] = scrubbed["ok"]
        scrubbed = candidate
    return scrubbed


def _scrub_tool_exception(ctx: AgentContext, tool_name: str, exception: BaseException) -> Any:
    try:
        detail = str(exception)
    except BaseException:
        detail = ""
    error = f"{tool_name} failed: {detail}" if detail else f"{tool_name} failed"
    del exception, detail
    return scrub_model_facing_tool_result(ctx, {"ok": False, "error": error})


_MCPCallPhase = Literal["session_prepare", "context_enter", "dispatch", "evidence_drain", "context_exit"]
# The segments charged to the wall clock. ``evidence_drain`` names where a call died but is never
# charged here, so the residual stays the remainder of what the record actually reports.
_MCP_CALL_SEGMENTS: tuple[_MCPCallPhase, ...] = ("session_prepare", "context_enter", "dispatch", "context_exit")


class _PhaseClock:
    """Segments one MCP call's wall clock. Per-segment flooring keeps the residual non-negative."""

    def __init__(self) -> None:
        self._started = time.monotonic()
        self._mark = self._started
        self._bucket: _MCPCallPhase | None = None
        self._wall: int | None = None
        self._paused_at: float | None = None
        self._excluded_seconds = 0.0
        self.current: _MCPCallPhase | None = None
        self.elapsed: dict[_MCPCallPhase, int] = {}
        self.drain_ms: int | None = None

    def _charge(self, now: float) -> None:
        if self._bucket is not None:
            self.elapsed[self._bucket] = self.elapsed.get(self._bucket, 0) + int((now - self._mark) * 1000)
        self._mark = now

    def enter(self, phase: _MCPCallPhase) -> None:
        now = time.monotonic()
        if self._paused_at is not None:
            self._excluded_seconds += now - self._paused_at
            self._paused_at = None
            self._mark = now
        else:
            self._charge(now)
        self._bucket = phase
        self.current = phase

    def unwind(self) -> None:
        """Charge the teardown to ``context_exit`` while ``current`` keeps naming the segment that failed."""
        self._charge(time.monotonic())
        self._bucket = "context_exit"

    def pause(self) -> None:
        """Exclude result projection and evidence enrichment from server-call timing."""
        now = time.monotonic()
        self._charge(now)
        self._bucket = None
        self._paused_at = now

    def close(self) -> int:
        if self._wall is None:
            now = time.monotonic()
            if self._paused_at is not None:
                self._excluded_seconds += now - self._paused_at
                self._paused_at = None
                self._mark = now
            else:
                self._charge(now)
            self._bucket = None
            self._wall = int((now - self._started - self._excluded_seconds) * 1000)
        return self._wall

    def record_drain(self, started: float, *, failed: bool) -> None:
        """Settling the page runs after the wall is frozen: the caller waits for it, but it is not part of the call."""
        self.drain_ms = int((time.monotonic() - started) * 1000)
        if failed:
            self.current = "evidence_drain"

    def settle(self) -> None:
        """The call answered, so no segment holds a failure and ``timing_phase`` would misname a returned error."""
        self.current = None


def _server_mark(raw_mcp: dict[str, Any], name: str) -> int | None:
    timing = raw_mcp.get("timing_ms")
    value = timing.get(name) if isinstance(timing, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _log_mcp_timing(
    ctx: CopilotContext,
    tool_name: str,
    mcp_tool_name: str,
    phases: _PhaseClock,
    raw_mcp: dict[str, Any],
    call_path: Literal["model", "internal"],
    call_status: Literal["ok", "timeout", "error", "session_error", "cancelled", "not_connected"] = "ok",
    *,
    dispatch_started: bool | None = None,
) -> None:
    with contained_effect("emit MCP tool timing", tool_name=tool_name, call_status=call_status):
        reported = _server_mark(raw_mcp, "total")
        attach_ms = _server_mark(raw_mcp, "attach")
        # The tool's total excludes the browser attach it did before opening its timer, so the span the
        # server actually held the call is the two together.
        server_span_ms = None if reported is None else reported + (attach_ms or 0)
        wall_clock_ms = phases.close()
        spent = {phase: phases.elapsed.get(phase, 0) for phase in _MCP_CALL_SEGMENTS}
        dispatch_ms = spent["dispatch"]
        # The server measures on its own clock, so a span outside the dispatch segment cannot be
        # subtracted from it without inventing a component.
        server_fits = server_span_ms is not None and 0 <= server_span_ms <= dispatch_ms
        untimed_ms = dispatch_ms - server_span_ms if server_fits and server_span_ms is not None else None
        LOG.info(
            "MCP tool timing",
            tool_name=tool_name,
            mcp_tool_name=mcp_tool_name,
            wall_clock_ms=wall_clock_ms,
            server_timing_ms=server_span_ms,
            server_attach_ms=attach_ms,
            call_path=call_path,
            call_status=call_status,
            dispatch_started=dispatch_started,
            phase_session_prepare_ms=spent["session_prepare"],
            phase_context_enter_ms=spent["context_enter"],
            phase_dispatch_ms=dispatch_ms,
            phase_context_exit_ms=spent["context_exit"],
            post_call_evidence_drain_ms=phases.drain_ms,
            phase_residual_ms=wall_clock_ms - sum(spent.values()),
            timing_phase=phases.current if call_status != "ok" else None,
            phase_dispatch_untimed_ms=untimed_ms,
            timing_server_overrun=None if server_span_ms is None else not server_fits,
            **_copilot_log_fields(ctx),
        )


def _browser_session_loss_result(
    result: dict[str, Any],
    *,
    disposition: _BrowserSessionLossDisposition,
    deadline_expired: bool = False,
) -> dict[str, Any]:
    data = result.get("data")
    continuity = {
        "source": "direct_mcp",
        "disposition": disposition,
        "cause": (
            "fixed_deadline_expiry"
            if deadline_expired
            else "connection_generation_retired"
            if disposition == "generation_retired"
            else "unknown"
        ),
        "fresh_state_required": disposition in {"reestablished", "generation_retired"},
        "prior_action_effect": "unknown",
    }
    result_data = dict(data) if isinstance(data, dict) else {}
    result_data["browser_session_continuity"] = continuity
    if disposition == "reestablished":
        error = (
            "The browser session reached the time limit fixed for it and was ended during this "
            "operation. A fresh browser session is ready, on the same kind of time limit; inspect "
            "the page before continuing because the prior operation's effect is unknown."
            if deadline_expired
            else "The browser session was lost during this operation. A fresh browser session is ready; "
            "inspect the page before continuing because the prior operation's effect is unknown."
        )
    elif disposition == "generation_retired":
        error = (
            "The browser connection used by this operation was replaced during shared-session recovery. "
            "Inspect the current page before retrying because the prior operation's effect is unknown."
        )
    elif current_call_browser_session_override() is not None:
        # The chat's browser is fine; the one this call was aimed at is gone. Retrying the turn,
        # which is what the chat-flavoured wording asks for, would not bring it back.
        error = (
            "The browser this call targeted is no longer available, so its page cannot be observed. "
            "Continue from the evidence the run already returned, or run the blocks again."
        )
    else:
        error = _SESSION_DEADLINE_EXPIRED_USER_FACING_REASON if deadline_expired else _SESSION_LOST_USER_FACING_REASON
    return {
        **result,
        "ok": False,
        "error_code": (
            _BROWSER_GENERATION_RETIRED_ERROR_CODE
            if disposition == "generation_retired"
            else _SESSION_EXPIRED_ERROR_CODE
        ),
        "error": error,
        "data": result_data,
    }


def _browser_session_loss_blocker_signal(
    *,
    tool_name: str,
    call_path: Literal["model", "internal"],
    lost_session_id: str,
    deadline_expired: bool = False,
) -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=(
            "The browser session reached the time limit fixed for it and one replacement attempt "
            "failed. End the turn from the recorded session-loss evidence."
            if deadline_expired
            else "The browser session was confirmed lost and one replacement attempt failed. "
            "End the turn from the recorded session-loss evidence."
        ),
        user_facing_reason=(
            _SESSION_DEADLINE_EXPIRED_USER_FACING_REASON if deadline_expired else _SESSION_LOST_USER_FACING_REASON
        ),
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


async def _session_reached_its_fixed_deadline(organization_id: str, session_id: str) -> bool:
    """Whether the lost session was ended by its own time limit rather than failing unexplained.

    A session on infrastructure that pins the deadline at provisioning is killed on the clock while
    it is still healthy, so the loss the turn sees is expected and periodic rather than a browser
    fault (SKY-15044). Asked as "has that deadline passed?" rather than read off the row's status:
    the worker writes the terminal status only after tearing the provider's browser down, so an
    attach failing inside that window still finds a row marked running. The deadline is fixed when
    the browser is handed over and never moves, so it answers correctly throughout.
    """
    reached = False
    # Contained, comparison included: this only names a cause, and nothing about resolving it --
    # including a manager that answers with something uncomparable -- may stop the replacement.
    with contained_effect("resolve copilot browser session end reason", session_id=session_id):
        remaining_seconds = await app.PERSISTENT_SESSIONS_MANAGER.seconds_until_fixed_deadline(
            session_id, organization_id
        )
        reached = remaining_seconds is not None and remaining_seconds <= 0
    return reached


async def _handle_browser_session_loss(
    ctx: AgentContext,
    *,
    tool_name: str,
    call_path: Literal["model", "internal"],
    lost_session_id: str,
) -> Literal["reestablished", "failed"]:
    targeted_session_id = current_call_browser_session_override()
    if targeted_session_id is not None and lost_session_id == targeted_session_id != ctx.browser_session_id:
        # Continuity recovery closes the lost browser and rebinds the chat to a replacement, which
        # is only ever right for the chat's own session. A call targeted at another browser reports
        # that browser as gone; it must not tear it down, and must not swap the chat's out from
        # under the turn because a browser it does not own died.
        LOG.info(
            "Browser session loss on a targeted session; leaving chat continuity untouched",
            tool=tool_name,
            call_path=call_path,
        )
        return "failed"

    local_replacements = getattr(ctx, "browser_session_replacements", {})
    if lost_session_id in local_replacements:
        return "reestablished" if local_replacements[lost_session_id] is not None else "failed"

    async with _browser_session_continuity_lock(ctx.organization_id, lost_session_id):
        recorded = await _get_continuity_outcome(ctx.organization_id, lost_session_id)
        if recorded is not None:
            _apply_continuity_outcome(ctx, recorded, tool_name=tool_name, call_path=call_path)
            return recorded.disposition

        root_session_id = await _get_continuity_root(ctx.organization_id, lost_session_id)
        # Read before the close: the finalized row carries the reason, and closing it writes one.
        deadline_expired = await _session_reached_its_fixed_deadline(ctx.organization_id, lost_session_id)
        _emit_continuity_event(
            ctx,
            tool_name=tool_name,
            call_path=call_path,
            lost_session_id=lost_session_id,
            replacement_session_id=None,
            disposition="detected",
            deadline_expired=deadline_expired,
        )
        await close_browser_session_quietly(ctx.organization_id, lost_session_id)
        retire_browser_session_id(ctx, lost_session_id)

        if root_session_id is not None:
            outcome = _BrowserSessionContinuityOutcome(
                lost_session_id=lost_session_id,
                root_session_id=root_session_id,
                disposition="failed",
                replacement_session_id=None,
                deadline_expired=deadline_expired,
            )
        else:
            try:
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
                deadline_expired=deadline_expired,
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
            deadline_expired=outcome.deadline_expired,
        )
        return outcome.disposition


async def _browser_session_error_disposition(
    ctx: AgentContext,
    error: CopilotBrowserGenerationRetired | CopilotBrowserSessionUnavailable,
    *,
    tool_name: str,
    call_path: Literal["model", "internal"],
) -> _BrowserSessionLossDisposition:
    if (
        isinstance(error, CopilotBrowserGenerationRetired)
        and error.retirement_reason is BrowserRetirementReason.replacement
    ):
        return "generation_retired"
    return await _handle_browser_session_loss(
        ctx,
        tool_name=tool_name,
        call_path=call_path,
        lost_session_id=error.session_id,
    )


def _emit_continuity_event(
    ctx: AgentContext,
    *,
    tool_name: str,
    call_path: Literal["model", "internal"],
    lost_session_id: str,
    replacement_session_id: str | None,
    disposition: Literal["detected", "reestablished", "failed"],
    deadline_expired: bool,
) -> None:
    # The payload is built inside the guard too: this emit sits ahead of the recovery
    # call, so a failure anywhere in it -- not just in the sink -- would leave
    # ensure_browser_session uncalled and the disposition unrecorded.
    with contained_effect("emit copilot session continuity event", session_id=lost_session_id):
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
            "deadline_expired": deadline_expired,
            **_copilot_log_fields(cast("CopilotContext", ctx)),
        }
        log = LOG.info if disposition == "reestablished" else LOG.warning
        try:
            log("copilot_browser_session_continuity_loss", **fields)
        except Exception:
            _FALLBACK_LOGGER.warning(
                "copilot_browser_session_continuity_loss session_id=%s disposition=%s",
                lost_session_id,
                disposition,
            )


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
    ctx.browser_session_continuity_deadline_expired = outcome.deadline_expired
    if outcome.disposition == "failed":
        stash_blocker_signal(
            ctx,
            _browser_session_loss_blocker_signal(
                tool_name=tool_name,
                call_path=call_path,
                lost_session_id=outcome.lost_session_id,
                deadline_expired=outcome.deadline_expired,
            ),
        )


async def _prepare_browser_session_for_dispatch(
    ctx: AgentContext,
    *,
    tool_name: str,
    call_path: Literal["model", "internal"],
    observed_generation: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, _BrowserSessionLossDisposition | None]:
    """Verify the current session and return any error, continuity result, and fresh disposition."""
    targeted_session_id = current_call_browser_session_override()
    if targeted_session_id is not None and targeted_session_id != ctx.browser_session_id:
        # This call acts in a browser the chat does not own, so the chat's continuity is not its
        # precondition. The dispatch is the oracle for the targeted session; a dead one lands in
        # _handle_browser_session_loss, which refuses the call without disturbing the chat.
        return None, None, None
    async with _context_browser_session_recovery_lock(ctx):
        if getattr(ctx, "browser_session_continuity_generation", 0) != observed_generation:
            disposition: Literal["reestablished", "failed"] = (
                "reestablished"
                if getattr(ctx, "browser_session_continuity_disposition", None) == "reestablished"
                else "failed"
            )
            return (
                None,
                _browser_session_loss_result(
                    {},
                    disposition=disposition,
                    deadline_expired=ctx.browser_session_continuity_deadline_expired,
                ),
                disposition,
            )

        prior_session_id = ctx.browser_session_id
        if not prior_session_id:
            return await ensure_browser_session(ctx), None, None

        async with _browser_session_continuity_lock(ctx.organization_id, prior_session_id):
            recorded = await _get_continuity_outcome(ctx.organization_id, prior_session_id)
            if recorded is not None:
                _apply_continuity_outcome(ctx, recorded, tool_name=tool_name, call_path=call_path)
                return (
                    None,
                    _browser_session_loss_result(
                        {},
                        disposition=recorded.disposition,
                        deadline_expired=recorded.deadline_expired,
                    ),
                    recorded.disposition,
                )
        # The attach in mcp_browser_context is the oracle; a lost session is handled where it is discovered.
        return None, None, None


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
        **input_schema,
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
    tool_name: str = "",
) -> CallToolResult:
    if not copilot_result:
        return CallToolResult(content=[], isError=True)
    sanitized = sanitize_tool_result_for_llm(tool_name, copilot_result)
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
        *,
        ordered_allowlist: tuple[str, ...] | None = None,
        enforce_dispatch_allowlist: bool = False,
    ) -> None:
        super().__init__(use_structured_content=False)
        self._transport = transport
        self._overlays = overlays
        self._alias_map = alias_map  # copilot_name -> mcp_name
        self._reverse_alias: dict[str, str] = {v: k for k, v in alias_map.items()}
        self._ordered_allowlist = ordered_allowlist
        self._enforce_allowlist_on_dispatch = enforce_dispatch_allowlist
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

        selected_tools = raw_tools
        if self._ordered_allowlist is not None:
            raw_by_name = {tool.name: tool for tool in raw_tools}
            if len(raw_by_name) != len(raw_tools):
                raise RuntimeError("MCP transport returned duplicate tool names")
            missing = [name for name in self._ordered_allowlist if name not in raw_by_name]
            if missing:
                raise RuntimeError(f"MCP transport omitted allowed tools: {', '.join(missing)}")
            selected_tools = [raw_by_name[name] for name in self._ordered_allowlist]

        for tool in selected_tools:
            if tool.name not in self._allowlist:
                continue

            copilot_name = self._reverse_alias.get(tool.name, tool.name)

            overlay = self._overlays.get(copilot_name, SchemaOverlay())

            schema = _apply_schema_overlay(tool.inputSchema, overlay)
            schema = _requested_output_path_choices(schema, requested_output_path_choices)
            description = overlay.description or tool.description or ""
            if overlay.description_suffix:
                description = f"{description} {overlay.description_suffix}".strip()

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
        copilot_ctx = self._context_provider()
        overlay = self._overlays.get(tool_name, SchemaOverlay())
        # Bound out here so one call's browser reaches everything it touches: pre-hooks, session
        # preparation, dispatch and post-hooks. Binding only the dispatch would collect evidence
        # from one browser, act in another, then record the result from the first.
        # Resolved once. A sibling call in the same turn can record or clear the run session, so
        # resolving again for dispatch could bind the hooks to one browser and the dispatch to another.
        call_binding = (
            resolve_browser_session_binding(copilot_ctx, arguments or {}) if overlay.requires_browser else None
        )
        try:
            if overlay.requires_browser:
                async with browser_page_custody_lock(
                    copilot_ctx, session_id=call_binding.session_id_for(copilot_ctx) if call_binding else None
                ):
                    with (
                        pending_operation(f"mcp.call_tool:{tool_name}"),
                        bound_call_browser_session(call_binding.session_id_override if call_binding else None),
                    ):
                        return await self._call_tool(tool_name, arguments, meta, binding=call_binding)
            else:
                with (
                    pending_operation(f"mcp.call_tool:{tool_name}"),
                    bound_call_browser_session(call_binding.session_id_override if call_binding else None),
                ):
                    return await self._call_tool(tool_name, arguments, meta, binding=call_binding)
        except BaseException as exc:
            if not app.AGENT_FUNCTION.prepare_codeblock_control_flow_exception(exc):
                LOG.warning("MCP tool dispatch failed")
                return CallToolResult(content=[], isError=True)
            propagated_error = exc.with_traceback(None)
            # Cancellation tracebacks retain this frame. Drop every local that can
            # transitively reference parameter-bearing context before propagating it.
            del self, tool_name, arguments, meta, copilot_ctx, overlay, call_binding, exc
        raise propagated_error from None

    async def _call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
        *,
        binding: BrowserSessionBinding | None = None,
    ) -> CallToolResult:
        if not self._client:
            raise RuntimeError("Not connected — call connect() first")

        arguments = arguments or {}
        arguments = {k: v for k, v in arguments.items() if k not in _INTERNAL_TOOL_ARG_KEYS}
        copilot_ctx = self._context_provider()
        if self._enforce_allowlist_on_dispatch and tool_name not in self._alias_map:
            raise ValueError(f"MCP tool is not available on this Copilot surface: {tool_name}")
        overlay = self._overlays.get(tool_name, SchemaOverlay())
        mcp_name = self._alias_map.get(tool_name, tool_name)
        if self._enforce_allowlist_on_dispatch and mcp_name not in self._allowlist:
            raise ValueError(f"MCP tool is not available on this Copilot surface: {tool_name}")
        observed_continuity_generation = copilot_ctx.browser_session_continuity_generation
        attempt_browser_session_id = current_call_browser_session_override() or getattr(
            copilot_ctx, "browser_session_id", None
        )
        uses_shared_browser_outcome = mcp_name in _SHARED_BROWSER_OUTCOME_TOOLS
        dispatch_started = False
        phases = _PhaseClock()

        if binding is None:
            binding = resolve_browser_session_binding(copilot_ctx, arguments or {})

        if overlay.requires_browser and binding.unavailable_reason:
            # Refused before dispatch: a call aimed at the run's browser must not quietly act in
            # the chat's, where a click or a fill would be a real side effect in the wrong place.
            result = {"ok": False, "error": binding.unavailable_reason, **binding.provenance()}
            record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, result)
            return _copilot_to_call_tool_result(result, tool_name)

        policy = copilot_ctx.request_policy
        if overlay.requires_browser and isinstance(policy, RequestPolicy) and policy.raw_secret_detected:
            result = {
                "ok": False,
                "error": "A raw-secret draft cannot use browser tools. Save only the redacted draft.",
            }
            if uses_shared_browser_outcome:
                outcome = _not_dispatched_browser_call_outcome(
                    raw_tool_name=mcp_name,
                    source_browser_session_id=attempt_browser_session_id,
                    source_browser_session_generation=observed_continuity_generation,
                    raw_result=result,
                    ctx=copilot_ctx,
                )
                outcome = _scrub_browser_call_outcome(copilot_ctx, outcome)
                _record_browser_call_outcome(copilot_ctx, outcome, call_path="model")
                result = _project_browser_call_outcome(outcome, display_tool_name=tool_name)
            else:
                result = scrub_model_facing_tool_result(copilot_ctx, result)
            LOG.info("Raw-secret safety blocked MCP browser tool", tool_name=tool_name)
            record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, result)
            return _copilot_to_call_tool_result(result, tool_name)

        async def _run_pre_hook() -> CallToolResult | None:
            if overlay.pre_hook is None:
                return None
            try:
                hook_result = await overlay.pre_hook(arguments, copilot_ctx)
            except asyncio.CancelledError:
                if uses_shared_browser_outcome:
                    outcome = _cancelled_browser_call_outcome(
                        raw_tool_name=mcp_name,
                        source_browser_session_id=attempt_browser_session_id,
                        source_browser_session_generation=observed_continuity_generation,
                        dispatch_started=False,
                        ctx=copilot_ctx,
                    )
                    _record_browser_call_outcome(copilot_ctx, outcome, call_path="model")
                raise
            if hook_result is not None:
                if uses_shared_browser_outcome:
                    outcome = _not_dispatched_browser_call_outcome(
                        raw_tool_name=mcp_name,
                        source_browser_session_id=attempt_browser_session_id,
                        source_browser_session_generation=observed_continuity_generation,
                        raw_result=hook_result,
                        ctx=copilot_ctx,
                    )
                    outcome = _scrub_browser_call_outcome(copilot_ctx, outcome)
                    _record_browser_call_outcome(copilot_ctx, outcome, call_path="model")
                    hook_result = _project_browser_call_outcome(outcome, display_tool_name=tool_name)
                else:
                    hook_result = scrub_model_facing_tool_result(copilot_ctx, hook_result)
                record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, hook_result)
                return _copilot_to_call_tool_result(hook_result, tool_name)
            return None

        if not overlay.requires_browser:
            pre_hook_result = await _run_pre_hook()
            if pre_hook_result is not None:
                return pre_hook_result

        mcp_args = _transform_args(arguments, overlay)

        if overlay.requires_browser:
            phases.enter("session_prepare")
            try:
                err, continuity_result, continuity_disposition = await _prepare_browser_session_for_dispatch(
                    copilot_ctx,
                    tool_name=tool_name,
                    call_path="model",
                    observed_generation=observed_continuity_generation,
                )
            except asyncio.CancelledError:
                _log_mcp_timing(copilot_ctx, tool_name, mcp_name, phases, {}, "model", "cancelled")
                if uses_shared_browser_outcome:
                    outcome = _cancelled_browser_call_outcome(
                        raw_tool_name=mcp_name,
                        source_browser_session_id=attempt_browser_session_id,
                        source_browser_session_generation=observed_continuity_generation,
                        dispatch_started=False,
                        ctx=copilot_ctx,
                    )
                    _record_browser_call_outcome(copilot_ctx, outcome, call_path="model")
                raise
            except Exception as exc:
                _log_mcp_timing(copilot_ctx, tool_name, mcp_name, phases, {}, "model", "session_error")
                if uses_shared_browser_outcome:
                    outcome = _browser_protocol_exception_outcome(
                        raw_tool_name=mcp_name,
                        source_browser_session_id=attempt_browser_session_id,
                        source_browser_session_generation=observed_continuity_generation,
                        dispatched=False,
                        exception=exc,
                    )
                    _record_browser_call_outcome(copilot_ctx, outcome, call_path="model")
                raise
            if err:
                _log_mcp_timing(copilot_ctx, tool_name, mcp_name, phases, {}, "model", "session_error")
                if uses_shared_browser_outcome:
                    outcome = _not_dispatched_browser_call_outcome(
                        raw_tool_name=mcp_name,
                        source_browser_session_id=attempt_browser_session_id,
                        source_browser_session_generation=observed_continuity_generation,
                        raw_result=err,
                        ctx=copilot_ctx,
                    )
                    outcome = _scrub_browser_call_outcome(copilot_ctx, outcome)
                    _record_browser_call_outcome(copilot_ctx, outcome, call_path="model")
                    err = _project_browser_call_outcome(outcome, display_tool_name=tool_name)
                else:
                    err = scrub_model_facing_tool_result(copilot_ctx, err)
                record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, err)
                return _copilot_to_call_tool_result(err, tool_name)
            if continuity_result is not None:
                _log_mcp_timing(copilot_ctx, tool_name, mcp_name, phases, {}, "model", "session_error")
                if uses_shared_browser_outcome:
                    outcome = _not_dispatched_browser_call_outcome(
                        raw_tool_name=mcp_name,
                        source_browser_session_id=attempt_browser_session_id,
                        source_browser_session_generation=observed_continuity_generation,
                        raw_result=continuity_result,
                        ctx=copilot_ctx,
                        session_loss_disposition=continuity_disposition,
                    )
                    outcome = _scrub_browser_call_outcome(copilot_ctx, outcome)
                    _record_browser_call_outcome(copilot_ctx, outcome, call_path="model")
                    continuity_result = _project_browser_call_outcome(outcome, display_tool_name=tool_name)
                else:
                    continuity_result = scrub_model_facing_tool_result(copilot_ctx, continuity_result)
                record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, continuity_result)
                return _copilot_to_call_tool_result(continuity_result, tool_name)
            mcp_args["session_id"] = binding.session_id_for(copilot_ctx)
        call_browser_session_id = binding.session_id_for(copilot_ctx) if overlay.requires_browser else None
        call_browser_session_generation = copilot_ctx.browser_session_continuity_generation

        async def _project_result(
            raw_result: FastMCPCallToolResult,
            *,
            defer_evidence: bool = False,
            evidence_lock_held: bool = False,
        ) -> tuple[CallToolResult, dict[str, Any], bool, Callable[[], None]]:
            browser_outcome: _BrowserCallOutcome | None = None
            if uses_shared_browser_outcome:
                browser_outcome = _normalize_browser_call_outcome(
                    raw_tool_name=mcp_name,
                    source_browser_session_id=call_browser_session_id,
                    source_browser_session_generation=call_browser_session_generation,
                    raw_result=raw_result,
                )
                raw_mcp = browser_outcome.raw_result()
                failed = not browser_outcome.ok
            else:
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
            phases.settle()
            phases.pause()
            # Scrub before the post hook so evidence the hooks record from raw_mcp
            # (flow evidence, scout observations) is scrubbed too.
            raw_mcp = scrub_model_facing_tool_result(copilot_ctx, raw_mcp)
            session_lost = False
            error_code = browser_outcome.error_code if browser_outcome is not None else _browser_error_code(raw_mcp)
            if (
                overlay.requires_browser
                and copilot_ctx.turn_origin != TurnOrigin.runtime_self_heal
                and isinstance(call_browser_session_id, str)
                and call_browser_session_id
                and error_code == _SESSION_EXPIRED_ERROR_CODE
            ):
                session_lost = True
                try:
                    disposition = await _handle_browser_session_loss(
                        copilot_ctx,
                        tool_name=tool_name,
                        call_path="model",
                        lost_session_id=call_browser_session_id,
                    )
                except asyncio.CancelledError:
                    if browser_outcome is not None:
                        cancelled_outcome = replace(
                            browser_outcome,
                            cancelled=True,
                            ok=False,
                            error_kind="protocol",
                            completion_browser_session_id=copilot_ctx.browser_session_id,
                            completion_browser_session_generation=copilot_ctx.browser_session_continuity_generation,
                        )
                        _record_browser_call_outcome(copilot_ctx, cancelled_outcome, call_path="model")
                    raise
                if browser_outcome is not None:
                    browser_outcome = replace(
                        browser_outcome.with_raw_result(raw_mcp),
                        session_loss_disposition=disposition,
                        session_loss_deadline_expired=copilot_ctx.browser_session_continuity_deadline_expired,
                        replacement_browser_session_id=(
                            copilot_ctx.browser_session_id if disposition == "reestablished" else None
                        ),
                    )
                else:
                    copilot_result = _browser_session_loss_result(
                        mcp_to_copilot(raw_mcp),
                        disposition=disposition,
                        deadline_expired=copilot_ctx.browser_session_continuity_deadline_expired,
                    )

            if browser_outcome is not None:
                if browser_outcome.session_loss_disposition is None:
                    browser_outcome = browser_outcome.with_raw_result(raw_mcp)
                browser_outcome = replace(
                    browser_outcome,
                    completion_browser_session_id=copilot_ctx.browser_session_id,
                    completion_browser_session_generation=copilot_ctx.browser_session_continuity_generation,
                )
                copilot_result = _project_browser_call_outcome(browser_outcome, display_tool_name=tool_name)
            elif not session_lost:
                copilot_result = mcp_to_copilot(raw_mcp) if raw_mcp else {}

            if overlay.requires_browser and isinstance(copilot_result, dict):
                # Stamped before the post-hook, which is allowed to fail back to this base result: a
                # browser answer whose provenance dropped is a silent cross-browser read.
                copilot_result.update(binding.provenance())

            if overlay.post_hook and not session_lost:
                async with AsyncExitStack() as evidence_stack:
                    if not evidence_lock_held:
                        await evidence_stack.enter_async_context(browser_evidence_commit_lock(copilot_ctx))
                    base_copilot_result = deepcopy(copilot_result)
                    ctx_snapshot = _snapshot_post_hook_context(copilot_ctx)
                    try:
                        copilot_result = await overlay.post_hook(copilot_result, raw_mcp, copilot_ctx)
                    except asyncio.CancelledError:
                        _restore_post_hook_context(copilot_ctx, ctx_snapshot)
                        raise
                    except Exception:
                        # A post-hook enriches evidence only; a crash must not fail the browser action or keep partial credit.
                        _restore_post_hook_context(copilot_ctx, ctx_snapshot)
                        LOG.warning("MCP post-hook failed; returning base tool result", tool=tool_name)
                        copilot_result = base_copilot_result
                    # The last disclosure boundary, so it also runs after a crashed post-hook. It fails
                    # closed when an unexpected producer marks the exact session while an enrichment awaits.
                    sensitive_result_is_scrubbable = (
                        overlay.redacts_sensitive_origin_structured_result
                        and not sensitive_origin_page_facts_withheld(
                            copilot_ctx, getattr(copilot_ctx, "last_run_blocks_workflow_run_id", None)
                        )
                    )
                    if (
                        overlay.requires_browser
                        and sensitive_origin_page_is_tainted(copilot_ctx)
                        and not sensitive_result_is_scrubbable
                    ):
                        _restore_post_hook_context(copilot_ctx, ctx_snapshot)
                        copilot_result = {
                            "ok": False,
                            "error": SENSITIVE_ORIGIN_PAGE_ERROR,
                            **binding.provenance(),
                        }

            copilot_result = scrub_model_facing_tool_result(copilot_ctx, copilot_result)

            def _commit_evidence() -> None:
                if browser_outcome is not None:
                    _record_browser_call_outcome(copilot_ctx, browser_outcome, call_path="model")
                record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, copilot_result)
                screenshot_data = copilot_result.get("data")
                screenshot_data = screenshot_data if isinstance(screenshot_data, dict) else {}
                captured_url = screenshot_data.get("url") or screenshot_data.get("current_url")
                observation_step = copilot_result.get("observation_step")
                if observation_step is None:
                    observation_step = screenshot_data.get("observation_step")
                workflow_run_id = copilot_result.get("workflow_run_id") or screenshot_data.get("workflow_run_id")
                enqueue_screenshot_from_result(
                    copilot_ctx,
                    copilot_result,
                    provenance=ScreenshotProvenance(
                        source_tool=tool_name,
                        captured_url=captured_url if isinstance(captured_url, str) and captured_url else None,
                        observation_step=observation_step
                        if isinstance(observation_step, int) and not isinstance(observation_step, bool)
                        else None,
                        browser_session_id=call_browser_session_id,
                        workflow_run_id=workflow_run_id
                        if isinstance(workflow_run_id, str) and workflow_run_id
                        else None,
                        action_relation=ScreenshotActionRelation.TOOL_RESULT,
                    ),
                )

            if not defer_evidence:
                _commit_evidence()
            return _copilot_to_call_tool_result(copilot_result, tool_name), raw_mcp, failed, _commit_evidence

        try:
            # wait_for(timeout=None) is a plain await, so only overlays that declare an action
            # ceiling get one. Browser-state resolution has its own shared typed ceiling before
            # dispatch, where expiry can still truthfully report that no browser action started.
            if overlay.requires_browser:
                phases.enter("context_enter")
                # The snapshot and its possible rollback are one transaction against the shared
                # AgentContext. Holding this across the generation scope prevents a retired call
                # from restoring over evidence committed by a concurrent sibling call.
                async with browser_evidence_commit_lock(copilot_ctx):
                    lease_context_snapshot = _snapshot_post_hook_context(copilot_ctx)
                    # Only a call the model pointed elsewhere carries an override; the ordinary debug
                    # path resolves from the context, so a session re-established mid-dispatch applies.
                    browser_scope = (
                        mcp_browser_context(copilot_ctx, session_id_override=binding.session_id_override)
                        if binding.session_id_override
                        else mcp_browser_context(copilot_ctx)
                    )
                    try:
                        async with browser_scope:
                            pre_hook_result = await _run_pre_hook()
                            if pre_hook_result is not None:
                                return pre_hook_result
                            phases.enter("dispatch")
                            dispatch_started = True
                            try:
                                raw_result = await asyncio.wait_for(
                                    self._client.call_tool(mcp_name, mcp_args, raise_on_error=False),
                                    timeout=overlay.timeout,
                                )
                            except BaseException:
                                phases.unwind()
                                raise
                            projected_result, timing_raw_mcp, failed, commit_evidence = await _project_result(
                                raw_result,
                                defer_evidence=True,
                                evidence_lock_held=True,
                            )
                            phases.enter("context_exit")
                    except (asyncio.CancelledError, CopilotBrowserGenerationRetired):
                        _restore_post_hook_context(copilot_ctx, lease_context_snapshot)
                        raise
                    commit_evidence()
                phases.settle()
                _log_mcp_timing(
                    copilot_ctx,
                    tool_name,
                    mcp_name,
                    phases,
                    timing_raw_mcp,
                    "model",
                    "error" if failed else "ok",
                )
                return projected_result
            else:
                phases.enter("dispatch")
                dispatch_started = True
                raw_result = await asyncio.wait_for(
                    self._client.call_tool(mcp_name, mcp_args, raise_on_error=False),
                    timeout=overlay.timeout,
                )
                projected_result, timing_raw_mcp, failed, _ = await _project_result(raw_result)
                _log_mcp_timing(
                    copilot_ctx,
                    tool_name,
                    mcp_name,
                    phases,
                    timing_raw_mcp,
                    "model",
                    "error" if failed else "ok",
                )
                return projected_result
        except TimeoutError:
            LOG.warning("MCP tool call timed out", tool=tool_name, ceiling_seconds=overlay.timeout)
            _log_mcp_timing(copilot_ctx, tool_name, mcp_name, phases, {}, "model", "timeout")
            # The call is cancelled where it stands, so a tool that changes the page may already have
            # changed it. Reporting a plain failure invites a retry that acts on the page twice.
            err = {
                "ok": False,
                "error": (
                    f"{tool_name} did not answer within {overlay.timeout}s and was cancelled. "
                    "Whether it took effect is unknown; read the page before trying it again."
                ),
            }
            if uses_shared_browser_outcome:
                outcome = _browser_call_outcome_from_mapping(
                    raw_tool_name=mcp_name,
                    source_browser_session_id=call_browser_session_id,
                    source_browser_session_generation=call_browser_session_generation,
                    dispatched=dispatch_started,
                    raw_result=err,
                    error_kind="protocol",
                )
                outcome = replace(
                    outcome,
                    completion_browser_session_id=copilot_ctx.browser_session_id,
                    completion_browser_session_generation=copilot_ctx.browser_session_continuity_generation,
                )
                _record_browser_call_outcome(copilot_ctx, outcome, call_path="model")
                err = _project_browser_call_outcome(
                    _scrub_browser_call_outcome(copilot_ctx, outcome),
                    display_tool_name=tool_name,
                )
            else:
                err = scrub_model_facing_tool_result(copilot_ctx, err)
            record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, err)
            return _copilot_to_call_tool_result(err, tool_name)
        except (CopilotBrowserGenerationRetired, CopilotBrowserSessionUnavailable) as exc:
            _log_mcp_timing(copilot_ctx, tool_name, mcp_name, phases, {}, "model", "session_error")
            try:
                disposition = await _browser_session_error_disposition(
                    copilot_ctx,
                    exc,
                    tool_name=tool_name,
                    call_path="model",
                )
            except asyncio.CancelledError:
                if uses_shared_browser_outcome:
                    outcome = _cancelled_browser_call_outcome(
                        raw_tool_name=mcp_name,
                        source_browser_session_id=call_browser_session_id,
                        source_browser_session_generation=call_browser_session_generation,
                        dispatch_started=dispatch_started,
                        ctx=copilot_ctx,
                    )
                    _record_browser_call_outcome(copilot_ctx, outcome, call_path="model")
                raise
            if uses_shared_browser_outcome:
                outcome = _browser_call_outcome_from_mapping(
                    raw_tool_name=mcp_name,
                    source_browser_session_id=call_browser_session_id,
                    source_browser_session_generation=call_browser_session_generation,
                    dispatched=dispatch_started,
                    raw_result={},
                    error_kind="protocol",
                )
                outcome = replace(
                    outcome,
                    session_loss_disposition=disposition,
                    session_loss_deadline_expired=copilot_ctx.browser_session_continuity_deadline_expired,
                    replacement_browser_session_id=(
                        copilot_ctx.browser_session_id if disposition == "reestablished" else None
                    ),
                    completion_browser_session_id=copilot_ctx.browser_session_id,
                    completion_browser_session_generation=copilot_ctx.browser_session_continuity_generation,
                )
                _record_browser_call_outcome(copilot_ctx, outcome, call_path="model")
                err = _project_browser_call_outcome(
                    _scrub_browser_call_outcome(copilot_ctx, outcome),
                    display_tool_name=tool_name,
                )
            else:
                err = scrub_model_facing_tool_result(
                    copilot_ctx,
                    _browser_session_loss_result(
                        {},
                        disposition=disposition,
                        deadline_expired=copilot_ctx.browser_session_continuity_deadline_expired,
                    ),
                )
            record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, err)
            return _copilot_to_call_tool_result(err, tool_name)
        except asyncio.CancelledError:
            _log_mcp_timing(copilot_ctx, tool_name, mcp_name, phases, {}, "model", "cancelled")
            if uses_shared_browser_outcome:
                outcome = _cancelled_browser_call_outcome(
                    raw_tool_name=mcp_name,
                    source_browser_session_id=call_browser_session_id,
                    source_browser_session_generation=call_browser_session_generation,
                    dispatch_started=dispatch_started,
                    ctx=copilot_ctx,
                )
                _record_browser_call_outcome(copilot_ctx, outcome, call_path="model")
            raise
        except Exception as exc:
            LOG.warning("MCP tool call failed", tool=tool_name)
            _log_mcp_timing(copilot_ctx, tool_name, mcp_name, phases, {}, "model", "error")
            if uses_shared_browser_outcome:
                outcome = _browser_protocol_exception_outcome(
                    raw_tool_name=mcp_name,
                    source_browser_session_id=call_browser_session_id,
                    source_browser_session_generation=call_browser_session_generation,
                    dispatched=dispatch_started,
                    exception=exc,
                )
                outcome = replace(
                    outcome,
                    completion_browser_session_id=copilot_ctx.browser_session_id,
                    completion_browser_session_generation=copilot_ctx.browser_session_continuity_generation,
                )
                _record_browser_call_outcome(copilot_ctx, outcome, call_path="model")
                err = _project_browser_call_outcome(
                    _scrub_browser_call_outcome(copilot_ctx, outcome),
                    display_tool_name=tool_name,
                )
            else:
                err = _scrub_tool_exception(copilot_ctx, tool_name, exc)
            record_tool_step_result_for_ctx(copilot_ctx, tool_name, arguments, err)
            return _copilot_to_call_tool_result(err, tool_name)

    async def call_internal_tool(
        self,
        mcp_tool_name: str,
        mcp_args: dict[str, Any],
    ) -> dict[str, Any]:
        propagated_error: BaseException
        try:
            return (await self._call_internal_tool(mcp_tool_name, mcp_args)).result
        except BaseException as exc:
            if not app.AGENT_FUNCTION.prepare_codeblock_control_flow_exception(exc):
                LOG.warning("Internal MCP tool dispatch failed")
                return {}
            propagated_error = exc.with_traceback(None)
            del self, mcp_tool_name, mcp_args, exc
        raise propagated_error from None

    async def call_internal_browser_tool(
        self,
        mcp_tool_name: Literal["skyvern_evaluate", "skyvern_screenshot"],
        mcp_args: dict[str, Any],
    ) -> _InternalBrowserCallResult:
        """Typed sibling for native consumers that need browser-call provenance."""
        propagated_error: BaseException
        try:
            call = await self._call_internal_tool(mcp_tool_name, mcp_args)
            if call.browser_outcome is None:
                raise RuntimeError(f"Missing browser-call outcome for {mcp_tool_name}")
            return _InternalBrowserCallResult(result=call.result, outcome=call.browser_outcome)
        except BaseException as exc:
            if not app.AGENT_FUNCTION.prepare_codeblock_control_flow_exception(exc):
                LOG.warning("Internal MCP browser tool dispatch failed")
                raise
            propagated_error = exc.with_traceback(None)
            del self, mcp_tool_name, mcp_args, exc
        raise propagated_error from None

    async def _call_internal_tool(
        self,
        mcp_tool_name: str,
        mcp_args: dict[str, Any],
    ) -> _InternalToolCallResult:
        """Raw FastMCP call for internal copilot subsystems (discovery walker).

        Bypasses overlay hooks, loop detection, and screenshot recording —
        those are model-facing concerns. Still routes through
        ``ensure_browser_session`` and ``mcp_browser_context`` for session/auth
        scoping. Mirrors the error-handling block from ``call_tool`` so MCP-
        side validation or tool errors surface as ``ok=False`` with an
        extracted error string rather than silently defaulting to
        ``ok=True``.
        """
        phases = _PhaseClock()
        copilot_name = self._reverse_alias.get(mcp_tool_name, mcp_tool_name)
        ctx = self._context_provider()
        requested_session_id = mcp_args.get("session_id")
        if not isinstance(requested_session_id, str) or not requested_session_id:
            requested_session_id = None
        observed_continuity_generation = ctx.browser_session_continuity_generation
        call_session_override = current_call_browser_session_override()
        attempt_browser_session_id = requested_session_id or call_session_override or ctx.browser_session_id
        uses_shared_browser_outcome = mcp_tool_name in _SHARED_BROWSER_OUTCOME_TOOLS
        dispatch_started = False
        if not self._client:
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, phases, {}, "internal", "not_connected")
            result = {"ok": False, "error": "MCP client not connected"}
            if uses_shared_browser_outcome:
                outcome = _not_dispatched_browser_call_outcome(
                    raw_tool_name=mcp_tool_name,
                    source_browser_session_id=attempt_browser_session_id,
                    source_browser_session_generation=observed_continuity_generation,
                    raw_result=result,
                    ctx=ctx,
                )
                outcome = _scrub_browser_call_outcome(ctx, outcome)
                _record_browser_call_outcome(ctx, outcome, call_path="internal")
                return _InternalToolCallResult(
                    result=_project_browser_call_outcome(outcome, display_tool_name=mcp_tool_name),
                    browser_outcome=outcome,
                )
            return _InternalToolCallResult(result=scrub_model_facing_tool_result(ctx, result))
        phases.enter("session_prepare")
        try:
            err, continuity_result, continuity_disposition = await _prepare_browser_session_for_dispatch(
                ctx,
                tool_name=copilot_name,
                call_path="internal",
                observed_generation=observed_continuity_generation,
            )
        except asyncio.CancelledError:
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, phases, {}, "internal", "cancelled")
            if uses_shared_browser_outcome:
                outcome = _cancelled_browser_call_outcome(
                    raw_tool_name=mcp_tool_name,
                    source_browser_session_id=attempt_browser_session_id,
                    source_browser_session_generation=observed_continuity_generation,
                    dispatch_started=False,
                    ctx=ctx,
                )
                _record_browser_call_outcome(ctx, outcome, call_path="internal")
            raise
        except Exception as exc:
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, phases, {}, "internal", "session_error")
            if uses_shared_browser_outcome:
                outcome = _browser_protocol_exception_outcome(
                    raw_tool_name=mcp_tool_name,
                    source_browser_session_id=attempt_browser_session_id,
                    source_browser_session_generation=observed_continuity_generation,
                    dispatched=False,
                    exception=exc,
                )
                _record_browser_call_outcome(ctx, outcome, call_path="internal")
            raise
        if err:
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, phases, {}, "internal", "session_error")
            if uses_shared_browser_outcome:
                outcome = _not_dispatched_browser_call_outcome(
                    raw_tool_name=mcp_tool_name,
                    source_browser_session_id=attempt_browser_session_id,
                    source_browser_session_generation=observed_continuity_generation,
                    raw_result=err,
                    ctx=ctx,
                )
                outcome = _scrub_browser_call_outcome(ctx, outcome)
                _record_browser_call_outcome(ctx, outcome, call_path="internal")
                return _InternalToolCallResult(
                    result=_project_browser_call_outcome(outcome, display_tool_name=mcp_tool_name),
                    browser_outcome=outcome,
                )
            return _InternalToolCallResult(result=scrub_model_facing_tool_result(ctx, err))
        if continuity_result is not None:
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, phases, {}, "internal", "session_error")
            if uses_shared_browser_outcome:
                outcome = _not_dispatched_browser_call_outcome(
                    raw_tool_name=mcp_tool_name,
                    source_browser_session_id=attempt_browser_session_id,
                    source_browser_session_generation=observed_continuity_generation,
                    raw_result=continuity_result,
                    ctx=ctx,
                    session_loss_disposition=continuity_disposition,
                )
                outcome = _scrub_browser_call_outcome(ctx, outcome)
                _record_browser_call_outcome(ctx, outcome, call_path="internal")
                return _InternalToolCallResult(
                    result=_project_browser_call_outcome(outcome, display_tool_name=mcp_tool_name),
                    browser_outcome=outcome,
                )
            return _InternalToolCallResult(result=scrub_model_facing_tool_result(ctx, continuity_result))
        # An explicit session is a dispatch lease chosen before session preparation awaits. Do not
        # relabel that call with mutable ambient context if another concurrent tool replaces it.
        call_browser_session_id = requested_session_id or call_session_override or ctx.browser_session_id
        merged_args = {**mcp_args, "session_id": call_browser_session_id}
        call_browser_session_generation = ctx.browser_session_continuity_generation
        browser_outcome: _BrowserCallOutcome | None = None
        post_dispatch_status: Literal["ok", "error", "session_error"] = "ok"
        try:
            phases.enter("context_enter")
            # Only pass the override when there is one, as the model-facing path does: an
            # unconditional keyword reaches callers that resolve the session themselves.
            browser_scope = (
                mcp_browser_context(ctx, session_id_override=call_session_override)
                if call_session_override
                else mcp_browser_context(ctx)
            )
            async with browser_scope:
                phases.enter("dispatch")
                dispatch_started = True
                try:
                    raw = await self._client.call_tool(mcp_tool_name, merged_args, raise_on_error=False)
                    if uses_shared_browser_outcome:
                        browser_outcome = _normalize_browser_call_outcome(
                            raw_tool_name=mcp_tool_name,
                            source_browser_session_id=call_browser_session_id,
                            source_browser_session_generation=call_browser_session_generation,
                            raw_result=raw,
                        )
                except BaseException:
                    phases.unwind()
                    raise
                phases.enter("context_exit")
            # The evidence drain runs outside the dispatch span this record covers.
            phases.close()
            if self._evidence_candidate_origin is not None:
                drain_started = time.monotonic()
                try:
                    await asyncio.sleep(0)
                    await self._drain_evidence_candidate_response_tasks()
                except asyncio.CancelledError:
                    phases.record_drain(drain_started, failed=True)
                    if browser_outcome is not None:
                        browser_outcome = replace(
                            browser_outcome,
                            evidence_drain_complete=False,
                            cancelled=True,
                            completion_browser_session_id=ctx.browser_session_id,
                            completion_browser_session_generation=ctx.browser_session_continuity_generation,
                        )
                        _record_browser_call_outcome(ctx, browser_outcome, call_path="internal")
                    raise
                except CopilotBrowserSessionUnavailable:
                    phases.record_drain(drain_started, failed=True)
                    if browser_outcome is None:
                        raise
                    post_dispatch_status = "session_error"
                    if browser_outcome is not None:
                        browser_outcome = replace(browser_outcome, evidence_drain_complete=False)
                    LOG.warning("Internal MCP evidence drain lost its browser session", tool=mcp_tool_name)
                except Exception:
                    phases.record_drain(drain_started, failed=True)
                    if browser_outcome is None:
                        raise
                    post_dispatch_status = "error"
                    if browser_outcome is not None:
                        browser_outcome = replace(browser_outcome, evidence_drain_complete=False)
                    LOG.warning("Internal MCP evidence drain failed", tool=mcp_tool_name)
                else:
                    phases.record_drain(drain_started, failed=False)
                    if browser_outcome is not None:
                        browser_outcome = replace(browser_outcome, evidence_drain_complete=True)
        except asyncio.CancelledError:
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, phases, {}, "internal", "cancelled")
            if uses_shared_browser_outcome and browser_outcome is None:
                outcome = _cancelled_browser_call_outcome(
                    raw_tool_name=mcp_tool_name,
                    source_browser_session_id=call_browser_session_id,
                    source_browser_session_generation=call_browser_session_generation,
                    dispatch_started=dispatch_started,
                    ctx=ctx,
                )
                _record_browser_call_outcome(ctx, outcome, call_path="internal")
            raise
        except (CopilotBrowserGenerationRetired, CopilotBrowserSessionUnavailable) as exc:
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, phases, {}, "internal", "session_error")
            try:
                disposition = await _browser_session_error_disposition(
                    ctx,
                    exc,
                    tool_name=copilot_name,
                    call_path="internal",
                )
            except asyncio.CancelledError:
                if uses_shared_browser_outcome:
                    outcome = _cancelled_browser_call_outcome(
                        raw_tool_name=mcp_tool_name,
                        source_browser_session_id=call_browser_session_id,
                        source_browser_session_generation=call_browser_session_generation,
                        dispatch_started=dispatch_started,
                        ctx=ctx,
                    )
                    _record_browser_call_outcome(ctx, outcome, call_path="internal")
                raise
            if uses_shared_browser_outcome:
                outcome = _browser_call_outcome_from_mapping(
                    raw_tool_name=mcp_tool_name,
                    source_browser_session_id=call_browser_session_id,
                    source_browser_session_generation=call_browser_session_generation,
                    dispatched=dispatch_started,
                    raw_result={},
                    error_kind="protocol",
                )
                outcome = replace(
                    outcome,
                    session_loss_disposition=disposition,
                    session_loss_deadline_expired=ctx.browser_session_continuity_deadline_expired,
                    replacement_browser_session_id=ctx.browser_session_id if disposition == "reestablished" else None,
                    completion_browser_session_id=ctx.browser_session_id,
                    completion_browser_session_generation=ctx.browser_session_continuity_generation,
                )
                outcome = _scrub_browser_call_outcome(ctx, outcome)
                _record_browser_call_outcome(ctx, outcome, call_path="internal")
                return _InternalToolCallResult(
                    result=_project_browser_call_outcome(outcome, display_tool_name=mcp_tool_name),
                    browser_outcome=outcome,
                )
            return _InternalToolCallResult(
                result=scrub_model_facing_tool_result(
                    ctx,
                    _browser_session_loss_result(
                        {},
                        disposition=disposition,
                        deadline_expired=ctx.browser_session_continuity_deadline_expired,
                    ),
                )
            )
        except Exception as exc:
            LOG.warning("Internal MCP tool call failed", tool=mcp_tool_name)
            _log_mcp_timing(ctx, copilot_name, mcp_tool_name, phases, {}, "internal", "error")
            if uses_shared_browser_outcome:
                outcome = _browser_protocol_exception_outcome(
                    raw_tool_name=mcp_tool_name,
                    source_browser_session_id=call_browser_session_id,
                    source_browser_session_generation=call_browser_session_generation,
                    dispatched=dispatch_started,
                    exception=exc,
                )
                outcome = replace(
                    _scrub_browser_call_outcome(ctx, outcome),
                    completion_browser_session_id=ctx.browser_session_id,
                    completion_browser_session_generation=ctx.browser_session_continuity_generation,
                )
                _record_browser_call_outcome(ctx, outcome, call_path="internal")
                return _InternalToolCallResult(
                    result=_project_browser_call_outcome(outcome, display_tool_name=mcp_tool_name),
                    browser_outcome=outcome,
                )
            return _InternalToolCallResult(result=_scrub_tool_exception(ctx, mcp_tool_name, exc))
        if browser_outcome is not None:
            raw_mcp = browser_outcome.raw_result()
            failed = not browser_outcome.ok
        else:
            raw_mcp = dict(raw.structured_content or {})
            if raw.is_error:
                raw_mcp["ok"] = False
                if not raw.structured_content and raw.content:
                    text_parts = [c.text for c in raw.content if hasattr(c, "text")]
                    raw_mcp["error"] = " ".join(text_parts) if text_parts else "Unknown MCP error"
                else:
                    raw_mcp["error"] = raw_mcp.get("error") or "Unknown MCP error"
            failed = raw.is_error or raw_mcp.get("ok", True) is not True
        if post_dispatch_status == "ok":
            phases.settle()
        call_status = "error" if failed else post_dispatch_status
        _log_mcp_timing(ctx, copilot_name, mcp_tool_name, phases, raw_mcp, "internal", call_status)
        scrubbed = scrub_model_facing_tool_result(ctx, raw_mcp)
        error_code = browser_outcome.error_code if browser_outcome is not None else _browser_error_code(scrubbed)
        if (
            ctx.turn_origin != TurnOrigin.runtime_self_heal
            and call_browser_session_id is not None
            and error_code == _SESSION_EXPIRED_ERROR_CODE
        ):
            try:
                disposition = await _handle_browser_session_loss(
                    ctx,
                    tool_name=copilot_name,
                    call_path="internal",
                    lost_session_id=call_browser_session_id,
                )
            except asyncio.CancelledError:
                if browser_outcome is not None:
                    cancelled_outcome = replace(
                        browser_outcome,
                        cancelled=True,
                        ok=False,
                        error_kind="protocol",
                        completion_browser_session_id=ctx.browser_session_id,
                        completion_browser_session_generation=ctx.browser_session_continuity_generation,
                    )
                    _record_browser_call_outcome(ctx, cancelled_outcome, call_path="internal")
                raise
            if browser_outcome is not None:
                browser_outcome = replace(
                    browser_outcome.with_raw_result(scrubbed),
                    session_loss_disposition=disposition,
                    session_loss_deadline_expired=ctx.browser_session_continuity_deadline_expired,
                    replacement_browser_session_id=ctx.browser_session_id if disposition == "reestablished" else None,
                )
                browser_outcome = replace(
                    browser_outcome,
                    completion_browser_session_id=ctx.browser_session_id,
                    completion_browser_session_generation=ctx.browser_session_continuity_generation,
                )
                _record_browser_call_outcome(ctx, browser_outcome, call_path="internal")
                return _InternalToolCallResult(
                    result=_project_browser_call_outcome(browser_outcome, display_tool_name=mcp_tool_name),
                    browser_outcome=browser_outcome,
                )
            return _InternalToolCallResult(
                result=_browser_session_loss_result(
                    mcp_to_copilot(scrubbed),
                    disposition=disposition,
                    deadline_expired=ctx.browser_session_continuity_deadline_expired,
                )
            )
        if browser_outcome is not None:
            browser_outcome = replace(
                browser_outcome.with_raw_result(scrubbed),
                completion_browser_session_id=ctx.browser_session_id,
                completion_browser_session_generation=ctx.browser_session_continuity_generation,
            )
            _record_browser_call_outcome(ctx, browser_outcome, call_path="internal")
            return _InternalToolCallResult(
                result=_project_browser_call_outcome(browser_outcome, display_tool_name=mcp_tool_name),
                browser_outcome=browser_outcome,
            )
        return _InternalToolCallResult(result=mcp_to_copilot(scrubbed) if scrubbed else {})

    async def list_prompts(self) -> ListPromptsResult:
        return ListPromptsResult(prompts=[])

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> GetPromptResult:
        raise ValueError(f"Prompts not supported: {name}")
