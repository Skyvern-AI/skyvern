import asyncio

import structlog
from fastapi import HTTPException, status

from skyvern.config import settings
from skyvern.exceptions import OrganizationNotFound, TaskNotFound, WorkflowRunNotFound
from skyvern.forge import app
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.runs import (
    BulkCancelRunsResponse,
    RunEngine,
    RunResponse,
    RunUsageResponse,
    RunType,
    TaskRunRequest,
    TaskRunResponse,
)
from skyvern.schemas.webhooks import RunWebhookReplayResponse
from skyvern.services import task_v1_service, task_v2_service, webhook_service, workflow_service

LOG = structlog.get_logger()


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _build_task_run_usage(run: object) -> RunUsageResponse | None:
    compute_cost = _as_float(getattr(run, "compute_cost", None))
    llm_cost = _as_float(getattr(run, "llm_cost", None))
    proxy_cost = _as_float(getattr(run, "proxy_cost", None))
    captcha_cost = _as_float(getattr(run, "captcha_cost", None))
    duration_ms = getattr(run, "duration_ms", None)

    costs = [cost for cost in [compute_cost, llm_cost, proxy_cost, captcha_cost] if cost is not None]
    if not costs and duration_ms is None:
        return None

    return RunUsageResponse(
        source="task_run",
        duration_ms=duration_ms,
        total_cost_usd=sum(costs) if costs else None,
        compute_cost_usd=compute_cost,
        llm_cost_usd=llm_cost,
        proxy_cost_usd=proxy_cost,
        captcha_cost_usd=captcha_cost,
    )


def _with_task_run_usage(response: RunResponse | None, run: object) -> RunResponse | None:
    if response is None:
        return None

    usage = _build_task_run_usage(run)
    if usage is None:
        return response

    return response.model_copy(update={"usage": usage})


async def get_run_response(run_id: str, organization_id: str | None = None) -> RunResponse | None:
    run = await app.DATABASE.tasks.get_run(run_id, organization_id=organization_id)
    if not run:
        # try to see if it's a workflow run id for task v2
        task_v2 = await app.DATABASE.observer.get_task_v2_by_workflow_run_id(run_id, organization_id=organization_id)
        if task_v2:
            run = await app.DATABASE.tasks.get_run(task_v2.observer_cruise_id, organization_id=organization_id)

    if not run:
        return None

    if (
        run.task_run_type == RunType.task_v1
        or run.task_run_type == RunType.openai_cua
        or run.task_run_type == RunType.anthropic_cua
        or run.task_run_type == RunType.ui_tars
        or run.task_run_type == RunType.yutori_navigator
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
        elif run.task_run_type == RunType.yutori_navigator:
            run_engine = RunEngine.yutori_navigator

        response = TaskRunResponse(
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
            recording_archived=task_v1_response.recording_archived,
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
            step_count=task_v1_response.step_count,
        )
        return _with_task_run_usage(response, run)
    elif run.task_run_type == RunType.task_v2:
        task_v2 = await app.DATABASE.observer.get_task_v2(run.run_id, organization_id=organization_id)
        if not task_v2:
            return None
        response = await task_v2_service.build_task_v2_run_response(task_v2)
        return _with_task_run_usage(response, run)
    elif run.task_run_type == RunType.workflow_run:
        return await workflow_service.get_workflow_run_response(run.run_id, organization_id=organization_id)
    raise ValueError(f"Invalid task run type: {run.task_run_type}")


async def cancel_task_v1(task_id: str, organization_id: str | None = None, api_key: str | None = None) -> None:
    task = await app.DATABASE.tasks.get_task(task_id, organization_id=organization_id)
    if not task:
        raise TaskNotFound(task_id=task_id)
    task = await app.agent.update_task(task, status=TaskStatus.canceled)
    await app.agent.execute_task_webhook(task=task, api_key=api_key)


async def cancel_task_v2(task_id: str, organization_id: str | None = None) -> None:
    task_v2 = await app.DATABASE.observer.get_task_v2(task_id, organization_id=organization_id)
    if not task_v2:
        raise TaskNotFound(task_id=task_id)
    await task_v2_service.mark_task_v2_as_canceled(
        task_v2_id=task_id, workflow_run_id=task_v2.workflow_run_id, organization_id=organization_id
    )


async def cancel_workflow_run(
    workflow_run_id: str, organization_id: str | None = None, api_key: str | None = None
) -> None:
    workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )
    if not workflow_run:
        raise WorkflowRunNotFound(workflow_run_id=workflow_run_id)

    # get all the child workflow runs and cancel them
    child_workflow_runs = await app.DATABASE.workflow_runs.get_workflow_runs_by_parent_workflow_run_id(
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
    run = await app.DATABASE.tasks.get_run(run_id, organization_id=organization_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run not found {run_id}",
        )

    if run.task_run_type in [
        RunType.task_v1,
        RunType.openai_cua,
        RunType.anthropic_cua,
        RunType.ui_tars,
        RunType.yutori_navigator,
    ]:
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


async def bulk_cancel_runs(
    run_ids: list[str], organization_id: str | None = None, api_key: str | None = None
) -> BulkCancelRunsResponse:
    cancelled: list[str] = []
    failed: list[str] = []

    async def _cancel_one(run_id: str) -> None:
        try:
            await cancel_run(run_id, organization_id=organization_id, api_key=api_key)
            cancelled.append(run_id)
        except Exception:
            LOG.warning("bulk_cancel_runs: failed to cancel run", run_id=run_id, exc_info=True)
            failed.append(run_id)

    await asyncio.gather(*[_cancel_one(run_id) for run_id in dict.fromkeys(run_ids)])
    return BulkCancelRunsResponse(cancelled=cancelled, failed=failed)


async def retry_run_webhook(
    run_id: str,
    organization_id: str | None = None,
    api_key: str | None = None,
    webhook_url: str | None = None,
) -> RunWebhookReplayResponse:
    """Retry sending the webhook for a run, optionally to a custom URL."""
    if not organization_id:
        raise OrganizationNotFound(organization_id="")
    return await webhook_service.replay_run_webhook(
        organization_id=organization_id,
        run_id=run_id,
        target_url=webhook_url,
        api_key=api_key,
    )
