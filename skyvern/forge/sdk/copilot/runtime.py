"""Shared copilot runtime types and helpers."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Sequence
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
from skyvern.forge.sdk.copilot.output_contracts import OutputContractAdvisoryState
from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotEntry
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.turn_origin import (
    HealAdoptionFailed,
    TurnOrigin,
    is_self_heal_session_id,
    make_self_heal_session_id,
)
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence
from skyvern.forge.sdk.core import skyvern_context
from skyvern.library.skyvern_browser import SkyvernBrowser
from skyvern.webeye.browser_state import BrowserState

if TYPE_CHECKING:
    from playwright.async_api import Page

    from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
    from skyvern.forge.sdk.copilot.build_test_outcome import (
        RecordedBuildTestOutcome,
        RecordedOutcomeBindingConstraint,
        RecordedOutcomeGroundingRequirement,
    )
    from skyvern.forge.sdk.copilot.completion_criteria_store import CompletionCriteriaTurnState
    from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult
    from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext
    from skyvern.forge.sdk.copilot.output_extraction_plan import FrozenRequestedOutputExtractionCandidate
    from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget
    from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
    from skyvern.forge.sdk.copilot.result_evidence import LoadedResultCompositionEvidence, ScoutObservationContract
    from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
    from skyvern.forge.sdk.copilot.schema_incompatibility import SchemaIncompatibility
    from skyvern.forge.sdk.copilot.turn_halt import TurnHalt
    from skyvern.forge.sdk.copilot.turn_ownership import GatePrecedenceConflictEvent, TurnClaimant, TurnOwnership
    from skyvern.forge.sdk.core.event_source_stream import EventSourceStream
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
AuthorTimeGateAblationPayloadValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | None
    | Sequence["AuthorTimeGateAblationPayloadValue"]
    | dict[str, "AuthorTimeGateAblationPayloadValue"]
)
AuthorTimeGateAblationPayload: TypeAlias = dict[str, AuthorTimeGateAblationPayloadValue]
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


@dataclass(frozen=True)
class PreRunPageReference:
    text: str
    workflow_run_id: str


@dataclass(frozen=True)
class RegisteredArtifactEntry:
    artifact_id: str
    file_name: str
    parsed_text: str


@dataclass(frozen=True)
class RegisteredArtifactEvidence:
    entries: tuple[RegisteredArtifactEntry, ...]
    workflow_run_id: str


@dataclass(frozen=True)
class AuthorTimeGateAblationEvent:
    gate_id: str
    reason_code: str
    fingerprint: str
    log_only: bool
    blocked_tool: str | None = None
    payload: AuthorTimeGateAblationPayload = field(default_factory=dict)


class ScoutedInputCorrespondence(TypedDict):
    input_key: str
    matched_literal: str
    parameter_value: str
    surface: str
    transform: str
    position: int


class ScoutedInteraction(TypedDict):
    tool_name: str
    selector: NotRequired[str]
    source_url: NotRequired[str]
    value: NotRequired[str]
    # Grounded value-containment witnesses computed at the update_workflow confluence; drive
    # generator-owned templated locators. Empty/absent => literal replay.
    input_correspondences: NotRequired[list[ScoutedInputCorrespondence]]
    typed_value: NotRequired[str]
    key: NotRequired[str]
    typed_length: NotRequired[int]
    # Raw scout-typed value for run-scoped test binding, gated at capture by should_reject_type_text_value.
    # Turn-ephemeral; excluded from every persistence path (default_value promotion, typed identity, YAML).
    raw_typed_value: NotRequired[str]
    role: NotRequired[str]
    accessible_name: NotRequired[str]
    # Captured for the type_text lane only; absent on credential fills (secret-leak boundary).
    control_readonly: NotRequired[bool]
    control_disabled: NotRequired[bool]
    control_value_satisfied: NotRequired[bool]
    trajectory_index: NotRequired[int]
    carried: NotRequired[bool]
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
    turn_origin: TurnOrigin = TurnOrigin.interactive
    injected_browser_state: BrowserState | None = None
    heal_workflow_run_id: str | None = None
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
    code_authoring_guardrail_reject_count: int = 0
    last_code_authoring_reject_was_credential_priority: bool = False
    # Climbs on each click that made no verified forward progress (failed/timed-out
    # click or a hollow post-click observe); resets on verified progress.
    consecutive_no_progress_interaction_count: int = 0
    last_scout_act_observe_outcome: str | None = None
    last_scout_act_observe_packet: dict[str, Any] | None = None
    last_scout_act_observe_recapture_attempted: bool = False
    last_scout_act_observe_recapture_result: str = ""
    ambiguous_bare_selector_rescout_context_key: str | None = None
    pending_code_authoring_runtime_repair_context: CodeAuthoringRepairContext | None = None
    last_code_authoring_repair_context: CodeAuthoringRepairContext | None = None
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
    latest_recorded_build_test_outcome: RecordedBuildTestOutcome | None = None
    recorded_build_test_outcome_history: list[dict[str, object]] = field(default_factory=list)
    recorded_persisted_block_run_workflow_run_id: str | None = None
    recorded_outcome_grounding_requirement: RecordedOutcomeGroundingRequirement | None = None
    recorded_outcome_binding_constraint: RecordedOutcomeBindingConstraint | None = None
    consecutive_non_converging_repair_count: int = 0
    completion_verification_result: CompletionVerificationResult | None = None
    completion_criteria_turn_state: CompletionCriteriaTurnState | None = None
    verified_terminal_proposal_ready: bool = False
    outcome_verification_trace_snapshot: dict[str, Any] = field(default_factory=dict)
    composition_page_evidence: dict[str, Any] | None = None
    # Pre-run page state pinned at the run seam before the post-run capture overwrites the slot;
    # stamped with the graded run id so a stale prior-run pin cannot anchor the absence scan.
    pre_run_page_reference: PreRunPageReference | None = None
    # Parsed text of this run's registered download artifacts, stamped with the run id.
    registered_artifact_evidence: RegisteredArtifactEvidence | None = None
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
    prior_fill_carry: list[dict[str, str | int | bool | list[str] | None]] = field(default_factory=list)
    fill_carry_rebound_done: bool = False
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
    latest_evaluate_result_composition_steer: LoadedResultCompositionEvidence | None = None
    latest_evaluate_result_composition_signature: str | None = None
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
    # Ordered (method, receiver) browser mutations of the last successfully persisted draft's code
    # blocks; None until a persist succeeds this turn. Gates the scouted-spine under-build reject and turn-end nudge.
    persisted_draft_browser_calls: list[tuple[str, str]] | None = None
    scouted_spine_checkpoint_fired: bool = False
    # Author-time output-contract cross-turn state, keyed by the contract signature; set lazily by workflow_update.
    output_contract_pinned_block_label_by_signature: dict[str, str] = field(default_factory=dict)
    output_contract_reject_count_by_signature: dict[str, int] = field(default_factory=dict)
    output_contract_deferral_count_by_signature: dict[str, int] = field(default_factory=dict)
    runtime_output_repair_attempt_by_signature: dict[str, bool] = field(default_factory=dict)
    # Progress-gated reset ledger: the last rejected draft's structural fingerprint and
    # whether an imposition landed since, so a genuinely-changed re-attempt resets the
    # steering-reject streak instead of counting cosmetic churn toward the cap.
    output_contract_last_reject_fingerprint_by_signature: dict[str, str] = field(default_factory=dict)
    output_contract_imposed_since_last_reject_by_signature: dict[str, bool] = field(default_factory=dict)
    # Structural fingerprint captured when a structure directive was armed; a re-entry whose
    # fingerprint still matches means the directive went unconsumed (cosmetic churn), which
    # escalates the actuation lattice instead of re-arming the same directive forever.
    output_contract_armed_directive_fingerprint_by_signature: dict[str, str] = field(default_factory=dict)
    # Armed when a collapsed-spine violation cannot be split; carries split blockers and stage count to the
    # next authoring prompt, keyed by a composite {signature, label, authored-YAML hash} so a new draft re-arms.
    output_contract_spine_directive_blockers_by_attempt_key: dict[str, list[str]] = field(default_factory=dict)
    output_contract_spine_directive_stage_count_by_attempt_key: dict[str, int] = field(default_factory=dict)
    output_contract_output_owner_directive_candidates_by_signature: dict[str, list[str]] = field(default_factory=dict)
    # Two-phase advisory grant per output-contract signature (any family, gated on observable source):
    # the resolver GRANTs one adjudicating run, the run-dispatch seam CONSUMEs it, and a terminal requires
    # CONSUMED so a double preflight pass cannot burn it.
    output_contract_actuation_by_signature: dict[str, OutputContractAdvisoryState] = field(default_factory=dict)
    # Liveness gate distinct from the reject counter: actuations (directive arms) landed since the last
    # executed run, reset only by a run dispatch, so a never-converging draft still reaches arm-D in bounded steps.
    output_contract_actuation_count_by_signature: dict[str, int] = field(default_factory=dict)
    # Set when a de-click-only actuation (imposition/directive carrying the requested output paths) left the
    # spine click-only; the no-observable-source terminal fires only after such an attempt, never on a lone
    # flaky scout pass. Cleared when the spine gains a source, on imposition, or on run dispatch.
    output_contract_declick_attempted_by_signature: dict[str, bool] = field(default_factory=dict)
    # One-shot per signature: a consumed advisory run whose observed output bound no required path may
    # re-enter the ladder once before any terminal.
    output_contract_dispatch_reopened_by_signature: dict[str, bool] = field(default_factory=dict)
    # The exhaustion terminal requires this, and no rung sets it while code blocks stay on raw
    # Playwright, so that terminal is unreachable until output grounding returns.
    output_contract_page_extraction_imposed_by_signature: dict[str, bool] = field(default_factory=dict)
    # Run-output evidence recorded at the run-result seam: a dispatched run's output-contract signatures
    # mapped to their required paths (armed at seam-admit and page-source imposition), then the observed
    # result — whether the run's registered output was seen at all, and whether it covered any required path.
    output_contract_pending_run_evidence: dict[str, list[str]] = field(default_factory=dict)
    output_contract_run_output_observed_by_signature: dict[str, bool] = field(default_factory=dict)
    output_contract_run_bound_required_path_by_signature: dict[str, bool] = field(default_factory=dict)
    # Lifecycle-progress token the loop-defer choke-point snapshots on each swallowed loop signal; a second
    # swallow with no advance expires the grant into a typed terminal instead of holding to the timeout wall.
    output_contract_defer_progress_token: tuple[int, int, int, int] | None = None
    # Per tool-call latch: the imposition seam already ran the actuation ladder this call, so the shared
    # reject-counting seam does not adjudicate the same signature twice. Reset at each imposition entry.
    output_contract_bail_actuated_this_call: bool = False
    synthesized_block_offered: bool = False
    synthesized_block_offered_trajectory_len: int = 0
    synthesized_block_offered_goal_complete: bool = False
    requested_output_extraction_candidate: FrozenRequestedOutputExtractionCandidate | None = None
    # Candidate frozen by an imposition that has not been persisted yet; promoted to the committed
    # candidate only once the update it rode in on succeeds.
    pending_requested_output_extraction_candidate: FrozenRequestedOutputExtractionCandidate | None = None
    # Set by the imposition seam when a goal-complete spine is on its way into a draft; the successful update
    # promotes it to the landed latch only when the persisted draft covers the freshly scouted spine.
    pending_goal_complete_landing: bool = False
    synthesized_goal_complete_landed: bool = False
    # Imposition answered this persist attempt on a goal-complete trajectory, so the persist-seam under-build
    # guard (the fallback for a non-rewriting imposition) must not also answer for it.
    spine_imposition_owned_attempt: bool = False
    synthesized_block_reopened_after_failed_run: bool = False
    synthesized_block_reopened_for_output_coverage: bool = False
    synthesized_block_reopened_for_credential_scout: bool = False
    scouted_output_covered_paths: set[str] = field(default_factory=set)
    scout_observation_contract: ScoutObservationContract | None = None
    uncovered_output_rescout_context_key: str | None = None
    uncovered_output_rescout_steer_key: str | None = None
    credential_scout_rescout_context_key: str | None = None
    # Which requires-live-scout fields (username/password, non-empty) each scouted credential
    # carries; recorded at credential resolve time and rehydrated from FillCarry across turns.
    scouted_credential_field_inventory_by_credential_id: dict[str, frozenset[str]] = field(default_factory=dict)
    # Highest trajectory_index visible at the latest parsed evaluate observation and whether that page
    # showed a password-type control; orders page evidence against post-fill submits across evictions.
    last_scout_observation_trajectory_index: int | None = None
    last_scout_observation_has_password_control: bool = False
    # Count of times the scout-act download gate rejected a download-intent block this turn. Bounds
    # the author->scout->re-author cycle so a genuinely un-scoutable affordance halts honestly.
    download_scout_required_rejections: int = 0
    # Required parameter keys the build-test resolution seam could not bind from a user param,
    # a non-empty default, or a scout value. Reset per run; read when composing the run outcome.
    unbound_required_parameter_keys: list[str] = field(default_factory=list)
    # Source page of an in-flight scout action, captured before it may navigate away.
    pending_scout_source_url: str | None = None
    pending_scout_typed_value: str | None = None
    # (selector, role, accessible_name) read before an in-flight click that may navigate: a post-action
    # read would describe the landing element, so a navigating click's anchor is captured pre-navigation.
    pending_scout_role_name: tuple[str, str, str] | None = None
    # Selector of an in-flight click, captured pre-dispatch so a failed/timed-out click can gate a
    # settle re-perception on whether that selector still resolves to a live element.
    pending_scout_click_selector: str | None = None
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
    # Latest edited-schema-incompatibility terminal outcome, set when an edited
    # extraction_schema declares fields that map to no output the block produces.
    # Surfaced into the persisted TurnOutcome so a later turn can report it.
    latest_schema_incompatibility: SchemaIncompatibility | None = None
    author_time_gate_ablation_events: list[AuthorTimeGateAblationEvent] = field(default_factory=list)
    # Single-owner turn-precedence contract. One mechanism owns a turn's steering
    # at a time; a contradicting weaker claim is recorded here and yields.
    turn_ownership: TurnOwnership | None = None
    gate_precedence_conflict_events: list[GatePrecedenceConflictEvent] = field(default_factory=list)
    # Claimant whose owned claim stashed the current blocker_signal; the stash choke-point clears
    # it whenever the held signal changes identity, so a plain stash can never alias a stale owner.
    blocker_signal_claimant: TurnClaimant | None = None


def copilot_author_time_gate_log_only_enabled() -> bool:
    return not settings.is_cloud_environment() and settings.WORKFLOW_COPILOT_AUTHOR_TIME_GATE_LOG_ONLY


def record_author_time_gate_ablation_event(
    ctx: AgentContext,
    *,
    gate_id: str,
    reason_code: str,
    fingerprint: str,
    blocked_tool: str | None = None,
    payload: AuthorTimeGateAblationPayload | None = None,
) -> bool:
    if not copilot_author_time_gate_log_only_enabled():
        return False
    event = AuthorTimeGateAblationEvent(
        gate_id=gate_id,
        reason_code=reason_code,
        fingerprint=fingerprint,
        blocked_tool=blocked_tool,
        payload=dict(payload or {}),
        log_only=True,
    )
    ctx.author_time_gate_ablation_events.append(event)
    LOG.info(
        "copilot_author_time_gate_ablation_event",
        gate_id=event.gate_id,
        reason_code=event.reason_code,
        fingerprint=event.fingerprint,
        blocked_tool=event.blocked_tool,
        log_only=event.log_only,
        payload=event.payload,
    )
    return True


def output_contract_ladder_unresolved(ctx: AgentContext) -> bool:
    """True while an output-contract signature has a live actuation ladder — a landed actuation or a GRANTED
    advisory — that has not yet reached a typed terminal or a dispatched (CONSUMED) run. Loop and churn detectors
    defer to this state so the bounded actuation ladder, not a generic max-turn backstop, owns the turn's outcome.
    Keyed on actuation state, not the reject counter, so a bail with no live actuation path cannot defer forever;
    a CONSUMED or EXPIRED signature is resolved and re-enables the detectors."""
    resolved_states = {OutputContractAdvisoryState.CONSUMED, OutputContractAdvisoryState.EXPIRED}
    actuation_states = ctx.output_contract_actuation_by_signature
    if any(state == OutputContractAdvisoryState.GRANTED for state in actuation_states.values()):
        return True
    return any(
        int(count or 0) >= 1 and actuation_states.get(sig) not in resolved_states
        for sig, count in ctx.output_contract_actuation_count_by_signature.items()
    )


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


async def _resolve_self_heal_browser_state(ctx: AgentContext) -> tuple[str, BrowserState, Page]:
    browser_state = ctx.injected_browser_state
    if browser_state is None:
        raise HealAdoptionFailed("injected_browser_state_missing")
    if not _browser_context_is_attachable(browser_state.browser_context):
        raise HealAdoptionFailed("injected_browser_context_unusable")
    try:
        page = await browser_state.get_working_page()
    except Exception as exc:
        LOG.warning(
            "Self-heal browser adoption failed while probing working page",
            organization_id=ctx.organization_id,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise HealAdoptionFailed("injected_working_page_unavailable") from exc
    if page is None:
        raise HealAdoptionFailed("injected_working_page_unavailable")
    workflow_run_id = ctx.heal_workflow_run_id
    if not workflow_run_id:
        raise HealAdoptionFailed("self_heal_workflow_run_id_missing")
    session_id = make_self_heal_session_id(workflow_run_id)
    return session_id, browser_state, page


@asynccontextmanager
async def mcp_browser_context(ctx: AgentContext) -> AsyncIterator[None]:
    """Push copilot browser state into the MCP session ContextVar for tool calls."""
    browser_session_id = ctx.browser_session_id
    # Equality, not identity: a plain-string origin must still route to the fail-closed heal branch.
    if ctx.turn_origin != TurnOrigin.runtime_self_heal and not browser_session_id:
        raise RuntimeError("No browser_session_id set on agent context")
    if browser_session_id is None:
        # Self-heal only; always overwritten below before use. Just satisfies the
        # str-tuple typing of sdk_action_workflow_run_cache_key.
        browser_session_id = ""
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

    browser_state: BrowserState | None
    working_page: Page | None = None
    if ctx.turn_origin == TurnOrigin.runtime_self_heal:
        browser_session_id, browser_state, working_page = await _resolve_self_heal_browser_state(ctx)
        ctx.browser_session_id = browser_session_id
        sdk_action_workflow_run_cache_key = (ctx.organization_id, browser_session_id)
    else:
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
        if working_page is not None:
            # Seed the tab pin from the already-probed page (mirrors what skyvern_tab_switch
            # sets interactively) so self-heal tools land on the adopted tab instead of the
            # new SkyvernBrowser's pages[-1] fallback.
            state._active_page = working_page
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
    """Create a browser session if needed. Returns None on success, error dict on failure.

    Exception: the self-heal path raises HealAdoptionFailed instead of returning an
    error dict, so a failed adoption aborts the turn rather than degrading to a normal
    tool-level error. Callers must let it propagate.
    """
    if ctx.turn_origin == TurnOrigin.runtime_self_heal:
        browser_session_id, _, _ = await _resolve_self_heal_browser_state(ctx)
        ctx.browser_session_id = browser_session_id
        return None

    if is_self_heal_session_id(ctx.browser_session_id):
        LOG.warning(
            "Supplied self-heal browser_session_id on interactive path; auto-creating",
            session_id=ctx.browser_session_id,
            organization_id=ctx.organization_id,
        )
        ctx.browser_session_id = None

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
