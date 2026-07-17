import typing as t

import structlog
from fastapi import BackgroundTasks, Request

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.exceptions import InvalidTemplateWorkflowPermanentId
from skyvern.forge.sdk.workflow.models.tags import TagWriteContext
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody, WorkflowRun
from skyvern.schemas.runs import RunStatus, RunType, WorkflowRunRequest, WorkflowRunResponse

LOG = structlog.get_logger(__name__)


def workflow_request_body_from_existing_run(
    workflow_run: WorkflowRun,
    parameters: dict[str, t.Any] | None = None,
    run_metadata: dict[str, str] | None = None,
) -> WorkflowRequestBody:
    return WorkflowRequestBody(
        data=parameters,
        proxy_location=workflow_run.proxy_location,
        webhook_callback_url=workflow_run.webhook_callback_url,
        totp_verification_url=workflow_run.totp_verification_url,
        totp_identifier=workflow_run.totp_identifier,
        browser_session_id=workflow_run.browser_session_id,
        browser_profile_id=workflow_run.browser_profile_id,
        max_screenshot_scrolls=workflow_run.max_screenshot_scrolls,
        max_elapsed_time_minutes=workflow_run.max_elapsed_time_minutes,
        extra_http_headers=workflow_run.extra_http_headers,
        cdp_connect_headers=workflow_run.cdp_connect_headers,
        browser_address=workflow_run.browser_address,
        run_with=workflow_run.run_with,
        ai_fallback=workflow_run.ai_fallback,
        run_metadata=run_metadata,
    )


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
    parent_workflow_run_id: str | None = None,
    trigger_type: WorkflowRunTriggerType | None = None,
    workflow_schedule_id: str | None = None,
    workflow_run_id: str | None = None,
    retried_from_workflow_run_id: str | None = None,
    fallback_attempt: int | None = None,
    ignore_inherited_workflow_system_prompt: bool = False,
    copilot_session_id: str | None = None,
    resolved_workflow_id: str | None = None,
    tag_write_context: TagWriteContext | None = None,
) -> WorkflowRun:
    """
    Prepare a workflow to be run.

    ``resolved_workflow_id`` pins the exact workflow version row; when None, resolve by
    permanent id + version.
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
        workflow_run_id=workflow_run_id,
        parent_workflow_run_id=parent_workflow_run_id,
        trigger_type=trigger_type,
        workflow_schedule_id=workflow_schedule_id,
        retried_from_workflow_run_id=retried_from_workflow_run_id,
        fallback_attempt=fallback_attempt,
        ignore_inherited_workflow_system_prompt=ignore_inherited_workflow_system_prompt,
        copilot_session_id=copilot_session_id,
        resolved_workflow_id=resolved_workflow_id,
        tag_write_context=tag_write_context,
    )

    if resolved_workflow_id is not None:
        workflow = await app.WORKFLOW_SERVICE.get_workflow(
            workflow_id=resolved_workflow_id,
            organization_id=None if template else organization.organization_id,
        )
    else:
        workflow = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
            workflow_permanent_id=workflow_id,
            organization_id=None if template else organization.organization_id,
            version=version,
        )

    await app.DATABASE.tasks.create_task_run(
        task_run_type=RunType.workflow_run,
        organization_id=organization.organization_id,
        run_id=workflow_run.workflow_run_id,
        title=workflow.title,
        status=RunStatus.queued,
        workflow_permanent_id=workflow_id,
        parent_workflow_run_id=parent_workflow_run_id,
        debug_session_id=debug_session_id,
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
    parent_workflow_run_id: str | None = None,
    trigger_type: WorkflowRunTriggerType | None = None,
    workflow_schedule_id: str | None = None,
    retried_from_workflow_run_id: str | None = None,
    fallback_attempt: int | None = None,
    ignore_inherited_workflow_system_prompt: bool = False,
    tag_write_context: TagWriteContext | None = None,
) -> WorkflowRun:
    workflow_run = await prepare_workflow(
        workflow_id=workflow_id,
        organization=organization,
        workflow_request=workflow_request,
        template=template,
        version=version,
        max_steps=max_steps,
        request_id=request_id,
        parent_workflow_run_id=parent_workflow_run_id,
        trigger_type=trigger_type,
        workflow_schedule_id=workflow_schedule_id,
        retried_from_workflow_run_id=retried_from_workflow_run_id,
        fallback_attempt=fallback_attempt,
        ignore_inherited_workflow_system_prompt=ignore_inherited_workflow_system_prompt,
        tag_write_context=tag_write_context,
    )

    await AsyncExecutorFactory.get_executor().execute_workflow(
        request=request,
        background_tasks=background_tasks,
        organization=organization,
        workflow_id=workflow_run.workflow_id,
        workflow_run_id=workflow_run.workflow_run_id,
        workflow_permanent_id=workflow_run.workflow_permanent_id,
        max_steps_override=max_steps,
        browser_session_id=workflow_run.browser_session_id,
        api_key=api_key,
        block_labels=block_labels,
        block_outputs=block_outputs,
    )

    return workflow_run


async def get_workflow_run_response(
    workflow_run_id: str, organization_id: str | None = None
) -> WorkflowRunResponse | None:
    workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(workflow_run_id, organization_id=organization_id)
    if not workflow_run:
        return None
    workflow_run_resp = await app.WORKFLOW_SERVICE.build_workflow_run_status_response_by_workflow_id(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=organization_id,
        include_step_count=True,
    )
    app_url = f"{settings.SKYVERN_APP_URL.rstrip('/')}/runs/{workflow_run.workflow_run_id}"
    return WorkflowRunResponse(
        run_id=workflow_run_id,
        run_type=RunType.workflow_run,
        status=RunStatus(workflow_run.status),
        output=workflow_run_resp.outputs,
        downloaded_files=workflow_run_resp.downloaded_files,
        recording_url=workflow_run_resp.recording_url,
        recording_archived=workflow_run_resp.recording_archived,
        screenshot_urls=workflow_run_resp.screenshot_urls,
        failure_reason=workflow_run_resp.failure_reason,
        queued_at=workflow_run.queued_at,
        started_at=workflow_run.started_at,
        finished_at=workflow_run.finished_at,
        app_url=app_url,
        created_at=workflow_run.created_at,
        modified_at=workflow_run.modified_at,
        run_with=workflow_run.run_with,
        ai_fallback=workflow_run.ai_fallback,
        browser_session_id=workflow_run.browser_session_id,
        browser_profile_id=workflow_run.browser_profile_id,
        max_screenshot_scrolls=workflow_run.max_screenshot_scrolls,
        script_run=workflow_run.script_run,
        script_id=workflow_run.script_run.script_id if workflow_run.script_run else None,
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
            browser_session_id=workflow_run.browser_session_id,
        ),
        errors=workflow_run_resp.errors,
        step_count=workflow_run_resp.total_steps,
    )
