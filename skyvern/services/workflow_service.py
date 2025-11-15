import typing as t

import structlog
from fastapi import BackgroundTasks, Request

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.exceptions import InvalidTemplateWorkflowPermanentId
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody, WorkflowRun
from skyvern.schemas.runs import RunStatus, RunType, WorkflowRunRequest, WorkflowRunResponse

LOG = structlog.get_logger(__name__)


async def prepare_workflow(
    workflow_id: str,
    organization: Organization,
    workflow_request: WorkflowRequestBody,  # this is the deprecated workflow request body
    template: bool = False,
    version: int | None = None,
    max_steps: int | None = None,
    request_id: str | None = None,
    debug_session_id: str | None = None,
    code_gen: bool | None = None,
) -> WorkflowRun:
    """
    Prepare a workflow to be run.
    """
    if template:
        if workflow_id not in await app.STORAGE.retrieve_global_workflows():
            raise InvalidTemplateWorkflowPermanentId(workflow_permanent_id=workflow_id)

    workflow_run = await app.WORKFLOW_SERVICE.setup_workflow_run(
        request_id=request_id,
        workflow_request=workflow_request,
        workflow_permanent_id=workflow_id,
        organization=organization,
        version=version,
        max_steps_override=max_steps,
        is_template_workflow=template,
        debug_session_id=debug_session_id,
        code_gen=code_gen,
    )

    workflow = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_id,
        organization_id=None if template else organization.organization_id,
        version=version,
    )

    await app.DATABASE.create_task_run(
        task_run_type=RunType.workflow_run,
        organization_id=organization.organization_id,
        run_id=workflow_run.workflow_run_id,
        title=workflow.title,
    )

    if max_steps:
        LOG.info("Overriding max steps per run", max_steps_override=max_steps)

    return workflow_run


async def run_workflow(
    workflow_id: str,
    organization: Organization,
    workflow_request: WorkflowRequestBody,  # this is the deprecated workflow request body
    template: bool = False,
    version: int | None = None,
    max_steps: int | None = None,
    api_key: str | None = None,
    request_id: str | None = None,
    request: Request | None = None,
    background_tasks: BackgroundTasks | None = None,
    block_labels: list[str] | None = None,
    block_outputs: dict[str, t.Any] | None = None,
) -> WorkflowRun:
    workflow_run = await prepare_workflow(
        workflow_id=workflow_id,
        organization=organization,
        workflow_request=workflow_request,
        template=template,
        version=version,
        max_steps=max_steps,
        request_id=request_id,
    )

    await AsyncExecutorFactory.get_executor().execute_workflow(
        request=request,
        background_tasks=background_tasks,
        organization=organization,
        workflow_id=workflow_run.workflow_id,
        workflow_run_id=workflow_run.workflow_run_id,
        workflow_permanent_id=workflow_run.workflow_permanent_id,
        max_steps_override=max_steps,
        browser_session_id=workflow_request.browser_session_id,
        api_key=api_key,
        block_labels=block_labels,
        block_outputs=block_outputs,
    )

    return workflow_run


async def get_workflow_run_response(
    workflow_run_id: str, organization_id: str | None = None
) -> WorkflowRunResponse | None:
    workflow_run = await app.DATABASE.get_workflow_run(workflow_run_id, organization_id=organization_id)
    if not workflow_run:
        return None
    workflow_run_resp = await app.WORKFLOW_SERVICE.build_workflow_run_status_response_by_workflow_id(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=organization_id,
    )
    app_url = f"{settings.SKYVERN_APP_URL.rstrip('/')}/runs/{workflow_run.workflow_run_id}"
    return WorkflowRunResponse(
        run_id=workflow_run_id,
        run_type=RunType.workflow_run,
        status=RunStatus(workflow_run.status),
        output=workflow_run_resp.outputs,
        downloaded_files=workflow_run_resp.downloaded_files,
        recording_url=workflow_run_resp.recording_url,
        screenshot_urls=workflow_run_resp.screenshot_urls,
        failure_reason=workflow_run_resp.failure_reason,
        queued_at=workflow_run.queued_at,
        started_at=workflow_run.started_at,
        finished_at=workflow_run.finished_at,
        app_url=app_url,
        created_at=workflow_run.created_at,
        modified_at=workflow_run.modified_at,
        browser_profile_id=workflow_run.browser_profile_id,
        run_request=WorkflowRunRequest(
            workflow_id=workflow_run.workflow_permanent_id,
            title=workflow_run_resp.workflow_title,
            parameters=workflow_run_resp.parameters,
            proxy_location=workflow_run.proxy_location,
            webhook_url=workflow_run.webhook_callback_url or None,
            totp_url=workflow_run.totp_verification_url or None,
            totp_identifier=workflow_run.totp_identifier,
            max_screenshot_scrolls=workflow_run.max_screenshot_scrolls,
            browser_address=workflow_run.browser_address,
            browser_profile_id=workflow_run.browser_profile_id,
            # TODO: add browser session id
        ),
        errors=workflow_run_resp.errors,
    )
