"""
Diagnosis Chat Service

This service provides AI-powered diagnosis capabilities for workflow run failures.
It enables users to chat with an AI assistant to understand what went wrong in their
workflow runs, get suggestions for fixes, and escalate issues when needed.
"""

import json
from datetime import UTC, datetime
from typing import Any, AsyncIterator

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.schemas.diagnosis_chat import (
    DiagnosisChatHistoryMessage,
    DiagnosisChatHistoryResponse,
    DiagnosisConversation,
    DiagnosisConversationStatus,
    DiagnosisEscalateResponse,
    DiagnosisMessageRole,
    DiagnosisStreamArtifact,
    DiagnosisStreamComplete,
    DiagnosisStreamContent,
    DiagnosisStreamError,
    DiagnosisStreamProcessing,
    RunContextSummary,
)

LOG = structlog.get_logger()

# Artifact types that are useful for diagnosis
DIAGNOSIS_ARTIFACT_TYPES = [
    ArtifactType.SCREENSHOT_LLM,
    ArtifactType.SCREENSHOT_ACTION,
    ArtifactType.SCREENSHOT_FINAL,
    ArtifactType.HTML_SCRAPE,
    ArtifactType.VISIBLE_ELEMENTS_TREE,
    ArtifactType.LLM_PROMPT,
    ArtifactType.LLM_RESPONSE,
    ArtifactType.SKYVERN_LOG,
    ArtifactType.BROWSER_CONSOLE_LOG,
]

# System prompt for the diagnosis assistant
DIAGNOSIS_SYSTEM_PROMPT = """You are an expert Skyvern workflow diagnosis assistant. Your role is to help users understand why their browser automation workflow failed and suggest fixes.

## Context
You have access to workflow run information including:
- Workflow definition and configuration
- Block execution history and status
- Screenshots at various stages
- DOM state and element trees
- LLM prompts and responses
- Browser console logs

## Guidelines
1. **Be Clear and Concise**: Explain issues in plain language, avoiding jargon where possible.
2. **Focus on Root Causes**: Identify the underlying reason for failure, not just symptoms.
3. **Provide Actionable Suggestions**: Give specific, implementable fixes when possible.
4. **Use Evidence**: Reference specific artifacts (screenshots, logs) to support your analysis.
5. **Be Honest About Limitations**: If you can't determine the cause, say so clearly.

## Common Failure Patterns
- Element not found: The target element may have changed, be behind an overlay, or require scrolling
- Timeout: Page load or action took too long
- Navigation issues: Unexpected redirects or popups
- Authentication: Session expired or credentials invalid
- Data extraction: Schema mismatch or unexpected page structure

When asked about workflow modifications, provide suggestions in a clear format that users can implement.
"""


async def load_run_context(workflow_run_id: str, organization_id: str) -> RunContextSummary | None:
    """
    Load context about a workflow run for diagnosis.

    Args:
        workflow_run_id: The workflow run to analyze
        organization_id: The organization owning the run

    Returns:
        RunContextSummary with essential run information, or None if not found
    """
    try:
        workflow_run = await app.DATABASE.get_workflow_run(workflow_run_id, organization_id)
        if not workflow_run:
            return None

        workflow = await app.DATABASE.get_workflow(workflow_run.workflow_id, organization_id)

        # Get block execution history
        blocks = await app.DATABASE.get_workflow_run_blocks(workflow_run_id, organization_id)
        failed_blocks = [
            block.label or block.block_type for block in blocks if block.status in ["failed", "terminated"]
        ]

        # Collect error messages
        error_messages = []
        if workflow_run.failure_reason:
            error_messages.append(workflow_run.failure_reason)
        for block in blocks:
            if block.failure_reason:
                error_messages.append(f"[{block.label or block.block_type}] {block.failure_reason}")

        return RunContextSummary(
            workflow_run_id=workflow_run_id,
            workflow_title=workflow.title if workflow else None,
            workflow_permanent_id=workflow_run.workflow_permanent_id,
            status=workflow_run.status,
            failure_reason=workflow_run.failure_reason,
            started_at=workflow_run.started_at,
            finished_at=workflow_run.finished_at,
            block_count=len(blocks),
            failed_blocks=failed_blocks,
            error_messages=error_messages,
        )
    except Exception as e:
        LOG.error("Failed to load run context", workflow_run_id=workflow_run_id, error=str(e))
        return None


async def get_run_artifacts(
    workflow_run_id: str,
    organization_id: str,
    artifact_types: list[ArtifactType] | None = None,
) -> list[Artifact]:
    """
    Retrieve artifacts for a workflow run.

    Args:
        workflow_run_id: The workflow run to get artifacts for
        organization_id: The organization owning the run
        artifact_types: Optional filter for specific artifact types

    Returns:
        List of artifacts matching the criteria
    """
    try:
        artifacts = await app.DATABASE.get_artifacts_by_entity_id(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )

        if artifact_types:
            artifacts = [a for a in artifacts if a.artifact_type in artifact_types]

        return artifacts
    except Exception as e:
        LOG.error("Failed to get run artifacts", workflow_run_id=workflow_run_id, error=str(e))
        return []


async def get_artifact_content(artifact: Artifact) -> bytes | None:
    """
    Retrieve the content of an artifact.

    Args:
        artifact: The artifact to retrieve content for

    Returns:
        Artifact content as bytes, or None if not found
    """
    try:
        return await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
    except Exception as e:
        LOG.error("Failed to retrieve artifact content", artifact_id=artifact.artifact_id, error=str(e))
        return None


async def get_or_create_conversation(
    workflow_run_id: str,
    organization_id: str,
    diagnosis_conversation_id: str | None = None,
) -> DiagnosisConversation:
    """
    Get an existing conversation or create a new one.

    Args:
        workflow_run_id: The workflow run being diagnosed
        organization_id: The organization owning the run
        diagnosis_conversation_id: Optional existing conversation ID

    Returns:
        DiagnosisConversation instance
    """
    if diagnosis_conversation_id:
        conversation = await app.DATABASE.get_diagnosis_conversation(diagnosis_conversation_id, organization_id)
        if conversation:
            return conversation

    # Check for existing active conversation for this run
    existing = await app.DATABASE.get_diagnosis_conversation_by_workflow_run(workflow_run_id, organization_id)
    if existing and existing.status == DiagnosisConversationStatus.ACTIVE:
        return existing

    # Create new conversation
    return await app.DATABASE.create_diagnosis_conversation(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )


async def get_conversation_history(
    workflow_run_id: str,
    organization_id: str,
) -> DiagnosisChatHistoryResponse:
    """
    Get the conversation history for a workflow run.

    Args:
        workflow_run_id: The workflow run being diagnosed
        organization_id: The organization owning the run

    Returns:
        DiagnosisChatHistoryResponse with messages and status
    """
    conversation = await app.DATABASE.get_diagnosis_conversation_by_workflow_run(workflow_run_id, organization_id)

    if not conversation:
        return DiagnosisChatHistoryResponse(
            diagnosis_conversation_id=None,
            workflow_run_id=workflow_run_id,
            status=DiagnosisConversationStatus.ACTIVE,
            messages=[],
            escalation_ticket_url=None,
        )

    messages = await app.DATABASE.get_diagnosis_messages(conversation.diagnosis_conversation_id)

    return DiagnosisChatHistoryResponse(
        diagnosis_conversation_id=conversation.diagnosis_conversation_id,
        workflow_run_id=workflow_run_id,
        status=conversation.status,
        messages=[
            DiagnosisChatHistoryMessage(
                role=msg.role,
                content=msg.content,
                created_at=msg.created_at,
            )
            for msg in messages
        ],
        escalation_ticket_url=conversation.escalation_ticket_url,
    )


def _build_context_message(context: RunContextSummary) -> str:
    """Build a context message summarizing the workflow run."""
    lines = [
        "## Workflow Run Context",
        f"- **Run ID**: {context.workflow_run_id}",
        f"- **Status**: {context.status}",
    ]

    if context.workflow_title:
        lines.append(f"- **Workflow**: {context.workflow_title}")

    if context.started_at:
        lines.append(f"- **Started**: {context.started_at.isoformat()}")

    if context.finished_at:
        lines.append(f"- **Finished**: {context.finished_at.isoformat()}")

    lines.append(f"- **Total Blocks**: {context.block_count}")

    if context.failed_blocks:
        lines.append(f"- **Failed Blocks**: {', '.join(context.failed_blocks)}")

    if context.failure_reason:
        lines.append(f"\n## Failure Reason\n{context.failure_reason}")

    if context.error_messages:
        lines.append("\n## Error Messages")
        for msg in context.error_messages[:5]:  # Limit to first 5
            lines.append(f"- {msg}")

    return "\n".join(lines)


async def process_message(
    workflow_run_id: str,
    organization_id: str,
    user_message: str,
    diagnosis_conversation_id: str | None = None,
) -> AsyncIterator[
    DiagnosisStreamProcessing
    | DiagnosisStreamContent
    | DiagnosisStreamArtifact
    | DiagnosisStreamComplete
    | DiagnosisStreamError
]:
    """
    Process a user message and generate a streaming response.

    Args:
        workflow_run_id: The workflow run being diagnosed
        organization_id: The organization owning the run
        user_message: The user's message
        diagnosis_conversation_id: Optional existing conversation ID

    Yields:
        Stream of diagnosis response messages
    """
    try:
        # Send processing update
        yield DiagnosisStreamProcessing(
            status="Loading workflow run context...",
            timestamp=datetime.now(UTC),
        )

        # Load run context
        context = await load_run_context(workflow_run_id, organization_id)
        if not context:
            yield DiagnosisStreamError(
                error="Workflow run not found or access denied",
                timestamp=datetime.now(UTC),
            )
            return

        # Get or create conversation
        yield DiagnosisStreamProcessing(
            status="Preparing conversation...",
            timestamp=datetime.now(UTC),
        )

        conversation = await get_or_create_conversation(workflow_run_id, organization_id, diagnosis_conversation_id)

        # Store user message
        await app.DATABASE.create_diagnosis_message(
            diagnosis_conversation_id=conversation.diagnosis_conversation_id,
            organization_id=organization_id,
            role=DiagnosisMessageRole.USER,
            content=user_message,
        )

        # Get conversation history for context
        history = await app.DATABASE.get_diagnosis_messages(conversation.diagnosis_conversation_id)

        # Build prompt with context
        yield DiagnosisStreamProcessing(
            status="Analyzing run data...",
            timestamp=datetime.now(UTC),
        )

        # Build messages for LLM
        messages = [
            {"role": "system", "content": DIAGNOSIS_SYSTEM_PROMPT},
            {"role": "user", "content": _build_context_message(context)},
        ]

        # Add conversation history
        for msg in history[:-1]:  # Exclude the message we just added
            messages.append(
                {
                    "role": "user" if msg.role == DiagnosisMessageRole.USER else "assistant",
                    "content": msg.content,
                }
            )

        # Add current user message
        messages.append({"role": "user", "content": user_message})

        # Call LLM for response
        yield DiagnosisStreamProcessing(
            status="Generating diagnosis...",
            timestamp=datetime.now(UTC),
        )

        # Use the main LLM handler
        full_response = ""
        input_tokens = 0
        output_tokens = 0

        try:
            # Build the prompt as a single string for the handler
            prompt = json.dumps(messages)

            response = await app.LLM_API_HANDLER(
                prompt=prompt,
                prompt_name="diagnosis_chat",
                organization_id=organization_id,
                raw_response=True,
                force_dict=False,
            )

            # Handle the response
            if isinstance(response, dict):
                if "content" in response:
                    full_response = response["content"]
                elif "message" in response:
                    full_response = response["message"]
                else:
                    full_response = str(response)

                input_tokens = response.get("input_tokens", 0)
                output_tokens = response.get("output_tokens", 0)
            else:
                full_response = str(response)

            # Stream the content
            yield DiagnosisStreamContent(
                content=full_response,
                timestamp=datetime.now(UTC),
            )

        except Exception as e:
            LOG.error("LLM call failed", error=str(e))
            yield DiagnosisStreamError(
                error=f"Failed to generate diagnosis: {str(e)}",
                timestamp=datetime.now(UTC),
            )
            return

        # Store assistant response
        await app.DATABASE.create_diagnosis_message(
            diagnosis_conversation_id=conversation.diagnosis_conversation_id,
            organization_id=organization_id,
            role=DiagnosisMessageRole.ASSISTANT,
            content=full_response,
            input_token_count=input_tokens,
            output_token_count=output_tokens,
        )

        # Send completion
        yield DiagnosisStreamComplete(
            diagnosis_conversation_id=conversation.diagnosis_conversation_id,
            full_response=full_response,
            input_token_count=input_tokens,
            output_token_count=output_tokens,
            timestamp=datetime.now(UTC),
        )

    except Exception as e:
        LOG.exception("Error processing diagnosis message", error=str(e))
        yield DiagnosisStreamError(
            error=f"An error occurred: {str(e)}",
            timestamp=datetime.now(UTC),
        )


async def escalate_conversation(
    diagnosis_conversation_id: str,
    organization_id: str,
    additional_context: str | None = None,
) -> DiagnosisEscalateResponse | None:
    """
    Escalate a diagnosis conversation to a support ticket.

    Args:
        diagnosis_conversation_id: The conversation to escalate
        organization_id: The organization owning the conversation
        additional_context: Optional additional context from the user

    Returns:
        DiagnosisEscalateResponse with ticket details, or None if failed
    """
    try:
        conversation = await app.DATABASE.get_diagnosis_conversation(diagnosis_conversation_id, organization_id)
        if not conversation:
            LOG.error("Conversation not found for escalation", id=diagnosis_conversation_id)
            return None

        # Get conversation history
        messages = await app.DATABASE.get_diagnosis_messages(diagnosis_conversation_id)

        # Get run context
        context = await load_run_context(conversation.workflow_run_id, organization_id)

        # Build ticket description
        description_parts = [
            f"## Workflow Run: {conversation.workflow_run_id}",
        ]

        if context:
            if context.workflow_title:
                description_parts.append(f"**Workflow**: {context.workflow_title}")
            description_parts.append(f"**Status**: {context.status}")
            if context.failure_reason:
                description_parts.append(f"**Failure Reason**: {context.failure_reason}")

        description_parts.append("\n## Diagnosis Conversation")
        for msg in messages[-10:]:  # Last 10 messages
            role_label = "User" if msg.role == DiagnosisMessageRole.USER else "Assistant"
            description_parts.append(f"\n**{role_label}**: {msg.content}")

        if additional_context:
            description_parts.append(f"\n## Additional Context\n{additional_context}")

        ticket_description = "\n".join(description_parts)

        # For now, we'll create a placeholder ticket
        # In production, this would integrate with Linear or another ticket system
        # The ticket_description would be sent to the ticketing system
        ticket_id = f"TICKET-{conversation.diagnosis_conversation_id[:8]}"
        ticket_url = f"https://support.example.com/tickets/{ticket_id}"

        LOG.debug(
            "Creating support ticket",
            ticket_id=ticket_id,
            description_length=len(ticket_description),
        )

        # Update conversation status
        await app.DATABASE.update_diagnosis_conversation(
            diagnosis_conversation_id=diagnosis_conversation_id,
            organization_id=organization_id,
            status=DiagnosisConversationStatus.ESCALATED,
            escalation_ticket_id=ticket_id,
            escalation_ticket_url=ticket_url,
        )

        LOG.info(
            "Conversation escalated",
            conversation_id=diagnosis_conversation_id,
            ticket_id=ticket_id,
        )

        return DiagnosisEscalateResponse(
            diagnosis_conversation_id=diagnosis_conversation_id,
            escalation_ticket_id=ticket_id,
            escalation_ticket_url=ticket_url,
            status=DiagnosisConversationStatus.ESCALATED,
        )

    except Exception as e:
        LOG.exception("Failed to escalate conversation", error=str(e))
        return None


async def suggest_workflow_fix(
    diagnosis_conversation_id: str,
    organization_id: str,
) -> dict[str, Any] | None:
    """
    Generate workflow modification suggestions based on the diagnosis.

    Args:
        diagnosis_conversation_id: The conversation with diagnosis context
        organization_id: The organization owning the conversation

    Returns:
        Dictionary with suggestions and proposed changes, or None if failed
    """
    try:
        conversation = await app.DATABASE.get_diagnosis_conversation(diagnosis_conversation_id, organization_id)
        if not conversation:
            return None

        # Get conversation history
        messages = await app.DATABASE.get_diagnosis_messages(diagnosis_conversation_id)

        # Get workflow definition
        workflow_run = await app.DATABASE.get_workflow_run(conversation.workflow_run_id, organization_id)
        if not workflow_run:
            return None

        workflow = await app.DATABASE.get_workflow(workflow_run.workflow_id, organization_id)
        if not workflow:
            return None

        # Build prompt for fix suggestions
        conversation_context = "\n".join(
            [
                f"{'User' if m.role == DiagnosisMessageRole.USER else 'Assistant'}: {m.content}"
                for m in messages[-5:]  # Last 5 messages
            ]
        )

        prompt = f"""Based on the following diagnosis conversation and workflow definition, suggest specific fixes.

## Diagnosis Conversation
{conversation_context}

## Current Workflow Definition
{json.dumps(workflow.workflow_definition, indent=2)}

Please provide:
1. A list of specific suggested fixes
2. If applicable, proposed changes to the workflow definition (as JSON)

Format your response as JSON with the following structure:
{{
    "suggestions": ["fix 1", "fix 2", ...],
    "proposed_workflow_changes": {{ ... }} or null
}}
"""

        response = await app.LLM_API_HANDLER(
            prompt=prompt,
            prompt_name="diagnosis_suggest_fix",
            organization_id=organization_id,
        )

        if isinstance(response, dict):
            return response

        return {"suggestions": [str(response)], "proposed_workflow_changes": None}

    except Exception as e:
        LOG.exception("Failed to suggest workflow fix", error=str(e))
        return None
