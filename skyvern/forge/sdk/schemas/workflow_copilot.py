from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from skyvern.forge.sdk.copilot.context import ProposalDisposition, ResponseType, TurnNarrativePayload
from skyvern.forge.sdk.copilot.run_outcome import RunOutcomeReasonCode, RunOutcomeVerdict
from skyvern.forge.sdk.schemas.copilot_turn_outcome import TurnOutcome


class WorkflowCopilotChat(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workflow_copilot_chat_id: str = Field(..., description="ID for the workflow copilot chat")
    organization_id: str = Field(..., description="Organization ID for the chat")
    workflow_permanent_id: str = Field(..., description="Workflow permanent ID for the chat")
    proposed_workflow: dict | None = Field(None, description="Latest workflow proposed by the copilot")
    auto_accept: bool | None = Field(False, description="Whether copilot auto-accepts workflow updates")
    created_at: datetime = Field(..., description="When the chat was created")
    modified_at: datetime = Field(..., description="When the chat was last modified")


class WorkflowCopilotCompletionCriteriaSet(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    completion_criteria_set_id: str
    organization_id: str
    workflow_copilot_chat_id: str
    goal_epoch: int
    status: str
    criteria: list[dict]
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


class WorkflowCopilotChatSender(StrEnum):
    USER = "user"
    AI = "ai"


# Wire-format mirror of ``copilot.turn_intent.TurnIntentMode`` — lives here
# rather than importing the source enum because ``turn_intent`` already
# imports this module (a back-edge would be circular). Values MUST stay in
# lockstep; a cross-enum equality test in the unit tests catches drift.
class WorkflowCopilotTurnMode(StrEnum):
    BUILD = "build"
    EDIT = "edit"
    DIAGNOSE = "diagnose"
    DOCS_ANSWER = "docs_answer"
    DRAFT_ONLY = "draft_only"
    CLARIFY = "clarify"
    REFUSE = "refuse"
    UNKNOWN = "unknown"


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
    audio_artifact_id: str | None = Field(
        None,
        description="Artifact ID for audio captured while dictating this message.",
    )
    workflow_yaml: str = Field(..., description="Current workflow YAML including unsaved changes")
    mode: Literal["ask", "build"] | None = Field(
        None, description="Per-request copilot path selector; None falls back to feature flags."
    )
    code_block: bool | None = Field(
        None, description="Per-request code-block authoring; honored only on the build/v2 path."
    )
    cancel_token: str | None = Field(
        None,
        description=(
            "Client-generated UUID. POST it to /workflow/copilot/cancel to hard-cancel this turn. "
            "Optional; legacy clients omit it and cancel becomes a no-op for those requests."
        ),
    )
    target_block_label: str | None = Field(
        None,
        description=(
            "When set, the copilot regenerates only this code block from its goal and leaves every "
            "other block unchanged. Used by the block-level Generate action."
        ),
    )


class WorkflowCopilotCancelRequest(BaseModel):
    cancel_token: str = Field(..., description="The cancel_token sent on the original /chat-post request")


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
    workflow_copilot_chat_id: str | None = Field(None, description="Latest chat ID for the workflow")
    chat_history: list[WorkflowCopilotChatHistoryMessage] = Field(default_factory=list, description="Chat messages")
    proposed_workflow: dict | None = Field(None, description="Latest workflow proposed by the copilot")
    auto_accept: bool | None = Field(None, description="Whether copilot auto-accepts workflow updates")


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
    RUN_OUTCOME = "run_outcome"
    TURN_START = "turn_start"
    DESIGN_START = "design_start"
    DESIGN_END = "design_end"
    WORKFLOW_DRAFT = "workflow_draft"


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
    proposal_disposition: ProposalDisposition = Field(
        "auto_applicable",
        description="Whether this proposal may auto-apply or must be reviewed explicitly.",
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


class WorkflowCopilotStreamErrorUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(WorkflowCopilotStreamMessageType.ERROR, description="Message type")
    error: str = Field(..., description="Error message")
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


class WorkflowCopilotToolResultUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.TOOL_RESULT, description="Message type"
    )
    tool_name: str = Field(..., description="Name of the tool that was called")
    success: bool = Field(..., description="Whether the tool call succeeded")
    summary: str = Field(..., description="Brief human-readable summary of the result")
    iteration: int = Field(..., description="Agent loop iteration number")
    tool_call_id: str = Field(..., description="Unique ID for this tool invocation")
    detail: str | None = Field(
        None,
        description=(
            "Longer-cap sanitized failure text for tooltip display. None on success. "
            "Distinct from `summary`, which is capped tighter for the visible bullet."
        ),
    )


class WorkflowCopilotCondensingUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.CONDENSING, description="Message type"
    )
    status: str = Field(..., description="Condensing status: 'started' or 'completed'")


class WorkflowCopilotNarrationUpdate(BaseModel):
    # Ephemeral, user-facing one-sentence status line emitted periodically while
    # the agent runs. Distinct from PROCESSING_UPDATE (terse status text) so the
    # frontend can style narration as a separate "thinking" channel. Not
    # persisted to chat history -- reload shows only user and final-assistant
    # rows.
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.NARRATION, description="Message type"
    )
    narration: str = Field(..., description="One-sentence user-facing progress narration")
    iteration: int = Field(..., description="Agent loop iteration number this narration describes")
    timestamp: datetime = Field(..., description="Server timestamp")


class WorkflowCopilotBlockProgressUpdate(BaseModel):
    # Per-block lifecycle event from inside long-running tool calls.
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.BLOCK_PROGRESS, description="Message type"
    )
    workflow_run_block_id: str = Field(..., description="Stable per-block id; used as the row key in the activity pane")
    block_label: str = Field(..., description="Workflow block label (e.g. 'enter_name')")
    block_type: str = Field(..., description="Workflow block type (e.g. 'navigation', 'extraction')")
    status: str = Field(
        ..., description="BlockStatus value: running, completed, failed, terminated, timed_out, canceled, skipped"
    )
    iteration: int = Field(..., description="Agent loop iteration number this block belongs to")
    timestamp: datetime = Field(..., description="Server timestamp")


class WorkflowCopilotRunOutcomeUpdate(BaseModel):
    # Emitted once as an "evaluating" hold when an ok run enters adjudication and
    # once with the final recorded verdict; rows render success from this, not raw status.
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.RUN_OUTCOME, description="Message type"
    )
    workflow_run_id: str = Field(..., description="Workflow run the verdict applies to")
    workflow_run_block_ids: list[str] = Field(
        default_factory=list, description="Run-block ids of the adjudicated run; match the FE per-row keys"
    )
    block_labels: list[str] = Field(
        default_factory=list, description="Block labels of the adjudicated run; key the persisted narrative payload"
    )
    verdict: RunOutcomeVerdict = Field(..., description="Recorded outcome verdict for the run")
    reason_code: RunOutcomeReasonCode | None = Field(
        None, description="Machine-readable cause for a not_demonstrated verdict"
    )
    display_reason: str | None = Field(None, description="Short product-safe reason for user-facing rendering")
    iteration: int = Field(..., description="Agent loop iteration number")
    timestamp: datetime = Field(..., description="Server timestamp")


class WorkflowCopilotTurnStartUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.TURN_START, description="Message type"
    )
    turn_id: str = Field(..., description="UUID for this turn; correlates with the matching terminal frame")
    turn_index: int = Field(..., description="Zero-based ordinal of this turn within the chat")
    mode: WorkflowCopilotTurnMode = Field(..., description="TurnIntent mode for this turn")
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


class WorkflowYAMLConversionRequest(BaseModel):
    workflow_definition_yaml: str = Field(..., description="Workflow definition YAML to convert to blocks")
    workflow_id: str = Field(..., description="Workflow ID")


class WorkflowYAMLConversionResponse(BaseModel):
    workflow_definition: dict = Field(..., description="Converted workflow definition with blocks")
