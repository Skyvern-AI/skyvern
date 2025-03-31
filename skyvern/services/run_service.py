from skyvern.forge import app
from skyvern.schemas.runs import RunEngine, RunResponse, RunType, TaskRunRequest, TaskRunResponse


async def get_run_response(run_id: str, organization_id: str | None = None) -> RunResponse | None:
    run = await app.DATABASE.get_run(run_id, organization_id=organization_id)
    if not run:
        return None

    if run.task_run_type == RunType.task_v1:
        # fetch task v1 from db and transform to task run response
        task_v1 = await app.DATABASE.get_task(run.run_id, organization_id=organization_id)
        if not task_v1:
            return None
        return TaskRunResponse(
            run_id=run.run_id,
            run_type=run.task_run_type,
            status=str(task_v1.status),
            output=task_v1.extracted_information,
            failure_reason=task_v1.failure_reason,
            created_at=task_v1.created_at,
            modified_at=task_v1.modified_at,
            run_request=TaskRunRequest(
                engine=RunEngine.skyvern_v1,
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
        return TaskRunResponse(
            run_id=run.run_id,
            run_type=run.task_run_type,
            status=task_v2.status,
            output=task_v2.output,
            # TODO: add failure reason
            # failure_reason=task_v2.failure_reason,
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
