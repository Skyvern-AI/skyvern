"""Structured context for copilot cross-turn memory."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, get_args

import structlog
from pydantic import BaseModel, Field
from typing_extensions import NotRequired, TypedDict

from skyvern.forge.sdk.copilot.authoring_parameter_binding import AuthoringParameterBindingDirective
from skyvern.forge.sdk.copilot.build_phase import BuildPhase
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig
from skyvern.forge.sdk.copilot.result_evidence import (
    LoadedResultCompositionEvidence,
    loaded_result_target_structure_signature,
)
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence
from skyvern.forge.sdk.workflow.models.workflow import Workflow

LOG = structlog.get_logger()

ResponseType = Literal["REPLY", "ASK_QUESTION", "REPLACE_WORKFLOW"]
COPILOT_RESPONSE_TYPES: tuple[ResponseType, ...] = get_args(ResponseType)
ProposalDisposition = Literal["no_proposal", "auto_applicable", "review_untested", "review_tested"]


class DeliveredUnverifiedPublicOutputs(dict[str, Any]):
    """Run-output values explicitly selected for terminal presentation.

    The values remain dynamically shaped JSON until the presentation sanitizer
    validates them.  The concrete marker prevents arbitrary result-factory
    callers from minting the public structured-output surface.
    """


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
    terminalEnvelope: NotRequired[dict[str, Any]]
    # The ADR-0005 terminal adjudication (enforcement.verified_goal_claim_authorized):
    # True only when outcome evidence authorizes a tested-success claim.
    verifiedSuccess: NotRequired[bool]
    # Verdict-state summary from the turn's latest evaluated adjudication.
    outcomeAdjudication: NotRequired[NarrativeOutcomeAdjudication]
    # Sanitized JSON boundary for reviewing outputs that were delivered but not independently verified.
    deliveredUnverifiedObservedOutputs: NotRequired[dict[str, Any]]
    # {"reason": <credential_prompt_reason() token>}, set when this turn surfaces a credential need.
    credentialPrompt: NotRequired[dict[str, str]]
    # {"outcome": "connected"|"skipped"|"timeout", "credentialId": ...}, set when a mid-build
    # credential pause (credential_pause.py) resolved during this turn.
    credentialPause: NotRequired[dict[str, str]]
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
    from skyvern.forge.sdk.copilot.build_test_outcome import (
        MetadataRejectLadderState,
        RecordedBuildTestOutcome,
        RecordedOutcomeBindingConstraint,
        RecordedOutcomeGroundingRequirement,
    )
    from skyvern.forge.sdk.copilot.completion_criteria_store import CompletionCriteriaTurnState
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import DiagnosisRepairContract
    from skyvern.forge.sdk.copilot.narration import NarratorState
    from skyvern.forge.sdk.copilot.output_extraction_plan import FrozenRequestedOutputExtractionCandidate
    from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
    from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
    from skyvern.forge.sdk.copilot.schema_incompatibility import SchemaIncompatibility
    from skyvern.forge.sdk.copilot.turn_context import TurnContextPacket
    from skyvern.forge.sdk.copilot.turn_halt import TurnHalt
    from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentClassifierResult
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


class ApprovedCredential(BaseModel):
    credential_id: str


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


FillCarryToolName = Literal["type_text", "select_option", "fill_credential_field"]


class FillCarry(BaseModel):
    source_url: str = ""
    selector: str = ""
    tool_name: FillCarryToolName
    role: str = ""
    accessible_name: str = ""
    typed_length: int = 0
    typed_value: str = ""
    value: str = ""
    control_readonly: bool | None = None
    control_disabled: bool | None = None
    control_value_satisfied: bool | None = None
    credential_id: str = ""
    credential_field: str = ""
    available_fields: list[str] | None = None


class LoadedResultTargetContext(BaseModel):
    selector: str = ""
    is_table: bool = False
    row_selector: str = ""
    row_count: int | None = None
    structure_signature: str = ""


OUTPUT_OWNER_AMBIGUITY_REASON_CODE = "output_owner_ambiguous"


class CodeAuthoringRepairContext(BaseModel):
    block_label: str
    reason_code: str
    unresolved_names: list[str] = Field(default_factory=list)
    parameter_keys: list[str] = Field(default_factory=list)
    available_parameter_keys: list[str] = Field(default_factory=list)
    binding_candidates: list[str] = Field(default_factory=list)
    selector: str | None = None
    source_url: str | None = None
    refiner_selector: str | None = None
    selector_alternatives: list[dict[str, str]] = Field(default_factory=list)
    allowed_global_names: list[str] = Field(default_factory=list)
    allowed_helper_surface: dict[str, list[str]] = Field(default_factory=dict)
    runtime_failure_reason: str | None = None
    runtime_failure_class: str | None = None
    output_dependency_failure_class: str | None = None
    missing_output_key: str | None = None
    available_output_keys: list[str] = Field(default_factory=list)
    current_block_parameter_keys: list[str] = Field(default_factory=list)
    required_goal_value_paths: list[str] = Field(default_factory=list)
    required_extraction_schema_paths: list[str] = Field(default_factory=list)
    required_code_return_paths: list[str] = Field(default_factory=list)
    metadata_contract_source: str = ""
    metadata_contract_reason_code: str = ""
    failed_block_status: str | None = None
    workflow_run_id: str | None = None
    current_origin: str | None = None
    current_url_present: bool = False
    current_title_present: bool = False
    page_evidence_source: str | None = None
    observed_after_workflow_run: bool = False
    page_form_summaries: list[str] = Field(default_factory=list)
    page_result_summaries: list[str] = Field(default_factory=list)
    page_action_summaries: list[str] = Field(default_factory=list)
    page_challenge_summaries: list[str] = Field(default_factory=list)
    required_block_structure: str = ""
    spine_stage_count: int | None = None
    spine_split_blockers: list[str] = Field(default_factory=list)
    output_owner_candidate_labels: list[str] = Field(default_factory=list)
    parameter_binding_directive: AuthoringParameterBindingDirective | None = None
    repair_instruction: str = "add workflow-input-like names to parameter_keys, or stop referencing them."


class StructuredContext(BaseModel):
    user_goal: str = ""
    urls_visited: list[UrlVisit] = Field(default_factory=list)
    fields_filled: list[FieldFilled] = Field(default_factory=list)
    credentials_checked: list[CredentialCheck] = Field(default_factory=list)
    approved_credentials: list[ApprovedCredential] = Field(default_factory=list)
    decisions_made: list[str] = Field(default_factory=list)
    workflow_state: str = ""
    # Per-chat discovery budget. Survives turn boundaries via
    # AgentResult.global_llm_context — finalized deterministically at every
    # AgentResult exit by `finalize_discovery_counter_in_global_llm_context`.
    discovery_calls_made: int = 0
    page_inspection_calls_made: int = 0
    observed_acted_pages: list[ObservedPage] = Field(default_factory=list)
    loaded_result_targets: list[LoadedResultTargetContext] = Field(default_factory=list)
    fill_carry: list[FillCarry] = Field(default_factory=list)

    def to_json_str(self) -> str:
        payload = self.model_dump(mode="json")
        payload["loaded_result_targets"] = [
            _sanitized_loaded_result_target_payload(target) for target in self.loaded_result_targets
        ]
        payload["fill_carry"] = [carry.model_dump(mode="json", exclude_none=True) for carry in self.fill_carry]
        return json.dumps(payload, indent=2)

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


def _sanitized_loaded_result_target_payload(
    target: LoadedResultTargetContext,
) -> dict[str, object]:
    structure_signature = loaded_result_target_structure_signature(
        is_table=target.is_table,
        row_count=target.row_count,
    )
    return {
        "is_table": target.is_table,
        "row_count": target.row_count,
        "structure_signature": structure_signature,
    }


def sanitize_global_llm_context_for_prompt(global_llm_context: str | None) -> str:
    raw = global_llm_context or ""
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(payload, dict):
        return raw
    targets = payload.get("loaded_result_targets")
    if not isinstance(targets, list):
        return raw

    sanitized_targets: list[dict[str, object]] = []
    for target in targets:
        if not isinstance(target, Mapping):
            continue
        try:
            target_context = LoadedResultTargetContext.model_validate(target)
        except Exception:
            continue
        sanitized_targets.append(_sanitized_loaded_result_target_payload(target_context))
    payload["loaded_result_targets"] = sanitized_targets
    return json.dumps(payload, indent=2)


def render_loaded_result_context_for_prompt(global_llm_context: str) -> str:
    structured = StructuredContext.from_json_str(global_llm_context)
    if not structured.loaded_result_targets:
        return ""
    lines = [
        "Author an extraction or validation block from these loaded-result targets.",
        "Do not call evaluate just to re-read the same loaded results.",
    ]
    for index, target in enumerate(structured.loaded_result_targets, start=1):
        lines.append(f"- target {index}:")
        lines.append(f"  table: {str(target.is_table).lower()}")
        if target.row_count is not None:
            lines.append(f"  row_count: {target.row_count}")
        if target.structure_signature:
            lines.append(f"  structure_signature: {target.structure_signature}")
    return "\n".join(lines)


_MAX_OBSERVED_ACTED_PAGES = 20
_MAX_FILL_CARRY = 20
_FILL_CARRY_TEXT_CAP = 240
_FILL_CARRY_TOOLS = frozenset({"type_text", "select_option", "fill_credential_field"})
_FILL_CARRY_CREDENTIAL_FIELDS = frozenset({"username", "password", "totp"})
FillCarryPrimitive = str | int | bool | None


def _carry_text(value: FillCarryPrimitive, *, max_chars: int = _FILL_CARRY_TEXT_CAP) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_chars]


def _carry_int(value: FillCarryPrimitive) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _carry_bool(value: FillCarryPrimitive) -> bool | None:
    return value if isinstance(value, bool) else None


def _fill_carry_from_scout_trajectory(
    trajectory: Sequence[Mapping[str, FillCarryPrimitive]],
    credential_field_inventory: Mapping[str, frozenset[str]] | None = None,
) -> list[FillCarry]:
    carry: list[FillCarry] = []
    for interaction in trajectory:
        tool_name = _carry_text(interaction.get("tool_name"), max_chars=40)
        selector = _carry_text(interaction.get("selector"))
        source_url = _carry_text(interaction.get("source_url"), max_chars=2048)
        if tool_name not in _FILL_CARRY_TOOLS or not selector or not source_url:
            continue
        role = _carry_text(interaction.get("role"), max_chars=80)
        accessible_name = _carry_text(interaction.get("accessible_name"), max_chars=160)
        typed_length = _carry_int(interaction.get("typed_length"))
        if tool_name == "type_text":
            carry.append(
                FillCarry(
                    source_url=source_url,
                    selector=selector,
                    tool_name="type_text",
                    role=role,
                    accessible_name=accessible_name,
                    typed_length=typed_length,
                    typed_value=_carry_text(interaction.get("typed_value")),
                    control_readonly=_carry_bool(interaction.get("control_readonly")),
                    control_disabled=_carry_bool(interaction.get("control_disabled")),
                    control_value_satisfied=_carry_bool(interaction.get("control_value_satisfied")),
                )
            )
        elif tool_name == "select_option":
            value = _carry_text(interaction.get("value"))
            if value:
                carry.append(
                    FillCarry(
                        source_url=source_url,
                        selector=selector,
                        tool_name="select_option",
                        role=role,
                        accessible_name=accessible_name,
                        value=value,
                    )
                )
        elif tool_name == "fill_credential_field":
            credential_id = _carry_text(interaction.get("credential_id"))
            credential_field = _carry_text(interaction.get("credential_field"), max_chars=20)
            if credential_id and credential_field in _FILL_CARRY_CREDENTIAL_FIELDS:
                inventory = (credential_field_inventory or {}).get(credential_id)
                carry.append(
                    FillCarry(
                        source_url=source_url,
                        selector=selector,
                        tool_name="fill_credential_field",
                        role=role,
                        accessible_name=accessible_name,
                        typed_length=typed_length,
                        credential_id=credential_id,
                        credential_field=credential_field,
                        available_fields=sorted(inventory) if inventory else None,
                    )
                )
    return carry[-_MAX_FILL_CARRY:]


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


def _loaded_result_targets_from_steer(
    steer: LoadedResultCompositionEvidence | None,
) -> list[LoadedResultTargetContext]:
    if steer is None:
        return []
    return [
        LoadedResultTargetContext(
            is_table=target.is_table,
            row_count=target.row_count,
            structure_signature=target.structure_signature,
        )
        for target in steer.targets
    ]


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
    loaded_result_targets = _loaded_result_targets_from_steer(
        getattr(ctx, "latest_evaluate_result_composition_steer", None)
    )
    raw_scout_trajectory = getattr(ctx, "scout_trajectory", None)
    scout_trajectory = raw_scout_trajectory if isinstance(raw_scout_trajectory, Sequence) else ()
    raw_inventory = getattr(ctx, "scouted_credential_field_inventory_by_credential_id", None)
    fill_carry = _fill_carry_from_scout_trajectory(
        [interaction for interaction in scout_trajectory if isinstance(interaction, Mapping)],
        credential_field_inventory=raw_inventory if isinstance(raw_inventory, Mapping) else None,
    )
    if (
        not raw_context
        and this_turn == 0
        and inspections_this_turn == 0
        and not flow_evidence
        and not loaded_result_targets
        and not fill_carry
    ):
        return None
    sc = StructuredContext.from_json_str(raw_context)
    sc.discovery_calls_made = prior + this_turn
    sc.page_inspection_calls_made = prior_inspections + inspections_this_turn
    sc.observed_acted_pages = _merge_observed_acted_pages(sc.observed_acted_pages, flow_evidence)
    # Replace with this turn's targets so stale extraction hints do not persist.
    sc.loaded_result_targets = loaded_result_targets
    sc.fill_carry = fill_carry
    if fill_carry:
        LOG.info(
            "copilot_fill_carry_persisted",
            source_url=fill_carry[0].source_url,
            field_count=len(fill_carry),
        )
    return sc.to_json_str()


_MAX_APPROVED_CREDENTIALS = 20


def record_approved_credentials_in_global_llm_context(ctx: CopilotContext, raw_context: str | None) -> str | None:
    """Persist resolved credentials as durable cross-turn approval. Records only from
    resolved_credentials, never discovered_credentials, so ADR-0002's run/draft split
    holds by construction.
    """
    policy = ctx.request_policy
    if policy is None or not policy.resolved_credentials:
        return raw_context
    sc = StructuredContext.from_json_str(raw_context)
    existing_ids = {record.credential_id for record in sc.approved_credentials}
    for credential in policy.resolved_credentials:
        if credential.credential_id in existing_ids:
            continue
        sc.approved_credentials.append(ApprovedCredential(credential_id=credential.credential_id))
        existing_ids.add(credential.credential_id)
    if len(sc.approved_credentials) > _MAX_APPROVED_CREDENTIALS:
        sc.approved_credentials = sc.approved_credentials[-_MAX_APPROVED_CREDENTIALS:]
    return sc.to_json_str()


def adopt_model_authored_context(trusted_raw: str | None, model_raw: object) -> StructuredContext:
    """Take the model's context but keep `approved_credentials` server-owned.

    Approval is recorded only from server-resolved credentials; an entry the model
    supplied would be promoted into `resolved_credentials` on the next turn and clear
    the unapproved-credential gate for a credential the user never named. Membership
    of the org is not evidence the user named it.
    """
    trusted = StructuredContext.from_json_str(trusted_raw)
    structured = trusted
    if isinstance(model_raw, dict):
        try:
            structured = StructuredContext.model_validate(model_raw)
        except Exception:
            structured = trusted
    elif isinstance(model_raw, str):
        structured = StructuredContext.from_json_str(model_raw)
    structured.approved_credentials = list(trusted.approved_credentials)
    return structured


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
    # Shadow-only typed terminal-state envelope persisted and streamed on terminal frames.
    terminal_envelope: dict[str, Any] | None = None
    staged_workflow_yaml: str | None = None
    staged_workflow: Workflow | None = None
    has_staged_proposal: bool = False
    code_artifact_metadata: dict[str, dict[str, Any]] | None = None
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
    copilot_config: CopilotConfig | None = None
    block_authoring_policy: BlockAuthoringPolicy = BlockAuthoringPolicy.STANDARD
    impose_synthesized_code_block: bool = False
    target_block_label: str | None = None
    turn_intent: TurnIntent | None = None
    # Retained so a policy mutated after the pre-flight credential pause can be
    # re-derived into an authority envelope without re-running the classifier.
    turn_intent_classifier_result: TurnIntentClassifierResult | None = None
    turn_context_packet: TurnContextPacket | None = None
    prior_turn_outcome: TurnOutcome | None = None
    latest_diagnosis_repair_contract: DiagnosisRepairContract | None = None
    blocked_reply_signatures: list[str] = field(default_factory=list)
    requested_output_extraction_candidate: FrozenRequestedOutputExtractionCandidate | None = None

    # Mid-build credential pause (credential_pause.py). last_run_skipped_unbound_credentials
    # is set by tools/__init__.py's update_and_run_blocks skip branch; client_supports_credential_pause
    # is set from the chat request at construction; the rest are owned by maybe_credential_pause.
    last_run_skipped_unbound_credentials: bool = False
    client_supports_credential_pause: bool = False
    credential_pause_used: bool = False
    copilot_credential_pause_seconds: float = 0.0
    credential_pause_outcome: str | None = None

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
    latest_schema_incompatibility: SchemaIncompatibility | None = None

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
    # In-turn run-outcome trace derived from assignments to ``last_run_outcome``
    # (the same source that powers run_outcome SSE frames). Append-only across
    # per-run pointer resets (``last_run_outcome = None``); cleared only by the
    # workflow-edit evidence reset, which invalidates pre-edit run evidence.
    terminal_envelope_run_outcomes: list[RecordedRunOutcome] = field(default_factory=list)
    delivered_unverified_terminal: bool = False
    delivered_unverified_workflow_run_id: str | None = None
    delivered_unverified_observed_outputs: dict[str, Any] = field(default_factory=DeliveredUnverifiedPublicOutputs)
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
    verified_terminal_block_outputs: dict[str, Any] = field(default_factory=dict)
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
    # Unlike the progress-gated repair ceiling, this climbs even when every
    # rejection is different; it resets only on an accepted persist.
    code_authoring_guardrail_reject_count: int = 0
    # True when the most-recent such rejection deferred to the credential-scout
    # gate, so the churn backstop yields to that message instead of pre-empting it.
    last_code_authoring_reject_was_credential_priority: bool = False
    pending_code_authoring_runtime_repair_context: CodeAuthoringRepairContext | None = None
    last_code_authoring_repair_context: CodeAuthoringRepairContext | None = None
    latest_recorded_build_test_outcome: RecordedBuildTestOutcome | None = None
    metadata_reject_ladder_state: MetadataRejectLadderState | None = None
    recorded_build_test_outcome_history: list[dict[str, object]] = field(default_factory=list)
    recorded_persisted_block_run_workflow_run_id: str | None = None
    recorded_outcome_grounding_requirement: RecordedOutcomeGroundingRequirement | None = None
    recorded_outcome_binding_constraint: RecordedOutcomeBindingConstraint | None = None
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

    def __post_init__(self) -> None:
        parent_post_init = getattr(super(), "__post_init__", None)
        if callable(parent_post_init):
            parent_post_init()
        from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome

        if isinstance(self.last_run_outcome, RecordedRunOutcome):
            super().__setattr__("terminal_envelope_run_outcomes", [self.last_run_outcome])

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name != "last_run_outcome":
            return
        from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome

        if not isinstance(value, RecordedRunOutcome):
            # ``last_run_outcome = None`` is a per-run pointer reset, not an
            # evidence reset — the trace survives so a later run in the same
            # turn cannot mask an earlier honest not_demonstrated.
            return
        outcomes = getattr(self, "terminal_envelope_run_outcomes", None)
        if isinstance(outcomes, list):
            outcomes.append(value)
        else:
            super().__setattr__("terminal_envelope_run_outcomes", [value])

    def has_genuine_workflow_attempt(self) -> bool:
        """This turn persisted a workflow proposal or executed a real build-test run; excludes
        ``test_after_update_done``, which is stamped for any ``run_blocks_and_collect_debug`` scout
        probe (including early-return probes that record no run) and so is not a genuine-attempt signal."""
        if self.update_workflow_called:
            return True
        if self.last_update_block_count is not None:
            return True
        if self.last_test_ok is not None:
            return True
        for run_id in (
            self.last_run_blocks_workflow_run_id,
            self.last_successful_run_blocks_workflow_run_id,
            self.last_outcome_gate_workflow_run_id,
        ):
            if run_id is not None and run_id.strip():
                return True
        return False

    def genuine_attempt_parity_fields(self) -> dict[str, bool | int | str | None]:
        return {
            "has_genuine_workflow_attempt": self.has_genuine_workflow_attempt(),
            "update_workflow_called": self.update_workflow_called,
            "test_after_update_done": self.test_after_update_done,
            "last_update_block_count": self.last_update_block_count,
            "last_test_ok": self.last_test_ok,
            "last_run_blocks_workflow_run_id": self.last_run_blocks_workflow_run_id,
            "last_successful_run_blocks_workflow_run_id": self.last_successful_run_blocks_workflow_run_id,
            "last_outcome_gate_workflow_run_id": self.last_outcome_gate_workflow_run_id,
            "ctx_last_workflow_present": self.last_workflow is not None,
        }
