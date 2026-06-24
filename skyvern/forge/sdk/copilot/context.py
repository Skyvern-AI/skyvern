"""Structured context for copilot cross-turn memory."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, get_args

from pydantic import BaseModel, Field
from typing_extensions import NotRequired, TypedDict

from skyvern.forge.sdk.copilot.build_phase import BuildPhase
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence
from skyvern.forge.sdk.workflow.models.workflow import Workflow

ResponseType = Literal["REPLY", "ASK_QUESTION", "REPLACE_WORKFLOW"]
COPILOT_RESPONSE_TYPES: tuple[ResponseType, ...] = get_args(ResponseType)
ProposalDisposition = Literal["no_proposal", "auto_applicable", "review_untested", "review_tested"]


class NarrativeDraft(TypedDict):
    blockCount: int
    blockLabels: list[str]
    summary: str | None


# Shape must match the FE ``ActivityEntry`` in narrativeState.ts; toolName is
# present only for tool_call/tool_result and success only for tool_result.
class NarrativeActivityEntry(TypedDict):
    kind: str
    text: str
    iteration: int
    toolName: NotRequired[str]
    displayLabel: NotRequired[str]
    success: NotRequired[bool]
    id: str


class NarrativeBlock(TypedDict):
    label: str
    blockType: str
    state: str
    lastSeenIteration: int
    activity: list[NarrativeActivityEntry]
    startedAt: str | None
    endedAt: str | None
    outcome: NotRequired[str]
    outcomeReason: NotRequired[str]


class NarrativeOutcomeAdjudication(TypedDict):
    satisfiedCount: int
    unsatisfiedCount: int
    unknownCount: int
    # "verified_goal_satisfied" | "built_unverified".
    claimTier: str
    criteriaEpoch: NotRequired[int]
    criteriaLifecycleReason: NotRequired[str]


# Mirror of the FE TurnNarrativeState; camelCase keys match the wire shape.
class TurnNarrativePayload(TypedDict):
    turnId: str | None
    turnIndex: int
    mode: str
    responseType: NotRequired[ResponseType]
    cancelled: NotRequired[bool]
    proposalDisposition: NotRequired[ProposalDisposition]
    # TurnOutcome.response_kind value: "build" | "clarify" | "diagnose" | "refuse" | "recover".
    responseKind: NotRequired[str]
    # The ADR-0005 terminal adjudication (enforcement.verified_goal_claim_authorized):
    # True only when outcome evidence authorizes a tested-success claim.
    verifiedSuccess: NotRequired[bool]
    # Verdict-state summary from the turn's latest evaluated adjudication.
    outcomeAdjudication: NotRequired[NarrativeOutcomeAdjudication]
    designStarted: bool
    designEnded: bool
    draft: NarrativeDraft | None
    blocks: list[NarrativeBlock]
    terminal: str
    terminalMessage: str | None
    narrativeSummary: str | None
    priorBlockCount: int | None
    designActivity: list[NarrativeActivityEntry]
    startedAt: str | None
    endedAt: str | None


if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
    from skyvern.forge.sdk.copilot.completion_criteria_store import CompletionCriteriaTurnState
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import DiagnosisRepairContract
    from skyvern.forge.sdk.copilot.narration import NarratorState
    from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
    from skyvern.forge.sdk.copilot.turn_context import TurnContextPacket
    from skyvern.forge.sdk.copilot.turn_halt import TurnHalt
    from skyvern.forge.sdk.copilot.turn_intent import TurnIntent
    from skyvern.forge.sdk.schemas.copilot_turn_outcome import TurnOutcome


class UrlVisit(BaseModel):
    url: str
    summary: str = ""


class FieldFilled(BaseModel):
    selector: str = ""
    label: str = ""
    value: str = ""


class CredentialCheck(BaseModel):
    credential_name: str = ""
    credential_id: str | None = None
    found: bool = False


class ObservedPage(BaseModel):
    """Compact cross-turn record of a page the agent scouted (SKY-10562).

    Carries only what the composition gate needs to credit a prior observation:
    the full scheme-bearing url (so urlparse-based _same_page/_same_origin match),
    whether bounded page schema was captured, and how the state was reached. Full
    page schemas stay within-turn; this keeps a resumed turn from re-scouting (or
    deadlocking against a spent inspection budget) for pages already observed.
    """

    url: str = ""
    had_bounded_schema: bool = False
    reached_via: str = ""


class StructuredContext(BaseModel):
    user_goal: str = ""
    urls_visited: list[UrlVisit] = Field(default_factory=list)
    fields_filled: list[FieldFilled] = Field(default_factory=list)
    credentials_checked: list[CredentialCheck] = Field(default_factory=list)
    decisions_made: list[str] = Field(default_factory=list)
    workflow_state: str = ""
    # Per-chat discovery budget. Survives turn boundaries via
    # AgentResult.global_llm_context — finalized deterministically at every
    # AgentResult exit by `finalize_discovery_counter_in_global_llm_context`.
    discovery_calls_made: int = 0
    page_inspection_calls_made: int = 0
    observed_acted_pages: list[ObservedPage] = Field(default_factory=list)

    def to_json_str(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json_str(cls, raw: str | None) -> StructuredContext:
        if not raw:
            return cls()
        raw = raw.strip()
        if raw.startswith("{"):
            try:
                return cls.model_validate_json(raw)
            except Exception:
                return cls(user_goal=raw)
        return cls(user_goal=raw)

    def merge_turn_summary(self, tool_activity: list[dict]) -> None:
        for entry in tool_activity:
            tool = entry.get("tool", "")
            summary = entry.get("summary", "")

            if tool == "navigate_browser":
                url = summary.removeprefix("Navigated to ").strip()
                if url and not any(v.url == url for v in self.urls_visited):
                    self.urls_visited.append(UrlVisit(url=url, summary=""))

            elif tool == "list_credentials":
                resolved = entry.get("credentials")
                if isinstance(resolved, list) and resolved:
                    for credential in resolved:
                        if not isinstance(credential, dict):
                            continue
                        credential_id = credential.get("credential_id")
                        if not isinstance(credential_id, str):
                            continue
                        name = credential.get("name")
                        self.credentials_checked.append(
                            CredentialCheck(
                                credential_name=name if isinstance(name, str) else "",
                                credential_id=credential_id,
                                found=True,
                            )
                        )
                else:
                    match = re.search(r"Found (\d+)", summary)
                    found = int(match.group(1)) > 0 if match else False
                    self.credentials_checked.append(CredentialCheck(credential_name=summary, found=found))

            elif tool == "type_text":
                parts = summary.split("into ")
                selector = parts[-1].strip("'\"") if len(parts) > 1 else ""
                # Intentionally omit value: typed text may contain PII / credentials.
                self.fields_filled.append(FieldFilled(selector=selector, label=selector))

            elif tool == "update_workflow":
                self.workflow_state = summary

            elif tool in (
                "click",
                "evaluate",
                "run_blocks_and_collect_debug",
                "update_and_run_blocks",
                "get_run_results",
            ):
                self.decisions_made.append(f"{tool}: {summary}")

            elif tool == "get_browser_screenshot":
                if "(" in summary and ")" in summary:
                    url = summary.split("(", 1)[1].rsplit(")", 1)[0]
                    if url and not any(v.url == url for v in self.urls_visited):
                        self.urls_visited.append(UrlVisit(url=url, summary="screenshot"))

            output = entry.get("output_preview")
            if output and tool in ("run_blocks_and_collect_debug", "update_and_run_blocks", "get_run_results"):
                preview = output[:300] if len(output) > 300 else output
                self.decisions_made.append(f"  output: {preview}")

        if len(self.decisions_made) > 20:
            self.decisions_made = self.decisions_made[-15:]
        if len(self.urls_visited) > 50:
            self.urls_visited = self.urls_visited[-40:]
        if len(self.fields_filled) > 50:
            self.fields_filled = self.fields_filled[-40:]
        if len(self.credentials_checked) > 50:
            self.credentials_checked = self.credentials_checked[-40:]


_MAX_OBSERVED_ACTED_PAGES = 20


def _merge_observed_acted_pages(prior: list[ObservedPage], flow_evidence: list[dict[str, Any]]) -> list[ObservedPage]:
    """Fold this turn's flow-evidence trajectory into the persisted summary.

    Keyed by url; a later observation of the same url replaces the earlier one,
    and a bounded-schema or interaction observation never regresses to a weaker
    one for the same page.
    """
    by_url: dict[str, ObservedPage] = {page.url: page for page in prior if page.url}
    for entry in flow_evidence:
        evidence = entry.get("evidence")
        url = entry.get("url")
        if (not isinstance(url, str) or not url.strip()) and isinstance(evidence, dict):
            url = evidence.get("current_url") or evidence.get("inspected_url")
        if not isinstance(url, str) or not url.strip():
            continue
        existing = by_url.get(url)
        had_schema = bool(entry.get("had_bounded_schema")) or (existing.had_bounded_schema if existing else False)
        reached_via = str(entry.get("reached_via") or (existing.reached_via if existing else ""))
        if existing and existing.reached_via == "interaction":
            reached_via = "interaction"
        by_url[url] = ObservedPage(url=url, had_bounded_schema=had_schema, reached_via=reached_via)
    return list(by_url.values())[-_MAX_OBSERVED_ACTED_PAGES:]


def finalize_discovery_counter_in_global_llm_context(ctx: Any, raw_context: str | None) -> str | None:
    """Fold the per-chat discovery counter into the outgoing global_llm_context.

    Called from agent.py's `_make_agent_result` factory so every AgentResult
    exit path — timeout, cancel, max-turns, output-policy block, request-
    policy clarification, infeasibility clarification, non-retriable nav error,
    normal translate-result, missing-SDK fallback, unexpected-error fallback —
    carries the updated count.

    Returns None when there is nothing to record and no prior context, so the
    pre-existing 'no global_llm_context' behaviour is preserved.
    """
    prior = int(getattr(ctx, "prior_discovery_calls_made", 0) or 0)
    this_turn = int(getattr(ctx, "discovery_calls_this_turn", 0) or 0)
    prior_inspections = int(getattr(ctx, "prior_page_inspection_calls_made", 0) or 0)
    inspections_this_turn = int(getattr(ctx, "page_inspection_calls_this_turn", 0) or 0)
    flow_evidence = getattr(ctx, "flow_evidence", None) or []
    if not raw_context and this_turn == 0 and inspections_this_turn == 0 and not flow_evidence:
        return None
    sc = StructuredContext.from_json_str(raw_context)
    sc.discovery_calls_made = prior + this_turn
    sc.page_inspection_calls_made = prior_inspections + inspections_this_turn
    sc.observed_acted_pages = _merge_observed_acted_pages(sc.observed_acted_pages, flow_evidence)
    return sc.to_json_str()


@dataclass
class AgentResult:
    user_response: str
    updated_workflow: Workflow | None
    global_llm_context: str | None
    response_type: ResponseType = "REPLY"
    workflow_yaml: str | None = None
    workflow_was_persisted: bool = False
    # Route nulls any persisted proposed_workflow when this is set.
    clear_proposed_workflow: bool = False
    # Actual API token usage accumulated across the agent run. None when no
    # provider reported usage on the stream — distinguishes "no data" from
    # "0 tokens" so eval cost grading can flag missing telemetry instead of
    # silently passing as cheap.
    total_tokens: int | None = None
    # Set when the agent absorbed an asyncio cancellation initiated by an
    # explicit user Stop. Lets the route route to a cancel-specific
    # persistence path (rollback + ``Cancelled by user.`` chat row) without
    # losing ``workflow_was_persisted`` the way a re-raise would.
    cancelled: bool = False
    # Controls whether the route may auto-apply the proposal or must force explicit review.
    proposal_disposition: ProposalDisposition = "auto_applicable"
    # Successful code-only build turns can be applied without requiring the
    # chat's sticky "Always accept" setting.
    apply_without_review: bool = False
    output_policy_diagnostics: dict[str, Any] | None = None
    turn_outcome: TurnOutcome | None = None
    turn_id: str | None = None
    narrative_summary: str | None = None
    # Persisted on the assistant chat message so the bubble survives a reload.
    narrative_payload: TurnNarrativePayload | None = None
    staged_workflow_yaml: str | None = None
    staged_workflow: Workflow | None = None
    has_staged_proposal: bool = False
    # Set when ``_update_workflow`` wrote canonical mid-turn (param / top-level
    # settings changes); terminal handlers roll back on non-auto-accept.
    canonical_was_persisted_due_to_param_change: bool = False
    # Criteria lifecycle decision + adjudication counters the route persists
    # after the turn; None when persisted criteria are disabled.
    completion_criteria_turn_state: CompletionCriteriaTurnState | None = None


@dataclass(frozen=True)
class InFlightStreamToolCall:
    call_id: str
    tool_name: str
    iteration: int


@dataclass
class CopilotContext(AgentContext):
    """Unified context for the copilot agent run.

    Extends AgentContext with enforcement state, tool tracking, and
    workflow state needed by the SDK-based agent loop.

    Field-shadowing note: the enforcement / workflow / frontier state fields
    declared below are intentionally redeclared on this subclass. The parent
    ``AgentContext`` (in ``runtime.py``) still carries the same names with the
    same defaults for legacy paths that instantiate ``AgentContext`` directly.
    Python's MRO resolves to the child's declaration when a ``CopilotContext``
    instance is used — that's the desired behavior here. Stripping the
    duplicates from the parent is tracked in SKY-8974; until that lands, if
    you add a new field here, keep the defaults in sync with the parent to
    avoid drift.
    """

    workflow_copilot_chat_id: str | None = None

    # Enforcement state
    navigate_called: bool = False
    observation_after_navigate: bool = False
    navigate_enforcement_done: bool = False
    update_workflow_called: bool = False
    test_after_update_done: bool = False
    post_update_nudge_count: int = 0
    coverage_nudge_count: int = 0
    format_nudge_count: int = 0
    no_workflow_nudge_count: int = 0
    copilot_total_timeout_exceeded: bool = False
    user_message: str = ""
    block_goal_main_goal: str = ""
    allow_untested_workflow_draft: bool = False
    request_policy: RequestPolicy | None = None
    block_authoring_policy: BlockAuthoringPolicy = BlockAuthoringPolicy.STANDARD
    impose_synthesized_code_block: bool = False
    target_block_label: str | None = None
    turn_intent: TurnIntent | None = None
    turn_context_packet: TurnContextPacket | None = None
    latest_diagnosis_repair_contract: DiagnosisRepairContract | None = None
    blocked_reply_signatures: list[str] = field(default_factory=list)

    # Tool tracking
    consecutive_tool_tracker: list[str] = field(default_factory=list)
    tool_activity: list[dict[str, Any]] = field(default_factory=list)
    # A goal-satisfied stop raised from on_tool_end ends the SDK stream before
    # the satisfying tool's tool_output event flushes; these carry what the
    # exit path needs to emit the missing TOOL_RESULT frame.
    in_flight_stream_tool_call: InFlightStreamToolCall | None = None
    goal_satisfied_tool_name: str | None = None
    goal_satisfied_tool_output: dict[str, Any] | None = None
    latest_tool_blocker_signal: CopilotToolBlockerSignal | None = None
    tool_blocker_signals: list[CopilotToolBlockerSignal] = field(default_factory=list)
    turn_halt: TurnHalt | None = None

    # ``None`` until usage is observed; ``0`` only when a provider explicitly
    # reported zero. Distinct values let cost grading flag missing telemetry.
    total_tokens_used: int | None = None
    input_tokens_used: int | None = None
    output_tokens_used: int | None = None

    # Workflow state
    last_workflow: Workflow | None = None
    last_workflow_yaml: str | None = None
    # Always False under staging; ``has_staged_proposal`` carries the signal.
    workflow_persisted: bool = False
    last_update_block_count: int | None = None
    last_test_ok: bool | None = None
    last_test_failure_reason: str | None = None
    last_artifact_health_blocker_reason: str | None = None
    last_artifact_health_blocker_labels: list[str] = field(default_factory=list)
    last_artifact_health_failure_classes: list[str] = field(default_factory=list)
    failed_test_nudge_count: int = 0
    explore_without_workflow_nudge_count: int = 0
    code_only_code_schema_seen: bool = False
    code_only_target_page_evidence_seen: bool = False
    last_failed_workflow_yaml: str | None = None
    code_native_pending_capability: str | None = None
    # Set when a block-running tool timed out and the run's true outcome
    # could not be reconciled (post-drain row was ``canceled``, non-final, or
    # unreadable). Blocks further block-running tool calls until the LLM
    # calls ``get_run_results(workflow_run_id=<same>)`` AND that read returns
    # a status in ``_TRUSTED_POST_DRAIN_STATUSES``. Turn-scoped by
    # construction — ``CopilotContext`` is re-created per agent turn — so
    # this guards auto-retry WITHIN a turn but not cross-turn "user says
    # retry" requests.
    pending_reconciliation_run_id: str | None = None
    pending_reconciliation_requires_user_input: bool = False
    # Block-running tools make their own run context available for same-turn
    # reporting. This is deliberately not persisted across turns. The
    # successful variant is kept for "default to the last clean result"; the
    # generic variant allows the agent to re-read the same failed/canceled run
    # after a watchdog reconciliation read has cleared the retry guard.
    last_run_blocks_workflow_run_id: str | None = None
    last_successful_run_blocks_workflow_run_id: str | None = None
    last_outcome_gate_workflow_run_id: str | None = None
    # Consecutive failed runs where navigation completed but the scraper
    # could not read the page (generic "failed to load the website" template).
    # Resets on any non-matching run outcome. Streak crosses workflow-shape
    # changes deliberately — the frontier fingerprint resets each time the
    # copilot rewrites the workflow, but the underlying site-block pattern is
    # shape-independent.
    probable_site_block_streak_count: int = 0
    probable_site_block_stop_nudge_count: int = 0
    per_tool_budget_nudge_count: int = 0
    effective_workflow_proxy_location: Any | None = None
    # Labels of navigation blocks that were canceled/failed inside a
    # PER_TOOL_BUDGET run. Armed after get_run_results inspects the budgeted
    # run, and cleared only when a workflow update removes or changes the
    # label away from navigation. Prevents rerunning the same oversized
    # navigation block unchanged.
    per_tool_budget_problem_block_labels: list[str] = field(default_factory=list)
    # Armed when a PER_TOOL_BUDGET run leaves the browser on a meaningful page.
    # The next block-running call must be preceded by page inspection so the
    # agent recovers from observed state instead of replaying the same search.
    post_budget_page_inspection_required: bool = False
    post_budget_page_inspection_url: str | None = None
    post_budget_page_inspection_run_id: str | None = None

    # Per-request frontier state. `verified_block_outputs` and
    # `verified_prefix_labels` are populated ONLY from fully-successful runs —
    # a single failed block in the executed suffix leaves the prior verified
    # state untouched, because the browser session is now in post-failure
    # state and the prefix labels can no longer be trusted as an anchor.
    verified_block_outputs: dict[str, Any] = field(default_factory=dict)
    verified_prefix_labels: list[str] = field(default_factory=list)
    verified_prefix_current_url: str | None = None
    last_requested_block_labels: list[str] = field(default_factory=list)
    last_executed_block_labels: list[str] = field(default_factory=list)
    last_full_workflow_test_ok: bool = False
    last_unverified_block_labels: list[str] = field(default_factory=list)
    workflow_verification_evidence: WorkflowVerificationEvidence = field(default_factory=WorkflowVerificationEvidence)
    last_frontier_start_label: str | None = None
    last_frontier_fingerprint: str | None = None
    last_failure_signature: str | None = None
    repeated_failure_streak_count: int = 0
    last_repair_non_convergence_signature: str | None = None
    consecutive_non_converging_repair_count: int = 0
    # Unlike the identity-keyed repair ceiling, this climbs even when every
    # rejection is different; it resets only on an accepted persist.
    code_authoring_guardrail_reject_count: int = 0
    # True when the most-recent such rejection deferred to the credential-scout
    # gate, so the churn backstop yields to that message instead of pre-empting it.
    last_code_authoring_reject_was_credential_priority: bool = False
    # Turn-scoped monotonic marks of verified forward progress: the union of
    # completion criteria the judge confirmed satisfied so far this turn, and the
    # high-water length of the verified block prefix. A repair that grows either
    # made progress and resets the non-convergence streak.
    verified_criteria_high_water: frozenset[str] = frozenset()
    verified_prefix_high_water_len: int = 0
    # A fresh clean full-workflow pass counts as progress exactly once: latched
    # here so a stale carry-over ``last_full_workflow_test_ok`` on a later non-run
    # REPAIR verdict cannot keep resetting the non-convergence streak.
    verified_full_pass_consumed: bool = False
    # Highest streak level at which we've already emitted a repeated-failure
    # nudge. Prevents the warn nudge from re-firing every turn while the
    # streak is still at 2, and guarantees the stop nudge fires exactly once
    # when the streak reaches 3.
    repeated_failure_nudge_emitted_at_streak: int = 0
    # Set by _record_run_blocks_result when the most recent failed run matches
    # SKIP_INNER_NAV_RETRY_ERRORS (DNS / cert / SSL / invalid URL). Drives the
    # one-shot non-retriable-nav stop nudge and the deterministic exit-path
    # exception in run_with_enforcement. Cleared at the top of every call to
    # _record_run_blocks_result so stale state can't leak across runs.
    last_test_non_retriable_nav_error: str | None = None
    # Normalized signature of the non-retriable nav error last nudged on.
    # Lets the stop nudge re-fire if the user retries with a different bad URL
    # (different signature) in the same session. Cleared on meaningful success.
    non_retriable_nav_error_last_emitted_signature: str | None = None
    last_failure_category_top: str | None = None
    # Hash of the ordered (action_type, element_id) tuples from the last run's
    # action trace. When the same fingerprint repeats run-over-run with no
    # intervening success, the agent is stuck re-firing the same clicks/inputs —
    # typically because a captcha/popup/anti-bot is blocking progress. The
    # streak counter drives the hard-abort short-circuit in _tool_loop_error.
    # ``pending_action_sequence_fingerprint`` holds the fingerprint of the run
    # that JUST completed, computed by ``_run_blocks_and_collect_debug`` before
    # action_trace is stripped. ``update_repeated_failure_state`` compares it
    # to ``last_action_sequence_fingerprint`` (the prior run's fingerprint),
    # updates the streak, then promotes pending → last.
    last_action_sequence_fingerprint: str | None = None
    pending_action_sequence_fingerprint: str | None = None
    repeated_action_fingerprint_streak_count: int = 0

    copilot_run_start_monotonic: float | None = None

    last_good_workflow: Workflow | None = None
    last_good_workflow_yaml: str | None = None

    # Populated lazily by ``stream_to_sse`` and reused across enforcement
    # iterations so cadence/last-emitted-at survive ``run_with_enforcement``
    # retries. Declared here (rather than attached dynamically) so future
    # refactors can't strip it silently.
    narrator_state: NarratorState | None = None

    # Default COMPOSING is the safe non-BUILD value; the orchestrator
    # overrides at turn start via `initial_build_phase`.
    build_phase: BuildPhase = BuildPhase.COMPOSING
    # Hydrated from inbound StructuredContext.discovery_calls_made at turn start.
    prior_discovery_calls_made: int = 0
    discovery_calls_this_turn: int = 0
    prior_page_inspection_calls_made: int = 0
    page_inspection_calls_this_turn: int = 0
    discovery_step_count: int = 0
    discovery_started_monotonic: float | None = None
    discovery_evidence_trail: list[dict[str, Any]] = field(default_factory=list)
    resolved_discovery_entrypoint_url: str | None = None
    resolved_discovery_failure_reason: str | None = None
    resolved_discovery_entrypoint_inspection_baseline: int = 0
    discovery_entrypoint_url_question_nudge_count: int = 0
    pre_discovery_url_question_nudge_count: int = 0
    # Set in `_run_attempt` after SkyvernOverlayMCPServer is constructed.
    # The discovery tool reaches the connected FastMCP client through this.
    discovery_mcp_server: Any | None = None

    # default_factory is the safety net — Python dataclass inheritance
    # disallows non-default fields after default ones, and the parent
    # ``AgentContext`` has many defaulted fields. The route generates the
    # canonical turn_id and passes it as an explicit kwarg at every
    # construction site, overriding this default.
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    turn_index: int = 0
    design_start_emitted: bool = False
    design_end_emitted: bool = False
    narrative_summary: str | None = None

    staged_workflow_yaml: str | None = None
    staged_workflow: Workflow | None = None
    has_staged_proposal: bool = False
    # Prior turn's uncommitted draft; carries blocks even when the request body and canonical row are empty.
    prior_copilot_workflow_yaml: str | None = None
    # Set when ``_update_workflow`` wrote canonical mid-turn (param / top-level
    # settings changes); terminal handlers roll back on non-auto-accept.
    canonical_was_persisted_due_to_param_change: bool = False
    completion_criteria_turn_state: CompletionCriteriaTurnState | None = None
    prior_block_count: int | None = None
    block_state_map: dict[str, str] = field(default_factory=dict)
    block_started_at_map: dict[str, str] = field(default_factory=dict)
    block_ended_at_map: dict[str, str] = field(default_factory=dict)
    turn_started_at: str | None = None
    turn_ended_at: str | None = None
