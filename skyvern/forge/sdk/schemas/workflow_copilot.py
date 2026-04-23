from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from skyvern.forge.sdk.copilot.context import ResponseType


class WorkflowCopilotChat(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workflow_copilot_chat_id: str = Field(..., description="ID for the workflow copilot chat")
    organization_id: str = Field(..., description="Organization ID for the chat")
    workflow_permanent_id: str = Field(..., description="Workflow permanent ID for the chat")
    proposed_workflow: dict | None = Field(None, description="Latest workflow proposed by the copilot")
    auto_accept: bool | None = Field(False, description="Whether copilot auto-accepts workflow updates")
    created_at: datetime = Field(..., description="When the chat was created")
    modified_at: datetime = Field(..., description="When the chat was last modified")


class WorkflowCopilotChatSender(StrEnum):
    USER = "user"
    AI = "ai"


class WorkflowCopilotChatMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workflow_copilot_chat_message_id: str = Field(..., description="ID for the workflow copilot chat message")
    workflow_copilot_chat_id: str = Field(..., description="ID of the parent workflow copilot chat")
    sender: WorkflowCopilotChatSender = Field(..., description="Message sender")
    content: str = Field(..., description="Message content")
    global_llm_context: str | None = Field(None, description="Optional global LLM context for the message")
    created_at: datetime = Field(..., description="When the message was created")
    modified_at: datetime = Field(..., description="When the message was last modified")


class WorkflowCopilotChatRequest(BaseModel):
    workflow_permanent_id: str = Field(..., description="Workflow permanent ID for the chat")
    workflow_id: str = Field(..., description="Workflow ID (mutable version ID)")
    workflow_copilot_chat_id: str | None = Field(None, description="The chat ID to send the message to")
    workflow_run_id: str | None = Field(None, description="The workflow run ID to use for the context")
    message: str = Field(..., description="The message that user sends")
    workflow_yaml: str = Field(..., description="Current workflow YAML including unsaved changes")


class WorkflowCopilotClearProposedWorkflowRequest(BaseModel):
    workflow_copilot_chat_id: str = Field(..., description="The chat ID to update")
    auto_accept: bool = Field(..., description="Whether to auto-accept future workflow updates")


class WorkflowCopilotChatHistoryMessage(BaseModel):
    sender: WorkflowCopilotChatSender = Field(..., description="Message sender")
    content: str = Field(..., description="Message content")
    created_at: datetime = Field(..., description="When the message was created")


class WorkflowCopilotChatHistoryResponse(BaseModel):
    workflow_copilot_chat_id: str | None = Field(None, description="Latest chat ID for the workflow")
    chat_history: list[WorkflowCopilotChatHistoryMessage] = Field(default_factory=list, description="Chat messages")
    proposed_workflow: dict | None = Field(None, description="Latest workflow proposed by the copilot")
    auto_accept: bool | None = Field(None, description="Whether copilot auto-accepts workflow updates")


class WorkflowCopilotStreamMessageType(StrEnum):
    PROCESSING_UPDATE = "processing_update"
    RESPONSE = "response"
    ERROR = "error"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CONDENSING = "condensing"
    NARRATION = "narration"


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


class WorkflowCopilotStreamErrorUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(WorkflowCopilotStreamMessageType.ERROR, description="Message type")
    error: str = Field(..., description="Error message")


class WorkflowCopilotToolCallUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(
        WorkflowCopilotStreamMessageType.TOOL_CALL, description="Message type"
    )
    tool_name: str = Field(..., description="Name of the tool being called")
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


class WorkflowYAMLConversionRequest(BaseModel):
    workflow_definition_yaml: str = Field(..., description="Workflow definition YAML to convert to blocks")
    workflow_id: str = Field(..., description="Workflow ID")


class WorkflowYAMLConversionResponse(BaseModel):
    workflow_definition: dict = Field(..., description="Converted workflow definition with blocks")
