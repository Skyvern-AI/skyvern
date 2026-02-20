from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DiagnosisConversationStatus(StrEnum):
    ACTIVE = "active"
    ESCALATED = "escalated"
    RESOLVED = "resolved"


class DiagnosisMessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class DiagnosisConversation(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    diagnosis_conversation_id: str = Field(..., description="ID for the diagnosis conversation")
    organization_id: str = Field(..., description="Organization ID for the conversation")
    workflow_run_id: str = Field(..., description="Workflow run ID being diagnosed")
    escalation_ticket_id: str | None = Field(None, description="External ticket ID if escalated (e.g., Linear issue)")
    escalation_ticket_url: str | None = Field(None, description="URL to the escalation ticket")
    status: DiagnosisConversationStatus = Field(DiagnosisConversationStatus.ACTIVE, description="Conversation status")
    summary: str | None = Field(None, description="Summary of the diagnosis")
    created_at: datetime = Field(..., description="When the conversation was created")
    modified_at: datetime = Field(..., description="When the conversation was last modified")


class DiagnosisMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    diagnosis_message_id: str = Field(..., description="ID for the diagnosis message")
    diagnosis_conversation_id: str = Field(..., description="ID of the parent conversation")
    organization_id: str = Field(..., description="Organization ID")
    role: DiagnosisMessageRole = Field(..., description="Message sender role")
    content: str = Field(..., description="Message content")
    message_metadata: dict[str, Any] | None = Field(None, description="Additional metadata (artifacts, tool calls)")
    input_token_count: int | None = Field(None, description="Input tokens used for this message")
    output_token_count: int | None = Field(None, description="Output tokens used for this message")
    created_at: datetime = Field(..., description="When the message was created")
    modified_at: datetime = Field(..., description="When the message was last modified")


class DiagnosisChatRequest(BaseModel):
    """Request to send a message in a diagnosis chat session."""

    message: str = Field(..., description="The user's message")
    diagnosis_conversation_id: str | None = Field(
        None, description="Existing conversation ID (creates new if not provided)"
    )


class DiagnosisChatHistoryMessage(BaseModel):
    """Simplified message for history display."""

    role: DiagnosisMessageRole = Field(..., description="Message sender role")
    content: str = Field(..., description="Message content")
    created_at: datetime = Field(..., description="When the message was created")


class DiagnosisChatHistoryResponse(BaseModel):
    """Response containing conversation history."""

    diagnosis_conversation_id: str | None = Field(None, description="Conversation ID")
    workflow_run_id: str = Field(..., description="Workflow run ID being diagnosed")
    status: DiagnosisConversationStatus = Field(..., description="Conversation status")
    messages: list[DiagnosisChatHistoryMessage] = Field(default_factory=list, description="Chat messages")
    escalation_ticket_url: str | None = Field(None, description="URL to escalation ticket if escalated")


class DiagnosisStreamMessageType(StrEnum):
    """Types of messages in the diagnosis stream."""

    PROCESSING = "processing"
    CONTENT = "content"
    ARTIFACT = "artifact"
    COMPLETE = "complete"
    ERROR = "error"


class DiagnosisStreamProcessing(BaseModel):
    """Streaming message indicating processing status."""

    type: DiagnosisStreamMessageType = Field(DiagnosisStreamMessageType.PROCESSING, description="Message type")
    status: str = Field(..., description="Processing status text")
    timestamp: datetime = Field(..., description="Server timestamp")


class DiagnosisStreamContent(BaseModel):
    """Streaming message containing response content."""

    type: DiagnosisStreamMessageType = Field(DiagnosisStreamMessageType.CONTENT, description="Message type")
    content: str = Field(..., description="Content chunk")
    timestamp: datetime = Field(..., description="Server timestamp")


class DiagnosisStreamArtifact(BaseModel):
    """Streaming message containing artifact reference."""

    type: DiagnosisStreamMessageType = Field(DiagnosisStreamMessageType.ARTIFACT, description="Message type")
    artifact_type: str = Field(..., description="Type of artifact")
    artifact_url: str = Field(..., description="URL to the artifact")
    description: str | None = Field(None, description="Description of the artifact")
    timestamp: datetime = Field(..., description="Server timestamp")


class DiagnosisStreamComplete(BaseModel):
    """Streaming message indicating completion."""

    type: DiagnosisStreamMessageType = Field(DiagnosisStreamMessageType.COMPLETE, description="Message type")
    diagnosis_conversation_id: str = Field(..., description="Conversation ID")
    full_response: str = Field(..., description="Complete response text")
    input_token_count: int | None = Field(None, description="Input tokens used")
    output_token_count: int | None = Field(None, description="Output tokens used")
    timestamp: datetime = Field(..., description="Server timestamp")


class DiagnosisStreamError(BaseModel):
    """Streaming message indicating an error."""

    type: DiagnosisStreamMessageType = Field(DiagnosisStreamMessageType.ERROR, description="Message type")
    error: str = Field(..., description="Error message")
    timestamp: datetime = Field(..., description="Server timestamp")


class DiagnosisEscalateRequest(BaseModel):
    """Request to escalate a diagnosis to a support ticket."""

    diagnosis_conversation_id: str = Field(..., description="Conversation ID to escalate")
    additional_context: str | None = Field(None, description="Additional context from the user")


class DiagnosisEscalateResponse(BaseModel):
    """Response after escalating a diagnosis."""

    diagnosis_conversation_id: str = Field(..., description="Conversation ID")
    escalation_ticket_id: str = Field(..., description="ID of the created ticket")
    escalation_ticket_url: str = Field(..., description="URL to the created ticket")
    status: DiagnosisConversationStatus = Field(..., description="Updated conversation status")


class DiagnosisSuggestFixRequest(BaseModel):
    """Request to suggest workflow fixes based on the diagnosis."""

    diagnosis_conversation_id: str = Field(..., description="Conversation ID")


class DiagnosisSuggestFixResponse(BaseModel):
    """Response containing suggested workflow fixes."""

    diagnosis_conversation_id: str = Field(..., description="Conversation ID")
    suggestions: list[str] = Field(..., description="List of suggested fixes")
    proposed_workflow_changes: dict | None = Field(None, description="Proposed changes to the workflow definition")


class RunContextSummary(BaseModel):
    """Summary of workflow run context for diagnosis."""

    workflow_run_id: str
    workflow_title: str | None = None
    workflow_permanent_id: str | None = None
    status: str
    failure_reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    block_count: int = 0
    failed_blocks: list[str] = Field(default_factory=list)
    error_messages: list[str] = Field(default_factory=list)
