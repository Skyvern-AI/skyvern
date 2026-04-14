"""
Endpoints for prompt management.
"""

import structlog
from fastapi import Depends, HTTPException, Query, status

from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.exceptions import (
    EmptyLLMResponseError,
    InvalidLLMResponseFormat,
    InvalidLLMResponseType,
    LLMProviderError,
)
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.prompts import (
    GenerateWorkflowTitleRequest,
    GenerateWorkflowTitleResponse,
    ImprovePromptRequest,
    ImprovePromptResponse,
    SummarizeOutputRequest,
    SummarizeOutputResponse,
)
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.service import generate_title_from_blocks_info
from skyvern.utils.strings import escape_code_fences

LOG = structlog.get_logger()


class Constants:
    DEFAULT_TEMPLATE_NAME = "improve-prompt-for-ai-browser-agent"
    EXTRACTION_TEMPLATE_NAME = "improve-prompt-for-data-extraction"
    IMPROVE_PROMPT_USE_CASE_TO_TEMPLATE_MAP = {
        "new_workflow": DEFAULT_TEMPLATE_NAME,
        "task_v2_prompt": DEFAULT_TEMPLATE_NAME,
        "workflow_editor.extraction.data_extraction_goal": EXTRACTION_TEMPLATE_NAME,
        "workflow_editor.extraction.data_schema": EXTRACTION_TEMPLATE_NAME,
        "workflow_editor.task.data_extraction_goal": EXTRACTION_TEMPLATE_NAME,
    }


def resolve_template_name(use_case: str) -> str:
    """
    Map a use-case to the template the LLM should receive.

    Defaults to the generic template so new use-cases can be added from the UI
    without requiring backend changes.
    """
    template_name = Constants.IMPROVE_PROMPT_USE_CASE_TO_TEMPLATE_MAP.get(use_case)
    if template_name:
        return template_name

    LOG.info(
        "Unknown improve prompt use case, falling back to default template",
        use_case=use_case,
        template_name=Constants.DEFAULT_TEMPLATE_NAME,
    )

    return Constants.DEFAULT_TEMPLATE_NAME


@base_router.post(
    "/prompts/improve",
    tags=["Prompts"],
    description="Improve a prompt based on a specific use-case",
    summary="Improve prompt",
    include_in_schema=False,
)
async def improve_prompt(
    request: ImprovePromptRequest,
    use_case: str = Query(..., alias="use-case", description="The use-case for prompt improvement"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ImprovePromptResponse:
    """
    Improve a prompt based on a specific use-case.
    """
    template_name = resolve_template_name(use_case)

    llm_prompt = prompt_engine.load_prompt(
        context=request.context,
        prompt=request.prompt,
        template=template_name,
    )

    LOG.info(
        "Improving prompt",
        use_case=use_case,
        organization_id=current_org.organization_id,
        context=request.context,
    )

    try:
        llm_response = await app.LLM_API_HANDLER(
            prompt=llm_prompt,
            prompt_name=template_name,
            organization_id=current_org.organization_id,
        )

        if isinstance(llm_response, dict) and "output" in llm_response:
            output = llm_response["output"]
        else:
            output = llm_response

        if not isinstance(output, dict):
            error = "LLM response is not valid JSON."
            output = ""
        elif "improved_prompt" not in output:
            error = "LLM response missing 'improved_prompt' field."
            output = ""
        else:
            error = None
            output = output["improved_prompt"]

        LOG.info(
            "Prompt improved",
            use_case=use_case,
            organization_id=current_org.organization_id,
            prompt=request.prompt,
            improved_prompt=output,
        )

        response = ImprovePromptResponse(
            error=error,
            improved=output.strip(),
            original=request.prompt,
        )

        return response

    except LLMProviderError:
        LOG.error("Failed to improve prompt", use_case=use_case, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to improve prompt. Please try again later.",
        )
    except Exception as e:
        LOG.error("Unexpected error improving prompt", use_case=use_case, error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to improve prompt: {str(e)}",
        )


@base_router.post(
    "/prompts/generate-workflow-title",
    tags=["Prompts"],
    description="Generate a meaningful workflow title from block content",
    summary="Generate workflow title",
    include_in_schema=False,
)
async def generate_workflow_title(
    request: GenerateWorkflowTitleRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> GenerateWorkflowTitleResponse:
    """Generate a meaningful workflow title based on block content using LLM."""
    LOG.info(
        "Generating workflow title",
        organization_id=current_org.organization_id,
        num_blocks=len(request.blocks),
    )

    try:
        blocks_info = [block.model_dump(exclude_none=True) for block in request.blocks]
        title = await generate_title_from_blocks_info(
            organization_id=current_org.organization_id,
            blocks_info=blocks_info,
        )
        return GenerateWorkflowTitleResponse(title=title)
    except LLMProviderError:
        LOG.error("Failed to generate workflow title", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to generate title. Please try again later.",
        )
    except Exception as e:
        LOG.error("Unexpected error generating workflow title", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to generate title: {str(e)}",
        )


@base_router.post(
    "/prompts/summarize-output",
    tags=["Prompts"],
    description="Summarize workflow run output JSON into a human-readable summary",
    summary="Summarize output",
    include_in_schema=False,
)
async def summarize_output(
    request: SummarizeOutputRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> SummarizeOutputResponse:
    template_name = "summarize-workflow-run-output"

    llm_prompt = prompt_engine.load_prompt(
        template=template_name,
        output_json=escape_code_fences(request.output_json),
        workflow_title=escape_code_fences(request.workflow_title),
        block_label=escape_code_fences(request.block_label),
    )

    LOG.info(
        "Summarizing workflow run output",
        organization_id=current_org.organization_id,
    )

    try:
        llm_response = await app.LLM_API_HANDLER(
            prompt=llm_prompt,
            prompt_name=template_name,
            organization_id=current_org.organization_id,
        )

        if isinstance(llm_response, dict) and "output" in llm_response:
            output = llm_response["output"]
        else:
            output = llm_response

        if not isinstance(output, dict):
            return SummarizeOutputResponse(
                error="LLM response is not valid JSON.",
                summary="",
            )
        if "summary" not in output:
            return SummarizeOutputResponse(
                error="LLM response missing 'summary' field.",
                summary="",
            )
        if not isinstance(output["summary"], str):
            return SummarizeOutputResponse(
                error="LLM 'summary' field is not a string.",
                summary="",
            )

        return SummarizeOutputResponse(
            error=None,
            summary=output["summary"].strip(),
        )

    except (InvalidLLMResponseFormat, InvalidLLMResponseType, EmptyLLMResponseError):
        LOG.warning("LLM returned malformed response while summarizing output", exc_info=True)
        return SummarizeOutputResponse(
            error="LLM response is not valid JSON.",
            summary="",
        )
    except LLMProviderError:
        LOG.error("LLM provider error while summarizing output", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to summarize output. Please try again later.",
        )
    except Exception:
        LOG.error("Unexpected error summarizing output", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to summarize output. Please try again later.",
        )
