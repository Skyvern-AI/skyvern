"""Shared copilot runtime types and helpers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypeAlias, TypedDict, cast

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
from skyvern.forge.sdk.copilot.budget_expiry import BudgetExpiryState
from skyvern.forge.sdk.copilot.build_test_connect_failure import (
    SUPERSEDED_BY_NEWER_TEST_REASON,
    BuildTestConnectFailure,
    build_test_connect_failure_sentence,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.screenshot_utils import PendingFrameLease, ScreenshotEntry
from skyvern.forge.sdk.copilot.secret_scrub import (
    origin_runs_bound_to_scrubber,
    register_matching_origin_run_redaction_values,
)
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.turn_origin import (
    HealAdoptionFailed,
    TurnOrigin,
    is_self_heal_session_id,
    make_self_heal_session_id,
)
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.schemas.credentials import Credential
from skyvern.library.skyvern_browser import SkyvernBrowser
from skyvern.schemas.browser_session_close import BrowserSessionCloseReason
from skyvern.webeye.browser_errors import BrowserCdpConnectionError, BrowserTargetClosedError
from skyvern.webeye.browser_retirement import BrowserOperationRejected, BrowserRetirement, BrowserRetirementReason
from skyvern.webeye.browser_state import BrowserState

if TYPE_CHECKING:
    from playwright.async_api import Page

    from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
    from skyvern.forge.sdk.copilot.build_test_outcome import (
        RecordedBuildTestOutcome,
    )
    from skyvern.forge.sdk.copilot.completion_criteria_store import CompletionCriteriaTurnState
    from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult
    from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext
    from skyvern.forge.sdk.copilot.mcp_adapter import SkyvernOverlayMCPServer
    from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
    from skyvern.forge.sdk.copilot.result_evidence import ScoutObservationContract
    from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
    from skyvern.forge.sdk.copilot.turn_halt import TurnHalt
    from skyvern.forge.sdk.core.event_source_stream import EventSourceStream
    from skyvern.forge.sdk.schemas.copilot_turn_outcome import ConnectedAccountChoice

LOG = structlog.get_logger()

# Where a planned frontier run starts its browser from; only a non-``unanchored`` start proves the
# run began in a composition state the workflow itself established.
FrontierStartProvenance = Literal["initial", "replayed", "resumed", "unanchored"]


def _immutable_redaction_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _immutable_redaction_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_immutable_redaction_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_immutable_redaction_value(item) for item in value)
    return deepcopy(value)


@dataclass(frozen=True)
class OriginRunRedactionRegistry:
    """Run-bound redaction sets for model disclosures and persisted artifacts."""

    workflow_run_id: str
    parameters: Mapping[str, Any]
    contains_sensitive_values: bool
    contains_all_sensitive_values: bool
    contains_all_static_sensitive_values: bool = True
    awaiting_runtime_secret_values: bool = False
    artifact_parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", _immutable_redaction_value(self.parameters))
        object.__setattr__(self, "artifact_parameters", _immutable_redaction_value(self.artifact_parameters))


_SESSION_CLEANUP_TIMEOUT_SECONDS = 5.0
# Browser contexts can lag the persistent-session row under load; this keeps
# Copilot from handing a not-yet-attachable session to the next MCP tool.
_BROWSER_BOOT_WAIT_SECONDS = 30.0
_BROWSER_BOOT_POLL_INTERVAL_SECONDS = 0.25
# A session this close to a deadline its infrastructure will not move is not worth attaching to:
# a vendor replacement was measured booting in 13.5s, so this leaves room to have one ready.
_FIXED_DEADLINE_REPLACEMENT_MARGIN_SECONDS = 60.0
_ABANDONED_BROWSER_STATE_RESOLVES: set[asyncio.Task[BrowserState | None]] = set()
# How long a superseded run's owner gets to drop the lease. The worker releases only after its own
# cooperative cancel check, so a cleared runnable_id is the evidence its browser work stopped.
_SUPERSEDE_RELEASE_WAIT_SECONDS = 15.0
_SUPERSEDE_RELEASE_POLL_INTERVAL_SECONDS = 0.5
# Time the typed occupied report needs to reach the user once the release wait gives up. The turn's
# own deadline must not fire inside that window, or outer cancellation replaces the typed report.
_SUPERSEDE_TERMINALIZATION_HEADROOM_SECONDS = 5.0
# WorkflowRunStatus cannot be imported here: block.py imports this module, so its own module is
# still initializing. It is a StrEnum, so its members compare equal to their string values.
_PAUSED_WORKFLOW_RUN_STATUS = "paused"


@dataclass(frozen=True)
class _ActiveMcpBrowserContext:
    task: asyncio.Task[Any]
    ctx: AgentContext
    session_id: str
    retirement: BrowserRetirement


_ACTIVE_MCP_BROWSER_CONTEXT: ContextVar[_ActiveMcpBrowserContext | None] = ContextVar(
    "active_mcp_browser_context",
    default=None,
)
CodeArtifactMetadataValue: TypeAlias = (
    str | int | float | bool | None | list["CodeArtifactMetadataValue"] | dict[str, "CodeArtifactMetadataValue"]
)
CodeArtifactMetadataPayload: TypeAlias = dict[str, CodeArtifactMetadataValue]
SdkActionWorkflowRunCacheKey: TypeAlias = tuple[str, str]


class CopilotBrowserSessionUnavailable(RuntimeError):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__("No browser context for copilot session")


class CopilotBrowserGenerationRetired(RuntimeError):
    def __init__(
        self,
        session_id: str,
        retirement_reason: BrowserRetirementReason = BrowserRetirementReason.replacement,
    ) -> None:
        self.session_id = session_id
        self.retirement_reason = retirement_reason
        detail = "ended" if retirement_reason is BrowserRetirementReason.session_ending else "was replaced"
        super().__init__(f"The browser connection used by this operation {detail}")


class CopilotBrowserLivenessUndetermined(RuntimeError):
    def __init__(self) -> None:
        # Distinct wording on purpose: _is_unrecoverable_browser_session_error reads this text, and
        # an undetermined signal must not count toward aborting the turn as session loss.
        super().__init__("Browser liveness for this copilot session could not be determined")


def _playwright_private_impl(browser_context: object) -> object | None:
    if not hasattr(browser_context, "_impl_obj"):
        return None
    return browser_context._impl_obj  # type: ignore[attr-defined]


def _object_bool_attr(value: object | None, attr_name: str) -> bool:
    return getattr(value, attr_name, False) is True


class BrowserProbeOutcome(StrEnum):
    attachable = "attachable"
    positively_unreachable = "positively_unreachable"
    could_not_determine = "could_not_determine"


def _browser_context_attachability(browser_context: object | None) -> BrowserProbeOutcome:
    if browser_context is None:
        return BrowserProbeOutcome.positively_unreachable

    # Playwright Python has no public BrowserContext.closed flag. These private
    # attrs are a best-effort early guard; fallback defaults keep future
    # Playwright changes from breaking the public browser.is_connected probe.
    impl = _playwright_private_impl(browser_context)
    if _object_bool_attr(impl, "_close_was_called") or _object_bool_attr(impl, "_closed"):
        return BrowserProbeOutcome.positively_unreachable

    # Test doubles and older Playwright-like wrappers may omit the public
    # browser property. Treat that as attachable after the private close check.
    browser = getattr(browser_context, "browser", None)
    if browser is not None:
        try:
            if not browser.is_connected():
                return BrowserProbeOutcome.positively_unreachable
        except Exception:
            # The connectivity signal itself failed. That is not an answer about the browser.
            return BrowserProbeOutcome.could_not_determine

    return BrowserProbeOutcome.attachable


def _browser_context_is_attachable(browser_context: object | None) -> bool:
    return _browser_context_attachability(browser_context) == BrowserProbeOutcome.attachable


def _copilot_session_can_access_localhost() -> bool:
    return settings.ENV == "local"


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


class ScoutedEquivalentInput(TypedDict):
    input_key: str
    parameter_value: str
    transform: str


class ScoutedInputCorrespondence(TypedDict):
    input_key: str
    matched_literal: str
    parameter_value: str
    surface: str
    transform: str
    position: int
    equivalent_inputs: NotRequired[list[ScoutedEquivalentInput]]


class ScoutedDynamicRowPeriodMatch(TypedDict):
    period: str
    selected_row_match_count: int
    row_match_count: int


class ScoutedDynamicRowEvidence(TypedDict):
    source_url: str
    target_selector: str
    row_selector: str
    row_text: str
    row_selector_count: int
    row_text_match_count: int
    period_matches: list[ScoutedDynamicRowPeriodMatch]
    selected_index: int
    evidence_fingerprint: str


class ScoutedSelectorCandidate(TypedDict):
    selector: str
    source: str
    match_count: int | None


class ScoutedInteraction(TypedDict):
    # Every field here crosses the turn boundary into persisted, model-visible context except
    # those named in context._TURN_EPHEMERAL_INTERACTION_FIELDS. A field added here that holds a
    # raw value — a literal, page text — has to be listed there too; nothing enforces the pair.
    tool_name: str
    # Set only when the action was demonstrated somewhere other than the chat's own browser, so
    # absence means the chat's browser. Without it a demonstration in the last run's browser is
    # indistinguishable from one the chat drove.
    demonstrated_browser_session_id: NotRequired[str]
    selector: NotRequired[str]
    executed_selector: NotRequired[str]
    selector_candidates: NotRequired[list[ScoutedSelectorCandidate]]
    selector_match_count: NotRequired[int]
    source_url: NotRequired[str]
    result_url: NotRequired[str]
    observed_effects: NotRequired[dict[str, bool]]
    observed_wait_ms: NotRequired[int]
    input_id: NotRequired[str]
    input_value: NotRequired[str]
    value: NotRequired[str]
    # Grounded value-containment witnesses computed at the update_workflow confluence; drive
    # generator-owned templated locators. Empty/absent => literal replay.
    input_correspondences: NotRequired[list[ScoutedInputCorrespondence]]
    dynamic_row_evidence: NotRequired[ScoutedDynamicRowEvidence]
    key: NotRequired[str]
    typed_length: NotRequired[int]
    role: NotRequired[str]
    accessible_name: NotRequired[str]
    role_name_match_count: NotRequired[int]
    # Captured for the type_text lane only; absent on credential fills (secret-leak boundary).
    control_readonly: NotRequired[bool]
    control_disabled: NotRequired[bool]
    control_value_satisfied: NotRequired[bool]
    # Exact-selector facts from an earlier bounded observation of the same page.
    # Synthesis compiles these into runtime readiness waits before replaying the
    # demonstrated action; they are evidence, not inferred failure categories.
    observed_hidden: NotRequired[bool]
    observed_disabled: NotRequired[bool]
    trajectory_index: NotRequired[int]
    observation_step: NotRequired[int]
    carried: NotRequired[bool]
    # A read the scout proved on the live page: the expression it ran and the output path the
    # value answers. Recorded so the model receives the observed read without guessing a selector.
    read_expression: NotRequired[str]
    read_output_path: NotRequired[str]
    # Whether the reader named this path or it was the only one left. A witness binds a value to a
    # path, so a read that merely inherited the path by elimination is not evidence of that path.
    read_output_path_source: NotRequired[str]
    read_result_shape: NotRequired[str]
    # The scalar the read actually returned, so a later binding can locate the element that still
    # carries it rather than re-deriving one from labels. Bounded and scalar-only; turn-ephemeral,
    # turn-ephemeral and excluded from every persistence path.
    read_result_value: NotRequired[str]
    # Set when a live scout-time count()==1 probe found the captured selector matching >1 element on its
    # source page; synthesis re-anchors or drops it rather than emitting a selector that strict-mode-fails.
    ambiguous: NotRequired[bool]
    # Credential fills carry references and metadata only — never secret values.
    credential_id: NotRequired[str]
    credential_field: NotRequired[str]
    credential_name: NotRequired[str]
    # Element identity fingerprint for credential-fill resolution: captured at fill time, attributes
    # only (never values). Enables unambiguous identification of the scouted credential element
    # across equivalent selectors (e.g., #pass vs input[type="password"]).
    element_fingerprint_id: NotRequired[str]
    element_fingerprint_name: NotRequired[str]
    element_fingerprint_type: NotRequired[str]
    element_fingerprint_placeholder: NotRequired[str]
    element_fingerprint_label: NotRequired[str]
    element_fingerprint_test_id: NotRequired[str]
    element_fingerprint_tag: NotRequired[str]
    element_fingerprint_probed: NotRequired[str]


@dataclass
class AgentContext:
    organization_id: str
    workflow_id: str
    workflow_permanent_id: str
    workflow_yaml: str
    browser_session_id: str | None
    stream: EventSourceStream
    persisted_workflow_yaml: str | None = None
    api_key: str | None = None
    turn_origin: TurnOrigin = TurnOrigin.interactive
    injected_browser_state: BrowserState | None = None
    heal_workflow_run_id: str | None = None
    # The deadline the current model stream runs under, published by the enforcement loop so a tool
    # that parks on a user decision can suspend it instead of being cancelled mid-question.
    model_stream_deadline: asyncio.Timeout | None = None
    # Build-test dispatch calls in the model response now executing, armed before any of them runs.
    # The SDK schedules them concurrently, so more than one means a sibling may hold this session.
    build_test_tool_calls_in_model_response: int = 0
    # The streaming adapter narrates any context it is handed, so the design-phase latches live here
    # rather than on the copilot subclass it is annotated for.
    design_start_emitted: bool = False
    design_end_emitted: bool = False
    # Ephemeral carrier for SDK-action run reuse, bounded by browser sessions touched in one Copilot run.
    sdk_action_workflow_run_ids_by_browser_session: dict[SdkActionWorkflowRunCacheKey, str] = field(
        default_factory=dict
    )
    browser_session_recovery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    browser_session_replacements: dict[str, str | None] = field(default_factory=dict)
    # Calls capture this before waiting for the recovery lock. A continuity recovery increments it
    # under that lock so every sibling queued against stale page state is suppressed.
    browser_session_continuity_generation: int = 0
    browser_session_continuity_disposition: str | None = None
    # Whether the session this turn lost was ended by the deadline its infrastructure fixes, rather
    # than by an unexplained browser failure. Reported to the model and the user as the cause.
    browser_session_continuity_deadline_expired: bool = False
    supports_vision: bool = True
    pending_screenshots: list[ScreenshotEntry] = field(default_factory=list)
    pending_frame_lease: PendingFrameLease | None = None
    tool_activity: list[dict[str, Any]] = field(default_factory=list)
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
    executed_block_labels: set[str] = field(default_factory=set)
    executed_block_fingerprints: dict[str, set[str]] = field(default_factory=dict)
    last_frontier_start_label: str | None = None
    verified_block_outputs: dict[str, Any] = field(default_factory=dict)
    verified_prefix_labels: list[str] = field(default_factory=list)
    # Page each verified block ended on, from the run rows the worker persisted; a block's entry is
    # the page its successor started from. The session id is the browser they were observed in,
    # because the same URL in a different browser is a different state.
    verified_prefix_block_end_urls: dict[str, str] = field(default_factory=dict)
    verified_prefix_block_end_session_id: str | None = None
    # Label of the block that ran last, which is where the browser stopped.
    verified_prefix_terminal_label: str | None = None
    # Set by the planner when it proved a resume against the browser above; the next run is
    # threaded into that browser instead of the chat's. Consumed and cleared by that run.
    frontier_resume_session_id: str | None = None
    # Where the planned run starts from, stamped once per plan and consumed by that run. Only a
    # non-``unanchored`` start can credit its labels as composition-verified.
    frontier_start_provenance: FrontierStartProvenance | None = None
    # Labels whose last successful run started from a provable composition state. Distinct from
    # verified_prefix_labels, which drives frontier advancement and must stay per-label.
    composition_verified_labels: list[str] = field(default_factory=list)
    last_full_workflow_test_ok: bool = False
    last_unverified_block_labels: list[str] = field(default_factory=list)
    workflow_verification_evidence: WorkflowVerificationEvidence = field(default_factory=WorkflowVerificationEvidence)

    # Enforcement state. Set lazily by streaming_adapter, tools, and
    # failure_tracking; declared here so enforcement_decision can read them on a
    # fresh context without AttributeError.
    navigate_called: bool = False
    observation_after_navigate: bool = False
    update_workflow_called: bool = False
    test_after_update_done: bool = False
    copilot_total_timeout_exceeded: bool = False
    copilot_turn_cancelled_iteration: int | None = None
    copilot_max_turns_exceeded: bool = False
    empty_completion: bool = False
    budget_expiry_state: BudgetExpiryState = field(default_factory=BudgetExpiryState)
    check_model_work_deadline: Callable[[], None] | None = field(default=None, repr=False)
    model_calls_this_turn: int = 0
    tool_calls_this_turn: int = 0
    enforcement_pass_count: int = 0
    pre_run_gated_output_warning_fingerprint: tuple[tuple[str, str, bool, str], ...] = ()
    last_test_ok: bool | None = None
    last_test_suspicious_success: bool = False
    last_test_anti_bot: str | None = None
    last_test_failure_reason: str | None = None
    last_failure_category_top: str | None = None
    last_update_block_count: int | None = None
    last_failed_workflow_yaml: str | None = None
    code_only_code_schema_seen: bool = False
    code_only_target_page_evidence_seen: bool = False
    code_native_pending_capability: str | None = None
    # Captures whether the latest click produced attached, hollow, or unchanged
    # post-action evidence; downstream repair reads the factual outcome.
    last_scout_act_observe_outcome: str | None = None
    last_scout_act_observe_packet: dict[str, Any] | None = None
    last_scout_act_observe_recapture_attempted: bool = False
    last_scout_act_observe_recapture_result: str = ""
    pending_code_authoring_runtime_repair_context: CodeAuthoringRepairContext | None = None
    last_code_authoring_repair_context: CodeAuthoringRepairContext | None = None
    last_test_non_retriable_nav_error: str | None = None
    last_infrastructure_tool_error: str | None = None
    workflow_persisted: bool = False
    last_workflow: Any | None = None
    last_workflow_yaml: str | None = None
    staged_workflow_yaml: str | None = None
    staged_workflow: Any | None = None
    has_staged_proposal: bool = False
    # The chat row's setting, not the turn's commit decision: the route can still refuse to apply a
    # staged draft at turn end. None on entrypoints that load no chat row.
    auto_accept: bool | None = None
    # Server-owned only: maps current raw code-block labels to opaque identities. It is never
    # serialized into the workflow YAML or model-facing repair evidence.
    runner_code_block_associations_by_label: dict[str, str] = field(default_factory=dict)
    # Prior turn's uncommitted draft; carries blocks even when the request body and canonical row are empty.
    prior_copilot_workflow_yaml: str | None = None
    canonical_was_persisted_due_to_param_change: bool = False
    allow_untested_workflow_draft: bool = False
    request_policy: RequestPolicy | None = None
    block_authoring_policy: BlockAuthoringPolicy = BlockAuthoringPolicy.STANDARD
    effective_workflow_proxy_location: Any | None = None

    copilot_run_start_monotonic: float | None = None

    last_good_workflow: Any | None = None
    last_good_workflow_yaml: str | None = None
    last_run_blocks_workflow_run_id: str | None = None
    last_run_blocks_browser_session_id: str | None = None
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
    block_run_calls_this_turn: int = 0
    completion_verification_result: CompletionVerificationResult | None = None
    completion_criteria_turn_state: CompletionCriteriaTurnState | None = None
    verified_terminal_proposal_ready: bool = False
    outcome_verification_trace_snapshot: dict[str, Any] = field(default_factory=dict)
    composition_page_evidence: dict[str, Any] | None = None
    # None means nobody has asked yet, which reads as unavailable: a challenge stays a wall until
    # something proves this deployment can clear it. The answer is keyed by the page it was resolved
    # against, because the gate behind it is a domain denylist.
    captcha_solver_available: bool | None = None
    captcha_solver_available_for_url: str | None = None
    # Pre-run page state pinned at the run seam before the post-run capture overwrites the slot;
    # stamped with the graded run id so a stale prior-run pin cannot anchor the absence scan.
    pre_run_page_reference: PreRunPageReference | None = None
    # Parsed text of this run's registered download artifacts, stamped with the run id.
    registered_artifact_evidence: RegisteredArtifactEvidence | None = None
    # Ordered, bounded list of typed page-evidence packets — one per page observed
    # while scouting the goal path, each tagged with how that state was reached.
    # Feeds the per-acted-page composition gate; never persisted into workflow YAML.
    flow_evidence: list[dict[str, Any]] = field(default_factory=list)
    # Challenge-advisory reasons already surfaced to the model this turn, so the advisory fires once.
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
    submitted_code_artifact_metadata_snapshot: Any = None
    # Hydrated at turn start from StructuredContext.observed_acted_pages; lets the
    # composition gate credit a page observed on a prior turn when this turn's
    # flow_evidence does not cover it (closes the spent-inspection-budget
    # deadlock). Each item: {url, had_bounded_schema, reached_via}.
    prior_observed_acted_pages: list[dict[str, Any]] = field(default_factory=list)
    prior_carried_trajectory: list[dict[str, str | int | bool | list[str] | None]] = field(default_factory=list)
    carried_trajectory_rebound_done: bool = False
    post_run_page_observation_tool: str | None = None
    post_run_page_observation_url: str | None = None
    post_run_page_observation_workflow_run_id: str | None = None
    post_run_page_observation_after_failed_test: bool = False
    post_run_page_observation_generation: int = 0
    post_run_current_page_inspection_workflow_run_id: str | None = None
    observed_browser_urls: list[str] = field(default_factory=list)
    # Ephemeral within-turn scout captures; not persisted across turns.
    scouted_interactions: list[ScoutedInteraction] = field(default_factory=list)
    # Append-only, non-deduped record of the scout's interaction sequence in
    # acted order. Unlike scouted_interactions (deduped for auto-credit), this
    # preserves repeats and ordering as factual model input.
    scout_trajectory: list[ScoutedInteraction] = field(default_factory=list)
    scouted_output_covered_paths: set[str] = field(default_factory=set)
    # Ids of active terminal_action completion criteria the scout has structurally reached past the
    # login prefix; releases the corresponding is_goal_complete terminal-action gate.
    scout_observed_terminal_criterion_ids: set[str] = field(default_factory=set)
    scout_observation_contract: ScoutObservationContract | None = None
    # Which requires-live-scout fields (username/password, non-empty) each scouted credential
    # carries; recorded at credential resolve time and rehydrated from FillCarry across turns.
    scouted_credential_field_inventory_by_credential_id: dict[str, frozenset[str]] = field(default_factory=dict)
    credential_fill_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serializes model-visible browser evidence commits with sensitive-run custody changes. Raw
    # browser work may overlap, but a post-hook or native credential fill owns this lock while it
    # decides whether its facts are admissible, so rollback cannot erase another call's evidence.
    browser_page_custody_locks_by_session_id: dict[str, asyncio.Lock] = field(default_factory=dict)
    browser_evidence_commit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # A tainted page cannot be released by fresh navigation while its sensitive workflow run still
    # owns the same browser. Paused runs retain this lease until a terminal result is observed.
    active_sensitive_origin_browser_session_ids: set[str] = field(default_factory=set)
    active_sensitive_origin_run_sessions: dict[str, str] = field(default_factory=dict)
    # Read once per turn: repeated fill attempts must not re-scan the org's credentials.
    org_credentials_for_turn: list[Credential] | None = None
    vault_login_uris_by_credential_id: dict[str, list[str]] = field(default_factory=dict)
    # Highest trajectory_index visible at the latest parsed evaluate observation and whether that page
    # showed a password-type control; orders page evidence against post-fill submits across evictions.
    last_scout_observation_trajectory_index: int | None = None
    last_scout_observation_has_password_control: bool = False
    # Required parameter keys the build-test resolution seam could not bind from a user param,
    # a non-empty default, or a scout value. Reset per run; read when composing the run outcome.
    unbound_required_parameter_keys: list[str] = field(default_factory=list)
    # Source page of an in-flight scout action, captured before it may navigate away.
    pending_scout_source_url: str | None = None
    pending_scout_selector_candidates: list[ScoutedSelectorCandidate] | None = None
    pending_scout_input_value: str | None = None
    # (selector, role, accessible_name) read before an in-flight click that may navigate: a post-action
    # read would describe the landing element, so a navigating click's anchor is captured pre-navigation.
    pending_scout_role_name: tuple[str, str, str] | None = None
    pending_scout_role_name_match_count: tuple[str, str, str, int] | None = None
    # Selector of an in-flight click, captured pre-dispatch so a failed/timed-out click can gate a
    # settle re-perception on whether that selector still resolves to a live element.
    pending_scout_click_selector: str | None = None
    # Browser-session download filenames snapshotted before a scout click, so the post-hook can tell
    # a download this click produced from one an earlier click left behind.
    pending_scout_download_snapshot: frozenset[str] | None = None
    # Whether the in-flight click fired a browser download, recorded by the listener armed at click
    # dispatch. Event-driven, so it holds before the session store registers the file (the store lags
    # the event by seconds, or never sees it on vendor sessions).
    pending_scout_download: bool = False
    # Removers for the download listeners armed for the in-flight click, on the clicked page and on
    # any popup it opened. Run when the click's result is consumed and again before the next click
    # arms: a listener left attached would write a later download into another click's window.
    pending_scout_download_detachers: list[Callable[[], None]] = field(default_factory=list)
    pending_scout_popup: Page | None = None
    pending_scout_popup_content_type: str | None = None
    # (selector, ambiguous) verdict from a pre-dispatch live count probe, applied to the recorded
    # interaction only when the post-action resolved selector matches the probed one.
    pending_scout_ambiguous: tuple[str, bool] | None = None
    pending_scout_selector_match_count: tuple[str, int] | None = None
    # (selector, role, accessible_name) captured pre-dispatch for an ambiguous selector only when the
    # get_by_role(role, name, exact=True) re-anchor resolves to exactly one live element on the source
    # page; a non-unique or nameless ambiguous selector leaves this None so synthesis drops the interaction.
    pending_scout_reanchor: tuple[str, str, str] | None = None
    # Source-bound row identity captured before a positional click dispatches. The post-hook consumes it
    # only for the exact selector/source pair, so navigation cannot transfer the witness to another click.
    # Expression of an in-flight evaluate, stashed pre-dispatch: the MCP response carries only the
    # result, so a post-hook that wants the expression must receive it from the invocation side.
    pending_scout_read_expression: str | None = None
    # Requested output the in-flight evaluate says it fills. Without it a read can only be attributed
    # when the turn requests exactly one output, so a multi-field request binds nothing.
    pending_scout_read_output_path: str | None = None
    # Connected overlay used by bounded pre-click evidence probes; declared so capture code accesses it
    # directly instead of silently accepting a dynamically attached dependency.
    discovery_mcp_server: SkyvernOverlayMCPServer | None = None
    # Exact secret strings filled into the live browser this turn (passwords,
    # call-time-minted OTP codes). Page-readback tool results are exact-string
    # scrubbed against this set before being recorded or returned to the model.
    secret_scrub_values: list[str] = field(default_factory=list)
    codeblock_redaction_parameters: dict[str, Any] = field(default_factory=dict)
    origin_run_redaction_registry: OriginRunRedactionRegistry | None = None
    # Browser session whose current page may contain values entered by a sensitive inline run.
    # This is mutable page custody, separate from the immutable run redaction registry: only a
    # successful fresh navigation of this exact session clears it.
    sensitive_origin_browser_session_ids: set[str] = field(default_factory=set)
    # Which run tainted which session, and which runs have had their complete registry bound to
    # the scrubber. A session's facts are disclosed only when every run that tainted it is bound,
    # because the registry describes one run while a page can hold values from several.
    sensitive_origin_run_sessions: dict[str, str] = field(default_factory=dict)
    origin_runs_bound_to_scrubber: set[str] = field(default_factory=set)

    # Set by tool gates / loop guards / tool-side error branches when a tool
    # dispatch is blocked. The finalization shim in agent.py reads this at
    # turn end and overrides the AgentResult with a deterministic
    # product-language reply. See blocker_signal.py for the contract.
    blocker_signal: CopilotToolBlockerSignal | None = None
    # Presentation-only recovery rows; authority remains in RequestPolicy.
    connected_account_recovery_choices: list[ConnectedAccountChoice] = field(default_factory=list)
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
            error_code = error.get("code")
            if isinstance(error_code, str) and error_code:
                result["error_code"] = error_code
        else:
            result["error"] = str(error)

    warnings = mcp_result.get("warnings")
    if warnings:
        result["warnings"] = warnings

    return result


_HEAL_ADOPTION_FAILURE_REASONS = frozenset(
    {
        "injected_browser_state_missing",
        "injected_browser_context_unusable",
        "injected_working_page_unavailable",
        "self_heal_workflow_run_id_missing",
    }
)


def _safe_heal_adoption_failure_reason(exc: HealAdoptionFailed) -> str:
    try:
        reason = object.__getattribute__(exc, "message")
    except BaseException:
        return "injected_browser_context_unusable"
    return (
        reason
        if type(reason) is str and reason in _HEAL_ADOPTION_FAILURE_REASONS
        else "injected_browser_context_unusable"
    )


def _redacted_heal_adoption_failure_reason(exc: HealAdoptionFailed, parameters: dict[str, Any]) -> str:
    reason = _safe_heal_adoption_failure_reason(exc)
    if not parameters:
        return reason
    try:
        candidate = app.AGENT_FUNCTION.redact_codeblock_parameter_values(reason, parameters)
        return candidate if isinstance(candidate, str) else ""
    except BaseException:
        return ""


async def _resolve_self_heal_browser_state(ctx: AgentContext) -> tuple[str, BrowserState, Page]:
    propagated_error: BaseException
    adoption_message: str | None = None
    try:
        return await _resolve_self_heal_browser_state_inner(ctx)
    except BaseException as exc:
        if type(exc) is HealAdoptionFailed:
            adoption_message = _redacted_heal_adoption_failure_reason(
                cast(HealAdoptionFailed, exc), ctx.codeblock_redaction_parameters
            )
        elif app.AGENT_FUNCTION.prepare_codeblock_control_flow_exception(exc):
            propagated_error = exc.with_traceback(None)
        else:
            adoption_message = "injected_browser_context_unusable"
        del ctx, exc
    if adoption_message is not None:
        raise HealAdoptionFailed(adoption_message) from None
    raise propagated_error from None


async def _resolve_self_heal_browser_state_inner(ctx: AgentContext) -> tuple[str, BrowserState, Page]:
    browser_state = ctx.injected_browser_state
    if browser_state is None:
        raise HealAdoptionFailed("injected_browser_state_missing")
    if not _browser_context_is_attachable(browser_state.browser_context):
        raise HealAdoptionFailed("injected_browser_context_unusable")
    try:
        page = await browser_state.get_working_page()
    except Exception:
        LOG.warning(
            "Self-heal browser adoption failed while probing working page",
            organization_id=ctx.organization_id,
        )
        adoption_failed = True
    else:
        adoption_failed = False
    if adoption_failed:
        raise HealAdoptionFailed("injected_working_page_unavailable")
    if page is None:
        raise HealAdoptionFailed("injected_working_page_unavailable")
    workflow_run_id = ctx.heal_workflow_run_id
    if not workflow_run_id:
        raise HealAdoptionFailed("self_heal_workflow_run_id_missing")
    session_id = make_self_heal_session_id(workflow_run_id)
    return session_id, browser_state, page


_CALL_BROWSER_SESSION_ID: ContextVar[str | None] = ContextVar("copilot_call_browser_session_id", default=None)


@contextmanager
def bound_call_browser_session(session_id: str | None) -> Iterator[None]:
    """Bind one tool call's browser for everything it reaches: hooks, preparation, dispatch.

    A ContextVar rather than a field on the shared context: sibling tool calls from one model turn
    run as separate tasks, so each keeps its own binding instead of reading another call's.
    """
    if session_id is None:
        yield
        return
    token = _CALL_BROWSER_SESSION_ID.set(session_id)
    try:
        yield
    finally:
        _CALL_BROWSER_SESSION_ID.reset(token)


def current_call_browser_session_override() -> str | None:
    """The browser this call was explicitly targeted at, or None when it named none."""
    return _CALL_BROWSER_SESSION_ID.get()


def effective_browser_session_id(ctx: AgentContext) -> str | None:
    """The browser the current tool call acts in — its target when it named one, else the chat's.

    Anything resolved inside a call's dynamic extent reads this: a helper that goes straight to
    ``ctx.browser_session_id`` describes the chat's page while the call acts on another.
    """
    return _CALL_BROWSER_SESSION_ID.get() or ctx.browser_session_id


SENSITIVE_ORIGIN_PAGE_ERROR = (
    "This browser page is unavailable after a run with sensitive inputs. Navigate to a specific named URL first; "
    "a successful fresh navigation makes browser inspection available again."
)
SENSITIVE_ORIGIN_ACTIVE_RUN_PAGE_ERROR = (
    "This browser page is unavailable while a run with sensitive inputs is active. Wait for that run to finish and "
    "call get_run_results; then navigate to a specific named URL to make browser inspection available again."
)


def sensitive_origin_page_is_tainted(ctx: AgentContext) -> bool:
    tainted_session_ids: set[str] = getattr(ctx, "sensitive_origin_browser_session_ids", set())
    if not tainted_session_ids:
        return False
    return effective_browser_session_id(ctx) in tainted_session_ids


def browser_page_custody_lock(ctx: AgentContext, *, session_id: str | None = None) -> asyncio.Lock:
    """Return the per-turn lock that makes sensitive-page custody and evidence commits atomic."""
    lock_key = session_id or effective_browser_session_id(ctx) or "__unbound_browser_session__"
    locks = getattr(ctx, "browser_page_custody_locks_by_session_id", None)
    if not isinstance(locks, dict):
        locks = {}
        ctx.browser_page_custody_locks_by_session_id = locks
    lock = locks.get(lock_key)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        locks[lock_key] = lock
    return lock


def browser_evidence_commit_lock(ctx: AgentContext) -> asyncio.Lock:
    lock = getattr(ctx, "browser_evidence_commit_lock", None)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        ctx.browser_evidence_commit_lock = lock
    return lock


def active_sensitive_origin_page_sessions(ctx: AgentContext) -> set[str]:
    active_session_ids = getattr(ctx, "active_sensitive_origin_browser_session_ids", None)
    if not isinstance(active_session_ids, set):
        active_session_ids = set()
        ctx.active_sensitive_origin_browser_session_ids = active_session_ids
    active_run_sessions = getattr(ctx, "active_sensitive_origin_run_sessions", None)
    if not isinstance(active_run_sessions, dict):
        active_run_sessions = {}
        ctx.active_sensitive_origin_run_sessions = active_run_sessions
    active_session_ids.update(session_id for session_id in active_run_sessions.values() if session_id)
    return active_session_ids


def register_sensitive_origin_run_lease(ctx: AgentContext, *, workflow_run_id: str, session_id: str) -> None:
    active_run_sessions = getattr(ctx, "active_sensitive_origin_run_sessions", None)
    if not isinstance(active_run_sessions, dict):
        active_run_sessions = {}
        ctx.active_sensitive_origin_run_sessions = active_run_sessions
    active_run_sessions[workflow_run_id] = session_id
    active_sensitive_origin_page_sessions(ctx).add(session_id)


def release_sensitive_origin_run_lease(ctx: AgentContext, *, workflow_run_id: str) -> None:
    active_run_sessions = getattr(ctx, "active_sensitive_origin_run_sessions", None)
    if not isinstance(active_run_sessions, dict):
        return
    released_session_id = active_run_sessions.pop(workflow_run_id, None)
    if released_session_id and released_session_id not in active_run_sessions.values():
        active_session_ids = getattr(ctx, "active_sensitive_origin_browser_session_ids", None)
        if isinstance(active_session_ids, set):
            active_session_ids.discard(released_session_id)


def sensitive_origin_page_has_active_run(ctx: AgentContext) -> bool:
    active_session_ids = active_sensitive_origin_page_sessions(ctx)
    if not active_session_ids:
        return False
    return effective_browser_session_id(ctx) in active_session_ids


def record_sensitive_origin_run_taint(ctx: AgentContext, *, workflow_run_id: str, session_id: str) -> None:
    tainted_session_ids = getattr(ctx, "sensitive_origin_browser_session_ids", None)
    if not isinstance(tainted_session_ids, set):
        tainted_session_ids = set()
        ctx.sensitive_origin_browser_session_ids = tainted_session_ids
    tainted_session_ids.add(session_id)
    run_sessions = getattr(ctx, "sensitive_origin_run_sessions", None)
    if not isinstance(run_sessions, dict):
        run_sessions = {}
        ctx.sensitive_origin_run_sessions = run_sessions
    run_sessions[workflow_run_id] = session_id


def sensitive_origin_runs_for_session(ctx: AgentContext, session_id: str | None) -> set[str]:
    run_sessions = getattr(ctx, "sensitive_origin_run_sessions", None)
    if not isinstance(run_sessions, dict) or session_id is None:
        return set()
    return {run_id for run_id, tainted_session_id in run_sessions.items() if tainted_session_id == session_id}


def sensitive_origin_page_facts_withheld(ctx: AgentContext, run_id: str | None) -> bool:
    """Whether a tainted page's structured facts stay withheld.

    Disclosure needs no run active on the page, the claimed run's complete registry bound to the
    scrubber, and the same for every other run that tainted this page: a run that ended without
    completing its registry may have left values the scrubber never saw.
    Answering False binds the claimed run's values to the scrubber as a side effect, which is what
    lets the caller scrub the facts it is about to return; do not skip or reorder the call.
    Frames are never licensed by this: pixels cannot be exact-value scrubbed.
    """
    if sensitive_origin_page_has_active_run(ctx):
        return True
    if not sensitive_origin_page_is_tainted(ctx):
        return False
    if not run_id or not register_matching_origin_run_redaction_values(ctx, run_id):
        return True
    tainting_run_ids = sensitive_origin_runs_for_session(ctx, effective_browser_session_id(ctx))
    return not tainting_run_ids <= origin_runs_bound_to_scrubber(ctx)


def clear_sensitive_origin_page_taint(ctx: AgentContext) -> None:
    session_id = effective_browser_session_id(ctx)
    active_session_ids = active_sensitive_origin_page_sessions(ctx)
    if session_id is not None and session_id not in active_session_ids:
        ctx.sensitive_origin_browser_session_ids.discard(session_id)
        run_sessions = getattr(ctx, "sensitive_origin_run_sessions", None)
        if isinstance(run_sessions, dict):
            for run_id in sensitive_origin_runs_for_session(ctx, session_id):
                run_sessions.pop(run_id, None)


async def resolve_browser_state_for_context(
    ctx: AgentContext,
    *,
    session_id: str | None = None,
) -> BrowserState | None:
    resolved_session_id = session_id if session_id is not None else _CALL_BROWSER_SESSION_ID.get()
    if resolved_session_id is None:
        resolved_session_id = ctx.browser_session_id
    if not resolved_session_id:
        return None
    if ctx.turn_origin == TurnOrigin.runtime_self_heal or is_self_heal_session_id(resolved_session_id):
        try:
            _resolved_session_id, browser_state, _ = await _resolve_self_heal_browser_state(ctx)
            if _resolved_session_id != resolved_session_id:
                LOG.info(
                    "Resolved self-heal browser session id differs from requested",
                    requested_session_id=resolved_session_id,
                    resolved_session_id=_resolved_session_id,
                    organization_id=ctx.organization_id,
                )
            return browser_state
        except HealAdoptionFailed:
            return None
    return await resolve_persistent_browser_state(
        session_id=resolved_session_id,
        organization_id=ctx.organization_id,
    )


def _abandonable_browser_state_resolve(session_id: str, organization_id: str) -> asyncio.Task[BrowserState | None]:
    """A fresh determination per caller, owned by the module rather than by the awaiting task."""
    task = asyncio.ensure_future(
        app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
            session_id=session_id,
            organization_id=organization_id,
        )
    )
    _ABANDONED_BROWSER_STATE_RESOLVES.add(task)

    def _release(finished: asyncio.Task[BrowserState | None]) -> None:
        _ABANDONED_BROWSER_STATE_RESOLVES.discard(finished)
        # Nobody is necessarily still waiting, and an abandoned determination must not surface as
        # "exception was never retrieved".
        if not finished.cancelled():
            finished.exception()

    task.add_done_callback(_release)
    return task


async def resolve_persistent_browser_state(
    *,
    session_id: str,
    organization_id: str,
) -> BrowserState | None:
    """The manager bounds this work; Copilot waits for the determination it asked for.

    Cancelling the manager's teardown-and-reattach mid-flight leaves it undone, so the next call
    repeats it from zero and a session whose determination outlasts one caller is never judged. A
    caller cancelled from outside therefore leaves the determination running: the manager
    serializes per session, so the next caller queues behind the work completing rather than
    redoing it.

    Every caller still issues its OWN lookup. A determination that is merely stuck must never be
    inherited by a later attach, which is the oracle and has to be free to answer on its own.
    """
    return await asyncio.shield(_abandonable_browser_state_resolve(session_id, organization_id))


async def close_browser_session_quietly(
    organization_id: str,
    session_id: str,
    *,
    reason: BrowserSessionCloseReason = BrowserSessionCloseReason.aborted,
) -> None:
    """Bounded: the session-manager backend is often the reason we are closing at all, so an
    unbounded close could hang the request."""
    try:
        await asyncio.wait_for(
            app.PERSISTENT_SESSIONS_MANAGER.close_session(organization_id, session_id, reason=reason),
            timeout=_SESSION_CLEANUP_TIMEOUT_SECONDS,
        )
    except Exception:
        LOG.debug("Failed to close browser session", session_id=session_id, exc_info=True)


def retire_browser_session_id(ctx: AgentContext, examined_session_id: str | None) -> None:
    """Retire an id that a completed resolve found unusable, unless a concurrent call already
    replaced it — nulling a live replacement would discard the session this exists to protect."""
    if ctx.browser_session_id == examined_session_id:
        ctx.browser_session_id = None


@asynccontextmanager
async def _mcp_browser_context_impl(
    ctx: AgentContext,
    *,
    session_id_override: str | None = None,
    resolved_browser_state: tuple[BrowserState | None] | None = None,
) -> AsyncIterator[None]:
    """Push copilot browser state into the MCP session ContextVar for tool calls.

    ``session_id_override`` carries one call's resolved browser so a tool the model targeted at the
    last run's browser resolves there, without a sibling call reading it off the shared context.
    """
    browser_session_id = session_id_override or ctx.browser_session_id
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
        browser_state = (
            resolved_browser_state[0]
            if resolved_browser_state is not None
            else await resolve_browser_state_for_context(ctx, session_id=browser_session_id)
        )
        attachability = (
            BrowserProbeOutcome.positively_unreachable
            if browser_state is None
            else _browser_context_attachability(browser_state.browser_context)
        )
        if browser_state is None or attachability != BrowserProbeOutcome.attachable:
            # Keep the session id out of the raised message -- it can propagate
            # to LLM- or user-visible output -- but log it for operators.
            retiring = attachability == BrowserProbeOutcome.positively_unreachable
            LOG.warning(
                "No browser context for copilot session",
                session_id=browser_session_id,
                organization_id=ctx.organization_id,
                attachability=attachability.value,
                # Whether the next call reuses this session or cold-boots a replacement, losing
                # whatever page state it held. The tool error alone does not say which.
                session_retired=retiring,
            )
            # A completed resolve that found nothing attachable is the positive evidence the probe
            # structurally cannot get, so this is where a dead id gets retired. An unavailable
            # connectivity signal is not that evidence, so the call fails but the id survives.
            if retiring:
                retire_browser_session_id(ctx, browser_session_id)
                raise CopilotBrowserSessionUnavailable(browser_session_id)
            raise CopilotBrowserLivenessUndetermined()

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
            organization_id=ctx.organization_id,
        )
        if working_page is not None:
            # Seed the tab pin from the already-probed page (mirrors what skyvern_tab_switch
            # sets interactively) so self-heal tools land on the adopted tab instead of the
            # new SkyvernBrowser's pages[-1] fallback.
            state._active_page = working_page
        register_copilot_session(browser_session_id, state, organization_id=ctx.organization_id)
        if is_self_heal_session_id(browser_session_id):
            LOG.info(
                "registered self-heal browser session",
                session_id=browser_session_id,
                organization_id=ctx.organization_id,
            )
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
            unregister_copilot_session(
                browser_session_id,
                organization_id=ctx.organization_id,
                expected_state=state,
            )
            if is_self_heal_session_id(browser_session_id):
                LOG.info("unregistered self-heal browser session", session_id=browser_session_id)
    finally:
        reset_api_key_override(override_token)


def _raise_if_browser_generation_retired(
    operation_task: asyncio.Task[Any],
    retirement: BrowserRetirement,
    session_id: str,
) -> None:
    if not retirement.started.is_set():
        return
    retirement.consume_cancellation(operation_task)
    if operation_task.cancelling():
        raise asyncio.CancelledError
    raise CopilotBrowserGenerationRetired(
        session_id,
        retirement.reason or BrowserRetirementReason.replacement,
    )


@asynccontextmanager
async def mcp_browser_context(ctx: AgentContext, *, session_id_override: str | None = None) -> AsyncIterator[None]:
    """Run one MCP browser dispatch on one admitted persistent-session generation."""
    browser_session_id = session_id_override or ctx.browser_session_id
    operation_task = asyncio.current_task()
    active_context = _ACTIVE_MCP_BROWSER_CONTEXT.get()
    if (
        active_context is not None
        and operation_task is active_context.task
        and ctx is active_context.ctx
        and browser_session_id == active_context.session_id
    ):
        _raise_if_browser_generation_retired(
            active_context.task,
            active_context.retirement,
            active_context.session_id,
        )
        # Browser pre-hooks use an internal MCP read in the same task. The outer scope already
        # owns the exact generation, API-key override, SessionState, and process registration.
        # Re-entering those owners would let the inner exit unregister the real action.
        yield
        _raise_if_browser_generation_retired(
            active_context.task,
            active_context.retirement,
            active_context.session_id,
        )
        return
    if ctx.turn_origin == TurnOrigin.runtime_self_heal or not browser_session_id or not ctx.api_key:
        async with _mcp_browser_context_impl(ctx, session_id_override=session_id_override):
            yield
        return

    browser_state = await resolve_browser_state_for_context(ctx, session_id=browser_session_id)
    if browser_state is None:
        async with _mcp_browser_context_impl(
            ctx,
            session_id_override=browser_session_id,
            resolved_browser_state=(None,),
        ):
            yield
        return

    async with app.PERSISTENT_SESSIONS_MANAGER.browser_operation(
        browser_session_id,
        browser_state,
    ) as operation:
        if operation is None:
            raise CopilotBrowserGenerationRetired(
                browser_session_id,
                BrowserRetirementReason.session_ending,
            )
        if isinstance(operation, BrowserOperationRejected):
            raise CopilotBrowserGenerationRetired(browser_session_id, operation.reason)
        if operation_task is None:
            raise RuntimeError("A Copilot browser context requires an asyncio task")
        scope_token = _ACTIVE_MCP_BROWSER_CONTEXT.set(
            _ActiveMcpBrowserContext(
                task=operation_task,
                ctx=ctx,
                session_id=browser_session_id,
                retirement=operation.retirement,
            )
        )
        try:
            try:
                async with _mcp_browser_context_impl(
                    ctx,
                    session_id_override=browser_session_id,
                    resolved_browser_state=(operation.browser_state,),
                ):
                    yield
            except asyncio.CancelledError:
                if not operation.retirement_started.is_set():
                    raise
                _raise_if_browser_generation_retired(operation_task, operation.retirement, browser_session_id)
            _raise_if_browser_generation_retired(operation_task, operation.retirement, browser_session_id)
        finally:
            _ACTIVE_MCP_BROWSER_CONTEXT.reset(scope_token)


async def _attach_current_browser_generation(ctx: AgentContext) -> None:
    """Follow one exact-generation replacement while verifying a stable session id."""
    try:
        async with mcp_browser_context(ctx):
            return
    except CopilotBrowserGenerationRetired:
        async with mcp_browser_context(ctx):
            return


async def _drop_browser_session_id_at_its_fixed_deadline(ctx: AgentContext) -> None:
    """Drop a held id whose infrastructure is about to end it, so a replacement is created instead.

    The attach in mcp_browser_context stays the oracle for a session that is already gone. This
    answers what the attach structurally cannot: a session in its final minute attaches fine and
    then dies mid-call, because its provider pinned the deadline when it created the browser and
    no renewal moves it (SKY-15044). Infrastructure that can serve a later deadline answers None
    and nothing here applies to it.
    """
    session_id = ctx.browser_session_id
    if not session_id:
        return
    try:
        remaining_seconds = await app.PERSISTENT_SESSIONS_MANAGER.seconds_until_fixed_deadline(
            session_id, ctx.organization_id
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # Not an answer about the session, and the attach that follows is one. Keeping the id costs
        # a failed call at worst; dropping it discards a live browser and its page state on a blip.
        LOG.warning(
            "Could not read the browser session's deadline; keeping the held session",
            session_id=session_id,
            organization_id=ctx.organization_id,
            exc_info=True,
        )
        return
    if remaining_seconds is None or remaining_seconds >= _FIXED_DEADLINE_REPLACEMENT_MARGIN_SECONDS:
        return
    LOG.info(
        "Held browser session is at the deadline its infrastructure fixes; replacing it before use",
        session_id=session_id,
        organization_id=ctx.organization_id,
        remaining_seconds=remaining_seconds,
    )
    # Stop using it, do not close it: the studio browser pane streams this same session, and it is
    # going to be torn down on its own deadline within the minute either way.
    retire_browser_session_id(ctx, session_id)


def _build_test_connect_failure_result(failure: BuildTestConnectFailure) -> dict[str, Any]:
    sentence = build_test_connect_failure_sentence(failure)
    return {
        "ok": False,
        "error": sentence,
        "data": {
            "overall_status": "setup_failed",
            "failure_reason": sentence,
            "browser_session_id": failure.browser_session_id,
            "build_test_connect_failure": failure.model_dump(mode="json", exclude_none=True),
            "blocks": [],
        },
    }


async def _provision_browser_session(ctx: AgentContext) -> BuildTestConnectFailure | None:
    """Create a browser session if the context holds none.

    Returns an immutable acquisition fact on failure and ``None`` on success. Generic callers
    translate that fact back to their established error shape; build-test callers retain it.

    An existing id is not probed here: the attach in mcp_browser_context is the oracle, and a
    session it finds gone is retired and replaced where that is discovered. The one thing the
    attach cannot see is a deadline landing mid-call, which is read from the manager instead.

    Exception: the self-heal path raises HealAdoptionFailed instead of returning an
    failure fact, so a failed adoption aborts the turn rather than degrading to a normal
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

    await _drop_browser_session_id_at_its_fixed_deadline(ctx)
    if ctx.browser_session_id:
        return None

    session = None
    installed_session_id: str | None = None
    try:
        with copilot_span("browser_session_create", data={"organization_id": ctx.organization_id}):
            session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
                organization_id=ctx.organization_id,
                timeout_minutes=30,
            )
        if ctx.browser_session_id:
            # A sibling call installed a session while this create was in flight. Adopt theirs and
            # close ours: assigning over it would leave a live browser referenced by nobody until
            # its timeout. The boot wait below then runs against the session that survived.
            LOG.info(
                "Closing a duplicate browser session; a concurrent call already installed one",
                session_id=session.persistent_browser_session_id,
                installed_session_id=ctx.browser_session_id,
                organization_id=ctx.organization_id,
            )
            await close_browser_session_quietly(
                ctx.organization_id,
                session.persistent_browser_session_id,
                reason=BrowserSessionCloseReason.user_requested,
            )
            session = None
        else:
            ctx.browser_session_id = session.persistent_browser_session_id
            installed_session_id = ctx.browser_session_id

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
    except asyncio.CancelledError:
        if session is not None:
            await close_browser_session_quietly(ctx.organization_id, session.persistent_browser_session_id)
        retire_browser_session_id(ctx, installed_session_id)
        raise
    except Exception as e:
        LOG.warning("Failed to auto-create browser session", error=str(e), exc_info=True)
        # Cleanup keys off the local `session`, not ctx.browser_session_id --
        # if the failure happened between create_session returning and the
        # attribute assignment, ctx still reads None but the session is live.
        # Wrap in wait_for because create_session likely failed due to a
        # degraded session-manager backend, and close_session hitting the
        # same backend could hang the whole request if left unbounded.
        if session is not None:
            await close_browser_session_quietly(ctx.organization_id, session.persistent_browser_session_id)
        # Only clear an id this call installed; a sibling's session is not ours to drop.
        retire_browser_session_id(ctx, installed_session_id)
        # Detail stays in the log above (exc_info=True). The returned string
        # flows back through the tool/agent path and could end up in
        # LLM-visible or user-visible output, so strip raw exception text
        # that may carry internal URLs, paths, or backend identifiers.
        failed_session_id = session.persistent_browser_session_id if session is not None else installed_session_id
        return BuildTestConnectFailure(
            state="provisioning_unavailable",
            browser_session_id=failed_session_id,
        )


async def ensure_browser_session(ctx: AgentContext) -> dict[str, Any] | None:
    failure = await _provision_browser_session(ctx)
    if failure is None:
        return None
    return {"ok": False, "error": "Failed to create browser session"}


async def ensure_build_test_browser_session(ctx: AgentContext) -> dict[str, Any] | None:
    failure = await _provision_browser_session(ctx)
    return None if failure is None else _build_test_connect_failure_result(failure)


async def verify_browser_session_by_attaching(ctx: AgentContext) -> dict[str, Any] | None:
    """For callers that hand the id to an out-of-process run without attaching: one attach here is
    the oracle. A session the manager says is gone is replaced; an attach that could not complete
    returns the facts instead of forwarding an unverified id.

    The attach cannot see a deadline that lands after it, and this path hands the id to a run that
    outlives the check by far more than the tool calls do, so the same pre-emption runs here."""
    await _drop_browser_session_id_at_its_fixed_deadline(ctx)
    if not ctx.browser_session_id:
        return await ensure_browser_session(ctx)
    examined_session_id = ctx.browser_session_id
    try:
        await _attach_current_browser_generation(ctx)
        return None
    except CopilotBrowserSessionUnavailable:
        retire_browser_session_id(ctx, examined_session_id)
        return await ensure_browser_session(ctx)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        LOG.warning(
            "Browser session attach could not complete; liveness undetermined",
            session_id=examined_session_id,
            organization_id=ctx.organization_id,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return {
            "ok": False,
            "error": (
                f"The browser session could not be verified: the attach raised {type(exc).__name__}. "
                "An indeterminate attach is not evidence the browser is dead."
            ),
            "probe_error_type": type(exc).__name__,
        }


def _supersede_release_budget_seconds(ctx: AgentContext) -> float:
    """Seconds the release poll may spend before the turn's own deadline needs what is left to
    terminalize. A deadline suspended for a user decision reports no ``when()`` and imposes no clamp."""
    deadline = ctx.model_stream_deadline
    when = deadline.when() if deadline is not None else None
    if when is None:
        return _SUPERSEDE_RELEASE_WAIT_SECONDS
    remaining = when - asyncio.get_running_loop().time() - _SUPERSEDE_TERMINALIZATION_HEADROOM_SECONDS
    return min(_SUPERSEDE_RELEASE_WAIT_SECONDS, remaining)


async def _build_test_session_occupancy_stop(
    ctx: AgentContext,
    *,
    session_id: str,
    copilot_chat_id: str | None,
    copilot_turn_id: str | None,
) -> BuildTestConnectFailure | None:
    """Whether a live lease on the attached session blocks dispatching a build test into it.

    ``None`` means the session was free when read: nothing held it, or this chat's own older test
    run was superseded and its owner dropped the lease. It is a read, not a reservation — the new
    run takes the lease later, in the worker, so a third party can still win that gap and the run
    dies on the shared lease exactly as it does today.
    """
    session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
        session_id=session_id,
        organization_id=ctx.organization_id,
    )
    if session is None or not session.runnable_id:
        return None
    holder_id = session.runnable_id

    def _stop(reason: str) -> BuildTestConnectFailure:
        LOG.info(
            "copilot_build_test_session_occupied",
            session_id=session_id,
            organization_id=ctx.organization_id,
            occupier_runnable_id=holder_id,
            occupier_runnable_type=session.runnable_type,
            reason=reason,
        )
        return BuildTestConnectFailure(
            state="occupied",
            browser_session_id=session_id,
            occupier_run_id=holder_id if session.runnable_type == "workflow_run" else None,
        )

    if session.runnable_type != "workflow_run":
        return _stop("non_run_owner")
    holder = await app.DATABASE.workflow_runs.get_workflow_run(
        workflow_run_id=holder_id,
        organization_id=ctx.organization_id,
    )
    if holder is None:
        return _stop("owner_run_missing")
    if holder.status.is_final():
        return _stop("owner_run_terminal")
    if not copilot_chat_id or holder.copilot_session_id != copilot_chat_id:
        return _stop("foreign_chat")
    # Descendant runs inherit the chat id from their parent, so a child would otherwise pass every
    # guard: cancelling it leaves the parent running and misreporting a failed child block.
    if holder.parent_workflow_run_id is not None:
        return _stop("owner_run_is_child")
    # A run the user is answering is live work, and ``is_final`` does not cover ``paused``.
    if holder.status == _PAUSED_WORKFLOW_RUN_STATUS:
        return _stop("owner_run_paused")
    if ctx.build_test_tool_calls_in_model_response > 1:
        return _stop("sibling_dispatch_this_turn")

    # Cancelling without time left to see the lease drop would destroy the older test and start
    # nothing in its place, so a spent budget reports occupied and leaves the holder running.
    release_budget = _supersede_release_budget_seconds(ctx)
    if release_budget <= 0:
        return _stop("supersede_release_budget_exhausted")
    try:
        canceled = await app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final(
            holder_id,
            failure_reason=SUPERSEDED_BY_NEWER_TEST_REASON,
        )
    except Exception:
        LOG.warning(
            "copilot_build_test_supersede_cancel_failed",
            session_id=session_id,
            organization_id=ctx.organization_id,
            superseded_workflow_run_id=holder_id,
            exc_info=True,
        )
        return _stop("supersede_cancel_failed")
    # A no-op cancel means the holder reached its own terminal state in the gap after the status
    # read, so this turn superseded nothing and must not claim it did.
    if canceled is not None and copilot_chat_id and copilot_turn_id:
        # The reason on the run row is the fast path, but the superseded run's own finalizer can
        # still overwrite it; this record is copilot's and nothing else writes it.
        await app.DATABASE.workflow_params.record_superseded_build_test_run(
            organization_id=ctx.organization_id,
            workflow_copilot_chat_id=copilot_chat_id,
            turn_id=copilot_turn_id,
            workflow_run_id=holder_id,
        )
    LOG.info(
        "copilot_build_test_session_superseded",
        session_id=session_id,
        organization_id=ctx.organization_id,
        superseded_workflow_run_id=holder_id,
    )
    deadline = time.monotonic() + release_budget
    while time.monotonic() < deadline:
        await asyncio.sleep(_SUPERSEDE_RELEASE_POLL_INTERVAL_SECONDS)
        current = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
            session_id=session_id,
            organization_id=ctx.organization_id,
        )
        if current is None:
            return _stop("session_row_gone_after_supersede")
        if not current.runnable_id:
            return None
        if current.runnable_id != holder_id:
            return _stop("relet_after_supersede")
    return _stop("supersede_release_timed_out")


async def verify_build_test_browser_session_by_attaching(
    ctx: AgentContext,
    *,
    copilot_chat_id: str | None = None,
    copilot_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Attach once for a build test; report supported acquisition state without replacing it."""
    await _drop_browser_session_id_at_its_fixed_deadline(ctx)
    if not ctx.browser_session_id:
        return await ensure_build_test_browser_session(ctx)
    examined_session_id = ctx.browser_session_id
    try:
        await _attach_current_browser_generation(ctx)
    except CopilotBrowserSessionUnavailable:
        retire_browser_session_id(ctx, examined_session_id)
        return _build_test_connect_failure_result(
            BuildTestConnectFailure(state="already_closed", browser_session_id=examined_session_id)
        )
    except BrowserTargetClosedError:
        retire_browser_session_id(ctx, examined_session_id)
        return _build_test_connect_failure_result(
            BuildTestConnectFailure(state="already_closed", browser_session_id=examined_session_id)
        )
    except BrowserCdpConnectionError:
        # A failed attach does not prove the remote session is dead; keep its id as
        # evidence and let the explicit fresh-session action choose replacement.
        return _build_test_connect_failure_result(
            BuildTestConnectFailure(state="cdp_connect_failed", browser_session_id=examined_session_id)
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        LOG.warning(
            "Build-test browser attach could not be classified",
            session_id=examined_session_id,
            organization_id=ctx.organization_id,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return {
            "ok": False,
            "error": "The build-test browser attach could not be classified.",
            "probe_error_type": type(exc).__name__,
        }

    occupancy = await _build_test_session_occupancy_stop(
        ctx,
        session_id=examined_session_id,
        copilot_chat_id=copilot_chat_id,
        copilot_turn_id=copilot_turn_id,
    )
    return None if occupancy is None else _build_test_connect_failure_result(occupancy)
