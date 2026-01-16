import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import structlog
import yaml
from fastapi import Depends, HTTPException, Request, status
from sse_starlette import EventSourceResponse, JSONServerSentEvent, ServerSentEvent

from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.experimentation.llm_prompt_config import get_llm_handler_for_prompt_type
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.routes.run_blocks import DEFAULT_LOGIN_PROMPT
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatHistoryResponse,
    WorkflowCopilotChatMessage,
    WorkflowCopilotChatRequest,
    WorkflowCopilotChatSender,
    WorkflowCopilotProcessingUpdate,
    WorkflowCopilotStreamErrorUpdate,
    WorkflowCopilotStreamMessageType,
    WorkflowCopilotStreamResponseUpdate,
)
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.parameter import ParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import (
    LoginBlockYAML,
    WorkflowCreateYAMLRequest,
)

WORKFLOW_KNOWLEDGE_BASE_PATH = Path("skyvern/forge/prompts/skyvern/workflow_knowledge_base.txt")
CHAT_HISTORY_CONTEXT_MESSAGES = 10
SSE_KEEPALIVE_INTERVAL_SECONDS = 10

LOG = structlog.get_logger()


@dataclass(frozen=True)
class RunInfo:
    block_label: str | None
    block_type: str
    block_status: str | None
    failure_reason: str | None
    html: str | None


async def _get_debug_artifact(organization_id: str, workflow_run_id: str) -> Artifact | None:
    artifacts = await app.DATABASE.get_artifacts_for_run(
        run_id=workflow_run_id, organization_id=organization_id, artifact_types=[ArtifactType.VISIBLE_ELEMENTS_TREE]
    )
    return artifacts[0] if isinstance(artifacts, list) and artifacts else None


async def _get_debug_run_info(organization_id: str, workflow_run_id: str | None) -> RunInfo | None:
    if not workflow_run_id:
        return None

    blocks = await app.DATABASE.get_workflow_run_blocks(
        workflow_run_id=workflow_run_id, organization_id=organization_id
    )
    if not blocks:
        return None

    block = blocks[0]

    artifact = await _get_debug_artifact(organization_id, workflow_run_id)
    if artifact:
        artifact_bytes = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
        html = artifact_bytes.decode("utf-8") if artifact_bytes else None
    else:
        html = None

    return RunInfo(
        block_label=block.label,
        block_type=block.block_type.name,
        block_status=block.status,
        failure_reason=block.failure_reason,
        html=html,
    )


async def copilot_call_llm(
    organization_id: str,
    chat_request: WorkflowCopilotChatRequest,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str | None,
    debug_run_info_text: str,
) -> tuple[str, WorkflowDefinition | None, str | None]:
    current_datetime = datetime.now(timezone.utc).isoformat()

    chat_history_text = ""
    if chat_history:
        history_lines = [f"{msg.sender}: {msg.content}" for msg in chat_history]
        chat_history_text = "\n".join(history_lines)

    workflow_knowledge_base = WORKFLOW_KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8")

    llm_prompt = prompt_engine.load_prompt(
        template="workflow-copilot",
        workflow_knowledge_base=workflow_knowledge_base,
        workflow_yaml=chat_request.workflow_yaml or "",
        user_message=chat_request.message,
        chat_history=chat_history_text,
        global_llm_context=global_llm_context or "",
        current_datetime=current_datetime,
        debug_run_info=debug_run_info_text,
    )

    LOG.info(
        "Calling LLM",
        user_message=chat_request.message,
        user_message_len=len(chat_request.message),
        workflow_yaml_len=len(chat_request.workflow_yaml or ""),
        chat_history_len=len(chat_history_text),
        global_llm_context_len=len(global_llm_context or ""),
        debug_run_info_len=len(debug_run_info_text),
        workflow_knowledge_base_len=len(workflow_knowledge_base),
        llm_prompt_len=len(llm_prompt),
        llm_prompt=llm_prompt,
    )
    llm_api_handler = (
        await get_llm_handler_for_prompt_type("workflow-copilot", chat_request.workflow_permanent_id, organization_id)
        or app.LLM_API_HANDLER
    )
    llm_start_time = time.monotonic()
    llm_response = await llm_api_handler(
        prompt=llm_prompt,
        prompt_name="workflow-copilot",
        organization_id=organization_id,
    )
    LOG.info(
        "LLM response",
        duration_seconds=time.monotonic() - llm_start_time,
        user_message_len=len(chat_request.message),
        workflow_yaml_len=len(chat_request.workflow_yaml or ""),
        chat_history_len=len(chat_history_text),
        global_llm_context_len=len(global_llm_context or ""),
        debug_run_info_len=len(debug_run_info_text),
        workflow_knowledge_base_len=len(workflow_knowledge_base),
        llm_response_len=len(llm_response),
        llm_response=llm_response,
    )

    if isinstance(llm_response, dict) and "output" in llm_response:
        action_data = llm_response["output"]
    else:
        action_data = llm_response

    if not isinstance(action_data, dict):
        LOG.error(
            "LLM response is not valid JSON",
            organization_id=organization_id,
            response_type=type(action_data).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid response from LLM",
        )

    action_type = action_data.get("type")
    user_response_value = action_data.get("user_response")
    if user_response_value is None:
        user_response = "I received your request but I'm not sure how to help. Could you rephrase?"
    else:
        user_response = str(user_response_value)
    LOG.info(
        "LLM response received",
        organization_id=organization_id,
        action_type=action_type,
    )

    global_llm_context = action_data.get("global_llm_context")
    if global_llm_context is not None:
        global_llm_context = str(global_llm_context)

    if action_type == "REPLACE_WORKFLOW":
        updated_workflow = await _process_workflow_yaml(chat_request.workflow_id, action_data.get("workflow_yaml", ""))
        return user_response, updated_workflow, global_llm_context
    elif action_type == "REPLY":
        return user_response, None, global_llm_context
    elif action_type == "ASK_QUESTION":
        return user_response, None, global_llm_context
    else:
        LOG.error(
            "Unknown action type from LLM",
            organization_id=organization_id,
            action_type=action_type,
        )
        return "I received your request but I'm not sure how to help. Could you rephrase?", None, None


async def _process_workflow_yaml(workflow_id: str, workflow_yaml: str) -> WorkflowDefinition:
    try:
        parsed_yaml = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError as e:
        LOG.error("Invalid YAML from LLM", yaml=workflow_yaml, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"LLM generated invalid YAML: {str(e)}",
        )

    try:
        # Fixing trivial common LLM mistakes
        workflow_definition = parsed_yaml.get("workflow_definition", None)
        if workflow_definition:
            blocks = workflow_definition.get("blocks", [])
            for block in blocks:
                block["title"] = block.get("title", "")

        workflow_yaml_request = WorkflowCreateYAMLRequest.model_validate(parsed_yaml)

        # Post-processing
        for block in workflow_yaml_request.workflow_definition.blocks:
            if isinstance(block, LoginBlockYAML) and not block.navigation_goal:
                block.navigation_goal = DEFAULT_LOGIN_PROMPT

        workflow_yaml_request.workflow_definition.parameters = [
            p for p in workflow_yaml_request.workflow_definition.parameters if p.parameter_type != ParameterType.OUTPUT
        ]

        updated_workflow = convert_workflow_definition(
            workflow_definition_yaml=workflow_yaml_request.workflow_definition,
            workflow_id=workflow_id,
        )
    except Exception as e:
        LOG.error("YAML from LLM does not conform to Skyvern workflow schema", yaml=workflow_yaml, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"LLM generated YAML that doesn't match workflow schema: {str(e)}",
        )
    return updated_workflow


@base_router.post("/workflow/copilot/chat-post", include_in_schema=False)
async def workflow_copilot_chat_post(
    request: Request,
    chat_request: WorkflowCopilotChatRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> EventSourceResponse:
    async def event_stream() -> AsyncGenerator[JSONServerSentEvent, None]:
        LOG.info(
            "Workflow copilot chat request",
            workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
            workflow_run_id=chat_request.workflow_run_id,
            message=chat_request.message,
            workflow_yaml_length=len(chat_request.workflow_yaml),
            organization_id=organization.organization_id,
        )

        try:
            yield JSONServerSentEvent(
                data=WorkflowCopilotProcessingUpdate(
                    type=WorkflowCopilotStreamMessageType.PROCESSING_UPDATE,
                    status="Processing...",
                    timestamp=datetime.now(timezone.utc),
                ).model_dump(mode="json"),
            )

            if chat_request.workflow_copilot_chat_id:
                chat = await app.DATABASE.get_workflow_copilot_chat_by_id(
                    organization_id=organization.organization_id,
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                )
                if not chat:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
                if chat_request.workflow_permanent_id != chat.workflow_permanent_id:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Wrong workflow permanent ID")
            else:
                chat = await app.DATABASE.create_workflow_copilot_chat(
                    organization_id=organization.organization_id,
                    workflow_permanent_id=chat_request.workflow_permanent_id,
                )

            chat_messages = await app.DATABASE.get_workflow_copilot_chat_messages(
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            )
            global_llm_context = None
            for message in reversed(chat_messages):
                if message.global_llm_context is not None:
                    global_llm_context = message.global_llm_context
                    break

            debug_run_info = await _get_debug_run_info(organization.organization_id, chat_request.workflow_run_id)

            # Format debug run info for prompt
            debug_run_info_text = ""
            if debug_run_info:
                debug_run_info_text = f"Block Label: {debug_run_info.block_label}"
                debug_run_info_text += f" Block Type: {debug_run_info.block_type}"
                debug_run_info_text += f" Status: {debug_run_info.block_status}"
                if debug_run_info.failure_reason:
                    debug_run_info_text += f"\nFailure Reason: {debug_run_info.failure_reason}"
                if debug_run_info.html:
                    debug_run_info_text += f"\n\nVisible Elements Tree (HTML):\n{debug_run_info.html}"

            yield JSONServerSentEvent(
                data=WorkflowCopilotProcessingUpdate(
                    type=WorkflowCopilotStreamMessageType.PROCESSING_UPDATE,
                    status="Thinking...",
                    timestamp=datetime.now(timezone.utc),
                ).model_dump(mode="json"),
            )

            if await request.is_disconnected():
                LOG.info(
                    "Workflow copilot chat request is disconnected before LLM call",
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                )
                return

            user_response, updated_workflow, updated_global_llm_context = await copilot_call_llm(
                organization.organization_id,
                chat_request,
                convert_to_history_messages(chat_messages[-CHAT_HISTORY_CONTEXT_MESSAGES:]),
                global_llm_context,
                debug_run_info_text,
            )

            if await request.is_disconnected():
                LOG.info(
                    "Workflow copilot chat request is disconnected after LLM call",
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                )
                return

            await app.DATABASE.create_workflow_copilot_chat_message(
                organization_id=chat.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                sender=WorkflowCopilotChatSender.USER,
                content=chat_request.message,
            )

            assistant_message = await app.DATABASE.create_workflow_copilot_chat_message(
                organization_id=chat.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                sender=WorkflowCopilotChatSender.AI,
                content=user_response,
                global_llm_context=updated_global_llm_context,
            )

            yield JSONServerSentEvent(
                data=WorkflowCopilotStreamResponseUpdate(
                    type=WorkflowCopilotStreamMessageType.RESPONSE,
                    workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                    message=user_response,
                    updated_workflow=updated_workflow.model_dump(mode="json") if updated_workflow else None,
                    response_time=assistant_message.created_at,
                ).model_dump(mode="json"),
            )
        except HTTPException as exc:
            if await request.is_disconnected():
                return
            yield JSONServerSentEvent(
                data=WorkflowCopilotStreamErrorUpdate(
                    type=WorkflowCopilotStreamMessageType.ERROR,
                    error=exc.detail,
                ).model_dump(mode="json"),
            )
        except LLMProviderError as exc:
            if await request.is_disconnected():
                return
            LOG.error(
                "LLM provider error",
                organization_id=organization.organization_id,
                error=str(exc),
                exc_info=True,
            )
            yield JSONServerSentEvent(
                data=WorkflowCopilotStreamErrorUpdate(
                    type=WorkflowCopilotStreamMessageType.ERROR,
                    error="Failed to process your request. Please try again.",
                ).model_dump(mode="json"),
            )
        except Exception as exc:
            if await request.is_disconnected():
                return
            LOG.error(
                "Unexpected error in workflow copilot",
                organization_id=organization.organization_id,
                error=str(exc),
                exc_info=True,
            )
            yield JSONServerSentEvent(
                data=WorkflowCopilotStreamErrorUpdate(
                    type=WorkflowCopilotStreamMessageType.ERROR, error="An error occurred. Please try again."
                ).model_dump(mode="json"),
            )

    def ping_message_factory() -> ServerSentEvent:
        return ServerSentEvent(comment="keep-alive")

    return EventSourceResponse(
        event_stream(),
        ping=SSE_KEEPALIVE_INTERVAL_SECONDS,
        ping_message_factory=ping_message_factory,
    )


@base_router.get("/workflow/copilot/chat-history", include_in_schema=False)
async def workflow_copilot_chat_history(
    workflow_permanent_id: str,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowCopilotChatHistoryResponse:
    latest_chat = await app.DATABASE.get_latest_workflow_copilot_chat(
        organization_id=organization.organization_id,
        workflow_permanent_id=workflow_permanent_id,
    )
    if not latest_chat:
        return WorkflowCopilotChatHistoryResponse(workflow_copilot_chat_id=None, chat_history=[])
    chat_messages = await app.DATABASE.get_workflow_copilot_chat_messages(
        workflow_copilot_chat_id=latest_chat.workflow_copilot_chat_id,
    )
    return WorkflowCopilotChatHistoryResponse(
        workflow_copilot_chat_id=latest_chat.workflow_copilot_chat_id,
        chat_history=convert_to_history_messages(chat_messages),
    )


def convert_to_history_messages(
    messages: list[WorkflowCopilotChatMessage],
) -> list[WorkflowCopilotChatHistoryMessage]:
    return [
        WorkflowCopilotChatHistoryMessage(
            sender=message.sender,
            content=message.content,
            created_at=message.created_at,
        )
        for message in messages
    ]
