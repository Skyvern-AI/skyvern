"""
API endpoints for the AI-powered diagnosis chatbot feature.

These endpoints allow users to:
- Chat with an AI assistant about workflow run failures
- View conversation history
- Escalate issues to support tickets
- Get suggestions for workflow fixes
"""

from datetime import UTC, datetime

import structlog
from fastapi import Depends, HTTPException, Request
from sse_starlette import EventSourceResponse

from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream, FastAPIEventSourceStream
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.diagnosis_chat import (
    DiagnosisChatHistoryResponse,
    DiagnosisChatRequest,
    DiagnosisEscalateRequest,
    DiagnosisEscalateResponse,
    DiagnosisStreamError,
    DiagnosisSuggestFixRequest,
    DiagnosisSuggestFixResponse,
)
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.services import diagnosis_chat_service

LOG = structlog.get_logger()


@base_router.get(
    "/workflow_runs/{workflow_run_id}/diagnosis/history",
    tags=["Diagnosis"],
    description="Get the diagnosis conversation history for a workflow run",
    summary="Get diagnosis history",
    responses={
        200: {"description": "Conversation history retrieved successfully"},
        404: {"description": "Workflow run not found"},
    },
)
async def get_diagnosis_history(
    workflow_run_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> DiagnosisChatHistoryResponse:
    """Get the diagnosis conversation history for a workflow run."""
    return await diagnosis_chat_service.get_conversation_history(
        workflow_run_id=workflow_run_id,
        organization_id=current_org.organization_id,
    )


@base_router.post(
    "/workflow_runs/{workflow_run_id}/diagnosis/chat",
    tags=["Diagnosis"],
    description="Send a message to the diagnosis chatbot and receive a streaming response",
    summary="Chat with diagnosis assistant",
    responses={
        200: {"description": "Streaming response started"},
        404: {"description": "Workflow run not found"},
    },
)
async def diagnosis_chat(
    request: Request,
    workflow_run_id: str,
    chat_request: DiagnosisChatRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> EventSourceResponse:
    """
    Send a message to the diagnosis chatbot.

    Returns a Server-Sent Events stream with the following message types:
    - processing: Status updates during analysis
    - content: Response content chunks
    - artifact: References to relevant artifacts
    - complete: Final response with token usage
    - error: Error messages if something goes wrong
    """

    async def handler(stream: EventSourceStream) -> None:
        try:
            async for message in diagnosis_chat_service.process_message(
                workflow_run_id=workflow_run_id,
                organization_id=current_org.organization_id,
                user_message=chat_request.message,
                diagnosis_conversation_id=chat_request.diagnosis_conversation_id,
            ):
                if await stream.is_disconnected():
                    break
                await stream.send(message)
        except Exception as e:
            LOG.exception("Error in diagnosis chat stream", error=str(e))
            await stream.send(
                DiagnosisStreamError(
                    error=f"An unexpected error occurred: {str(e)}",
                    timestamp=datetime.now(UTC),
                )
            )

    return FastAPIEventSourceStream.create(request, handler)


@base_router.post(
    "/workflow_runs/{workflow_run_id}/diagnosis/escalate",
    tags=["Diagnosis"],
    description="Escalate a diagnosis conversation to a support ticket",
    summary="Escalate diagnosis to support",
    responses={
        200: {"description": "Issue escalated successfully"},
        404: {"description": "Conversation not found"},
        400: {"description": "Invalid request"},
    },
)
async def escalate_diagnosis(
    workflow_run_id: str,
    escalate_request: DiagnosisEscalateRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> DiagnosisEscalateResponse:
    """Escalate a diagnosis conversation to a support ticket."""
    result = await diagnosis_chat_service.escalate_conversation(
        diagnosis_conversation_id=escalate_request.diagnosis_conversation_id,
        organization_id=current_org.organization_id,
        additional_context=escalate_request.additional_context,
    )

    if not result:
        raise HTTPException(status_code=404, detail="Conversation not found or escalation failed")

    return result


@base_router.post(
    "/workflow_runs/{workflow_run_id}/diagnosis/suggest-fix",
    tags=["Diagnosis"],
    description="Get AI-suggested fixes for the workflow based on the diagnosis",
    summary="Suggest workflow fixes",
    responses={
        200: {"description": "Suggestions generated successfully"},
        404: {"description": "Conversation not found"},
    },
)
async def suggest_workflow_fix(
    workflow_run_id: str,
    suggest_request: DiagnosisSuggestFixRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> DiagnosisSuggestFixResponse:
    """Get AI-suggested fixes for the workflow based on the diagnosis."""
    result = await diagnosis_chat_service.suggest_workflow_fix(
        diagnosis_conversation_id=suggest_request.diagnosis_conversation_id,
        organization_id=current_org.organization_id,
    )

    if not result:
        raise HTTPException(status_code=404, detail="Conversation not found or failed to generate suggestions")

    return DiagnosisSuggestFixResponse(
        diagnosis_conversation_id=suggest_request.diagnosis_conversation_id,
        suggestions=result.get("suggestions", []),
        proposed_workflow_changes=result.get("proposed_workflow_changes"),
    )
