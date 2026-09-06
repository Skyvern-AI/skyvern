"""Structured context for copilot cross-turn memory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, get_args

import structlog
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from typing_extensions import NotRequired, TypedDict

from skyvern.forge.sdk.copilot.authoring_parameter_binding import AuthoringParameterBindingDirective
from skyvern.forge.sdk.copilot.browser_ablation import BrowserAblationMetadata, CopilotEvalMode
from skyvern.forge.sdk.copilot.budget_expiry import BudgetExpiryState
from skyvern.forge.sdk.copilot.code_block_synthesis import CREDENTIAL_FILL_TOOL_NAME
from skyvern.forge.sdk.copilot.code_write_diff import TURN_PATCH_CHAR_BUDGET, CodeWriteDiff
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig
from skyvern.forge.sdk.copilot.credential_resolution import loggable_origin, safe_admitted_url
from skyvern.forge.sdk.copilot.google_connection_notice import (
    GoogleConnectionNotice,
    GoogleConnectionNoticePayload,
    GoogleSheetConnectionBinding,
)
from skyvern.forge.sdk.copilot.human_input_wait import HumanInputWait
from skyvern.forge.sdk.copilot.page_identity import page_location_fingerprint, safe_page_origin
from skyvern.forge.sdk.copilot.review_gate import NarrativeReviewProjection
from skyvern.forge.sdk.copilot.run_outcome import RunOutcomeRole
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_structured_prompt
from skyvern.forge.sdk.copilot.verification_evidence import WorkflowVerificationEvidence
from skyvern.forge.sdk.workflow.models.workflow import Workflow

LOG = structlog.get_logger()

ResponseType = Literal["REPLY", "ASK_QUESTION", "REPLACE_WORKFLOW"]
COPILOT_RESPONSE_TYPES: tuple[ResponseType, ...] = get_args(ResponseType)
ProposalDisposition = Literal["no_proposal", "auto_applicable", "review_untested", "review_tested"]

AskSubject = Literal["output_schema", "credentials", "target_url", "disambiguation", "deliverable_permission", "other"]
COPILOT_ASK_SUBJECTS: tuple[AskSubject, ...] = get_args(AskSubject)


def coerce_ask_subject(value: object) -> AskSubject | None:
    for subject in COPILOT_ASK_SUBJECTS:
        if value == subject:
            return subject
    return None


def parsed_ask_refs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [ref for ref in value if isinstance(ref, str) and ref]


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
    # activeLabel reads while the step runs; outcomeLabel replaces it once
    # finished. Absent when the narrator did not speak, leaving displayLabel.
    activeLabel: NotRequired[str]
    outcomeLabel: NotRequired[str]
    codeDiffs: NotRequired[list[CodeWriteDiff]]
    id: str
    # Server clock read for the event this entry describes, shared with the SSE
    # update so a rehydrated row renders the same elapsed the live row did.
    timestamp: NotRequired[str]


class NarrativeBlock(TypedDict):
    label: str
    workflowRunBlockId: NotRequired[str]
    blockType: str
    state: str
    lastSeenIteration: int
    activity: list[NarrativeActivityEntry]
    startedAt: str | None
    endedAt: str | None
    outcome: NotRequired[str]
    outcomeReason: NotRequired[str]
    outcomeRole: NotRequired[RunOutcomeRole]


class BlockRunIdentity(NamedTuple):
    workflow_run_block_id: str
    iteration: int


class NarrativeConnectedAccountChoice(TypedDict):
    connection_id: str
    name: str
    state: str
    email_address: str | None


class NarrativeTurnFacts(TypedDict):
    factsAvailable: bool
    evaluationState: str | None
    runId: str | None
    runCompleted: bool | None
    terminalCause: str | None
    blocksRunThisTurn: int | None
    authoredBlockCount: NotRequired[int]
    matchingSourceBlockCount: NotRequired[int]
    # The tested claim is decided once, in _turn_fact_bundle, so no surface re-derives it.
    ranCleanOnCurrentSource: bool


class NarrativeBudgetExpiry(TypedDict):
    budgetExpired: Literal[True]
    source: Literal["deadline", "max_turns"]
    reportProduced: bool | None
    stagedDraftId: str | None
    drainFingerprint: str | None


# Mirror of the FE TurnNarrativeState; camelCase keys match the wire shape.
class TurnNarrativePayload(TypedDict):
    turnId: str | None
    turnIndex: int
    responseType: NotRequired[ResponseType]
    cancelled: NotRequired[bool]
    proposalDisposition: NotRequired[ProposalDisposition]
    # TurnOutcome.response_kind value: "answer" | "build" | "clarify" | "diagnose" | "refuse" | "recover".
    responseKind: NotRequired[str]
    terminalEnvelope: NotRequired[dict[str, Any]]
    questionInteractions: NotRequired[list[dict[str, Any]]]
    # {"reason": <credential_prompt_reason() token>}, set when this turn surfaces a credential need.
    credentialPrompt: NotRequired[dict[str, str]]
    # {"outcome": "connected"|"skipped"|"timeout", "credentialId": ...}, set when a mid-build
    # credential pause (credential_pause.py) resolved during this turn.
    credentialPause: NotRequired[dict[str, str]]
    # {"credentialId": ..., "name": ...}, set when a credential was bound this turn without an ask
    # (deterministic auto-bind); the FE renders it as a receipt with a Change affordance.
    credentialAutoBound: NotRequired[dict[str, str]]
    connectedAccountChoices: NotRequired[list[NarrativeConnectedAccountChoice]]
    googleConnectionNotices: NotRequired[list[GoogleConnectionNoticePayload]]
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
    review: NotRequired[NarrativeReviewProjection]
    testedBlockFingerprints: NotRequired[dict[str, list[str]]]
    budgetExpiry: NotRequired[NarrativeBudgetExpiry]
    turnFacts: NotRequired[NarrativeTurnFacts]


if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
    from skyvern.forge.sdk.copilot.build_test_outcome import (
        RecordedBuildTestOutcome,
    )
    from skyvern.forge.sdk.copilot.completion_criteria_store import CompletionCriteriaTurnState
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import DiagnosisRepairContract
    from skyvern.forge.sdk.copilot.narration import NarratorState
    from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
    from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome
    from skyvern.forge.sdk.copilot.turn_context import TurnContextPacket
    from skyvern.forge.sdk.copilot.turn_halt import TurnHalt
    from skyvern.forge.sdk.schemas.copilot_turn_outcome import ConnectedAccountChoice, TurnOutcome


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
    # Set only for an approval minted from a carried proposal, whose reach stays the page that
    # vouched for it. Empty for a credential the user named, which never carried an origin.
    admitted_url: str = ""


ProposedCredentialOrigin = Literal["live_page_admitted", "executed_credential_fill"]


class ProposedCredential(BaseModel):
    credential_id: str
    admitted_url: str = ""
    origin_arm: ProposedCredentialOrigin | None = None
    # How many later asks this proposal has already survived. The renewal cannot tell an ask about
    # this credential from an ask about anything else, so the count is what bounds it.
    carries: int = 0


class ObservedPage(BaseModel):
    """Compact cross-turn record of a page the agent scouted (SKY-10562).

    Carries only what the composition gate needs to credit a prior observation:
    a model-safe origin, a keyed location fingerprint, whether bounded page schema
    was captured, and how the state was reached. Full page schemas and raw location
    state stay within-turn.
    """

    url: str = ""
    location_fingerprint: str = ""
    had_bounded_schema: bool = False
    reached_via: str = ""


# The fields ScoutedInteraction declares turn-ephemeral. Everything else on an
# interaction crosses the turn boundary as captured: the turn boundary is not a
# disclosure boundary, so there is no field roster here to keep in step with the record.
# ``input_value`` is the private same-turn literal; the model sees ``input_id``, which does
# cross. ``typed_value`` is absent because the record no longer has one — it was retired
# with the literal, and is discarded when a legacy payload is migrated below.
_TURN_EPHEMERAL_INTERACTION_FIELDS = frozenset({"input_value", "read_result_value", "selector_match_count"})
# Held by chats persisted before the record exposed secret-safe input identities.
_RETIRED_INTERACTION_FIELDS = frozenset({"typed_value"})


OUTPUT_OWNER_AMBIGUITY_REASON_CODE = "output_owner_ambiguous"


class PageObstructionSelectorCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selector: str
    source: str


class PageObstructionIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: str
    role: str
    label_context: str


class PageObstructionControl(BaseModel):
    """A producer-bounded control plus scalar capture extensions not known to this consumer."""

    model_config = ConfigDict(extra="allow", frozen=True)

    __pydantic_extra__: dict[str, str | bool | int | float] = Field(init=False)
    tag: str | None = None
    text: str | None = None
    aria_label: str | None = None
    title: str | None = None
    selector: str | None = None
    type: str | None = None
    selector_candidates: list[PageObstructionSelectorCandidate] = Field(default_factory=list)
    identity: PageObstructionIdentity | None = None


class PageObstruction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str | None = None
    source: str | None = None
    selector: str | None = None
    selector_candidates: list[PageObstructionSelectorCandidate] = Field(default_factory=list)
    identity: PageObstructionIdentity | None = None
    text: str | None = None
    visual_location: str | None = None
    underlying_page_blocked: bool | None = None
    visible_controls: list[PageObstructionControl] = Field(default_factory=list)


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
    current_url: str | None = None
    current_title: str | None = None
    page_evidence_source: str | None = None
    observed_after_workflow_run: bool = False
    rendered_value_excerpt: str | None = None
    page_form_summaries: list[str] = Field(default_factory=list)
    page_result_summaries: list[str] = Field(default_factory=list)
    page_action_summaries: list[str] = Field(default_factory=list)
    page_challenge_summaries: list[str] = Field(default_factory=list)
    page_obstruction_summaries: list[str] = Field(default_factory=list)
    page_obstructions: list[PageObstruction] = Field(default_factory=list)
    page_obstruction_omission_notices: list[str] = Field(default_factory=list)
    required_block_structure: str = ""
    spine_stage_count: int | None = None
    spine_split_blockers: list[str] = Field(default_factory=list)
    output_owner_candidate_labels: list[str] = Field(default_factory=list)
    parameter_binding_directive: AuthoringParameterBindingDirective | None = None
    repair_instruction: str = "add workflow-input-like names to parameter_keys, or stop referencing them."


class StructuredContext(BaseModel):
    # Without populate_by_name, the alias on carried_trajectory below would make the field
    # unconstructable by its own name.
    model_config = ConfigDict(populate_by_name=True)

    user_goal: str = ""
    urls_visited: list[UrlVisit] = Field(default_factory=list)
    fields_filled: list[FieldFilled] = Field(default_factory=list)
    credentials_checked: list[CredentialCheck] = Field(default_factory=list)
    approved_credentials: list[ApprovedCredential] = Field(default_factory=list)
    # Google connections the user picked from the account card. Separate from approved_credentials
    # because connections never enter resolved_credentials, which is ADR 0002's password-fill plane.
    approved_connections: list[ApprovedCredential] = Field(default_factory=list)
    # The credential the server itself resolved on a turn that ended by asking the user about it,
    # rewritten unconditionally at every turn end so it never outlives the turn after the ask.
    proposed_credential: ProposedCredential | None = None
    decisions_made: list[str] = Field(default_factory=list)
    workflow_state: str = ""
    page_inspection_calls_made: int = 0
    observed_acted_pages: list[ObservedPage] = Field(default_factory=list)
    # Chats persisted before this record was generalized still store it under ``fill_carry``.
    carried_trajectory: list[dict[str, Any]] = Field(
        default_factory=list,
        validation_alias=AliasChoices("carried_trajectory", "fill_carry"),
    )

    @field_validator("carried_trajectory", mode="before")
    @classmethod
    def _drop_retired_fields(cls, value: object) -> object:
        """A chat persisted under the old key still holds ``typed_value``, the literal the
        record retired in favour of a secret-safe input identity. Migration drops it here
        rather than in the ongoing filter, which admits every field the record still has.

        The turn-ephemeral pair goes with it. Nothing legitimate carries them — the outbound
        filter excludes both — so an inbound entry holding ``input_value`` is a stale payload
        re-introducing the private literal the identity replaced.
        """
        if not isinstance(value, list):
            return value
        dropped = _RETIRED_INTERACTION_FIELDS | _TURN_EPHEMERAL_INTERACTION_FIELDS
        return [
            {
                **({"executed_selector": entry["selector"]} if isinstance(entry.get("selector"), str) else {}),
                **{key: item for key, item in entry.items() if key not in dropped and key != "selector"},
            }
            if isinstance(entry, dict)
            else entry
            for entry in value
        ]

    entrypoint_url: str | None = None

    def to_json_str(self) -> str:
        payload = self.model_dump(mode="json")
        payload["carried_trajectory"] = [dict(entry) for entry in self.carried_trajectory]
        return json.dumps(payload, indent=2, default=str)

    @classmethod
    def from_json_str(cls, raw: str | None) -> StructuredContext:
        if not raw:
            return cls()
        raw = raw.strip()
        if raw.startswith("{"):
            try:
                return cls.model_validate_json(raw)
            except Exception:
                # The fallback erases every typed field, including credential approvals,
                # so a silent hit here is invisible authority loss (SKY-13986).
                LOG.warning(
                    "structured_context_parse_failed",
                    raw_length=len(raw),
                    raw_sha256=hashlib.sha256(raw.encode()).hexdigest()[:16],
                )
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
                "edit_block_and_run",
                "get_run_results",
            ):
                self.decisions_made.append(f"{tool}: {summary}")

            elif tool == "get_browser_screenshot":
                if "(" in summary and ")" in summary:
                    url = summary.split("(", 1)[1].rsplit(")", 1)[0]
                    if url and not any(v.url == url for v in self.urls_visited):
                        self.urls_visited.append(UrlVisit(url=url, summary="screenshot"))

            output = entry.get("output_preview")
            if output and tool in (
                "run_blocks_and_collect_debug",
                "update_and_run_blocks",
                "edit_block_and_run",
                "get_run_results",
            ):
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
    return json.dumps(payload, indent=2)


def build_model_safe_global_llm_context(global_llm_context: str | None) -> str:
    """The model-facing view of the serialized context: values redacted structurally,
    document kept valid. Policy code consumes the raw serialized context, never this view."""
    return sanitize_global_llm_context_for_prompt(redact_raw_secrets_for_structured_prompt(global_llm_context or ""))


_MAX_OBSERVED_ACTED_PAGES = 20
_MAX_CARRIED_INTERACTIONS = 60


def _carried_trajectory_from_scout_trajectory(
    trajectory: Sequence[Mapping[str, Any]],
    credential_field_inventory: Mapping[str, frozenset[str]] | None = None,
) -> list[dict[str, Any]]:
    """Carry each interaction forward as captured, minus the turn-ephemeral fields.

    No tool filter and no field roster: a field added to ``ScoutedInteraction`` crosses
    without an entry anywhere. Credential inventory is attached here because it is
    resolve-time metadata the interaction itself never carried.
    """
    carried: list[dict[str, Any]] = []
    for interaction in trajectory:
        if not str(interaction.get("tool_name") or "").strip():
            continue
        entry = {
            key: value
            for key, value in interaction.items()
            if key not in _TURN_EPHEMERAL_INTERACTION_FIELDS and key != "selector"
        }
        selector = interaction.get("selector")
        if isinstance(selector, str) and selector:
            entry.setdefault("executed_selector", selector)
        credential_id = entry.get("credential_id")
        inventory = (credential_field_inventory or {}).get(credential_id) if isinstance(credential_id, str) else None
        if inventory:
            entry["available_fields"] = sorted(inventory)
        carried.append(entry)
    return carried[-_MAX_CARRIED_INTERACTIONS:]


def _merge_carried_trajectory(
    prior: Sequence[Mapping[str, Any]], current: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Retained entries first, then this turn's, dropping entries already represented.

    A turn whose rebind was declined never re-derives the prior entries from its own
    trajectory, so replacing rather than merging would discard the record a turn at a
    time — the loss compounding on itself.
    """
    merged = [dict(entry) for entry in prior]
    # Hydration seeds this turn's trajectory with the retained record, marking each entry
    # carried, so when there is a prior record those entries are already represented and
    # re-appending would double them. Matching on content instead would collapse a genuine
    # repeat — a banner dismissed twice, the same value read twice — and silently shorten
    # the record. Tested against ``prior``, never the list being built, which grows.
    skip_carried = bool(merged)
    for entry in current:
        if skip_carried and entry.get("carried") is True:
            continue
        merged.append(dict(entry))
    return merged[-_MAX_CARRIED_INTERACTIONS:]


def _merge_observed_acted_pages(prior: list[ObservedPage], flow_evidence: list[dict[str, Any]]) -> list[ObservedPage]:
    """Fold this turn's flow-evidence trajectory into the persisted summary.

    Keyed by safe origin plus location fingerprint; a later observation of the same page replaces the earlier one,
    and a bounded-schema or interaction observation never regresses to a weaker
    one for the same page.
    """
    by_location: dict[tuple[str, str], ObservedPage] = {}
    for page in prior:
        safe_url = safe_page_origin(page.url)
        fingerprint = page.location_fingerprint or page_location_fingerprint(page.url)
        if not safe_url or not fingerprint:
            continue
        by_location[(safe_url, fingerprint)] = ObservedPage(
            url=safe_url,
            location_fingerprint=fingerprint,
            had_bounded_schema=page.had_bounded_schema,
            reached_via=page.reached_via,
        )
    for entry in flow_evidence:
        evidence = entry.get("evidence")
        url = entry.get("url")
        if (not isinstance(url, str) or not url.strip()) and isinstance(evidence, dict):
            url = evidence.get("current_url") or evidence.get("inspected_url")
        if not isinstance(url, str) or not url.strip():
            continue
        safe_url = safe_page_origin(url)
        if not safe_url:
            continue
        fingerprint = (
            str(evidence.get("current_url_location_fingerprint") or "") if isinstance(evidence, dict) else ""
        ) or page_location_fingerprint(url)
        if not fingerprint:
            continue
        location_key = (safe_url, fingerprint)
        existing = by_location.get(location_key)
        had_schema = bool(entry.get("had_bounded_schema")) or (existing.had_bounded_schema if existing else False)
        reached_via = str(entry.get("reached_via") or (existing.reached_via if existing else ""))
        if existing and existing.reached_via == "interaction":
            reached_via = "interaction"
        by_location[location_key] = ObservedPage(
            url=safe_url,
            location_fingerprint=fingerprint,
            had_bounded_schema=had_schema,
            reached_via=reached_via,
        )
    return list(by_location.values())[-_MAX_OBSERVED_ACTED_PAGES:]


def finalize_observation_context(ctx: Any, raw_context: str | None) -> str | None:
    """Fold durable observation evidence into the outgoing global LLM context.

    Called from agent.py's `_make_agent_result` factory so every AgentResult
    exit path — timeout, cancel, max-turns, output-policy block, request-
    policy clarification, infeasibility clarification, non-retriable nav error,
    normal translate-result, missing-SDK fallback, unexpected-error fallback —
    carries the updated count.

    Returns None when there is nothing to record and no prior context, so the
    pre-existing 'no global_llm_context' behaviour is preserved.
    """
    prior_inspections = int(getattr(ctx, "prior_page_inspection_calls_made", 0) or 0)
    inspections_this_turn = int(getattr(ctx, "page_inspection_calls_this_turn", 0) or 0)
    flow_evidence = getattr(ctx, "flow_evidence", None) or []
    raw_scout_trajectory = getattr(ctx, "scout_trajectory", None)
    scout_trajectory = raw_scout_trajectory if isinstance(raw_scout_trajectory, Sequence) else ()
    raw_inventory = getattr(ctx, "scouted_credential_field_inventory_by_credential_id", None)
    raw_entrypoint_url = getattr(ctx, "resolved_discovery_entrypoint_url", None)
    resolved_entrypoint_url = raw_entrypoint_url if isinstance(raw_entrypoint_url, str) else None
    carried_this_turn = _carried_trajectory_from_scout_trajectory(
        [interaction for interaction in scout_trajectory if isinstance(interaction, Mapping)],
        credential_field_inventory=raw_inventory if isinstance(raw_inventory, Mapping) else None,
    )
    if (
        not raw_context
        and inspections_this_turn == 0
        and not flow_evidence
        and not carried_this_turn
        and not resolved_entrypoint_url
    ):
        return None
    sc = StructuredContext.from_json_str(raw_context)
    if resolved_entrypoint_url:
        sc.entrypoint_url = resolved_entrypoint_url
    sc.page_inspection_calls_made = prior_inspections + inspections_this_turn
    sc.observed_acted_pages = _merge_observed_acted_pages(sc.observed_acted_pages, flow_evidence)
    sc.carried_trajectory = _merge_carried_trajectory(sc.carried_trajectory, carried_this_turn)
    if sc.carried_trajectory:
        LOG.info(
            "copilot_carried_trajectory_persisted",
            source_url=sc.carried_trajectory[0].get("source_url"),
            interaction_count=len(sc.carried_trajectory),
            this_turn_count=len(carried_this_turn),
        )
    return sc.to_json_str()


_MAX_APPROVED_CREDENTIALS = 20
# Copilot re-asks about the same credential a handful of times before the user answers — the
# production chat this carry was built for asked four. Past that the conversation has moved on,
# whatever the turns are shaped like.
_MAX_PROPOSAL_CARRIES = 5


def record_approved_credentials_in_global_llm_context(ctx: CopilotContext, raw_context: str | None) -> str | None:
    """Persist resolved credentials as durable cross-turn approval. Records only from
    resolved_credentials, never discovered_credentials, so ADR-0002's run/draft split
    holds by construction. A credential a live login page vouched for is left out: its
    evidence is that page, which a later turn has not seen, so it must be re-earned there.

    Google connections are recorded on the same terms but from the account the user picked,
    since connections never enter resolved_credentials. Without this the pick authorizes only
    the turn it arrived on, and a workflow that has not yet persisted re-asks every turn.
    """
    policy = ctx.request_policy
    if policy is None or not (policy.resolved_credentials or policy.selected_connected_account_id):
        return raw_context
    sc = StructuredContext.from_json_str(raw_context)
    if policy.selected_connected_account_id is not None and policy.selected_connected_account_id not in {
        record.credential_id for record in sc.approved_connections
    }:
        sc.approved_connections.append(ApprovedCredential(credential_id=policy.selected_connected_account_id))
        if len(sc.approved_connections) > _MAX_APPROVED_CREDENTIALS:
            sc.approved_connections = sc.approved_connections[-_MAX_APPROVED_CREDENTIALS:]
    existing_ids = {record.credential_id for record in sc.approved_credentials}
    for credential in policy.resolved_credentials:
        # A credential the user picked from the card is durable approval even though the resume
        # stamped an origin for it; only page-vouched ids have to be re-earned.
        # A stamp the carry restored is not page evidence the user has to re-earn: without this a
        # user who answers by naming the credential gets no durable approval, which is the re-ask
        # loop this amendment exists to end. A stamp a live page wrote this turn still counts.
        settled_ids = policy.current_turn_named_credential_ids
        # Read at turn end, when what the user settled is final: a card pick or a typed name landing
        # after the citation is still them answering this ask by choosing a different credential.
        carry_stamp_the_user_answered = (
            credential.credential_id in policy.seeded_proposal_credential_ids
            and credential.credential_id in policy.carry_cited_credential_ids
            and not (settled_ids and credential.credential_id not in settled_ids)
        )
        stamped_by_page = (
            credential.credential_id in policy.live_page_admitted_urls
            and credential.credential_id != ctx.credential_pause_connected_credential_id
            and not carry_stamp_the_user_answered
        )
        if credential.credential_id in existing_ids or stamped_by_page:
            continue
        # An approval minted from a carry keeps the origin that vouched for the credential. Without
        # it the id is resolved on every later turn with nothing pinning it, and the fill seam's
        # user-provided-site route then reaches any site named anywhere in the conversation.
        sc.approved_credentials.append(
            ApprovedCredential(
                credential_id=credential.credential_id,
                admitted_url=(
                    policy.live_page_admitted_urls.get(credential.credential_id, "")
                    if carry_stamp_the_user_answered
                    else ""
                ),
            )
        )
        existing_ids.add(credential.credential_id)
    if len(sc.approved_credentials) > _MAX_APPROVED_CREDENTIALS:
        sc.approved_credentials = sc.approved_credentials[-_MAX_APPROVED_CREDENTIALS:]
    return sc.to_json_str()


def _live_page_admitted_proposal(policy: RequestPolicy) -> ProposedCredential | None:
    """The credential a login page admitted on this turn, when it admitted exactly one.

    An id the carry itself seeded is not evidence: hydration restores the same two fields a fresh
    admission writes, so counting it here would re-mint the carry from its own state every turn.
    """
    # Count only what a page admitted this turn. Hydration appends the carry to the same list, so
    # counting it would bail whenever a page freshly admits a second credential — dropping the new
    # one and renewing the stale carry, on the turn copilot found a different login.
    admitted_this_turn = [
        credential
        for credential in policy.auto_bound_credentials
        if credential.credential_id not in policy.seeded_proposal_credential_ids
    ]
    if len(admitted_this_turn) != 1:
        return None
    credential_id = admitted_this_turn[0].credential_id
    admitted_url = policy.live_page_admitted_urls.get(credential_id) or ""
    if not admitted_url:
        return None
    return ProposedCredential(
        credential_id=credential_id,
        admitted_url=safe_admitted_url(admitted_url),
        origin_arm="live_page_admitted",
    )


def _carried_proposal_still_unanswered(policy: RequestPolicy, sc: StructuredContext) -> ProposedCredential | None:
    """The carry from an earlier turn, kept while the ask it was recorded for is still outstanding.

    Copilot asks about the same credential across several turns before the user answers, so a carry
    that died on the first re-ask would restore the loop this exists to end. The carry retires the
    moment the answer lands: an answered proposal is durable approval, and a turn that settles a
    different credential is the user declining this one. A turn that does not end by asking clears
    it, since the recorder only reaches here on an ask.
    """
    seed = sc.proposed_credential
    if seed is None or seed.credential_id not in policy.seeded_proposal_credential_ids:
        return None
    return seed


def _executed_credential_fill_proposal(ctx: CopilotContext) -> ProposedCredential | None:
    """The credential the server itself filled this turn, when the turn ran exactly one such fill.

    Read from this turn's own trajectory, never the persisted record: ``_merge_carried_trajectory``
    keeps earlier turns' entries verbatim and unmarked, so a filter over the merged list would match
    every historical fill and re-derive the carry forever. ``credentials_checked`` is not a source
    either — a vault lookup by name has no page behind it, so it leaves no origin to restore.
    """
    raw_trajectory = getattr(ctx, "scout_trajectory", None)
    trajectory = raw_trajectory if isinstance(raw_trajectory, Sequence) else ()
    fills = [
        entry
        for entry in trajectory
        if isinstance(entry, Mapping)
        and entry.get("carried") is not True
        and entry.get("tool_name") == CREDENTIAL_FILL_TOOL_NAME
        and isinstance(entry.get("credential_id"), str)
        and entry.get("credential_id")
        and isinstance(entry.get("executed_selector"), str)
        and entry.get("executed_selector")
        and isinstance(entry.get("source_url"), str)
        and entry.get("source_url")
    ]
    if len({str(entry["credential_id"]) for entry in fills}) != 1:
        return None
    return ProposedCredential(
        credential_id=str(fills[-1]["credential_id"]),
        admitted_url=safe_admitted_url(str(fills[-1]["source_url"])),
        origin_arm="executed_credential_fill",
    )


def _proposal_that_survives_this_turn(
    proposed: ProposedCredential, policy: RequestPolicy, sc: StructuredContext
) -> ProposedCredential | None:
    """Apply the retirement rules to whichever arm produced the proposal.

    Held here rather than in the arms because each arm otherwise enforces its own subset: a turn that
    ran a fill would mint a fresh record every time, resetting the count and outliving both the
    answer and a different credential the user settled — the login-retry loop the fill arm exists for
    is exactly where that ran forever.
    """
    if proposed.credential_id in policy.carry_cited_credential_ids:
        return None
    settled_ids = policy.current_turn_named_credential_ids
    if settled_ids and proposed.credential_id not in settled_ids:
        return None
    prior = sc.proposed_credential
    # Fresh evidence for a credential the user still has not answered for is the same unanswered ask,
    # so the count follows the credential rather than the arm that produced this turn's record.
    carries = prior.carries + 1 if prior is not None and prior.credential_id == proposed.credential_id else 0
    if carries >= _MAX_PROPOSAL_CARRIES:
        return None
    return proposed.model_copy(update={"carries": carries})


def record_proposed_credential_in_global_llm_context(
    ctx: CopilotContext, raw_context: str | None, response_type: str
) -> str | None:
    """Carry the credential the server itself bound or filled, when the turn ends by asking the user,
    so the next turn can verify a model citation against it. The write is unconditional in both
    directions, so any turn reaching here expires an older record.
    """
    policy = ctx.request_policy
    sc = StructuredContext.from_json_str(raw_context)
    proposed: ProposedCredential | None = None
    if policy is not None and response_type == "ASK_QUESTION":
        proposed = (
            _live_page_admitted_proposal(policy)
            or _executed_credential_fill_proposal(ctx)
            or _carried_proposal_still_unanswered(policy, sc)
        )
        if proposed is not None:
            proposed = _proposal_that_survives_this_turn(proposed, policy, sc)
        # The card settles its own id within the turn, so a card answer never needs the carry — and
        # a card answer naming a different credential retires the proposal it replaced.
        if proposed is not None and ctx.credential_pause_connected_credential_id is not None:
            proposed = None
    if proposed is None:
        return clear_proposed_credential(raw_context)
    LOG.info(
        "copilot_credential_proposal_recorded",
        credential_id=proposed.credential_id,
        origin_arm=proposed.origin_arm,
        admitted_url=loggable_origin(proposed.admitted_url),
    )
    if sc.proposed_credential == proposed:
        return raw_context
    sc.proposed_credential = proposed
    return sc.to_json_str()


def clear_proposed_credential(raw_context: str | None) -> str | None:
    """Drop the one-turn credential proposal from a context a turn is about to persist. Every path
    that persists without running the recorder above must call this, or the proposal outlives its
    turn and re-grants fill authority later.
    """
    if raw_context is None:
        return raw_context
    sc = StructuredContext.from_json_str(raw_context)
    if sc.proposed_credential is None:
        return raw_context
    sc.proposed_credential = None
    return sc.to_json_str()


def adopt_model_authored_context(trusted_raw: str | None, model_raw: object) -> StructuredContext:
    """Take the model's context but keep the server-owned fields server-owned.

    Approval is recorded only from server-resolved credentials; an entry the model
    supplied would be promoted into `resolved_credentials` on the next turn and clear
    the unapproved-credential gate for a credential the user never named. Membership
    of the org is not evidence the user named it. `approved_connections` is server-owned
    for the same reason: a model-authored entry would grant its own draft a run.

    `carried_trajectory` is the record of what the browser was observed doing, so an
    entry the model wrote would enter the factual record as an observation nothing made.
    It also displaces the real one: the turn-end merge treats whatever arrives here as the
    prior record and drops this turn's re-hydrated entries against it, so a model-authored
    list survives and the observed one does not.
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
    structured.approved_connections = list(trusted.approved_connections)
    structured.proposed_credential = trusted.proposed_credential
    structured.carried_trajectory = [dict(entry) for entry in trusted.carried_trajectory]
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
    # Model name for the terminal attempt (primary or fallback), including an interrupted attempt.
    # None when the turn never reached an attempt; telemetry only.
    resolved_model: str | None = None
    # Set when the agent absorbed an asyncio cancellation initiated by an
    # explicit user Stop. Lets the route route to a cancel-specific
    # persistence path (rollback + stop-report chat row) without
    # losing ``workflow_was_persisted`` the way a re-raise would.
    cancelled: bool = False
    # Facts the route needs to persist an interruption record; the route sees the
    # AgentResult, never the context that recorded them. None when unknown.
    cancellation_iteration: int | None = None
    cancellation_last_recorded_phase: str | None = None
    cancellation_workflow_run_id: str | None = None
    # Controls whether the route may auto-apply the proposal or must force explicit review.
    proposal_disposition: ProposalDisposition = "auto_applicable"
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
    executed_block_fingerprints: dict[str, set[str]] = field(default_factory=dict)
    # Legacy marker for turns that wrote canonical before snapshot isolation.
    # Terminal handlers retain rollback support for those persisted turns.
    canonical_was_persisted_due_to_param_change: bool = False
    # Exact model-owned contract deletion must survive to the auto-accept write; ordinary saves
    # still preserve a stored contract when their rebuilt definition omits this Copilot field.
    clear_persisted_completion_contract: bool = False
    # Criteria lifecycle decision + adjudication counters the route persists
    # after the turn; None when persisted criteria are disabled.
    completion_criteria_turn_state: CompletionCriteriaTurnState | None = None
    # Internal eval-only metadata. The normal response schema never serializes this field.
    browser_ablation_metadata: BrowserAblationMetadata | None = None


@dataclass(frozen=True)
class InFlightStreamToolCall:
    call_id: str
    tool_name: str
    iteration: int
    display_label: str | None = None


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
    copilot_cancel_token: str | None = None
    copilot_question_pause_seconds: float = 0.0
    human_input_wait: HumanInputWait = field(default_factory=HumanInputWait)
    eval_capture_case_id: str | None = None
    eval_mode: CopilotEvalMode | None = None
    eval_prompt_sha256: str | None = None
    eval_tool_surface_sha256: str | None = None
    eval_native_tool_names: tuple[str, ...] = ()
    eval_mcp_tool_names: tuple[str, ...] = ()
    eval_tool_activity: list[dict[str, Any]] = field(default_factory=list)
    eval_screenshot_frames: list[dict[str, Any]] = field(default_factory=list)

    # Enforcement state
    navigate_called: bool = False
    observation_after_navigate: bool = False
    update_workflow_called: bool = False
    test_after_update_done: bool = False
    copilot_total_timeout_exceeded: bool = False
    copilot_turn_cancelled_iteration: int | None = None
    copilot_max_turns_exceeded: bool = False
    budget_expiry_state: BudgetExpiryState = field(default_factory=BudgetExpiryState)
    check_model_work_deadline: Callable[[], None] | None = field(default=None, repr=False)
    model_calls_this_turn: int = 0
    tool_calls_this_turn: int = 0
    enforcement_pass_count: int = 0
    pre_run_gated_output_warning_fingerprint: tuple[tuple[str, str, bool, str], ...] = ()
    user_message: str = ""
    block_goal_main_goal: str = ""
    allow_untested_workflow_draft: bool = False
    request_policy: RequestPolicy | None = None
    copilot_config: CopilotConfig | None = None
    block_authoring_policy: BlockAuthoringPolicy = BlockAuthoringPolicy.STANDARD
    target_block_label: str | None = None
    selected_block_label: str | None = None
    turn_context_packet: TurnContextPacket | None = None
    prior_turn_outcome: TurnOutcome | None = None
    # Server-verified display data for recovering from a model-staged Google
    # connection that has no run authority. This never grants authority itself.
    connected_account_recovery_choices: list[ConnectedAccountChoice] = field(default_factory=list)
    latest_diagnosis_repair_contract: DiagnosisRepairContract | None = None
    blocked_reply_signatures: list[str] = field(default_factory=list)

    # Mid-build credential pause (credential_pause.py). last_run_skipped_unbound_credentials
    # is set by tools/__init__.py's update_and_run_blocks skip branch; client_supports_credential_pause
    # is set from the chat request at construction; the rest are owned by maybe_credential_pause.
    last_run_skipped_unbound_credentials: bool = False
    client_supports_credential_pause: bool = False
    credential_pause_used: bool = False
    # True only while a card is on screen. credential_pause_used stays true for the rest of the
    # turn once one has been raised, which cannot tell a concurrent sibling ask from a later one.
    credential_ask_in_flight: bool = False
    # A tool ask the user did not answer with a credential spends the one-card budget on a guess.
    # A run that then hits a real login wall has evidence the guess did not, so it gets the budget
    # back once.
    credential_pause_reaskable_by_run: bool = False
    copilot_credential_pause_seconds: float = 0.0
    credential_pause_outcome: str | None = None
    credential_pause_connected_credential_id: str | None = None
    # Set while a ``request_credential`` ask is open, so tool calls issued alongside it in the same
    # model response wait for the user's answer instead of racing it.
    credential_pause_settled: asyncio.Event | None = None
    # Preserve the immutable turn-open document because ``workflow_yaml`` is
    # reassigned after every accepted update in the same agent turn.
    google_connection_turn_start_workflow_yaml: str | None = field(init=False, default=None)
    google_connection_turn_start_bindings: tuple[GoogleSheetConnectionBinding, ...] | None = None
    google_connection_notices: list[GoogleConnectionNotice] = field(default_factory=list)

    # Tool tracking
    tool_activity: list[dict[str, Any]] = field(default_factory=list)
    # A goal-satisfied stop raised from on_tool_end ends the SDK stream before
    # the satisfying tool's tool_output event flushes; these carry what the
    # exit path needs to emit the missing TOOL_RESULT frame.
    in_flight_stream_tool_call: InFlightStreamToolCall | None = None
    goal_satisfied_tool_name: str | None = None
    goal_satisfied_tool_output: dict[str, Any] | None = None
    # Stashed by the write seam under the id of the tool call that produced it, and drained by
    # that same call's result: parallel tool calls are the provider default, so an arrival-ordered
    # stash would show a write's patch on a sibling's row. The budget is a dataclass default, so a
    # new turn's context starts with the full allowance.
    pending_code_write_diffs: dict[str, list[CodeWriteDiff]] = field(default_factory=dict)
    code_write_patch_budget: int = TURN_PATCH_CHAR_BUDGET
    latest_tool_blocker_signal: CopilotToolBlockerSignal | None = None
    tool_blocker_signals: list[CopilotToolBlockerSignal] = field(default_factory=list)
    turn_halt: TurnHalt | None = None

    # ``None`` until usage is observed; ``0`` only when a provider explicitly
    # reported zero. Distinct values let cost grading flag missing telemetry.
    total_tokens_used: int | None = None
    input_tokens_used: int | None = None
    output_tokens_used: int | None = None
    resolved_model: str | None = None

    # Workflow state
    persisted_workflow_yaml: str | None = None
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
    # Block-running tools make their own run context available for same-turn
    # reporting. This is deliberately not persisted across turns. The
    # successful variant is kept for "default to the last clean result"; the
    # generic variant allows the agent to re-read the same failed/canceled run
    # after a watchdog reconciliation read has cleared the retry guard.
    last_run_blocks_workflow_run_id: str | None = None
    last_successful_run_blocks_workflow_run_id: str | None = None
    # Runs this turn actually dispatched. ``last_run_blocks_workflow_run_id`` cannot answer that:
    # a repair turn is seeded with the run it was opened about, which this turn never dispatched.
    dispatched_run_ids_this_turn: set[str] = field(default_factory=set)
    # The browser session the last run actually executed in. On the fresh-session replay path this
    # is not ctx.browser_session_id, which stays pointed at the debug/scout browser.
    last_run_blocks_browser_session_id: str | None = None
    # In-turn run-outcome trace derived from assignments to ``last_run_outcome``
    # (the same source that powers run_outcome SSE frames). Append-only across
    # per-run pointer resets (``last_run_outcome = None``) and workflow edits.
    terminal_envelope_run_outcomes: list[RecordedRunOutcome] = field(default_factory=list)
    # Consecutive failed runs where navigation completed but the scraper
    # could not read the page (generic "failed to load the website" template).
    # Resets on any non-matching run outcome. Streak crosses workflow-shape
    effective_workflow_proxy_location: Any | None = None

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
    executed_block_labels: set[str] = field(default_factory=set)
    executed_block_fingerprints: dict[str, set[str]] = field(default_factory=dict)
    last_full_workflow_test_ok: bool = False
    last_unverified_block_labels: list[str] = field(default_factory=list)
    workflow_verification_evidence: WorkflowVerificationEvidence = field(default_factory=WorkflowVerificationEvidence)
    last_frontier_start_label: str | None = None
    pending_code_authoring_runtime_repair_context: CodeAuthoringRepairContext | None = None
    last_code_authoring_repair_context: CodeAuthoringRepairContext | None = None
    latest_recorded_build_test_outcome: RecordedBuildTestOutcome | None = None
    recorded_build_test_outcome_history: list[dict[str, object]] = field(default_factory=list)
    recorded_persisted_block_run_workflow_run_id: str | None = None
    # Set by _record_run_blocks_result when the most recent failed run matches
    # SKIP_INNER_NAV_RETRY_ERRORS (DNS / cert / SSL / invalid URL). Drives the
    # one-shot non-retriable-nav stop nudge and the deterministic exit-path
    # exception in run_with_enforcement. Cleared at the top of every call to
    # _record_run_blocks_result so stale state can't leak across runs.
    last_test_non_retriable_nav_error: str | None = None
    # Secure-runner codes from the latest run that were faults of the sandbox itself, joined.
    # Cleared per run in _record_run_blocks_result, so a later clean run releases the guard.
    last_infrastructure_tool_error: str | None = None
    # Normalized signature of the non-retriable nav error last nudged on.
    # Lets the stop nudge re-fire if the user retries with a different bad URL
    # (different signature) in the same session. Cleared on meaningful success.
    last_failure_category_top: str | None = None

    copilot_run_start_monotonic: float | None = None

    last_good_workflow: Workflow | None = None
    last_good_workflow_yaml: str | None = None

    # Populated lazily by ``stream_to_sse`` and reused across enforcement
    # iterations so cadence/last-emitted-at survive ``run_with_enforcement``
    # retries. Declared here (rather than attached dynamically) so future
    # refactors can't strip it silently.
    narrator_state: NarratorState | None = None

    prior_page_inspection_calls_made: int = 0
    page_inspection_calls_this_turn: int = 0
    discovery_step_count: int = 0
    discovery_evidence_trail: list[dict[str, Any]] = field(default_factory=list)
    resolved_discovery_entrypoint_url: str | None = None
    resolved_discovery_failure_reason: str | None = None
    resolved_discovery_entrypoint_inspection_baseline: int = 0
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
    narrative_summary: str | None = None

    staged_workflow_yaml: str | None = None
    staged_workflow: Workflow | None = None
    has_staged_proposal: bool = False
    # The chat row's setting, not the turn's commit decision: the route can still refuse to apply a
    # staged draft at turn end. None on entrypoints that load no chat row.
    auto_accept: bool | None = None
    # Prior turn's uncommitted draft; carries blocks even when the request body and canonical row are empty.
    prior_copilot_workflow_yaml: str | None = None
    # Legacy marker for turns that wrote canonical before snapshot isolation.
    # Terminal handlers retain rollback support for those persisted turns.
    canonical_was_persisted_due_to_param_change: bool = False
    clear_persisted_completion_contract: bool = False
    completion_criteria_turn_state: CompletionCriteriaTurnState | None = None
    prior_block_count: int | None = None
    block_state_map: dict[str, str] = field(default_factory=dict)
    block_started_at_map: dict[str, str] = field(default_factory=dict)
    block_ended_at_map: dict[str, str] = field(default_factory=dict)
    # Keyed by label, so a label that ran more than once in a turn keeps only its
    # last run's identity.
    block_run_identity_map: dict[str, BlockRunIdentity] = field(default_factory=dict)
    turn_started_at: str | None = None
    turn_ended_at: str | None = None

    def __post_init__(self) -> None:
        parent_post_init = getattr(super(), "__post_init__", None)
        if callable(parent_post_init):
            parent_post_init()
        from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome

        self.google_connection_turn_start_workflow_yaml = self.workflow_yaml

        if isinstance(self.last_run_outcome, RecordedRunOutcome):
            super().__setattr__("terminal_envelope_run_outcomes", [self.last_run_outcome])

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name != "last_run_outcome":
            return
        from skyvern.forge.sdk.copilot.run_outcome import RecordedRunOutcome

        if not isinstance(value, RecordedRunOutcome):
            # ``last_run_outcome = None`` is a per-run pointer reset, not an
            # archive reset. The trace retains the full in-order run record;
            # terminal projection deliberately anchors to its latest run.
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
            "ctx_last_workflow_present": self.last_workflow is not None,
        }
