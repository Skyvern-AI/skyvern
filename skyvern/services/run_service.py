from fastapi import HTTPException, status

from skyvern.exceptions import TaskNotFound, WorkflowRunNotFound
from skyvern.forge import app
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.runs import RunEngine, RunResponse, RunType, TaskRunRequest, TaskRunResponse
from skyvern.services import task_v2_service


async def get_run_response(run_id: str, organization_id: str | None = None) -> RunResponse | None:
    run = await app.DATABASE.get_run(run_id, organization_id=organization_id)
    if not run:
        return None

    if (
        run.task_run_type == RunType.task_v1
        or run.task_run_type == RunType.openai_cua
        or run.task_run_type == RunType.anthropic_cua
    ):
        # fetch task v1 from db and transform to task run response
        task_v1 = await app.DATABASE.get_task(run.run_id, organization_id=organization_id)
        if not task_v1:
            return None
        run_engine = RunEngine.skyvern_v1
        if run.task_run_type == RunType.openai_cua:
            run_engine = RunEngine.openai_cua
        elif run.task_run_type == RunType.anthropic_cua:
            run_engine = RunEngine.anthropic_cua
        return TaskRunResponse(
            run_id=run.run_id,
            run_type=run.task_run_type,
            status=str(task_v1.status),
            output=task_v1.extracted_information,
            failure_reason=task_v1.failure_reason,
            created_at=task_v1.created_at,
            modified_at=task_v1.modified_at,
            run_request=TaskRunRequest(
                engine=run_engine,
                prompt=task_v1.navigation_goal,
                url=task_v1.url,
                webhook_url=task_v1.webhook_callback_url,
                totp_identifier=task_v1.totp_identifier,
                totp_url=task_v1.totp_verification_url,
                proxy_location=task_v1.proxy_location,
                max_steps=task_v1.max_steps_per_run,
                data_extraction_schema=task_v1.extracted_information_schema,
                error_code_mapping=task_v1.error_code_mapping,
            ),
        )
    elif run.task_run_type == RunType.task_v2:
        task_v2 = await app.DATABASE.get_task_v2(run.run_id, organization_id=organization_id)
        if not task_v2:
            return None
        workflow_run = None
        if task_v2.workflow_run_id:
            workflow_run = await app.DATABASE.get_workflow_run(task_v2.workflow_run_id, organization_id=organization_id)
        return TaskRunResponse(
            run_id=run.run_id,
            run_type=run.task_run_type,
            status=task_v2.status,
            output=task_v2.output,
            failure_reason=workflow_run.failure_reason if workflow_run else None,
            created_at=task_v2.created_at,
            modified_at=task_v2.modified_at,
            run_request=TaskRunRequest(
                engine=RunEngine.skyvern_v2,
                prompt=task_v2.prompt,
                url=task_v2.url,
                webhook_url=task_v2.webhook_callback_url,
                totp_identifier=task_v2.totp_identifier,
                totp_url=task_v2.totp_verification_url,
                proxy_location=task_v2.proxy_location,
                data_extraction_schema=task_v2.extracted_information_schema,
                error_code_mapping=task_v2.error_code_mapping,
            ),
        )
    elif run.task_run_type == RunType.workflow_run:
        raise NotImplementedError("Workflow run response not implemented")
        # return WorkflowRunResponse(
        #     run_id=run.run_id,
        #     run_type=run.task_run_type,
        #     status=run.status,
        #     output=run.output,
        #     parameters=None,
        #     created_at=run.created_at,
        #     modified_at=run.modified_at,
        # )
    raise ValueError(f"Invalid task run type: {run.task_run_type}")


async def cancel_task_v1(task_id: str, organization_id: str | None = None, api_key: str | None = None) -> None:
    task = await app.DATABASE.get_task(task_id, organization_id=organization_id)
    if not task:
        raise TaskNotFound(task_id=task_id)
    task = await app.agent.update_task(task, status=TaskStatus.canceled)
    latest_step = await app.DATABASE.get_latest_step(task_id, organization_id=organization_id)
    await app.agent.execute_task_webhook(task=task, last_step=latest_step, api_key=api_key)


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

    if run.task_run_type in [RunType.task_v1, RunType.openai_cua, RunType.anthropic_cua]:
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
