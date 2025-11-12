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
    ImprovePromptUseCaseToTemplateMap = {
        "new_workflow": "improve-prompt-for-ai-browser-agent",
        "task_v2_prompt": "improve-prompt-for-ai-browser-agent",
    }


@base_router.post(
    "/prompts/improve",
    tags=["Prompts"],
    description="Improve a prompt based on a specific use-case",
    summary="Improve prompt",
)
async def improve_prompt(
    request: ImprovePromptRequest,
    use_case: str = Query(..., alias="use-case", description="The use-case for prompt improvement"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ImprovePromptResponse:
    """
    Improve a prompt based on a specific use-case.
    """
    if use_case not in Constants.ImprovePromptUseCaseToTemplateMap:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{use_case}' use-case is unsupported.",
        )

    template_name = Constants.ImprovePromptUseCaseToTemplateMap[use_case]

    llm_prompt = prompt_engine.load_prompt(
        context=request.context,
        prompt=request.prompt,
        template=template_name,
    )

    LOG.info(
        "Improving prompt",
        use_case=use_case,
        organization_id=current_org.organization_id,
        prompt=request.prompt,
        llm_prompt=llm_prompt,
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

        response = ImprovePromptResponse(
            error=error,
            improved=output,
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
