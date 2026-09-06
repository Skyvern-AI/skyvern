from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from skyvern.forge.sdk.copilot.ask_user import QuestionInteraction, QuestionResponse
from skyvern.forge.sdk.copilot.code_write_diff import CodeWriteDiff
from skyvern.forge.sdk.copilot.context import ProposalDisposition, ResponseType, TurnNarrativePayload
from skyvern.forge.sdk.copilot.run_outcome import RunOutcomeReasonCode, RunOutcomeRole, RunOutcomeVerdict
from skyvern.forge.sdk.schemas.copilot_turn_outcome import (
    CopilotCancelSource,
    PersistedCopilotComposerMode,
    TurnOutcome,
)


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
    proposed_workflow: dict | None = Field(None, description="Latest workflow proposed by the copilot")
    auto_accept: bool | None = Field(False, description="Whether copilot auto-accepts workflow updates")
    pending_turns: dict[str, CopilotPendingTurn] = Field(
        default_factory=dict, description="In-flight turns keyed by turn id"
    )

    @field_validator("pending_turns", mode="before")
    @classmethod
    def _default_pending_turns(cls, value: dict[str, Any] | None) -> dict[str, Any]:
        return value or {}

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


class WorkflowCopilotChatMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workflow_copilot_chat_message_id: str = Field(..., description="ID for the workflow copilot chat message")
    workflow_copilot_chat_id: str = Field(..., description="ID of the parent workflow copilot chat")
    sender: WorkflowCopilotChatSender = Field(..., description="Message sender")
    content: str = Field(..., description="Message content")
    audio_artifact_id: str | None = Field(None, description="Artifact ID for audio captured during dictation")
    global_llm_context: str | None = Field(None, description="Optional global LLM context for the message")
    turn_outcome: TurnOutcome | None = Field(None, description="Typed turn outcome (assistant rows)")
    narrative_payload: TurnNarrativePayload | None = Field(
        None,
        description="Persisted narrative bubble snapshot; lets a reload re-render per-block cards.",
    )
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
    audio_artifact_id: str | None = Field(
        None,
        description="Artifact ID for audio captured while dictating this message.",
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
    product_action: Literal["test_end_to_end", "diagnose_run"] | None = Field(
        None,
        description=(
            "Structured product action for this turn, dispatched by the server instead of the agent. "
            "'test_end_to_end' runs every block of the pending proposal in a browser session minted "
            "for that run. 'diagnose_run' opens repair on the finished run named by workflow_run_id."
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


class WorkflowCopilotApplyProposedWorkflowRequest(BaseModel):
    workflow_copilot_chat_id: str = Field(..., description="The chat whose proposed workflow should be applied")
    auto_accept: bool = Field(
        False,
        description="If true, flip the chat to auto-accept mode so future turns persist directly without review",
    )


class WorkflowCopilotChatHistoryMessage(BaseModel):
    sender: WorkflowCopilotChatSender = Field(..., description="Message sender")
    content: str = Field(..., description="Message content")
    audio_artifact_id: str | None = Field(None, description="Artifact ID for captured dictation audio")
    turn_outcome: TurnOutcome | None = Field(None, description="Typed turn outcome (assistant rows only)")
    narrative_payload: TurnNarrativePayload | None = Field(
        None,
        description="Persisted narrative bubble snapshot; lets a reload re-render per-block cards.",
    )
    created_at: datetime = Field(..., description="When the message was created")


class WorkflowCopilotChatHistoryResponse(BaseModel):
    question_interactions: list[QuestionInteraction] = Field(default_factory=list)
    pending_question_cancel_token: str | None = None
    workflow_copilot_chat_id: str | None = Field(None, description="Latest chat ID for the workflow")
    chat_history: list[WorkflowCopilotChatHistoryMessage] = Field(default_factory=list, description="Chat messages")
    proposed_workflow: dict | None = Field(None, description="Latest workflow proposed by the copilot")
    auto_accept: bool | None = Field(None, description="Whether copilot auto-accepts workflow updates")


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
    updated_workflow: dict | None = Field(None, description="The updated workflow")
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
    workflow: dict | None = Field(
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
    ] = Field(..., description="Typed signal that triggered the pause")
    message: str = Field(..., description="The agent's explanatory text at the moment of pausing")
    login_page_urls: list[str] = Field(default_factory=list, description="Candidate login page URLs, if known")
    credential_refs: list[str] = Field(default_factory=list, description="Credential IDs or names referenced")
    timeout_seconds: int = Field(..., description="How long the backend will wait before degrading to terminal")
    expires_at: datetime = Field(..., description="Server time after which the pause degrades to terminal")
    timestamp: datetime = Field(..., description="Server timestamp")


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
