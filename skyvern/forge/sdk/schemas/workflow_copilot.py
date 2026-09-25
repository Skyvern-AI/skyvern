from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    SecretStr,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

from skyvern.forge.sdk.copilot.ask_user import QuestionInteraction, QuestionResponse
from skyvern.forge.sdk.copilot.code_write_diff import CodeWriteDiff
from skyvern.forge.sdk.copilot.context import ProposalDisposition, ResponseType, TurnNarrativePayload
from skyvern.forge.sdk.copilot.run_outcome import RunOutcomeReasonCode, RunOutcomeRole, RunOutcomeVerdict
from skyvern.forge.sdk.schemas.copilot_turn_outcome import (
    CopilotCancelSource,
    PersistedCopilotComposerMode,
    TurnOutcome,
)
from skyvern.services.browser_recording.evidence import RecordingEvidencePacket
from skyvern.utils.secret_headers import mask_header_values
from skyvern.utils.yaml_loader import dump_workflow_yaml, safe_load_no_dates


def client_visible_proposed_workflow(proposal: dict[str, Any]) -> dict[str, Any]:
    visible = dict(proposal)
    visible.pop(COPILOT_PRIVATE_SETTINGS_KEY, None)
    if "cdp_connect_headers" in visible:
        headers = visible["cdp_connect_headers"]
        visible["cdp_connect_headers"] = mask_header_values(headers) if isinstance(headers, dict) else None
    if "_copilot_yaml" in visible:
        try:
            document = safe_load_no_dates(visible["_copilot_yaml"])
            if not isinstance(document, dict):
                visible.pop("_copilot_yaml")
            else:
                changed = False
                if "cdp_connect_headers" in document:
                    headers = document["cdp_connect_headers"]
                    document["cdp_connect_headers"] = mask_header_values(headers) if isinstance(headers, dict) else None
                    changed = True
                if changed:
                    visible["_copilot_yaml"] = dump_workflow_yaml(document)
        except Exception:  # noqa: BLE001 - Malformed legacy YAML must never bypass redaction.
            visible.pop("_copilot_yaml")
    return visible


ClientVisibleProposedWorkflow = Annotated[
    dict[str, Any], PlainSerializer(client_visible_proposed_workflow, when_used="json")
]

COPILOT_PROPOSAL_METADATA_KEY = "_copilot_proposal"
COPILOT_PRIVATE_SETTINGS_KEY = "_copilot_private_settings"
CopilotCandidateDisposition = Literal[
    "no_proposal",
    "auto_applicable",
    "review_untested",
    "review_tested",
    "accepting",
]


# An accept holds the candidate exclusively between its claim and its canonical write, so nothing
# can slip a newer candidate underneath the bytes it already read. The claim expires because the
# same outage that kills an accept mid-flight also kills the write that would release it.
COPILOT_PROPOSAL_CLAIM_LEASE = timedelta(minutes=5)


class CopilotProposalMetadata(BaseModel):
    """CAS token and exact run ownership for one durable Copilot candidate."""

    model_config = ConfigDict(extra="forbid")

    owner_turn_id: str
    revision: int = Field(ge=1)
    canonical_fingerprint: str
    # The canonical title at publication, or None on proposals written before it was recorded. A
    # write-back compares against it to tell a rename that landed since from the title it froze.
    canonical_title: str | None = None
    disposition: CopilotCandidateDisposition
    workflow_run_id: str | None = None
    claimed_at: datetime | None = None

    def claim_is_live(self, now: datetime) -> bool:
        if self.disposition != "accepting":
            return False
        if self.claimed_at is None:
            return False
        return now - self.claimed_at < COPILOT_PROPOSAL_CLAIM_LEASE

    def claim_expires_in(self, now: datetime) -> float | None:
        """Seconds left on a live claim, or None when no claim holds the candidate."""
        if self.claimed_at is None or not self.claim_is_live(now):
            return None
        return (self.claimed_at + COPILOT_PROPOSAL_CLAIM_LEASE - now).total_seconds()


class CopilotProposalRunOutput(BaseModel):
    output_parameter_id: str
    value: JsonValue


class CopilotProposalRunFacts(BaseModel):
    workflow_run_id: str
    status: str | None = None
    available: bool
    failure_reason: str | None = None
    outputs: list[CopilotProposalRunOutput] = Field(default_factory=list)


def copilot_proposal_metadata(value: object) -> CopilotProposalMetadata | None:
    if not isinstance(value, dict):
        return None
    raw = value.get(COPILOT_PROPOSAL_METADATA_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return CopilotProposalMetadata.model_validate(raw)
    except ValueError:
        return None


class CopilotPendingTurn(BaseModel):
    """Durable write-ahead marker for one in-flight copilot turn.

    Written in the same transaction as the turn's user message so a hard kill
    leaves enough state to reconcile the turn and roll canonical back.
    """

    model_config = ConfigDict(extra="ignore")

    turn_id: str
    started_at: datetime
    pre_turn_workflow: dict[str, Any] | None = None
    pre_turn_proposed_workflow: dict[str, Any] | None = None
    keep_pending_proposal: bool = False
    idempotency_digest: str | None = None
    copilot_effective_mode: PersistedCopilotComposerMode | None = None
    copilot_code_available: bool = False
    user_message_id: str | None = None
    recovering_at: datetime | None = None
    # Fingerprint of the canonical workflow as this turn last left it. None means the turn
    # never wrote canonical, so it owns no write to roll back.
    canonical_write_fingerprint: str | None = None
    question_interactions: list[QuestionInteraction] = Field(default_factory=list)
    question_heartbeat_at: datetime | None = None
    question_client_seen_at: datetime | None = None
    cancel_token: str | None = None
    # Build-test runs this turn ended so it could take the chat's browser. Copilot owns this list;
    # the run row's failure_reason carries the same fact but a later finalizer can overwrite it.
    superseded_build_test_run_ids: list[str] = Field(default_factory=list)


class WorkflowCopilotChat(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workflow_copilot_chat_id: str = Field(..., description="ID for the workflow copilot chat")
    organization_id: str = Field(..., description="Organization ID for the chat")
    workflow_permanent_id: str = Field(..., description="Workflow permanent ID for the chat")
    proposed_workflow: ClientVisibleProposedWorkflow | None = Field(
        None, description="Latest workflow proposed by the copilot"
    )
    auto_accept: bool | None = Field(False, description="Whether copilot auto-accepts workflow updates")
    pending_turns: dict[str, CopilotPendingTurn] = Field(
        default_factory=dict, description="In-flight turns keyed by turn id"
    )
    work_plan: list[str] = Field(default_factory=list, description="Latest work plan the copilot model wrote")

    @field_serializer("pending_turns", when_used="json")
    def _client_visible_pending_turns(self, turns: dict[str, CopilotPendingTurn]) -> dict[str, Any]:
        serialized = {turn_id: turn.model_dump(mode="json") for turn_id, turn in turns.items()}
        for turn in serialized.values():
            proposal = turn.get("pre_turn_proposed_workflow")
            if proposal is not None:
                turn["pre_turn_proposed_workflow"] = client_visible_proposed_workflow(proposal)
        return serialized

    @field_validator("pending_turns", mode="before")
    @classmethod
    def _default_pending_turns(cls, value: dict[str, Any] | None) -> dict[str, Any]:
        return value or {}

    @field_validator("work_plan", mode="before")
    @classmethod
    def _default_work_plan(cls, value: list[str] | None) -> list[str]:
        return value or []

    created_at: datetime = Field(..., description="When the chat was created")
    modified_at: datetime = Field(..., description="When the chat was last modified")


class WorkflowCopilotCompletionCriteriaSet(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    completion_criteria_set_id: str
    organization_id: str
    workflow_copilot_chat_id: str
    goal_epoch: int
    status: str
    criteria: list[dict[str, Any]]
    source_turn_id: str | None = None
    source_goal_text: str | None = None
    consecutive_all_no_evidence: int = 0
    tripwire_fired: bool = False
    last_fully_satisfied_workflow_yaml: str | None = None
    superseded_by_set_id: str | None = None
    superseded_at: datetime | None = None
    supersede_reason: str | None = None
    created_at: datetime
    modified_at: datetime


CriteriaSetNonAdoptableReason = Literal["unknown_shape", "undecodable_v1_criteria"]


@dataclass(frozen=True)
class NonAdoptableCriteriaSet:
    """A persisted criteria-set row whose shape cannot be decoded into the current
    model; callers must treat the row as absent/superseded, never as an empty contract."""

    reason: CriteriaSetNonAdoptableReason
    completion_criteria_set_id: str
    goal_epoch: int


class WorkflowCopilotChatSender(StrEnum):
    USER = "user"
    AI = "ai"
    PRODUCT = "product"


# A product row records a click the user made, so it opens a turn on the human side of
# the conversation; it is never rendered to the model as an assistant utterance.
TURN_OPENER_SENDERS = frozenset({WorkflowCopilotChatSender.USER, WorkflowCopilotChatSender.PRODUCT})


def chat_history_role(sender: WorkflowCopilotChatSender) -> str:
    """The label a transcript line carries into the model. ``product`` is not a role the model is
    told about, so a product row speaks in the same voice as the user whose click created it."""
    return WorkflowCopilotChatSender.USER.value if sender in TURN_OPENER_SENDERS else sender.value


_MAX_ATTACHED_FILENAME_CHARS = 255
# The prompt lists this many files per turn, current message first, so every file one message
# can carry is always visible to the model.
MAX_ATTACHED_FILES_PER_MESSAGE = 20


def _bounded_filename(value: Any) -> Any:
    """Cap a stored display name: the upload limit measures only file contents, and this name is
    replayed into every later prompt of the chat."""
    return value[:_MAX_ATTACHED_FILENAME_CHARS] if isinstance(value, str) else value


def _attached_files_or_empty(value: Any) -> Any:
    """A row written before the column existed reads back as NULL, and ``model_validate`` on the
    row passes that key explicitly — so a ``default_factory`` never fires and validation fails."""
    return [] if value is None else value


class CopilotVideoObservation(BaseModel):
    timestamp_seconds: float = Field(..., ge=0, le=300)
    description: str = Field(..., min_length=1, max_length=500)
    confidence: Literal["low", "medium", "high"]


class CopilotVideoEvidenceArtifact(BaseModel):
    """Compact, reusable perception output; raw video frames never enter the acting-model loop."""

    version: Literal["1"]
    duration_seconds: float = Field(..., gt=0, le=300)
    sampled_frame_count: int = Field(..., ge=1, le=120)
    observations: tuple[CopilotVideoObservation, ...] = Field(..., min_length=1, max_length=80)


class CopilotAttachedFile(BaseModel):
    """An uploaded file the user attached to a copilot turn.

    ``file_id`` is the only value a workflow dereferences: the storage URI behind it is read from
    the organization's ``uploaded_files`` row, never from this record. ``available`` is resolved
    fresh on every read, so an expired or deleted file reports as missing rather than as a
    reference the model can still use.
    """

    file_id: str = Field(..., description="Uploaded file id from POST /v1/upload_file")
    filename: Annotated[str, BeforeValidator(_bounded_filename)] = Field(
        ..., description="Display name recorded at upload time"
    )
    size_bytes: int | None = Field(None, description="Size recorded at upload time")
    available: bool = Field(True, description="Whether the file still resolves for this organization")
    video_safety_status: Literal["unsafe"] | None = Field(
        None,
        description="Server-owned sticky status for a video in which the safety boundary detected a raw secret",
    )
    video_processing_status: Literal["too_long"] | None = Field(
        None,
        description="Server-owned terminal status for a video that exceeds the supported duration",
    )
    video_evidence: CopilotVideoEvidenceArtifact | None = Field(
        None,
        description="Server-owned, reusable visual-observation timeline derived from an attached video",
    )


WorkflowCopilotMessageFeedbackRating = Literal["up", "down"]


class WorkflowCopilotMessageFeedback(BaseModel):
    rating: WorkflowCopilotMessageFeedbackRating = Field(..., description="Whether the turn did what the user asked")
    reason: str | None = Field(None, description="Optional free-text reason the user gave")
    rated_at: datetime = Field(..., description="When the rating was last set")


class WorkflowCopilotMessageFeedbackRequest(BaseModel):
    workflow_copilot_chat_id: str = Field(..., description="Chat that owns the rated message")
    workflow_copilot_chat_message_id: str | None = Field(None, description="Assistant message being rated")
    turn_id: str | None = Field(
        None, description="Turn whose assistant row is rated; a live turn knows this before the row id"
    )
    rating: WorkflowCopilotMessageFeedbackRating | None = Field(
        None, description="Thumbs up or down; null clears a previous rating"
    )
    reason: str | None = Field(None, max_length=2000, description="Optional free-text reason")

    @model_validator(mode="after")
    def _require_a_target(self) -> "WorkflowCopilotMessageFeedbackRequest":
        if not self.workflow_copilot_chat_message_id and not self.turn_id:
            raise ValueError("workflow_copilot_chat_message_id or turn_id is required")
        return self


class WorkflowCopilotMessageFeedbackResponse(BaseModel):
    workflow_copilot_chat_message_id: str
    feedback: WorkflowCopilotMessageFeedback | None


class WorkflowCopilotChatMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workflow_copilot_chat_message_id: str = Field(..., description="ID for the workflow copilot chat message")
    workflow_copilot_chat_id: str = Field(..., description="ID of the parent workflow copilot chat")
    sender: WorkflowCopilotChatSender = Field(..., description="Message sender")
    content: str = Field(..., description="Message content")
    audio_artifact_id: str | None = Field(None, description="Artifact ID for audio captured during dictation")
    attached_files: Annotated[list[CopilotAttachedFile], BeforeValidator(_attached_files_or_empty)] = Field(
        default_factory=list, description="Uploaded files the user attached to this message"
    )
    global_llm_context: str | None = Field(None, description="Optional global LLM context for the message")
    turn_outcome: TurnOutcome | None = Field(None, description="Typed turn outcome (assistant rows)")
    narrative_payload: TurnNarrativePayload | None = Field(
        None,
        description="Persisted narrative bubble snapshot; lets a reload re-render per-block cards.",
    )
    feedback: WorkflowCopilotMessageFeedback | None = Field(None, description="User's rating of this assistant turn")
    created_at: datetime = Field(..., description="When the message was created")
    modified_at: datetime = Field(..., description="When the message was last modified")


class WorkflowCopilotChatRequest(BaseModel):
    workflow_permanent_id: str = Field(..., description="Workflow permanent ID for the chat")
    workflow_id: str = Field(..., description="Workflow ID (mutable version ID)")
    workflow_copilot_chat_id: str | None = Field(None, description="The chat ID to send the message to")
    workflow_run_id: str | None = Field(None, description="The workflow run ID to use for the context")
    browser_session_id: str | None = Field(
        None,
        description="Optional persistent browser session ID to reuse instead of creating a new one.",
    )
    message: str = Field(..., description="The message that user sends")
    selected_connected_account_id: str | None = Field(
        None, description="Google account explicitly selected in the chat picker; validated against this organization."
    )
    selected_connected_account_from_pending_proposal: bool = Field(
        False,
        description=(
            "Server-set provenance for selected_connected_account_id: true when the route derived it from the "
            "pending proposal's binding instead of the user picking it. Carries no authority; a client-sent "
            "value can only weaken the recorded claim."
        ),
    )
    audio_artifact_id: str | None = Field(
        None,
        description="Artifact ID for audio captured while dictating this message.",
    )
    attached_file_ids: list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)]] = Field(
        default_factory=list,
        max_length=MAX_ATTACHED_FILES_PER_MESSAGE,
        description=(
            "Ids of files uploaded through POST /v1/upload_file that the user attached to this message. "
            "Only ids are accepted; the display name is read from this organization's own upload record."
        ),
    )
    workflow_yaml: str = Field(..., description="Current workflow YAML including unsaved changes")
    mode: Literal["build"] | None = Field(
        None,
        description="Deprecated Build-only compatibility field; cached Build clients may still send it.",
    )
    code_block: bool | None = Field(
        None,
        description="Per-request code-block authoring selection. Omission means plain non-code Build.",
    )
    cancel_token: str | None = Field(
        None,
        description=(
            "Client-generated UUID. POST it to /workflow/copilot/cancel to hard-cancel this turn. "
            "Optional; legacy clients omit it and cancel becomes a no-op for those requests."
        ),
    )
    idempotency_key: str | None = Field(
        None,
        max_length=256,
        description="Stable key for deduplicating a retried product action within this chat.",
    )
    target_block_label: str | None = Field(
        None,
        description=(
            "When set, the copilot regenerates only this code block from its goal and leaves every "
            "other block unchanged. Used by the block-level Generate action."
        ),
    )
    selected_block_label: str | None = Field(
        None,
        description=(
            "Label of the block currently selected on the studio canvas, if any. An ambient fact for "
            "resolving references like 'this block' — never a directive to act on that block."
        ),
    )
    supports_question_tool: bool = Field(False, description="The client can display and answer ask_user requests.")
    credential_recovery_token: SecretStr | None = Field(
        None,
        repr=False,
        exclude=True,
        description="Client-held capability for restoring this turn's pending credential card.",
    )
    supports_credential_pause_recovery: bool = Field(
        False, description="The client restores pending credential cards from chat history."
    )
    supports_credential_pause: bool = Field(
        False,
        description=(
            "True when the client can render the credential_required frame. Older clients default to False "
            "and silently drop unknown frame types, so the backend must not pause for them even if the "
            "server-side flag is on."
        ),
    )
    keep_pending_proposal: bool = Field(
        False,
        description=(
            "When true, a pending proposed_workflow from an earlier turn survives turns that end without "
            "a new proposal, so the client can keep rendering an actionable review gate."
        ),
    )
    product_action: Literal["test_end_to_end", "diagnose_run", "refine_recording"] | None = Field(
        None,
        description=(
            "Structured product action for this turn, dispatched by the server instead of the agent. "
            "'test_end_to_end' runs every block of the pending proposal in a browser session minted "
            "for that run. 'diagnose_run' opens repair on the finished run named by workflow_run_id. "
            "'refine_recording' infers a reusable workflow from recording_evidence."
        ),
    )
    recording_evidence: RecordingEvidencePacket | None = Field(
        None,
        description=(
            "Observation-only projection of a browser recording, required by the 'refine_recording' "
            "action. Reaches the model as untrusted evidence, never as part of the turn's message."
        ),
    )
    recording_in_progress: bool = Field(
        False,
        description=(
            "Set on an ordinary chat turn while a browser recording is capturing. The server attaches its own "
            "redacted projection of that recording as untrusted evidence; the client sends no recording content."
        ),
    )
    recording_deleted_step_ids: list[Annotated[str, StringConstraints(max_length=128)]] = Field(
        default_factory=list,
        max_length=500,
        description=(
            "Draft step ids the user deleted in the recording panel. The server drops them from its own "
            "draft before projecting live recording evidence; unknown ids are ignored."
        ),
    )
    eval_entrypoint_url: str | None = Field(
        None,
        description=(
            "Benchmark-only starting URL. Rejected unless the eval setting and the X-Copilot-Eval header "
            "are both present; never sent by the product."
        ),
    )


class WorkflowCopilotCancelRequest(BaseModel):
    workflow_copilot_chat_id: str | None = None
    cancel_token: str = Field(..., description="The cancel_token sent on the original /chat-post request")
    source: CopilotCancelSource | None = Field(
        default=None,
        description=(
            "Which gesture asked for the cancel, so cancel volume is attributable by source. "
            "Absent from a client too old to send it, which is recorded as unattributed rather "
            "than as an API caller."
        ),
    )


class WorkflowCopilotQuestionResponseRequest(QuestionResponse):
    workflow_copilot_chat_id: str
    interaction_id: str


class WorkflowCopilotCredentialResponseRequest(BaseModel):
    turn_id: str = Field(..., description="turn_id from the matching credential_required frame")
    workflow_copilot_chat_id: str = Field(..., description="chat ID from the matching credential_required frame")
    resume_token: str = Field(..., description="One-time resume token from the matching credential_required frame")
    action: Literal["connected", "skip"] = Field(..., description="The user's response to the credential card")
    credential_id: str | None = Field(None, description="Saved credential ID; required when action is 'connected'")


class WorkflowCopilotClearProposedWorkflowRequest(BaseModel):
    workflow_copilot_chat_id: str = Field(..., description="The chat ID to update")
    auto_accept: bool = Field(..., description="Whether to auto-accept future workflow updates")
    owner_turn_id: str | None = Field(None, description="Owner token returned with a typed proposal")
    revision: int | None = Field(None, ge=1, description="Revision token returned with a typed proposal")


class WorkflowCopilotDisableAutoAcceptRequest(BaseModel):
    workflow_copilot_chat_id: str = Field(..., description="The chat whose auto-accept should be turned off")


class WorkflowCopilotApplyProposedWorkflowRequest(BaseModel):
    workflow_copilot_chat_id: str = Field(..., description="The chat whose proposed workflow should be applied")
    auto_accept: bool = Field(
        False,
        description="If true, flip the chat to auto-accept mode so future turns persist directly without review",
    )
    owner_turn_id: str | None = Field(None, description="Owner token returned with a typed proposal")
    revision: int | None = Field(None, ge=1, description="Revision token returned with a typed proposal")


class WorkflowCopilotChatHistoryMessage(BaseModel):
    workflow_copilot_chat_message_id: str | None = Field(
        None, description="Persisted row id; absent on rows synthesized in-process"
    )
    sender: WorkflowCopilotChatSender = Field(..., description="Message sender")
    content: str = Field(..., description="Message content")
    turn_id: str | None = Field(None, description="Turn that owns this row")
    feedback: WorkflowCopilotMessageFeedback | None = Field(None, description="User's rating of this assistant turn")
    audio_artifact_id: str | None = Field(None, description="Artifact ID for captured dictation audio")
    attached_files: Annotated[list[CopilotAttachedFile], BeforeValidator(_attached_files_or_empty)] = Field(
        default_factory=list, description="Uploaded files the user attached to this message"
    )
    turn_outcome: TurnOutcome | None = Field(None, description="Typed turn outcome (assistant rows only)")
    narrative_payload: TurnNarrativePayload | None = Field(
        None,
        description="Persisted narrative bubble snapshot; lets a reload re-render per-block cards.",
    )
    created_at: datetime = Field(..., description="When the message was created")
    modified_at: datetime | None = Field(
        None,
        description="When this version was persisted; legacy in-process constructors may omit it",
    )


class WorkflowCopilotChatSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workflow_copilot_chat_id: str = Field(..., description="ID for the workflow copilot chat")
    workflow_permanent_id: str = Field(..., description="Workflow permanent ID the chat belongs to")
    workflow_title: str | None = Field(None, description="Title of the workflow the chat belongs to")
    title: str = Field(..., description="Single-line preview derived from the chat's first message")
    created_at: datetime = Field(..., description="When the chat was created")
    modified_at: datetime = Field(..., description="When the chat was last modified")
    awaiting_user_input: bool = Field(
        False,
        description="Whether the chat has a pending question record still waiting on the user",
    )


class WorkflowCopilotAudioUploadResponse(BaseModel):
    workflow_copilot_chat_id: str = Field(..., description="Chat ID the audio artifact is associated with")
    audio_artifact_id: str = Field(..., description="Stored audio artifact ID")


class WorkflowCopilotStreamMessageType(StrEnum):
    PROCESSING_UPDATE = "processing_update"
    RESPONSE = "response"
    ERROR = "error"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CONDENSING = "condensing"
    NARRATION = "narration"
    BLOCK_PROGRESS = "block_progress"
    RUN_STARTED = "run_started"
    RUN_OUTCOME = "run_outcome"
    TURN_START = "turn_start"
    DESIGN_START = "design_start"
    DESIGN_END = "design_end"
    WORKFLOW_DRAFT = "workflow_draft"
    CREDENTIAL_REQUIRED = "credential_required"
    CREDENTIAL_PAUSE_RESOLVED = "credential_pause_resolved"
    CODEGEN_PROGRESS = "codegen_progress"
    TITLE_UPDATE = "title_update"


class WorkflowCopilotProcessingUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.PROCESSING_UPDATE, description="Message type"
    )
    status: str = Field(..., description="Processing status text")
    timestamp: datetime = Field(..., description="Server timestamp")


class WorkflowCopilotStreamResponseUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.RESPONSE, description="Message type"
    )
    workflow_copilot_chat_id: str = Field(..., description="The chat ID")
    message: str = Field(..., description="The message sent to the user")
    updated_workflow: ClientVisibleProposedWorkflow | None = Field(None, description="The updated workflow")
    response_time: datetime = Field(..., description="When the assistant message was created")
    total_tokens: int | None = Field(
        None,
        description="Total tokens consumed by the agent during this turn; None when no provider reported usage",
    )
    response_type: ResponseType = Field("REPLY", description="Agent response classification")
    resolved_model: str | None = Field(
        None,
        description=(
            "Model name for the terminal attempt (primary or fallback), including an interrupted attempt; "
            "None when unknown."
        ),
    )
    proposal_disposition: ProposalDisposition = Field(
        "auto_applicable",
        description="Whether this proposal may auto-apply or must be reviewed explicitly.",
    )
    workflow_applied: bool = Field(
        False,
        description="True when the backend already committed this terminal workflow proposal.",
    )
    proposed_workflow_metadata: CopilotProposalMetadata | None = None
    proposed_workflow_run: CopilotProposalRunFacts | None = None
    cancelled: bool = Field(
        False,
        description="When true, this RESPONSE was emitted by a user cancel; clients must not auto-apply.",
    )
    output_policy_diagnostics: dict[str, Any] | None = Field(
        None,
        description="Diagnostic output-policy labels for raw-vs-final quality reporting.",
    )
    turn_id: str | None = Field(
        None,
        description="UUID generated by the route at turn start; correlates this terminal frame to the matching turn_start envelope.",
    )
    narrative_summary: str | None = Field(
        None,
        description="One-line accomplishment summary for the turn. Optional; the frontend falls back to the response message when absent.",
    )
    narrative_payload: TurnNarrativePayload | None = Field(
        None,
        description="Terminal narrative bubble snapshot for live clients; mirrors the persisted assistant chat row.",
    )
    work_plan: list[str] | None = Field(
        None,
        description=(
            "Latest work plan the copilot model wrote, as of this turn's end. An empty list means the "
            "model cleared it; None means this frame carries no snapshot and clients keep what they have."
        ),
    )


class WorkflowCopilotBrowserAblationResponseUpdate(WorkflowCopilotStreamResponseUpdate):
    eval_mode: Literal["browser_ablation"]
    browser_session_id: str | None
    prompt_sha256: str | None
    tool_surface_sha256: str | None
    input_tokens: int | None
    output_tokens: int | None
    tool_activity: list[dict[str, Any]]
    screenshot_frames: list[dict[str, Any]]


CopilotFailureKind = Literal["provider", "server", "configuration"]


class WorkflowCopilotStreamErrorUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(WorkflowCopilotStreamMessageType.ERROR, description="Message type")
    error: str = Field(..., description="Error message")
    failure_kind: CopilotFailureKind | None = Field(
        None,
        description=(
            "Coarse cause when the turn failed for a reason outside the agent's own reasoning. "
            "None means the agent itself produced this terminal error, which is a real product outcome."
        ),
    )
    turn_id: str | None = Field(
        None,
        description="UUID generated by the route at turn start; correlates this terminal frame to the matching turn_start envelope.",
    )
    narrative_summary: str | None = Field(
        None,
        description="One-line accomplishment summary; None for route-level error frames where no agent context was available.",
    )


class WorkflowCopilotToolCallUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.TOOL_CALL, description="Message type"
    )
    tool_name: str = Field(..., description="Name of the tool being called")
    display_label: str | None = Field(
        None,
        description="Product-safe label for rendering the tool call in user-visible activity surfaces",
    )
    tool_input: dict = Field(default_factory=dict, description="Sanitized tool input (no secrets)")
    iteration: int = Field(..., description="Agent loop iteration number")
    tool_call_id: str = Field(..., description="Unique ID for this tool invocation")
    timestamp: datetime | None = Field(
        None,
        description="Server timestamp for this event; the same clock read is persisted on the matching activity entry.",
    )


class WorkflowCopilotToolResultUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.TOOL_RESULT, description="Message type"
    )
    tool_name: str = Field(..., description="Name of the tool that was called")
    display_label: str | None = Field(
        None,
        description="Product-safe label for rendering the tool result in user-visible activity surfaces",
    )
    success: bool = Field(..., description="Whether the tool call succeeded")
    summary: str = Field(..., description="Brief human-readable summary of the result")
    iteration: int = Field(..., description="Agent loop iteration number")
    tool_call_id: str = Field(..., description="Unique ID for this tool invocation")
    code_diffs: list[CodeWriteDiff] | None = Field(
        None,
        description="Per changed code block: its label, the +N/-M line delta, and a size-capped scrubbed patch",
    )
    detail: str | None = Field(
        None,
        description=(
            "Longer-cap sanitized failure text for tooltip display. None on success. "
            "Distinct from `summary`, which is capped tighter for the visible bullet."
        ),
    )
    workflow_run_id: str | None = Field(
        None,
        description=(
            "The workflow run a block-running tool created, when this result came from one "
            "(update_and_run_blocks / run_blocks_and_collect_debug). Present whether the run "
            "passed or failed; None for non-run tools."
        ),
    )
    executed_source_reference: str | None = Field(
        None,
        description=(
            "Opaque reference to the exact source executed by a run_browser_code result. "
            "Present for that tool only so a later promotion can be tied to the executed cell."
        ),
    )
    timestamp: datetime | None = Field(
        None,
        description="Server timestamp for this event; the same clock read is persisted on the matching activity entry.",
    )


class WorkflowCopilotCondensingUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.CONDENSING, description="Message type"
    )
    status: str = Field(..., description="Condensing status: 'started' or 'completed'")


class WorkflowCopilotNarrationUpdate(BaseModel):
    # User-facing narration for one unit of work: why the step is happening,
    # plus the row titles that describe it while it runs and once it finishes.
    # Distinct from PROCESSING_UPDATE (terse status text) so the frontend can
    # style narration as a separate "thinking" channel. Persisted into the
    # turn's design activity so a reload shows what was live at the time.
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.NARRATION, description="Message type"
    )
    narration: str = Field(..., description="One-sentence reason this step is happening")
    active_label: str | None = Field(None, description="Narrator-authored row title while the step is running")
    outcome_label: str | None = Field(
        None, description="Narrator-authored row title once the step finished, naming the outcome"
    )
    iteration: int = Field(..., description="Agent loop iteration number this narration describes")
    timestamp: datetime = Field(..., description="Server timestamp")


class WorkflowCopilotBlockProgressUpdate(BaseModel):
    # Per-block lifecycle event from inside long-running tool calls.
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.BLOCK_PROGRESS, description="Message type"
    )
    workflow_run_block_id: str = Field(..., description="Stable per-block id; used as the row key in the activity pane")
    workflow_run_id: str | None = Field(
        None,
        description="Dispatched run id for this block; lets the FE fetch recorded actions live during execution. "
        "Optional for backward compatibility with clients that only read the run id from run_outcome.",
    )
    block_label: str = Field(..., description="Workflow block label (e.g. 'enter_name')")
    block_type: str = Field(..., description="Workflow block type (e.g. 'navigation', 'extraction')")
    status: str = Field(
        ..., description="BlockStatus value: running, completed, failed, terminated, timed_out, canceled, skipped"
    )
    iteration: int = Field(..., description="Agent loop iteration number this block belongs to")
    timestamp: datetime = Field(..., description="Server timestamp")


class WorkflowCopilotRunStartedUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.RUN_STARTED, description="Message type"
    )
    workflow_run_id: str = Field(..., description="Run id, emitted once the run row exists and before it produces work")
    timestamp: datetime = Field(..., description="Server timestamp")


class WorkflowCopilotRunOutcomeUpdate(BaseModel):
    # Current authoring emits one factual terminal record. Legacy persisted
    # frames may still carry the older evaluating/adjudicated shape.
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.RUN_OUTCOME, description="Message type"
    )
    workflow_run_id: str = Field(..., description="Workflow run the record applies to")
    workflow_run_block_ids: list[str] = Field(
        default_factory=list, description="Run-block ids of the recorded run; match the FE per-row keys"
    )
    block_labels: list[str] = Field(
        default_factory=list, description="Block labels of the recorded run; key the persisted narrative payload"
    )
    verdict: RunOutcomeVerdict = Field(..., description="Legacy-compatible recorded run state")
    role: RunOutcomeRole = Field("recorded", description="Display scope of the run record")
    reason_code: RunOutcomeReasonCode | None = Field(
        None, description="Machine-readable cause for a not_demonstrated verdict"
    )
    display_reason: str | None = Field(None, description="Short product-safe reason for user-facing rendering")
    browser_session_id: str | None = Field(
        None, description="Browser session that owned this run's recorded execution state"
    )
    workflow_permanent_id: str | None = Field(None, description="Workflow whose Copilot turn created this run")
    turn_id: str | None = Field(None, description="Parent Copilot turn for this child run")
    workflow_copilot_chat_id: str | None = Field(None, description="Parent Copilot chat for this child run")
    continuity_source: Literal["workflow_run"] = Field(
        "workflow_run", description="Distinguishes authoritative run state from pane-stream presentation state"
    )
    terminal_disposition: str | None = Field(
        None, description="Recorded run status or watchdog disposition that ended this run observation"
    )
    iteration: int = Field(..., description="Agent loop iteration number")
    timestamp: datetime = Field(..., description="Server timestamp")


class WorkflowCopilotTurnStartUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.TURN_START, description="Message type"
    )
    turn_id: str = Field(..., description="UUID for this turn; correlates with the matching terminal frame")
    turn_index: int = Field(..., description="Zero-based ordinal of this turn within the chat")
    timestamp: datetime = Field(..., description="Server timestamp")
    prior_block_count: int | None = Field(
        None,
        description="Block count of the canonical workflow at turn entry; drives the FE edit-vs-build chip.",
    )


class WorkflowCopilotDesignStartUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.DESIGN_START, description="Message type"
    )
    timestamp: datetime = Field(..., description="Server timestamp")


class WorkflowCopilotDesignEndUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.DESIGN_END, description="Message type"
    )
    timestamp: datetime = Field(..., description="Server timestamp")


class WorkflowCopilotWorkflowDraftUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.WORKFLOW_DRAFT, description="Message type"
    )
    block_count: int = Field(..., description="Number of blocks in the drafted workflow")
    block_labels: list[str] = Field(default_factory=list, description="Ordered block labels in the drafted workflow")
    summary: str | None = Field(None, description="Optional one-line description; populated by a follow-up PR")
    timestamp: datetime = Field(..., description="Server timestamp")
    workflow: ClientVisibleProposedWorkflow | None = Field(
        None,
        description="Staged workflow API response (same shape as terminal RESPONSE.updated_workflow). Drives mid-turn canvas updates.",
    )
    # A write and its test share one tool call, so waiting for the tool_result would show the
    # patch only after the run. These carry it at the moment the code is written instead.
    code_diffs: list[dict] | None = Field(
        None,
        description="Per changed code block: label, added/removed line counts, and a size-capped scrubbed patch.",
    )
    tool_call_id: str | None = Field(
        None,
        description="The write's originating tool call, so the frontend attaches the diffs to that call's row.",
    )


class WorkflowCopilotTitleUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.TITLE_UPDATE, description="Message type"
    )
    turn_id: str = Field(..., description="UUID for the turn that named the agent")
    workflow_permanent_id: str = Field(..., description="The agent that was named")
    title: str = Field(..., description="The persisted title; only ever emitted after the write succeeded")
    timestamp: datetime = Field(..., description="Server timestamp")


class WorkflowCopilotCredentialRequiredUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.CREDENTIAL_REQUIRED, description="Message type"
    )
    turn_id: str = Field(..., description="UUID for the paused turn; correlates with credential-response POSTs")
    workflow_copilot_chat_id: str = Field(..., description="The chat ID")
    resume_token: str = Field(..., description="One-time token the credential-response POST must echo back to resume")
    reason: Literal[
        "workflow_credential_inputs_unbound",
        "missing_credential_run_failure",
        "credential_deferred_draft",
        "login_credentials_unresolved",
        "credential_missing_totp",
        "credential_rejected_by_site",
    ] = Field(..., description="Typed signal that triggered the pause")
    message: str = Field(..., description="The agent's explanatory text at the moment of pausing")
    login_page_urls: list[str] = Field(default_factory=list, description="Candidate login page URLs, if known")
    credential_refs: list[str] = Field(default_factory=list, description="Credential IDs or names referenced")
    timeout_seconds: int = Field(..., description="How long the backend will wait before degrading to terminal")
    expires_at: datetime = Field(..., description="Server time after which the pause degrades to terminal")
    timestamp: datetime = Field(..., description="Server timestamp")


CredentialPauseResolvedOutcome = Literal["connected", "skipped", "not_admitted"]


class WorkflowCopilotCredentialPauseResolvedUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.CREDENTIAL_PAUSE_RESOLVED, description="Message type"
    )
    turn_id: str = Field(..., description="UUID for the paused turn")
    workflow_copilot_chat_id: str = Field(..., description="The chat ID")
    resume_token: str = Field(..., description="Token of the credential_required card this answer resolves")
    outcome: CredentialPauseResolvedOutcome = Field(
        ..., description="The waiter's final verdict, after admission; never the raw POSTed action"
    )
    credential_id: str | None = Field(None, description="The connected credential; set only when connected")
    name: str | None = Field(None, description="Display name of the connected credential; set only when connected")
    timestamp: datetime = Field(..., description="Server timestamp")


class WorkflowCopilotChatHistoryResponse(BaseModel):
    pending_credential_requests: list[WorkflowCopilotCredentialRequiredUpdate] = Field(default_factory=list)
    question_interactions: list[QuestionInteraction] = Field(default_factory=list)
    pending_question_cancel_token: str | None = None
    workflow_copilot_chat_id: str | None = Field(None, description="Latest chat ID for the workflow")
    request_turn_id: str | None = Field(
        None, description="Turn matched by request_cancel_token when recovery requests provide one"
    )
    chat_history: list[WorkflowCopilotChatHistoryMessage] = Field(default_factory=list, description="Chat messages")
    proposed_workflow: ClientVisibleProposedWorkflow | None = Field(
        None, description="Latest workflow proposed by the copilot"
    )
    proposed_workflow_metadata: CopilotProposalMetadata | None = None
    proposed_claim_expires_in_seconds: float | None = Field(
        None,
        description=(
            "Seconds the server's accepting claim has left. None when no live claim holds the proposal. "
            "A duration rather than a deadline, so a client with a skewed clock still agrees with the server."
        ),
    )
    proposed_workflow_run: CopilotProposalRunFacts | None = None
    auto_accept: bool | None = Field(None, description="Whether copilot auto-accepts workflow updates")

    work_plan: list[str] = Field(default_factory=list, description="Latest work plan the copilot model wrote")


class WorkflowCopilotCodegenProgressUpdate(BaseModel):
    """Live-only drafting progress while the LLM streams authoring-tool arguments. Not persisted; the
    workflow_draft frame supersedes it."""

    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.CODEGEN_PROGRESS, description="Message type"
    )
    tool_name: str = Field(..., description="Authoring tool whose arguments are being streamed")
    blocks_drafted: list[str] = Field(
        default_factory=list, description="Cumulative ordered unique block labels seen so far in this call"
    )
    chars_streamed: int = Field(..., description="Cumulative argument characters streamed so far in this call")
    iteration: int = Field(..., description="Agent loop iteration number; matches the TOOL_CALL frame that follows")
    timestamp: datetime = Field(..., description="Server timestamp")


class WorkflowYAMLConversionRequest(BaseModel):
    workflow_definition_yaml: str = Field(..., description="Workflow definition YAML to convert to blocks")
    workflow_id: str = Field(..., description="Workflow ID")


class WorkflowYAMLConversionResponse(BaseModel):
    workflow_definition: dict = Field(..., description="Converted workflow definition with blocks")
