"""Shared copilot runtime types and helpers."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, NotRequired, TypeAlias, TypedDict, cast

import structlog

from skyvern.cli.core.api_key_hash import hash_api_key_for_cache
from skyvern.cli.core.client import (
    get_active_api_key,
    get_skyvern,
    reset_api_key_override,
    set_api_key_override,
)
from skyvern.cli.core.result import BrowserContext as MCPBrowserContext
from skyvern.cli.core.session_manager import (
    SessionState,
    register_copilot_session,
    scoped_session,
    unregister_copilot_session,
)
from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotEntry
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence
from skyvern.forge.sdk.core import skyvern_context
from skyvern.library.skyvern_browser import SkyvernBrowser

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
    from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult
    from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget
    from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
    from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
    from skyvern.forge.sdk.copilot.turn_halt import TurnHalt
    from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream
    from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession

LOG = structlog.get_logger()

_SESSION_CLEANUP_TIMEOUT_SECONDS = 5.0
# Browser contexts can lag the persistent-session row under load; this keeps
# Copilot from handing a not-yet-attachable session to the next MCP tool.
_BROWSER_BOOT_WAIT_SECONDS = 30.0
_BROWSER_BOOT_POLL_INTERVAL_SECONDS = 0.25
_FINAL_BROWSER_SESSION_STATUSES: frozenset[str] = frozenset({"completed", "failed", "timeout"})
CodeArtifactMetadataValue: TypeAlias = (
    str | int | float | bool | None | list["CodeArtifactMetadataValue"] | dict[str, "CodeArtifactMetadataValue"]
)
CodeArtifactMetadataPayload: TypeAlias = dict[str, CodeArtifactMetadataValue]
SdkActionWorkflowRunCacheKey: TypeAlias = tuple[str, str]


def _playwright_private_impl(browser_context: object) -> object | None:
    if not hasattr(browser_context, "_impl_obj"):
        return None
    return browser_context._impl_obj  # type: ignore[attr-defined]


def _object_bool_attr(value: object | None, attr_name: str) -> bool:
    return getattr(value, attr_name, False) is True


def _browser_context_is_attachable(browser_context: object | None) -> bool:
    if browser_context is None:
        return False

    # Playwright Python has no public BrowserContext.closed flag. These private
    # attrs are a best-effort early guard; fallback defaults keep future
    # Playwright changes from breaking the public browser.is_connected probe.
    impl = _playwright_private_impl(browser_context)
    if _object_bool_attr(impl, "_close_was_called") or _object_bool_attr(impl, "_closed"):
        return False

    # Test doubles and older Playwright-like wrappers may omit the public
    # browser property. Treat that as attachable after the private close check.
    browser = getattr(browser_context, "browser", None)
    if browser is not None:
        try:
            if not browser.is_connected():
                return False
        except Exception:
            return False

    return True


def _copilot_session_can_access_localhost() -> bool:
    return settings.ENV == "local"


def _browser_session_status_is_final(status: str | None) -> bool:
    return status in _FINAL_BROWSER_SESSION_STATUSES


async def _get_persistent_browser_session(session_id: str, organization_id: str) -> PersistentBrowserSession | None:
    browser_sessions_repo = app.DATABASE.browser_sessions
    persistent_result = browser_sessions_repo.get_persistent_browser_session(session_id, organization_id)
    # Production returns an awaitable; sync test doubles keep this edge branch easy to exercise.
    if inspect.isawaitable(persistent_result):
        return await cast("Awaitable[PersistentBrowserSession | None]", persistent_result)
    return cast("PersistentBrowserSession | None", persistent_result)


@dataclass
class PendingBrowserInteractionObservation:
    tool_name: str
    url: str = ""


class ScoutedInteraction(TypedDict):
    tool_name: str
    selector: NotRequired[str]
    source_url: NotRequired[str]
    value: NotRequired[str]
    typed_value: NotRequired[str]
    key: NotRequired[str]
    typed_length: NotRequired[int]
    role: NotRequired[str]
    accessible_name: NotRequired[str]
    trajectory_index: NotRequired[int]
    # Credential fills carry references and metadata only — never secret values.
    credential_id: NotRequired[str]
    credential_field: NotRequired[str]
    credential_name: NotRequired[str]


@dataclass
class AgentContext:
    organization_id: str
    workflow_id: str
    workflow_permanent_id: str
    workflow_yaml: str
    browser_session_id: str | None
    stream: EventSourceStream
    api_key: str | None = None
    # Ephemeral carrier for SDK-action run reuse, bounded by browser sessions touched in one Copilot run.
    sdk_action_workflow_run_ids_by_browser_session: dict[SdkActionWorkflowRunCacheKey, str] = field(
        default_factory=dict
    )
    supports_vision: bool = True
    pending_screenshots: list[ScreenshotEntry] = field(default_factory=list)
    tool_activity: list[dict[str, Any]] = field(default_factory=list)
    failed_tool_step_tracker: dict[str, int] = field(default_factory=dict)
    unrecoverable_tool_error_streak_count: int = 0
    unrecoverable_tool_error_signature: str | None = None
    unrecoverable_tool_error_reason: str | None = None
    unrecoverable_tool_error_tool_name: str | None = None

    # Cross-turn agent state accumulated by tools.py as the agent runs.
    # Read back by failure_tracking / loop_detection to detect stuck loops,
    # preserve verified prefixes across partial runs, etc. All optional —
    # downstream accessors use ``getattr(ctx, name, default)`` where
    # tolerant-to-unset is the right default.
    last_requested_block_labels: list[str] = field(default_factory=list)
    last_executed_block_labels: list[str] = field(default_factory=list)
    last_frontier_start_label: str | None = None
    pending_action_sequence_fingerprint: str | None = None
    verified_block_outputs: dict[str, Any] = field(default_factory=dict)
    verified_prefix_labels: list[str] = field(default_factory=list)
    last_full_workflow_test_ok: bool = False
    last_unverified_block_labels: list[str] = field(default_factory=list)
    workflow_verification_evidence: WorkflowVerificationEvidence = field(default_factory=WorkflowVerificationEvidence)

    # Enforcement state. Set lazily by streaming_adapter, tools, and
    # failure_tracking; declared here so _check_enforcement can read them on a
    # fresh context without AttributeError.
    navigate_called: bool = False
    observation_after_navigate: bool = False
    navigate_enforcement_done: bool = False
    update_workflow_called: bool = False
    test_after_update_done: bool = False
    post_update_nudge_count: int = 0
    coverage_nudge_count: int = 0
    format_nudge_count: int = 0
    copilot_total_timeout_exceeded: bool = False
    failed_test_nudge_count: int = 0
    explore_without_workflow_nudge_count: int = 0
    null_data_streak_count: int = 0
    last_test_ok: bool | None = None
    last_test_suspicious_success: bool = False
    last_test_anti_bot: str | None = None
    last_test_failure_reason: str | None = None
    # Latest evaluated outcome-gate verdict this turn. Deliberately not reset
    # per-run: a later run that fails before verification keeps the verdict.
    last_outcome_gate_reason: str | None = None
    last_outcome_gate_workflow_run_id: str | None = None
    last_failure_category_top: str | None = None
    last_update_block_count: int | None = None
    last_failed_workflow_yaml: str | None = None
    code_only_code_schema_seen: bool = False
    code_only_target_page_evidence_seen: bool = False
    code_native_pending_capability: str | None = None
    repeated_failure_streak_count: int = 0
    repeated_failure_nudge_emitted_at_streak: int = 0
    challenge_gated_proxy_retry_count: int = 0
    last_test_non_retriable_nav_error: str | None = None
    non_retriable_nav_error_last_emitted_signature: str | None = None
    workflow_persisted: bool = False
    last_workflow: Any | None = None
    last_workflow_yaml: str | None = None
    staged_workflow_yaml: str | None = None
    staged_workflow: Any | None = None
    has_staged_proposal: bool = False
    # Prior turn's uncommitted draft; carries blocks even when the request body and canonical row are empty.
    prior_copilot_workflow_yaml: str | None = None
    canonical_was_persisted_due_to_param_change: bool = False
    allow_untested_workflow_draft: bool = False
    request_policy: RequestPolicy | None = None
    block_authoring_policy: BlockAuthoringPolicy = BlockAuthoringPolicy.STANDARD
    impose_synthesized_code_block: bool = False
    effective_workflow_proxy_location: Any | None = None

    copilot_run_start_monotonic: float | None = None

    last_good_workflow: Any | None = None
    last_good_workflow_yaml: str | None = None
    last_run_blocks_workflow_run_id: str | None = None
    last_artifact_health_blocker_reason: str | None = None
    last_artifact_health_blocker_labels: list[str] = field(default_factory=list)
    last_artifact_health_failure_classes: list[str] = field(default_factory=list)
    last_run_blocks_block_ids: list[str] = field(default_factory=list)
    last_run_blocks_block_labels: list[str] = field(default_factory=list)
    last_run_outcome: RecordedRunOutcome | None = None
    last_run_outcome_block_labels: list[str] = field(default_factory=list)
    completion_verification_result: CompletionVerificationResult | None = None
    outcome_verification_trace_snapshot: dict[str, Any] = field(default_factory=dict)
    composition_page_evidence: dict[str, Any] | None = None
    # Ordered, bounded list of typed page-evidence packets — one per page observed
    # while scouting the goal path, each tagged with how that state was reached.
    # Feeds the per-acted-page composition gate; never persisted into workflow YAML.
    flow_evidence: list[dict[str, Any]] = field(default_factory=list)
    pending_browser_interaction_observation: PendingBrowserInteractionObservation | None = None
    # In-turn side channel from workflow mutation calls: block label -> flow_evidence
    # observation step used to ground the newly authored page-acting block.
    block_observation_refs: dict[str, int] = field(default_factory=dict)
    # Raw tool input for block_observation_refs, retained only for diagnostics
    # when normalization drops malformed entries before composition validation.
    raw_block_observation_refs: object | None = None
    # Block-label keyed metadata describing authored code artifacts. This layer
    # only normalizes and carries the metadata; sufficiency checks live elsewhere.
    code_artifact_metadata: dict[str, CodeArtifactMetadataPayload] = field(default_factory=dict)
    raw_code_artifact_metadata: object | None = None
    # Hydrated at turn start from StructuredContext.observed_acted_pages; lets the
    # composition gate credit a page observed on a prior turn when this turn's
    # flow_evidence does not cover it (closes the spent-inspection-budget
    # deadlock). Each item: {url, had_bounded_schema, reached_via}.
    prior_observed_acted_pages: list[dict[str, Any]] = field(default_factory=list)
    post_budget_page_inspection_required: bool = False
    post_budget_page_inspection_url: str | None = None
    post_budget_page_inspection_run_id: str | None = None
    post_run_page_observation_tool: str | None = None
    post_run_page_observation_url: str | None = None
    post_run_page_observation_workflow_run_id: str | None = None
    post_run_page_observation_after_failed_test: bool = False
    post_run_current_page_inspection_workflow_run_id: str | None = None
    last_evaluate_actionable_signature: str | None = None
    last_evaluate_actionable_url: str | None = None
    last_auto_acted_signature: str | None = None
    observed_browser_urls: list[str] = field(default_factory=list)
    # Ephemeral within-turn scout captures; not persisted across turns.
    scouted_interactions: list[ScoutedInteraction] = field(default_factory=list)
    # Append-only, non-deduped record of the scout's interaction sequence in
    # acted order. Unlike scouted_interactions (deduped for auto-credit), this
    # preserves repeats and ordering so code_block_synthesis can emit a faithful
    # linear Playwright trajectory.
    scout_trajectory: list[ScoutedInteraction] = field(default_factory=list)
    # Latest typed reached-download target from the scout steer; the synthesizer compiles the terminal
    # expect_download step from it. Selector is the observed download link, not necessarily a trajectory click.
    reached_download_target: ReachedDownloadTarget | None = None
    synthesized_block_offered: bool = False
    # Count of times the scout-act download gate rejected a download-intent block this turn. Bounds
    # the author->scout->re-author cycle so a genuinely un-scoutable affordance halts honestly.
    download_scout_required_rejections: int = 0
    # Source page of an in-flight scout action, captured before it may navigate away.
    pending_scout_source_url: str | None = None
    pending_scout_typed_value: str | None = None
    # Exact secret strings filled into the live browser this turn (passwords,
    # call-time-minted OTP codes). Page-readback tool results are exact-string
    # scrubbed against this set before being recorded or returned to the model.
    secret_scrub_values: list[str] = field(default_factory=list)

    # Set by tool gates / loop guards / tool-side error branches when a tool
    # dispatch is blocked. The finalization shim in agent.py reads this at
    # turn end and overrides the AgentResult with a deterministic
    # product-language reply. See blocker_signal.py for the contract.
    blocker_signal: CopilotToolBlockerSignal | None = None
    turn_halt: TurnHalt | None = None
    # Most recently emitted blocker signal for the current tool output. Unlike
    # blocker_signal, this is last-wins so the activity-log projection can
    # render the current tool result from structured product text.
    latest_tool_blocker_signal: CopilotToolBlockerSignal | None = None
    tool_blocker_signals: list[CopilotToolBlockerSignal] = field(default_factory=list)


def mcp_to_copilot(mcp_result: dict[str, Any]) -> dict[str, Any]:
    """Convert an MCP result dict to the copilot {ok, data, error} format."""
    error = mcp_result.get("error")
    # Default ok=False when error is present so an upstream tool that returns
    # an error-shaped response without an explicit `ok` field doesn't produce
    # the contradictory {"ok": True, "error": "..."} envelope.
    result: dict[str, Any] = {"ok": mcp_result.get("ok", error is None)}

    data = mcp_result.get("data")
    if data is not None:
        result["data"] = data

    if error is not None:
        if isinstance(error, dict):
            # MCP error: {code, message, hint, details}
            msg = error.get("message", "Unknown error")
            hint = error.get("hint", "")
            result["error"] = f"{msg}. {hint}".strip() if hint else msg
        else:
            result["error"] = str(error)

    warnings = mcp_result.get("warnings")
    if warnings:
        result["warnings"] = warnings

    return result


@asynccontextmanager
async def mcp_browser_context(ctx: AgentContext) -> AsyncIterator[None]:
    """Push copilot browser state into the MCP session ContextVar for tool calls."""
    if not ctx.browser_session_id:
        raise RuntimeError("No browser_session_id set on agent context")
    browser_session_id = ctx.browser_session_id
    sdk_action_workflow_run_cache_key: SdkActionWorkflowRunCacheKey = (ctx.organization_id, browser_session_id)
    # Validate api_key at the boundary, before touching any backend.
    #
    # The copilot FastAPI route runs outside MCPAPIKeyMiddleware, so the CLI
    # falls back to settings.SKYVERN_API_KEY — the server default, not the
    # authenticated caller's key — unless we install set_api_key_override
    # below. Silently skipping the override when ctx.api_key is missing
    # would re-open the exact coarse-grained-auth hole the override exists
    # to close. Fail loudly instead. The copilot route is always behind
    # auth, so this is an assertion, not a runtime branch.
    if not ctx.api_key:
        LOG.warning(
            "mcp_browser_context invoked without api_key",
            session_id=browser_session_id,
            organization_id=ctx.organization_id,
        )
        raise RuntimeError("Copilot agent context missing api_key")

    browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
        session_id=browser_session_id,
        organization_id=ctx.organization_id,
    )
    if not browser_state or not _browser_context_is_attachable(browser_state.browser_context):
        # Keep the session id out of the raised message -- it can propagate
        # to LLM- or user-visible output -- but log it for operators.
        LOG.warning(
            "No browser context for copilot session",
            session_id=browser_session_id,
            organization_id=ctx.organization_id,
        )
        raise RuntimeError("No browser context for copilot session")

    override_token = set_api_key_override(ctx.api_key)
    try:
        skyvern_client = get_skyvern()
        skyvern_browser = SkyvernBrowser(
            skyvern_client,
            browser_state.browser_context,
            browser_session_id=browser_session_id,
        )
        skyvern_browser.workflow_run_id = ctx.sdk_action_workflow_run_ids_by_browser_session.get(
            sdk_action_workflow_run_cache_key
        )
        mcp_ctx = MCPBrowserContext(
            mode="cloud_session",
            session_id=browser_session_id,
            can_access_localhost=_copilot_session_can_access_localhost(),
        )
        active_key = get_active_api_key()
        state = SessionState(
            browser=skyvern_browser,
            context=mcp_ctx,
            api_key_hash=hash_api_key_for_cache(active_key) if active_key else None,
        )
        register_copilot_session(browser_session_id, state)
        try:
            async with scoped_session(state):
                yield
        finally:
            if skyvern_browser.workflow_run_id:
                ctx.sdk_action_workflow_run_ids_by_browser_session[sdk_action_workflow_run_cache_key] = (
                    skyvern_browser.workflow_run_id
                )
            else:
                ctx.sdk_action_workflow_run_ids_by_browser_session.pop(sdk_action_workflow_run_cache_key, None)
            unregister_copilot_session(browser_session_id)
    finally:
        reset_api_key_override(override_token)


async def ensure_browser_session(ctx: AgentContext) -> dict[str, Any] | None:
    """Create a browser session if needed. Returns None on success, error dict on failure."""
    if ctx.browser_session_id:
        persistent = await _get_persistent_browser_session(ctx.browser_session_id, ctx.organization_id)
        if persistent is not None and _browser_session_status_is_final(persistent.status):
            LOG.warning(
                "Supplied browser_session_id is closed or missing; auto-creating",
                session_id=ctx.browser_session_id,
                organization_id=ctx.organization_id,
                status=persistent.status,
            )
            ctx.browser_session_id = None

    if ctx.browser_session_id:
        try:
            state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                session_id=ctx.browser_session_id,
                organization_id=ctx.organization_id,
            )
            if state and _browser_context_is_attachable(state.browser_context):
                return None
            LOG.warning(
                "Supplied browser_session_id is no longer attachable; auto-creating",
                session_id=ctx.browser_session_id,
                organization_id=ctx.organization_id,
            )
        except Exception as exc:
            LOG.warning(
                "Browser state probe raised for supplied session; auto-creating",
                session_id=ctx.browser_session_id,
                organization_id=ctx.organization_id,
                error_type=type(exc).__name__,
                exc_info=True,
            )
        ctx.browser_session_id = None

    session = None
    try:
        with copilot_span("browser_session_create", data={"organization_id": ctx.organization_id}):
            session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
                organization_id=ctx.organization_id,
                timeout_minutes=30,
            )
        ctx.browser_session_id = session.persistent_browser_session_id

        # DefaultPersistentSessionsManager schedules chromium in a background
        # task and returns from create_session before browser_context is set,
        # so the next mcp_browser_context lookup raises. Wait for it.
        async with asyncio.timeout(_BROWSER_BOOT_WAIT_SECONDS):
            while True:
                state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                    session_id=ctx.browser_session_id,
                    organization_id=ctx.organization_id,
                )
                if state and _browser_context_is_attachable(state.browser_context):
                    break
                await asyncio.sleep(_BROWSER_BOOT_POLL_INTERVAL_SECONDS)

        sc = skyvern_context.current()
        if sc:
            sc.run_id = ctx.browser_session_id

        LOG.info(
            "Auto-created browser session for copilot",
            session_id=ctx.browser_session_id,
        )
        return None
    except Exception as e:
        LOG.warning("Failed to auto-create browser session", error=str(e), exc_info=True)
        # Cleanup keys off the local `session`, not ctx.browser_session_id --
        # if the failure happened between create_session returning and the
        # attribute assignment, ctx still reads None but the session is live.
        # Wrap in wait_for because create_session likely failed due to a
        # degraded session-manager backend, and close_session hitting the
        # same backend could hang the whole request if left unbounded.
        if session is not None:
            try:
                await asyncio.wait_for(
                    app.PERSISTENT_SESSIONS_MANAGER.close_session(
                        organization_id=ctx.organization_id,
                        browser_session_id=session.persistent_browser_session_id,
                    ),
                    timeout=_SESSION_CLEANUP_TIMEOUT_SECONDS,
                )
            except Exception:
                LOG.debug(
                    "Failed to clean up partial browser session",
                    session_id=session.persistent_browser_session_id,
                    exc_info=True,
                )
        ctx.browser_session_id = None
        # Detail stays in the log above (exc_info=True). The returned string
        # flows back through the tool/agent path and could end up in
        # LLM-visible or user-visible output, so strip raw exception text
        # that may carry internal URLs, paths, or backend identifiers.
        return {"ok": False, "error": "Failed to create browser session"}
