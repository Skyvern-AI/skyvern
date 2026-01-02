from fastapi import HTTPException, status

from skyvern.config import settings
from skyvern.exceptions import OrganizationNotFound, TaskNotFound, WorkflowRunNotFound
from skyvern.forge import app
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.runs import RunEngine, RunResponse, RunType, TaskRunRequest, TaskRunResponse
from skyvern.services import task_v1_service, task_v2_service, webhook_service, workflow_service


async def get_run_response(run_id: str, organization_id: str | None = None) -> RunResponse | None:
    run = await app.DATABASE.get_run(run_id, organization_id=organization_id)
    if not run:
        # try to see if it's a workflow run id for task v2
        task_v2 = await app.DATABASE.get_task_v2_by_workflow_run_id(run_id, organization_id=organization_id)
        if task_v2:
            run = await app.DATABASE.get_run(task_v2.observer_cruise_id, organization_id=organization_id)

    if not run:
        return None

    if (
        run.task_run_type == RunType.task_v1
        or run.task_run_type == RunType.openai_cua
        or run.task_run_type == RunType.anthropic_cua
        or run.task_run_type == RunType.ui_tars
    ):
        # fetch task v1 from db and transform to task run response
        try:
            task_v1_response = await task_v1_service.get_task_v1_response(
                task_id=run.run_id, organization_id=organization_id
            )
        except TaskNotFound:
            return None
        run_engine = RunEngine.skyvern_v1
        if run.task_run_type == RunType.openai_cua:
            run_engine = RunEngine.openai_cua
        elif run.task_run_type == RunType.anthropic_cua:
            run_engine = RunEngine.anthropic_cua
        elif run.task_run_type == RunType.ui_tars:
            run_engine = RunEngine.ui_tars

        return TaskRunResponse(
            run_id=run.run_id,
            run_type=run.task_run_type,
            status=str(task_v1_response.status),
            output=task_v1_response.extracted_information,
            failure_reason=task_v1_response.failure_reason,
            queued_at=task_v1_response.queued_at,
            started_at=task_v1_response.started_at,
            finished_at=task_v1_response.finished_at,
            created_at=task_v1_response.created_at,
            modified_at=task_v1_response.modified_at,
            app_url=f"{settings.SKYVERN_APP_URL.rstrip('/')}/tasks/{task_v1_response.task_id}",
            recording_url=task_v1_response.recording_url,
            screenshot_urls=task_v1_response.action_screenshot_urls,
            downloaded_files=task_v1_response.downloaded_files,
            run_request=TaskRunRequest(
                engine=run_engine,
                prompt=task_v1_response.request.navigation_goal,
                url=task_v1_response.request.url,
                webhook_url=task_v1_response.request.webhook_callback_url,
                totp_identifier=task_v1_response.request.totp_identifier,
                totp_url=task_v1_response.request.totp_verification_url,
                proxy_location=task_v1_response.request.proxy_location,
                max_steps=task_v1_response.max_steps_per_run,
                data_extraction_schema=task_v1_response.request.extracted_information_schema,
                error_code_mapping=task_v1_response.request.error_code_mapping,
                max_screenshot_scrolls=task_v1_response.request.max_screenshot_scrolls,
            ),
            errors=task_v1_response.errors,
        )
    elif run.task_run_type == RunType.task_v2:
        task_v2 = await app.DATABASE.get_task_v2(run.run_id, organization_id=organization_id)
        if not task_v2:
            return None
        return await task_v2_service.build_task_v2_run_response(task_v2)
    elif run.task_run_type == RunType.workflow_run:
        return await workflow_service.get_workflow_run_response(run.run_id, organization_id=organization_id)
    raise ValueError(f"Invalid task run type: {run.task_run_type}")


async def cancel_task_v1(task_id: str, organization_id: str | None = None, api_key: str | None = None) -> None:
    task = await app.DATABASE.get_task(task_id, organization_id=organization_id)
    if not task:
        raise TaskNotFound(task_id=task_id)
    task = await app.agent.update_task(task, status=TaskStatus.canceled)
    await app.agent.execute_task_webhook(task=task, api_key=api_key)


async def cancel_task_v2(task_id: str, organization_id: str | None = None) -> None:
    task_v2 = await app.DATABASE.get_task_v2(task_id, organization_id=organization_id)
    if not task_v2:
        raise TaskNotFound(task_id=task_id)
    await task_v2_service.mark_task_v2_as_canceled(
        task_v2_id=task_id, workflow_run_id=task_v2.workflow_run_id, organization_id=organization_id
    )


async def cancel_workflow_run(
    workflow_run_id: str, organization_id: str | None = None, api_key: str | None = None
) -> None:
    workflow_run = await app.DATABASE.get_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )
    if not workflow_run:
        raise WorkflowRunNotFound(workflow_run_id=workflow_run_id)

    # get all the child workflow runs and cancel them
    child_workflow_runs = await app.DATABASE.get_workflow_runs_by_parent_workflow_run_id(
        parent_workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )
    for child_workflow_run in child_workflow_runs:
        if child_workflow_run.status not in [
            WorkflowRunStatus.running,
            WorkflowRunStatus.created,
            WorkflowRunStatus.queued,
            WorkflowRunStatus.paused,
        ]:
            continue
        await app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled(child_workflow_run.workflow_run_id)
    await app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled(workflow_run_id)
    await app.WORKFLOW_SERVICE.execute_workflow_webhook(workflow_run, api_key=api_key)


async def cancel_run(run_id: str, organization_id: str | None = None, api_key: str | None = None) -> None:
    run = await app.DATABASE.get_run(run_id, organization_id=organization_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run not found {run_id}",
        )

    if run.task_run_type in [RunType.task_v1, RunType.openai_cua, RunType.anthropic_cua, RunType.ui_tars]:
        await cancel_task_v1(run_id, organization_id=organization_id, api_key=api_key)
    elif run.task_run_type == RunType.task_v2:
        await cancel_task_v2(run_id, organization_id=organization_id)
    elif run.task_run_type == RunType.workflow_run:
        await cancel_workflow_run(run_id, organization_id=organization_id, api_key=api_key)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid run type to cancel: {run.task_run_type}",
        )


async def retry_run_webhook(
    run_id: str,
    organization_id: str | None = None,
    api_key: str | None = None,
    webhook_url: str | None = None,
) -> None:
    """Retry sending the webhook for a run."""

    run = await app.DATABASE.get_run(run_id, organization_id=organization_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run not found {run_id}",
        )

    if webhook_url:
        if not organization_id:
            raise OrganizationNotFound(organization_id="")
        await webhook_service.replay_run_webhook(
            organization_id=organization_id,
            run_id=run_id,
            target_url=webhook_url,
            api_key=api_key,
        )
        return

    if run.task_run_type in [RunType.task_v1, RunType.openai_cua, RunType.anthropic_cua, RunType.ui_tars]:
        task = await app.DATABASE.get_task(run_id, organization_id=organization_id)
        if not task:
            raise TaskNotFound(task_id=run_id)
        latest_step = await app.DATABASE.get_latest_step(run_id, organization_id=organization_id)
        if latest_step:
            await app.agent.execute_task_webhook(task=task, api_key=api_key)
    elif run.task_run_type == RunType.task_v2:
        task_v2 = await app.DATABASE.get_task_v2(run_id, organization_id=organization_id)
        if not task_v2:
            raise TaskNotFound(task_id=run_id)
        await task_v2_service.send_task_v2_webhook(task_v2)
    elif run.task_run_type == RunType.workflow_run:
        workflow_run = await app.DATABASE.get_workflow_run(
            workflow_run_id=run_id,
            organization_id=organization_id,
        )
        if not workflow_run:
            raise WorkflowRunNotFound(workflow_run_id=run_id)
        await app.WORKFLOW_SERVICE.execute_workflow_webhook(workflow_run, api_key=api_key)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid run type to retry webhook: {run.task_run_type}",
        )
