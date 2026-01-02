from typing import Any

import structlog
from fastapi import Depends, HTTPException, status

from skyvern.core.script_generations.real_skyvern_page_ai import RealSkyvernPageAi
from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage
from skyvern.forge import app
from skyvern.forge.sdk.api.files import validate_download_url
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.sdk_actions import (
    RunSdkActionRequest,
    RunSdkActionResponse,
)
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.workflow import (
    WorkflowRequestBody,
    WorkflowRunStatus,
)
from skyvern.schemas.workflows import BlockType, WorkflowStatus

LOG = structlog.get_logger()


@base_router.post(
    "/sdk/run_action",
    response_model=RunSdkActionResponse,
    summary="Run an SDK action",
    description="Execute a single SDK action with the specified parameters",
    tags=["SDK"],
    openapi_extra={
        "x-fern-sdk-method-name": "run_sdk_action",
    },
)
@base_router.post("/sdk/run_action/", include_in_schema=False)
async def run_sdk_action(
    action_request: RunSdkActionRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> RunSdkActionResponse:
    """Execute a single SDK action with the specified parameters."""
    LOG.info(
        "Running SDK action",
        organization_id=organization.organization_id,
        action_type=action_request.action.type,
    )

    organization_id = organization.organization_id
    browser_session_id = action_request.browser_session_id
    browser_address = action_request.browser_address
    action = action_request.action

    # Use existing workflow_run_id if provided, otherwise create a new one
    if action_request.workflow_run_id:
        workflow_run = await app.DATABASE.get_workflow_run(
            workflow_run_id=action_request.workflow_run_id,
            organization_id=organization_id,
        )
        if not workflow_run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow run {action_request.workflow_run_id} not found",
            )
        workflow = await app.DATABASE.get_workflow(
            workflow_id=workflow_run.workflow_id,
            organization_id=organization_id,
        )
        if not workflow:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow {workflow_run.workflow_id} not found",
            )
    else:
        workflow = await app.WORKFLOW_SERVICE.create_empty_workflow(
            organization,
            title="SDK Workflow",
            status=WorkflowStatus.auto_generated,
        )
        workflow_run = await app.WORKFLOW_SERVICE.setup_workflow_run(
            request_id=None,
            workflow_request=WorkflowRequestBody(
                browser_session_id=browser_session_id,
                browser_address=browser_address,
            ),
            workflow_permanent_id=workflow.workflow_permanent_id,
            organization=organization,
            version=None,
        )
        workflow_run = await app.DATABASE.update_workflow_run(
            workflow_run_id=workflow_run.workflow_run_id,
            status=WorkflowRunStatus.completed,
        )

    task = await app.DATABASE.create_task(
        organization_id=organization_id,
        url=action_request.url,
        navigation_goal=action.get_navigation_goal(),
        navigation_payload=action.get_navigation_payload(),
        data_extraction_goal=None,
        title=f"SDK Action Task: {action_request.action.type}",
        workflow_run_id=workflow_run.workflow_run_id,
        browser_session_id=browser_session_id,
        browser_address=browser_address,
    )

    step = await app.DATABASE.create_step(
        task.task_id,
        order=0,
        retry_index=0,
        organization_id=organization.organization_id,
    )

    await app.DATABASE.create_workflow_run_block(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=organization_id,
        block_type=BlockType.ACTION,
        task_id=task.task_id,
    )

    await app.WORKFLOW_CONTEXT_MANAGER.initialize_workflow_run_context(
        organization,
        workflow_run.workflow_run_id,
        workflow.title,
        workflow.workflow_id,
        workflow.workflow_permanent_id,
        [],
        [],
        [],
        [],
        None,
        workflow,
    )

    context = skyvern_context.ensure_context()
    skyvern_context.set(
        SkyvernContext(
            request_id=context.request_id,
            organization_id=task.organization_id,
            task_id=task.task_id,
            step_id=step.step_id,
            browser_session_id=browser_session_id,
            max_screenshot_scrolls=task.max_screenshot_scrolls,
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
        )
    )
    result: Any | None = None
    try:
        scraped_page = await ScriptSkyvernPage.create_scraped_page(browser_session_id=browser_session_id)
        page = await scraped_page._browser_state.must_get_working_page()
        page_ai = RealSkyvernPageAi(scraped_page, page)

        if action.type == "ai_click":
            result = await page_ai.ai_click(
                selector=action.selector,
                intention=action.intention,
                data=action.data,
                timeout=action.timeout,
            )
        elif action.type == "ai_input_text":
            result = await page_ai.ai_input_text(
                selector=action.selector,
                value=action.value,
                intention=action.intention,
                data=action.data,
                totp_identifier=action.totp_identifier,
                totp_url=action.totp_url,
                timeout=action.timeout,
            )
        elif action.type == "ai_select_option":
            result = await page_ai.ai_select_option(
                selector=action.selector,
                value=action.value,
                intention=action.intention,
                data=action.data,
                timeout=action.timeout,
            )
        elif action.type == "ai_upload_file":
            if action.file_url and not validate_download_url(action.file_url):
                raise HTTPException(status_code=400, detail="Unsupported file url")
            result = await page_ai.ai_upload_file(
                selector=action.selector,
                files=action.file_url,
                intention=action.intention,
                data=action.data,
                timeout=action.timeout,
            )
        elif action.type == "ai_act":
            await page_ai.ai_act(
                prompt=action.intention,
            )
            result = None
        elif action.type == "extract":
            extract_result = await page_ai.ai_extract(
                prompt=action.prompt,
                schema=action.extract_schema,
                error_code_mapping=action.error_code_mapping,
                intention=action.intention,
                data=action.data,
            )
            result = extract_result
        elif action.type == "locate_element":
            xpath_result = await page_ai.ai_locate_element(
                prompt=action.prompt,
            )
            result = xpath_result
        elif action.type == "validate":
            validation_result = await page_ai.ai_validate(
                prompt=action.prompt,
                model=action.model,
            )
            result = validation_result
        elif action.type == "prompt":
            prompt_result = await page_ai.ai_prompt(
                prompt=action.prompt,
                schema=action.response_schema,
                model=action.model,
            )
            result = prompt_result
        await app.DATABASE.update_task(
            task_id=task.task_id,
            organization_id=organization_id,
            status=TaskStatus.completed,
        )
    except Exception as e:
        await app.DATABASE.update_task(
            task_id=task.task_id,
            organization_id=organization_id,
            status=TaskStatus.failed,
            failure_reason=str(e),
        )
        LOG.error(
            "SDK action failed",
            action_type=action.type,
            error=str(e),
            exc_info=True,
        )
        raise
    finally:
        skyvern_context.reset()

    return RunSdkActionResponse(
        workflow_run_id=workflow_run.workflow_run_id,
        result=result,
    )
