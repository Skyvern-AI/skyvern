from typing import Annotated, Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel

from skyvern import analytics
from skyvern.exceptions import StepNotFound
from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.security import generate_skyvern_signature
from skyvern.forge.sdk.models import Organization, Step
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, Task, TaskRequest, TaskResponse, TaskStatus
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.models.workflow import (
    RunWorkflowResponse,
    WorkflowRequestBody,
    WorkflowRunStatusResponse,
)

base_router = APIRouter()

LOG = structlog.get_logger()


@base_router.post("/webhook", tags=["server"])
async def webhook(
    request: Request,
    x_skyvern_signature: Annotated[str | None, Header()] = None,
    x_skyvern_timestamp: Annotated[str | None, Header()] = None,
) -> Response:
    analytics.capture("skyvern-oss-agent-webhook-received")
    payload = await request.body()

    if not x_skyvern_signature or not x_skyvern_timestamp:
        LOG.error(
            "Webhook signature or timestamp missing",
            x_skyvern_signature=x_skyvern_signature,
            x_skyvern_timestamp=x_skyvern_timestamp,
            payload=payload,
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing webhook signature or timestamp")

    generated_signature = generate_skyvern_signature(
        payload.decode("utf-8"),
        SettingsManager.get_settings().SKYVERN_API_KEY,
    )

    LOG.info(
        "Webhook received",
        x_skyvern_signature=x_skyvern_signature,
        x_skyvern_timestamp=x_skyvern_timestamp,
        payload=payload,
        generated_signature=generated_signature,
        valid_signature=x_skyvern_signature == generated_signature,
    )
    return Response(content="webhook validation", status_code=200)


@base_router.get("/heartbeat", tags=["server"])
async def check_server_status() -> Response:
    """
    Check if the server is running.
    """
    return Response(content="Server is running.", status_code=200)


@base_router.post("/tasks", tags=["agent"], response_model=CreateTaskResponse)
async def create_agent_task(
    background_tasks: BackgroundTasks,
    request: Request,
    task: TaskRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
    x_max_steps_override: Annotated[int | None, Header()] = None,
) -> CreateTaskResponse:
    analytics.capture("skyvern-oss-agent-task-create", data={"url": task.url})
    agent = request["agent"]

    created_task = await agent.create_task(task, current_org.organization_id)
    if x_max_steps_override:
        LOG.info("Overriding max steps per run", max_steps_override=x_max_steps_override)
    await app.ASYNC_EXECUTOR.execute_task(
        background_tasks=background_tasks,
        task=created_task,
        organization=current_org,
        max_steps_override=x_max_steps_override,
        api_key=x_api_key,
    )
    return CreateTaskResponse(task_id=created_task.task_id)


@base_router.post(
    "/tasks/{task_id}/steps/{step_id}",
    tags=["agent"],
    response_model=Step,
    summary="Executes a specific step",
)
@base_router.post(
    "/tasks/{task_id}/steps/",
    tags=["agent"],
    response_model=Step,
    summary="Executes the next step",
)
async def execute_agent_task_step(
    request: Request,
    task_id: str,
    step_id: str | None = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    analytics.capture("skyvern-oss-agent-task-step-execute")
    agent = request["agent"]
    task = await app.DATABASE.get_task(task_id, organization_id=current_org.organization_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No task found with id {task_id}",
        )
    # An empty step request means that the agent should execute the next step for the task.
    if not step_id:
        step = await app.DATABASE.get_latest_step(task_id=task_id, organization_id=current_org.organization_id)
        if not step:
            raise StepNotFound(current_org.organization_id, task_id)
        LOG.info(
            "Executing latest step since no step_id was provided",
            task_id=task_id,
            step_id=step.step_id,
            step_order=step.order,
            step_retry=step.retry_index,
        )
        if not step:
            LOG.error(
                "No steps found for task",
                task_id=task_id,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No steps found for task {task_id}",
            )
    else:
        step = await app.DATABASE.get_step(task_id, step_id, organization_id=current_org.organization_id)
        if not step:
            raise StepNotFound(current_org.organization_id, task_id, step_id)
        LOG.info(
            "Executing step",
            task_id=task_id,
            step_id=step.step_id,
            step_order=step.order,
            step_retry=step.retry_index,
        )
        if not step:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No step found with id {step_id}",
            )
    step, _, _ = await agent.execute_step(current_org, task, step)
    return Response(
        content=step.model_dump_json() if step else "",
        status_code=200,
        media_type="application/json",
    )


@base_router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    request: Request,
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> TaskResponse:
    analytics.capture("skyvern-oss-agent-task-get")
    request["agent"]
    task_obj = await app.DATABASE.get_task(task_id, organization_id=current_org.organization_id)
    if not task_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task not found {task_id}",
        )

    # get latest step
    latest_step = await app.DATABASE.get_latest_step(task_id, organization_id=current_org.organization_id)
    if not latest_step:
        return task_obj.to_task_response()

    screenshot_url = None
    # todo (kerem): only access artifacts through the artifact manager instead of db
    screenshot_artifact = await app.DATABASE.get_latest_artifact(
        task_id=task_obj.task_id,
        step_id=latest_step.step_id,
        artifact_types=[ArtifactType.SCREENSHOT_ACTION, ArtifactType.SCREENSHOT_FINAL],
        organization_id=current_org.organization_id,
    )
    if screenshot_artifact:
        screenshot_url = await app.ARTIFACT_MANAGER.get_share_link(screenshot_artifact)

    recording_artifact = await app.DATABASE.get_latest_artifact(
        task_id=task_obj.task_id,
        artifact_types=[ArtifactType.RECORDING],
        organization_id=current_org.organization_id,
    )
    recording_url = None
    if recording_artifact:
        recording_url = await app.ARTIFACT_MANAGER.get_share_link(recording_artifact)

    failure_reason = None
    if task_obj.status == TaskStatus.failed and (latest_step.output or task_obj.failure_reason):
        failure_reason = ""
        if task_obj.failure_reason:
            failure_reason += f"Reasoning: {task_obj.failure_reason or ''}"
            failure_reason += "\n"
        if latest_step.output and latest_step.output.action_results:
            failure_reason += "Exceptions: "
            failure_reason += str(
                [f"[{ar.exception_type}]: {ar.exception_message}" for ar in latest_step.output.action_results]
            )

    return task_obj.to_task_response(
        screenshot_url=screenshot_url,
        recording_url=recording_url,
        failure_reason=failure_reason,
    )


@base_router.post(
    "/tasks/{task_id}/retry_webhook",
    tags=["agent"],
    response_model=TaskResponse,
)
async def retry_webhook(
    request: Request,
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
) -> TaskResponse:
    analytics.capture("skyvern-oss-agent-task-retry-webhook")
    agent = request["agent"]
    task_obj = await app.DATABASE.get_task(task_id, organization_id=current_org.organization_id)
    if not task_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task not found {task_id}",
        )

    # get latest step
    latest_step = await app.DATABASE.get_latest_step(task_id, organization_id=current_org.organization_id)
    if not latest_step:
        return task_obj.to_task_response()

    # retry the webhook
    await agent.execute_task_webhook(task=task_obj, last_step=latest_step, api_key=x_api_key)

    return task_obj.to_task_response()


@base_router.get("/internal/tasks/{task_id}", response_model=list[Task])
async def get_task_internal(
    request: Request,
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    """
    Get all tasks.
    :param request:
    :param page: Starting page, defaults to 1
    :param page_size:
    :return: List of tasks with pagination without steps populated. Steps can be populated by calling the
        get_agent_task endpoint.
    """
    analytics.capture("skyvern-oss-agent-task-get-internal")
    task = await app.DATABASE.get_task(task_id, organization_id=current_org.organization_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task not found {task_id}",
        )
    return ORJSONResponse(task.model_dump())


@base_router.get("/tasks", tags=["agent"], response_model=list[Task])
async def get_agent_tasks(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    """
    Get all tasks.
    :param request:
    :param page: Starting page, defaults to 1
    :param page_size: Page size, defaults to 10
    :return: List of tasks with pagination without steps populated. Steps can be populated by calling the
        get_agent_task endpoint.
    """
    analytics.capture("skyvern-oss-agent-tasks-get")
    request["agent"]
    tasks = await app.DATABASE.get_tasks(page, page_size, organization_id=current_org.organization_id)
    return ORJSONResponse([task.to_task_response().model_dump() for task in tasks])


@base_router.get("/internal/tasks", tags=["agent"], response_model=list[Task])
async def get_agent_tasks_internal(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    """
    Get all tasks.
    :param request:
    :param page: Starting page, defaults to 1
    :param page_size: Page size, defaults to 10
    :return: List of tasks with pagination without steps populated. Steps can be populated by calling the
        get_agent_task endpoint.
    """
    analytics.capture("skyvern-oss-agent-tasks-get-internal")
    request["agent"]
    tasks = await app.DATABASE.get_tasks(page, page_size, organization_id=current_org.organization_id)
    return ORJSONResponse([task.model_dump() for task in tasks])


@base_router.get("/tasks/{task_id}/steps", tags=["agent"], response_model=list[Step])
async def get_agent_task_steps(
    request: Request,
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    """
    Get all steps for a task.
    :param request:
    :param task_id:
    :return: List of steps for a task with pagination.
    """
    analytics.capture("skyvern-oss-agent-task-steps-get")
    request["agent"]
    steps = await app.DATABASE.get_task_steps(task_id, organization_id=current_org.organization_id)
    return ORJSONResponse([step.model_dump() for step in steps])


@base_router.get("/tasks/{task_id}/steps/{step_id}/artifacts", tags=["agent"], response_model=list[Artifact])
async def get_agent_task_step_artifacts(
    request: Request,
    task_id: str,
    step_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    """
    Get all artifacts for a list of steps.
    :param request:
    :param task_id:
    :param step_id:
    :return: List of artifacts for a list of steps.
    """
    analytics.capture("skyvern-oss-agent-task-step-artifacts-get")
    request["agent"]
    artifacts = await app.DATABASE.get_artifacts_for_task_step(
        task_id,
        step_id,
        organization_id=current_org.organization_id,
    )
    return ORJSONResponse([artifact.model_dump() for artifact in artifacts])


class ActionResultTmp(BaseModel):
    action: dict[str, Any]
    data: dict[str, Any] | list | str | None = None
    exception_message: str | None = None
    success: bool = True


@base_router.get("/tasks/{task_id}/actions", response_model=list[ActionResultTmp])
async def get_task_actions(
    request: Request,
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[ActionResultTmp]:
    analytics.capture("skyvern-oss-agent-task-actions-get")
    request["agent"]
    steps = await app.DATABASE.get_task_step_models(task_id, organization_id=current_org.organization_id)
    results: list[ActionResultTmp] = []
    for step_s in steps:
        if not step_s.output or "action_results" not in step_s.output:
            continue
        for action_result in step_s.output["action_results"]:
            results.append(ActionResultTmp.model_validate(action_result))
    return results


@base_router.post("/workflows/{workflow_id}/run", response_model=RunWorkflowResponse)
async def execute_workflow(
    background_tasks: BackgroundTasks,
    request: Request,
    workflow_id: str,
    workflow_request: WorkflowRequestBody,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
    x_max_steps_override: Annotated[int | None, Header()] = None,
) -> RunWorkflowResponse:
    analytics.capture("skyvern-oss-agent-workflow-execute")
    LOG.info(
        f"Running workflow {workflow_id}",
        workflow_id=workflow_id,
    )
    context = skyvern_context.ensure_context()
    request_id = context.request_id
    workflow_run = await app.WORKFLOW_SERVICE.setup_workflow_run(
        request_id=request_id,
        workflow_request=workflow_request,
        workflow_id=workflow_id,
        organization_id=current_org.organization_id,
        max_steps_override=x_max_steps_override,
    )
    if x_max_steps_override:
        LOG.info("Overriding max steps per run", max_steps_override=x_max_steps_override)
    await app.ASYNC_EXECUTOR.execute_workflow(
        background_tasks=background_tasks,
        organization=current_org,
        workflow_id=workflow_id,
        workflow_run_id=workflow_run.workflow_run_id,
        max_steps_override=x_max_steps_override,
        api_key=x_api_key,
    )
    return RunWorkflowResponse(
        workflow_id=workflow_id,
        workflow_run_id=workflow_run.workflow_run_id,
    )


@base_router.get("/workflows/{workflow_id}/runs/{workflow_run_id}", response_model=WorkflowRunStatusResponse)
async def get_workflow_run(
    request: Request,
    workflow_id: str,
    workflow_run_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowRunStatusResponse:
    analytics.capture("skyvern-oss-agent-workflow-run-get")
    request["agent"]
    return await app.WORKFLOW_SERVICE.build_workflow_run_status_response(
        workflow_id=workflow_id, workflow_run_id=workflow_run_id, organization_id=current_org.organization_id
    )
