"""Copilot turn state. Workers import this module, so it must not import the MCP session manager or the SDK client."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypeAlias, TypedDict

from skyvern.forge.sdk.copilot.browser_code_contract import BrowserCodeHost
from skyvern.forge.sdk.copilot.budget_expiry import BudgetExpiryState
from skyvern.forge.sdk.copilot.config import (
    AuthoringCapability,
    BlockAuthoringPolicy,
    CopilotConfig,
    authoring_capability_from_policy,
    block_authoring_policy_from_capability,
)
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence

if TYPE_CHECKING:
    from playwright.async_api import Frame, Page

    from skyvern.cli.core.session_manager import RequestFailureLog
    from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
    from skyvern.forge.sdk.copilot.browser_ablation import CopilotEvalMode, CopilotToolSurfaceIdentity
    from skyvern.forge.sdk.copilot.build_test_connect_failure import BuildTestConnectFailure
    from skyvern.forge.sdk.copilot.build_test_outcome import RecordedBuildTestOutcome
    from skyvern.forge.sdk.copilot.completion_criteria_store import CompletionCriteriaTurnState
    from skyvern.forge.sdk.copilot.completion_verification import CompletionVerificationResult
    from skyvern.forge.sdk.copilot.context import CodeAuthoringRepairContext, SignedOutPageObservation
    from skyvern.forge.sdk.copilot.mcp_adapter import SkyvernOverlayMCPServer
    from skyvern.forge.sdk.copilot.repair_origin_run import (
        OriginOutputRefusalDetail,
        OutputCarrier,
        SelectedOutputSource,
    )
    from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
    from skyvern.forge.sdk.copilot.result_evidence import ScoutObservationContract
    from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
    from skyvern.forge.sdk.copilot.screenshot_utils import (
        ChatScreenshotFrame,
        PendingFrameLease,
        ScreenshotEntry,
        ViewportFrame,
    )
    from skyvern.forge.sdk.copilot.turn_halt import TurnHalt
    from skyvern.forge.sdk.core.event_source_stream import EventSourceStream
    from skyvern.forge.sdk.credential_site_policy import LoginSite
    from skyvern.forge.sdk.schemas.copilot_turn_outcome import ConnectedAccountChoice
    from skyvern.forge.sdk.schemas.credentials import Credential
    from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter
    from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRunParameter
    from skyvern.schemas.proxy_location import ProxyLocationInput
    from skyvern.webeye.browser_state import BrowserState, PageCrashRecord

# Where a planned frontier run starts its browser from; only a non-``unanchored`` start proves the
# run began in a composition state the workflow itself established.
FrontierStartProvenance = Literal["initial", "replayed", "resumed", "unanchored"]
CredentialOriginRecoveryState = Literal["pending", "asked", "declined"]
CredentialOriginDeclineStatus = Literal[
    "unavailable", "already_asked", "error", "unanswered", "skipped", "connected_unresolved", "connected_other_site"
]


@dataclass(frozen=True)
class CredentialOriginRecovery:
    origin: str
    state: CredentialOriginRecoveryState
    refused_credential_id: str


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
    awaiting_runtime_secret_values: bool = False
    artifact_parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", _immutable_redaction_value(self.parameters))
        object.__setattr__(self, "artifact_parameters", _immutable_redaction_value(self.artifact_parameters))


CodeArtifactMetadataValue: TypeAlias = (
    str | int | float | bool | None | list["CodeArtifactMetadataValue"] | dict[str, "CodeArtifactMetadataValue"]
)
CodeArtifactMetadataPayload: TypeAlias = dict[str, CodeArtifactMetadataValue]
SdkActionWorkflowRunCacheKey: TypeAlias = tuple[str, str]


@dataclass
class PendingBrowserInteractionObservation:
    tool_name: str
    url: str = ""


@dataclass(frozen=True)
class PreRunPageReference:
    text: str
    workflow_run_id: str


RegisteredDownloadTextError: TypeAlias = Literal["unavailable", "parse_failed", "parse_timeout", "not_utf8"]


@dataclass(frozen=True)
class RegisteredArtifactEntry:
    artifact_id: str
    file_name: str | None
    text: str | None = None
    page_count: int | None = None
    char_count: int | None = None
    truncated: bool | None = None
    text_layer_problem: str | None = None
    error: RegisteredDownloadTextError | None = None


@dataclass(frozen=True)
class RegisteredArtifactEvidence:
    entries: tuple[RegisteredArtifactEntry, ...]
    workflow_run_id: str
    omission_notices: tuple[str, ...] = ()


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
    visible_match_count: NotRequired[int]
    first_match_visible: NotRequired[bool]


class ScoutedDragDestination(TypedDict):
    selector: str
    role: NotRequired[str]
    accessible_name: NotRequired[str]


class ScoutedInteraction(TypedDict):
    # Every field here crosses the turn boundary into persisted, model-visible context except
    # those named in context._TURN_EPHEMERAL_INTERACTION_FIELDS. A field added here that holds a
    # raw value — a literal, page text — has to be listed there too; nothing enforces the pair.
    tool_name: str
    actor: NotRequired[Literal["user"]]
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
    # The vendor signature literal matched in the challenge frame's URL, so no page-controlled host is kept.
    challenge_vendor: NotRequired[str]
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
    destination: NotRequired[ScoutedDragDestination]
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


@dataclass(frozen=True)
class PrestartedBrowser:
    session_id: str
    ready_monotonic: float


@dataclass(frozen=True)
class BuildTestPrestart:
    task: asyncio.Task[PrestartedBrowser | None]
    proxy_location: ProxyLocationInput


@dataclass(frozen=True)
class AttachedBrowserDriver:
    session_id: str
    browser_state: BrowserState


@dataclass
class AgentContext:
    organization_id: str
    workflow_id: str
    workflow_permanent_id: str
    workflow_yaml: str | None
    browser_session_id: str | None
    stream: EventSourceStream
    persisted_workflow_yaml: str | None = None
    api_key: str | None = None
    # The model's own plan for this chat, written and replaced only by set_work_plan. None until
    # hydration runs, which is how an error-path context stays distinguishable from a cleared plan.
    work_plan: list[str] | None = None
    turn_origin: TurnOrigin = TurnOrigin.interactive
    injected_browser_state: BrowserState | None = None
    # Keyed by session id, so a later rebind of ``browser_session_id`` cannot aim the turn-exit
    # release at a session this turn never attached. ``dataclasses.replace`` shares this dict.
    attached_browser_drivers: dict[str, AttachedBrowserDriver] = field(default_factory=dict)
    # Tool tasks this turn has inside an admitted browser operation. A cancelled turn reaches its
    # finalizer before those tasks finish unwinding, and the manager refuses to retire a busy generation.
    admitted_browser_operations: dict[str, set[asyncio.Task[object]]] = field(default_factory=dict)
    heal_workflow_run_id: str | None = None
    eval_mode: CopilotEvalMode | None = None
    # The self-heal turn advertises its own browser-only surface rather than resolving one, so it
    # names no projection; the shared agent loop still reads this to decide dispatch enforcement.
    tool_surface_identity: CopilotToolSurfaceIdentity | None = None
    # True only while a card is on screen; it alone keeps a second card from opening alongside it.
    # credential_pause_used records that a pick card was raised this turn, so the run-derived ask opens no other.
    credential_ask_in_flight: bool = False
    credential_pause_used: bool = False
    # The deadline the current model stream runs under, published by the enforcement loop so a tool
    # that parks on a user decision can suspend it instead of being cancelled mid-question.
    model_stream_deadline: asyncio.Timeout | None = None
    # Runs started since the current model call began. The SDK saves a step to the session only once all of
    # its tools return, so after the hard backstop cuts a step these are the runs the session has no result for.
    open_step_run_ids: list[str] = field(default_factory=list)
    # True from on_llm_start to on_llm_end: no tool of that response has started, so aborting the
    # stream then discards only the call itself.
    model_call_in_flight: bool = False
    # The SDK streams each tool call to the chat before its response completes, so aborting after one
    # would leave a call on screen that never runs.
    model_call_streamed_tool_call: bool = False
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
    # Shared by this turn's per-dispatch SessionStates, whose page listeners keep writing between calls. Those listeners
    # keep a log alive past the turn, but only this turn reads it.
    request_failure_logs_by_browser_session: dict[str, RequestFailureLog] = field(default_factory=dict)
    # The live record list of the browser state each session last resolved to.
    page_crash_records_by_browser_session: dict[str, Sequence[PageCrashRecord]] = field(default_factory=dict)
    browser_session_recovery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    browser_session_recovery_owner: asyncio.Task[Any] | None = field(default=None, init=False, repr=False)
    browser_session_recovery_depth: int = field(default=0, init=False, repr=False)
    browser_session_replacements: dict[str, str | None] = field(default_factory=dict)
    # Calls capture this before waiting for the recovery lock. A continuity recovery increments it
    # under that lock so every sibling queued against stale page state is suppressed.
    browser_session_continuity_generation: int = 0
    browser_session_continuity_disposition: str | None = None
    # Whether the session this turn lost was ended by the deadline its infrastructure fixes, rather
    # than by an unexplained browser failure. Reported to the model and the user as the cause.
    browser_session_continuity_deadline_expired: bool = False
    browser_session_continuity_acquisition_failure: BuildTestConnectFailure | None = None
    # Each unsolved challenge solve can bill an external solver, so it is counted per browser session.
    unsolved_page_challenges_by_session_id: dict[str, int] = field(default_factory=dict)
    image_captcha_reads: int = 0
    supports_vision: bool = True
    pending_screenshots: list[ScreenshotEntry] = field(default_factory=list)
    pending_frame_lease: PendingFrameLease | None = None
    pending_chat_screenshots: list[ChatScreenshotFrame] = field(default_factory=list, repr=False)
    chat_screenshot_capture_ids: set[str] = field(default_factory=set)
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
    # Set by the planner for a mid-workflow start it cannot bind to a browser: its verified prefix
    # cannot be resumed, the stored order is not the run order, or it refills credentials. The
    # chat's browser is not a safe target for any of those. Consumed and cleared by that run.
    frontier_requires_own_browser: bool = False
    # Where the planned run starts from, stamped once per plan and consumed by that run. Only a
    # non-``unanchored`` start can credit its labels as composition-verified.
    frontier_start_provenance: FrontierStartProvenance | None = None
    # Labels whose last successful run started from a provable composition state. Distinct from
    # verified_prefix_labels, which drives frontier advancement and must stay per-label.
    composition_verified_labels: list[str] = field(default_factory=list)
    # Blocks a credited run's conditional routed around, keyed to that conditional. They count as settled for the
    # tested check and the frontier, but never as verified: they did not run.
    untaken_branch_labels: dict[str, str] = field(default_factory=dict)
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
    signed_out_page_observations: list[SignedOutPageObservation] = field(default_factory=list)
    signed_out_page_observation_attempts: list[str] = field(default_factory=list)
    last_test_non_retriable_nav_error: str | None = None
    last_infrastructure_tool_error: str | None = None
    workflow_persisted: bool = False
    last_workflow: Workflow | None = None
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
    copilot_config: CopilotConfig | None = None
    # Wire and persisted-metadata spelling of authoring_capability; authoring surfaces read the
    # capability, never this field.
    block_authoring_policy: BlockAuthoringPolicy = BlockAuthoringPolicy.TASK_V3_PURE
    agent_step_available: bool = False
    effective_workflow_proxy_location: ProxyLocationInput = None
    # The proxy the last dispatched run acted through, which is the attached session's whenever it
    # declares one. Kept apart from the declared location above, which the repair levers read.
    last_run_proxy_location: ProxyLocationInput = None
    # The run attached a browser session, so last_run_proxy_location is that session's answer and
    # the workflow's declared proxy must not stand in for it.
    last_run_proxy_from_session: bool = False
    # Whether the driver's own codes on the last run named Skyvern's proxy hop. Computed once
    # from the run result's error_codes so no later reader has to re-decide it from prose.
    last_test_proxy_owned_failure: bool = False

    copilot_run_start_monotonic: float | None = None

    last_good_workflow: Any | None = None
    last_good_workflow_yaml: str | None = None
    last_run_blocks_workflow_run_id: str | None = None
    last_run_blocks_browser_session_id: str | None = None
    last_run_binding_unavailable_reason: str | None = None
    last_artifact_health_blocker_reason: str | None = None
    last_artifact_health_blocker_labels: list[str] = field(default_factory=list)
    last_artifact_health_failure_classes: list[str] = field(default_factory=list)
    last_run_blocks_block_ids: list[str] = field(default_factory=list)
    # The subset of last_run_blocks_block_ids that carries the run verdict.
    last_run_outcome_block_ids: list[str] = field(default_factory=list)
    last_run_blocks_block_labels: list[str] = field(default_factory=list)
    last_run_outcome: RecordedRunOutcome | None = None
    latest_recorded_build_test_outcome: RecordedBuildTestOutcome | None = None
    recorded_build_test_outcome_history: list[dict[str, object]] = field(default_factory=list)
    recorded_persisted_block_run_workflow_run_id: str | None = None
    authored_block_families: dict[str, list[dict[str, object]]] = field(default_factory=dict)
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
    browser_code_host: BrowserCodeHost = field(default_factory=BrowserCodeHost)
    # One lock per browser session: the model can issue parallel tool calls, and two of them acting on
    # the same page at once interleave their clicks, reads and evidence.
    browser_session_locks_by_session_id: dict[str, asyncio.Lock] = field(default_factory=dict)
    # Serializes model-visible browser evidence commits. Raw browser work may overlap, but a post-hook
    # or native credential fill owns this lock while it commits, so rollback cannot erase another
    # call's evidence.
    browser_evidence_commit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Read once per turn: repeated fill attempts must not re-scan the org's credentials.
    org_credentials_for_turn: list[Credential] | None = None
    vault_login_uris_by_credential_id: dict[str, list[str]] = field(default_factory=dict)
    scoped_login_sites_by_credential_id: dict[str, list[LoginSite]] = field(default_factory=dict)
    # Highest trajectory_index visible at the latest parsed evaluate observation and whether that page
    # showed a password-type control; orders page evidence against post-fill submits across evictions.
    last_scout_observation_trajectory_index: int | None = None
    last_scout_observation_has_password_control: bool = False
    # Required parameter keys the build-test resolution seam could not bind from a user param, a scout
    # value, an origin-run value, or a non-empty default. Reset per run; read when composing the outcome.
    unbound_required_parameter_keys: list[str] = field(default_factory=list)
    # Stored inputs of the ownership-checked run last_run was seeded from, requested or inherited.
    # Reloaded every turn; excluded from repr because the values can be user files.
    repair_origin_input_values: tuple[tuple[WorkflowParameter, WorkflowRunParameter], ...] = field(
        default=(), repr=False
    )
    repair_origin_is_copilot_run: bool = False
    # Block outputs the same run recorded, or why they cannot be used; loaded only when a run was named.
    repair_origin_outputs: OutputCarrier = field(default=None, repr=False)
    repair_origin_outputs_run_id: str | None = None
    # Set by the planner, consumed and cleared by the run it planned, like frontier_resume_session_id.
    frontier_selected_output_sources: dict[str, SelectedOutputSource] = field(default_factory=dict, repr=False)
    frontier_origin_reused_labels: list[str] = field(default_factory=list)
    frontier_origin_output_refusal: OriginOutputRefusalDetail | None = None
    frontier_named_output_refusal: OriginOutputRefusalDetail | None = None
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
    # Every child frame that navigated to a challenge vendor during the in-flight click. A widget can
    # arrive with hidden helper frames beside it, so which one is on screen is decided at report time.
    pending_scout_challenge_frames: list[Frame] = field(default_factory=list)
    # The records this click wrote in each collection, so an effect observed after the fact updates
    # them directly instead of searching for a locator the scrub may have dropped.
    pending_scout_click_records: list[ScoutedInteraction] = field(default_factory=list)
    pending_scout_click_pre_frame: ViewportFrame | None = None
    # When the listener for the in-flight click went live, so the settle counts time already spent.
    pending_scout_challenge_armed_at: float | None = None
    # Removers for the frame listener armed for the in-flight click, run when the click post-hook
    # returns, so one click's widget cannot land in another click's window.
    pending_scout_challenge_detachers: list[Callable[[], None]] = field(default_factory=list)
    # Vendor frames already present when the click was armed, with the area each rendered at then, so
    # one the click reveals rather than navigates is still the click's effect.
    pending_scout_challenge_prior_frames: list[tuple[Frame, float | None]] = field(default_factory=list)
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
    # Browsers a run carrying secrets executed in this turn. Only run_browser_code reads it, to keep
    # screenshots, scripts and fetches (ways to carry a value off the page) out of its cells there.
    secret_run_browser_session_ids: set[str] = field(default_factory=set)
    # Keyed by (browser session, run): how to fetch the secrets that run has not handed over yet. A run
    # that paused or was stopped at the ceiling returns before the worker publishes them.
    awaited_run_secret_handoffs: dict[tuple[str, str], Callable[[], Awaitable[None]]] = field(default_factory=dict)
    # Browsers this turn minted, each mapped to the last run dispatched into it. A client's
    # browser_session_id is honoured only when it names a studio debug session, so these are
    # reachable by this turn alone and are closed when it ends rather than left to bill out their
    # budgets. A run left to its owner takes its browser out of the ledger.
    minted_browser_sessions: dict[str, str | None] = field(default_factory=dict)
    minted_browser_renewal_checks: dict[str, asyncio.TimerHandle] = field(default_factory=dict)
    # The next build test's blank browser, started while the model fixes a failed first-block test.
    build_test_prestart: BuildTestPrestart | None = None
    # Every unfinished pre-start mapped to the ctx copy it provisions on; claim and discard leave this alone.
    pending_build_test_prestarts: dict[asyncio.Task[PrestartedBrowser | None], AgentContext] = field(
        default_factory=dict
    )
    # The browser each turn-end cancel interrupted, since the cancelled provision retires the copy's id.
    cancelled_build_test_prestarts: dict[asyncio.Task[PrestartedBrowser | None], str] = field(default_factory=dict)
    # Set first thing at turn end: a pre-start that finishes later finds the sweeps already run and cleans up itself.
    prestart_teardown_started: bool = False
    # Closed when a newer test started; an older call's result that lands later must not rebind last_run to one.
    superseded_minted_browser_sessions: set[str] = field(default_factory=set)
    # A resumed browser's earlier run id and how many calls hold it while their runs are not yet submitted.
    minted_browser_claims: dict[str, tuple[str, int]] = field(default_factory=dict)
    browser_allocation_recorder: Callable[[str, str | None], Awaitable[None]] | None = None

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
    # The origin a credential fill was refused on, and how far the ask for a login there has got.
    # It steers the model and the card; it withdraws no tool.
    credential_origin_recovery: CredentialOriginRecovery | None = None
    # Every (origin, credential id) a fill refused this turn. A later pick ask on such a site gets a recovery card,
    # and its admission check excludes every login refused anywhere on that site.
    credential_origin_refusals: set[tuple[str, str]] = field(default_factory=set)

    @property
    def authoring_capability(self) -> AuthoringCapability:
        return replace(
            authoring_capability_from_policy(self.block_authoring_policy), agent_step=self.agent_step_available
        )

    @authoring_capability.setter
    def authoring_capability(self, capability: AuthoringCapability) -> None:
        self.block_authoring_policy = block_authoring_policy_from_capability(capability)
        self.agent_step_available = capability.agent_step
