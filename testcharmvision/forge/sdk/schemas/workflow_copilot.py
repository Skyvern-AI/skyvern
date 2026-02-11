from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


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
    workflow_id: str = Field(..., description="Workflow permanent ID for the chat")
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


class WorkflowCopilotStreamErrorUpdate(BaseModel):
    type: WorkflowCopilotStreamMessageType = Field(WorkflowCopilotStreamMessageType.ERROR, description="Message type")
    error: str = Field(..., description="Error message")


class WorkflowYAMLConversionRequest(BaseModel):
    workflow_definition_yaml: str = Field(..., description="Workflow definition YAML to convert to blocks")
    workflow_id: str = Field(..., description="Workflow ID")


class WorkflowYAMLConversionResponse(BaseModel):
    workflow_definition: dict = Field(..., description="Converted workflow definition with blocks")
