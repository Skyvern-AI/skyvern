from typing import Any

import structlog
from fastapi import Depends, HTTPException, status

from skyvern.core.script_generations.real_skyvern_page_ai import RealSkyvernPageAi
from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage
from skyvern.exceptions import ActionPolicyBlocked, ScrapingFailed, SkyvernActionFailed
from skyvern.forge import app
from skyvern.forge.sdk.api.files import validate_download_url
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.sdk_actions import (
    RunSdkActionRequest,
    RunSdkActionResponse,
)
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
from skyvern.schemas.workflows import BlockType, WorkflowStatus

LOG = structlog.get_logger()

# Concurrent run_action calls can share a client-provided workflow_run_id on this process-global
# manager, so the run-context must survive until the LAST sibling finishes. Reference-count it here
# rather than only tearing down self-created runs — a client-provided run's context was otherwise
# never removed, leaving a permanent liveness ghost that would veto a real run's terminal browser
# close. No await separates an inc from its matching dec, so a plain dict is race-free.
_sdk_action_context_refcounts: dict[str, int] = {}


def _acquire_sdk_action_context(workflow_run_id: str) -> None:
    _sdk_action_context_refcounts[workflow_run_id] = _sdk_action_context_refcounts.get(workflow_run_id, 0) + 1


def _release_sdk_action_context(workflow_run_id: str) -> bool:
    """Decrement the refcount; return True when this was the last live SDK action for the run."""
    remaining = _sdk_action_context_refcounts.get(workflow_run_id, 0) - 1
    if remaining <= 0:
        _sdk_action_context_refcounts.pop(workflow_run_id, None)
        return True
    _sdk_action_context_refcounts[workflow_run_id] = remaining
    return False


@base_router.post(
    "/sdk/run_action",
    response_model=RunSdkActionResponse,
    summary="Run an SDK action",
    description="Execute a single SDK action with the specified parameters",
    tags=["SDK"],
    openapi_extra={
        "x-excluded": True,
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
    created_workflow_run = not action_request.workflow_run_id
    if action_request.workflow_run_id:
        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id=action_request.workflow_run_id,
            organization_id=organization_id,
        )
        if not workflow_run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Workflow run {action_request.workflow_run_id} not found",
            )
        workflow = await app.DATABASE.workflows.get_workflow(
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
            trigger_type=WorkflowRunTriggerType.api,
        )
        workflow_run = await app.WORKFLOW_SERVICE.mark_workflow_run_as_completed(
            workflow_run_id=workflow_run.workflow_run_id,
        )

    task = await app.DATABASE.tasks.create_task(
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

    step = await app.DATABASE.tasks.create_step(
        task.task_id,
        order=0,
        retry_index=0,
        organization_id=organization.organization_id,
    )

    await app.DATABASE.observer.create_workflow_run_block(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=organization_id,
        block_type=BlockType.ACTION,
        task_id=task.task_id,
    )

    _acquire_sdk_action_context(workflow_run.workflow_run_id)
    # Nested finally: the OUTERMOST finally releases the ownership refcount with no await before
    # it, so it runs even when initialize_workflow_run_context fails/is cancelled or the
    # upload-drain await below is cancelled. Otherwise a client-provided run id could leak its
    # refcount and leave a permanent liveness ghost that vetoes a real run's terminal browser
    # close.
    try:
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
            mask_secrets=getattr(workflow, "mask_secrets", False),
        )

        context = skyvern_context.ensure_context()
        skyvern_context.replace(
            SkyvernContext(
                request_id=context.request_id,
                organization_id=task.organization_id,
                task_id=task.task_id,
                step_id=step.step_id,
                browser_session_id=browser_session_id,
                max_screenshot_scrolls=task.max_screenshot_scrolls,
                workflow_id=workflow.workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
                workflow_run_is_synthetic=created_workflow_run,
            )
        )
        result: Any | None = None
        try:
            app.AGENT_FUNCTION.register_browser_origin_authority(
                task_id=task.task_id,
                workflow_run_id=workflow_run.workflow_run_id,
                url=action_request.url,
            )
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
                if action.file_url and not validate_download_url(action.file_url, organization_id=organization_id):
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
            await app.DATABASE.tasks.update_task(
                task_id=task.task_id,
                organization_id=organization_id,
                status=TaskStatus.completed,
            )
        except ActionPolicyBlocked as e:
            await app.DATABASE.tasks.update_task(
                task_id=task.task_id,
                organization_id=organization_id,
                status=TaskStatus.failed,
                failure_reason=e.message,
            )
            LOG.warning(
                "SDK action blocked by extension policy",
                action_type=action.type,
                error=e.message,
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.message)
        except ScrapingFailed as e:
            await app.DATABASE.tasks.update_task(
                task_id=task.task_id,
                organization_id=organization_id,
                status=TaskStatus.failed,
                failure_reason=str(e.reason) if e.reason else str(e),
            )
            LOG.warning(
                "SDK action failed due to scraping error",
                action_type=action.type,
                error=str(e),
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.reason or str(e))
        except SkyvernActionFailed as e:
            await app.DATABASE.tasks.update_task(
                task_id=task.task_id,
                organization_id=organization_id,
                status=TaskStatus.failed,
                failure_reason=e.reason,
            )
            LOG.warning(
                "SDK action failed",
                action_type=action.type,
                error=e.reason,
            )
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=e.reason)
        except Exception as e:
            await app.DATABASE.tasks.update_task(
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
            # This route runs the whole mini-task in-process, so nothing in the worker lifecycle
            # ever drains its artifact uploads / step archives (SKY-12524). Best-effort: this drain
            # await may itself be cancelled — the run-context release lives in the OUTER finally so it
            # still runs in that case.
            try:
                await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks([task.task_id])
            except Exception:
                LOG.warning("Failed to drain artifact uploads for SDK action", task_id=task.task_id, exc_info=True)
            skyvern_context.reset()

        return RunSdkActionResponse(
            workflow_run_id=workflow_run.workflow_run_id,
            result=result,
        )
    finally:
        # Remove the run-context only once the last concurrent run_action for this run finishes.
        # A client-provided workflow_run_id is shared across sibling calls, so an unconditional
        # removal would yank the context out from under an in-flight sibling; never removing it
        # left a permanent liveness ghost. The refcount resolves both.
        if _release_sdk_action_context(workflow_run.workflow_run_id):
            app.WORKFLOW_CONTEXT_MANAGER.remove_workflow_run_context(workflow_run.workflow_run_id)
