from skyvern.forge import app
from skyvern.forge.sdk.schemas.task_runs import RunEngine, TaskRun, TaskRunResponse, TaskRunType


async def get_task_run(run_id: str, organization_id: str | None = None) -> TaskRun | None:
    return await app.DATABASE.get_task_run(run_id, organization_id=organization_id)


async def get_task_run_response(run_id: str, organization_id: str | None = None) -> TaskRunResponse | None:
    task_run = await get_task_run(run_id, organization_id=organization_id)
    if not task_run:
        return None

    if task_run.task_run_type == TaskRunType.task_v1:
        # fetch task v1 from db and transform to task run response
        task_v1 = await app.DATABASE.get_task(task_run.task_v1_id, organization_id=organization_id)
        if not task_v1:
            return None
        return TaskRunResponse(
            run_id=task_run.run_id,
            engine=RunEngine.skyvern_v1,
            status=task_v1.status,
            goal=task_v1.navigation_goal,
            url=task_v1.url,
            output=task_v1.extracted_information,
            failure_reason=task_v1.failure_reason,
            webhook_url=task_v1.webhook_callback_url,
            totp_identifier=task_v1.totp_identifier,
            totp_url=task_v1.totp_verification_url,
            proxy_location=task_v1.proxy_location,
            created_at=task_v1.created_at,
            modified_at=task_v1.modified_at,
        )
    elif task_run.task_run_type == TaskRunType.task_v2:
        task_v2 = await app.DATABASE.get_task_v2(task_run.task_v2_id, organization_id=organization_id)
        if not task_v2:
            return None
        return TaskRunResponse(
            run_id=task_run.run_id,
            engine=RunEngine.skyvern_v2,
            status=task_v2.status,
            goal=task_v2.prompt,
            url=task_v2.url,
            output=task_v2.output,
            failure_reason=task_v2.failure_reason,
            webhook_url=task_v2.webhook_callback_url,
            totp_identifier=task_v2.totp_identifier,
            totp_url=task_v2.totp_verification_url,
            proxy_location=task_v2.proxy_location,
            created_at=task_v2.created_at,
            modified_at=task_v2.modified_at,
        )
    raise ValueError(f"Invalid task run type: {task_run.task_run_type}")
