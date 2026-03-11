import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog
import yaml
from fastapi import Depends, HTTPException, Request, status
from pydantic import ValidationError
from sse_starlette import EventSourceResponse

from skyvern.forge import app
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.copilot.output_utils import truncate_output
from skyvern.forge.sdk.experimentation.llm_prompt_config import get_llm_handler_for_prompt_type
from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream, FastAPIEventSourceStream
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatHistoryResponse,
    WorkflowCopilotChatMessage,
    WorkflowCopilotChatRequest,
    WorkflowCopilotChatSender,
    WorkflowCopilotClearProposedWorkflowRequest,
    WorkflowCopilotProcessingUpdate,
    WorkflowCopilotStreamErrorUpdate,
    WorkflowCopilotStreamMessageType,
    WorkflowCopilotStreamResponseUpdate,
    WorkflowYAMLConversionRequest,
    WorkflowYAMLConversionResponse,
)
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.exceptions import BaseWorkflowHTTPException
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import WorkflowDefinitionYAML

CHAT_HISTORY_CONTEXT_MESSAGES = 10

LOG = structlog.get_logger()


@dataclass(frozen=True)
class BlockRunInfo:
    block_label: str | None
    block_type: str
    block_status: str | None
    failure_reason: str | None
    output: str | None


@dataclass(frozen=True)
class RunInfo:
    blocks: list[BlockRunInfo]
    html: str | None


def _should_restore_persisted_workflow(auto_accept: bool | None, agent_result: object | None) -> bool:
    """Return True when a persisted draft should be rolled back after an interrupted request."""
    return auto_accept is not True and bool(getattr(agent_result, "workflow_was_persisted", False))


async def _restore_workflow_on_error(original_workflow: Workflow | None, organization_id: str) -> None:
    if not original_workflow:
        return
    try:
        await app.WORKFLOW_SERVICE.update_workflow_definition(
            workflow_id=original_workflow.workflow_id,
            organization_id=organization_id,
            title=original_workflow.title,
            description=original_workflow.description,
            workflow_definition=original_workflow.workflow_definition,
        )
    except Exception:
        LOG.warning(
            "Failed to restore original workflow after error",
            workflow_id=original_workflow.workflow_id,
            exc_info=True,
        )


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

    block_infos = []
    for block in blocks:
        block_type_name = block.block_type.name if hasattr(block.block_type, "name") else str(block.block_type)
        block_infos.append(
            BlockRunInfo(
                block_label=block.label,
                block_type=block_type_name,
                block_status=block.status,
                failure_reason=block.failure_reason,
                output=truncate_output(getattr(block, "output", None)),
            )
        )

    artifact = await _get_debug_artifact(organization_id, workflow_run_id)
    if artifact:
        artifact_bytes = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
        html = artifact_bytes.decode("utf-8") if artifact_bytes else None
    else:
        html = None

    return RunInfo(blocks=block_infos, html=html)


@base_router.post("/workflow/copilot/chat-post", include_in_schema=False)
async def workflow_copilot_chat_post(
    request: Request,
    chat_request: WorkflowCopilotChatRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> EventSourceResponse:
    async def stream_handler(stream: EventSourceStream) -> None:
        LOG.info(
            "Workflow copilot chat request",
            workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
            workflow_run_id=chat_request.workflow_run_id,
            message=chat_request.message,
            workflow_yaml_length=len(chat_request.workflow_yaml),
            organization_id=organization.organization_id,
        )

        original_workflow = None
        chat = None
        agent_result = None

        try:
            await stream.send(
                WorkflowCopilotProcessingUpdate(
                    type=WorkflowCopilotStreamMessageType.PROCESSING_UPDATE,
                    status="Processing...",
                    timestamp=datetime.now(timezone.utc),
                )
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

            chat_request.workflow_copilot_chat_id = chat.workflow_copilot_chat_id

            chat_messages = await app.DATABASE.get_workflow_copilot_chat_messages(
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            )
            global_llm_context = None
            for message in reversed(chat_messages):
                if message.global_llm_context is not None:
                    global_llm_context = message.global_llm_context
                    break

            if chat.proposed_workflow and chat.proposed_workflow.get("_copilot_yaml"):
                chat_request.workflow_yaml = chat.proposed_workflow["_copilot_yaml"]

            debug_run_info = await _get_debug_run_info(organization.organization_id, chat_request.workflow_run_id)

            debug_run_info_text = ""
            if debug_run_info:
                parts = []
                for bi in debug_run_info.blocks:
                    block_text = f"Block: {bi.block_label} ({bi.block_type}) — {bi.block_status}"
                    if bi.failure_reason:
                        block_text += f"\n  Failure Reason: {bi.failure_reason}"
                    if bi.output:
                        block_text += f"\n  Output: {bi.output}"
                    parts.append(block_text)
                debug_run_info_text = "\n".join(parts)
                if debug_run_info.html:
                    debug_run_info_text += f"\n\nVisible Elements Tree (HTML):\n{debug_run_info.html}"

            await stream.send(
                WorkflowCopilotProcessingUpdate(
                    type=WorkflowCopilotStreamMessageType.PROCESSING_UPDATE,
                    status="Thinking...",
                    timestamp=datetime.now(timezone.utc),
                )
            )

            if await stream.is_disconnected():
                LOG.info(
                    "Workflow copilot chat request is disconnected before agent loop",
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                )
                return

            original_workflow = await app.DATABASE.get_workflow_by_permanent_id(
                workflow_permanent_id=chat_request.workflow_permanent_id,
                organization_id=organization.organization_id,
            )

            if not original_workflow:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

            chat_request.workflow_id = original_workflow.workflow_id

            llm_api_handler = (
                await get_llm_handler_for_prompt_type(
                    "workflow-copilot", chat_request.workflow_permanent_id, organization.organization_id
                )
                or app.LLM_API_HANDLER
            )

            from skyvern.forge.sdk.copilot.agent import run_copilot_agent

            api_key = request.headers.get("x-api-key")

            agent_result = await run_copilot_agent(
                stream=stream,
                organization_id=organization.organization_id,
                chat_request=chat_request,
                chat_history=convert_to_history_messages(chat_messages[-CHAT_HISTORY_CONTEXT_MESSAGES:]),
                global_llm_context=global_llm_context,
                debug_run_info_text=debug_run_info_text,
                llm_api_handler=llm_api_handler,
                api_key=api_key,
            )

            user_response = agent_result.user_response
            updated_workflow = agent_result.updated_workflow
            updated_global_llm_context = agent_result.global_llm_context

            if await stream.is_disconnected():
                LOG.info(
                    "Workflow copilot chat request is disconnected after agent loop",
                    workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
                )
                if _should_restore_persisted_workflow(chat.auto_accept, agent_result):
                    await _restore_workflow_on_error(original_workflow, organization.organization_id)
                return

            if chat.auto_accept is not True:
                if _should_restore_persisted_workflow(chat.auto_accept, agent_result):
                    await _restore_workflow_on_error(original_workflow, organization.organization_id)
                if updated_workflow:
                    proposed_data = updated_workflow.model_dump(mode="json")
                    if agent_result.workflow_yaml:
                        proposed_data["_copilot_yaml"] = agent_result.workflow_yaml
                    await app.DATABASE.update_workflow_copilot_chat(
                        organization_id=chat.organization_id,
                        workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                        proposed_workflow=proposed_data,
                    )

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

            await stream.send(
                WorkflowCopilotStreamResponseUpdate(
                    type=WorkflowCopilotStreamMessageType.RESPONSE,
                    workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                    message=user_response,
                    updated_workflow=updated_workflow.model_dump(mode="json") if updated_workflow else None,
                    response_time=assistant_message.created_at,
                )
            )
        except HTTPException as exc:
            await _restore_workflow_on_error(original_workflow, organization.organization_id)
            await stream.send(
                WorkflowCopilotStreamErrorUpdate(
                    type=WorkflowCopilotStreamMessageType.ERROR,
                    error=exc.detail,
                )
            )
        except LLMProviderError as exc:
            await _restore_workflow_on_error(original_workflow, organization.organization_id)
            LOG.error(
                "LLM provider error",
                organization_id=organization.organization_id,
                error=str(exc),
                exc_info=True,
            )
            await stream.send(
                WorkflowCopilotStreamErrorUpdate(
                    type=WorkflowCopilotStreamMessageType.ERROR,
                    error="Failed to process your request. Please try again.",
                )
            )
        except asyncio.CancelledError:
            if chat is not None and _should_restore_persisted_workflow(chat.auto_accept, agent_result):
                await asyncio.shield(_restore_workflow_on_error(original_workflow, organization.organization_id))
            LOG.info(
                "Client disconnected during workflow copilot",
                workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
            )
        except Exception as exc:
            await _restore_workflow_on_error(original_workflow, organization.organization_id)
            LOG.error(
                "Unexpected error in workflow copilot",
                organization_id=organization.organization_id,
                error=str(exc),
                exc_info=True,
            )
            await stream.send(
                WorkflowCopilotStreamErrorUpdate(
                    type=WorkflowCopilotStreamMessageType.ERROR,
                    error="An error occurred. Please try again.",
                )
            )

    return FastAPIEventSourceStream.create(request, stream_handler)


@base_router.get("/workflow/copilot/chat-history", include_in_schema=False)
async def workflow_copilot_chat_history(
    workflow_permanent_id: str,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowCopilotChatHistoryResponse:
    latest_chat = await app.DATABASE.get_latest_workflow_copilot_chat(
        organization_id=organization.organization_id,
        workflow_permanent_id=workflow_permanent_id,
    )
    if latest_chat:
        chat_messages = await app.DATABASE.get_workflow_copilot_chat_messages(latest_chat.workflow_copilot_chat_id)
    else:
        chat_messages = []
    return WorkflowCopilotChatHistoryResponse(
        workflow_copilot_chat_id=latest_chat.workflow_copilot_chat_id if latest_chat else None,
        chat_history=convert_to_history_messages(chat_messages),
        proposed_workflow=latest_chat.proposed_workflow if latest_chat else None,
        auto_accept=latest_chat.auto_accept if latest_chat else None,
    )


@base_router.post(
    "/workflow/copilot/clear-proposed-workflow", include_in_schema=False, status_code=status.HTTP_204_NO_CONTENT
)
async def workflow_copilot_clear_proposed_workflow(
    clear_request: WorkflowCopilotClearProposedWorkflowRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> None:
    updated_chat = await app.DATABASE.update_workflow_copilot_chat(
        organization_id=organization.organization_id,
        workflow_copilot_chat_id=clear_request.workflow_copilot_chat_id,
        proposed_workflow=None,
        auto_accept=clear_request.auto_accept,
    )
    if not updated_chat:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")


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


@base_router.post("/workflow/copilot/convert-yaml-to-blocks", include_in_schema=False)
async def workflow_copilot_convert_yaml_to_blocks(
    request: WorkflowYAMLConversionRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowYAMLConversionResponse:
    try:
        parsed_yaml = yaml.safe_load(request.workflow_definition_yaml)
        workflow_definition_yaml = WorkflowDefinitionYAML.model_validate(parsed_yaml)

        workflow_definition = convert_workflow_definition(
            workflow_definition_yaml=workflow_definition_yaml,
            workflow_id=request.workflow_id,
        )

        return WorkflowYAMLConversionResponse(workflow_definition=workflow_definition.model_dump(mode="json"))
    except (yaml.YAMLError, ValidationError, BaseWorkflowHTTPException) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to convert workflow YAML: {str(e)}",
        )
