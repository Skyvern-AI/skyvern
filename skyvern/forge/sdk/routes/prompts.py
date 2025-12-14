"""
Endpoints for prompt management.
"""

import structlog
from fastapi import Depends, HTTPException, Query, status

from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.prompts import ImprovePromptRequest, ImprovePromptResponse
from skyvern.forge.sdk.services import org_auth_service

LOG = structlog.get_logger()


class Constants:
    DEFAULT_TEMPLATE_NAME = "improve-prompt-for-ai-browser-agent"
    IMPROVE_PROMPT_USE_CASE_TO_TEMPLATE_MAP = {
        "new_workflow": DEFAULT_TEMPLATE_NAME,
        "task_v2_prompt": DEFAULT_TEMPLATE_NAME,
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
