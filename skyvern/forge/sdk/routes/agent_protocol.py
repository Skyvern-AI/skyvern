from typing import Annotated, Any

import structlog
import yaml
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel

from skyvern import analytics
from skyvern.exceptions import StepNotFound
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.permissions.permission_checker_factory import PermissionCheckerFactory
from skyvern.forge.sdk.core.security import generate_skyvern_signature
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.models import Organization, Step
from skyvern.forge.sdk.schemas.organizations import OrganizationUpdate
from skyvern.forge.sdk.schemas.task_generations import GenerateTaskRequest, TaskGeneration, TaskGenerationBase
from skyvern.forge.sdk.schemas.tasks import (
    CreateTaskResponse,
    ProxyLocation,
    Task,
    TaskRequest,
    TaskResponse,
    TaskStatus,
)
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.models.workflow import (
    RunWorkflowResponse,
    Workflow,
    WorkflowRequestBody,
    WorkflowRunStatusResponse,
)
from skyvern.forge.sdk.workflow.models.yaml import WorkflowCreateYAMLRequest

base_router = APIRouter()

LOG = structlog.get_logger()


@base_router.post("/webhook", tags=["server"])
@base_router.post("/webhook/", tags=["server"], include_in_schema=False)
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing webhook signature or timestamp",
        )

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
@base_router.get("/heartbeat/", tags=["server"], include_in_schema=False)
async def check_server_status() -> Response:
    """
    Check if the server is running.
    """
    return Response(content="Server is running.", status_code=200)


@base_router.post("/tasks", tags=["agent"], response_model=CreateTaskResponse)
@base_router.post(
    "/tasks/",
    tags=["agent"],
    response_model=CreateTaskResponse,
    include_in_schema=False,
)
async def create_agent_task(
    background_tasks: BackgroundTasks,
    task: TaskRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
    x_max_steps_override: Annotated[int | None, Header()] = None,
) -> CreateTaskResponse:
    analytics.capture("skyvern-oss-agent-task-create", data={"url": task.url})
    await PermissionCheckerFactory.get_instance().check(current_org)

    if current_org and current_org.organization_name == "CoverageCat":
        task.proxy_location = ProxyLocation.RESIDENTIAL

    created_task = await app.agent.create_task(task, current_org.organization_id)
    if x_max_steps_override:
        LOG.info("Overriding max steps per run", max_steps_override=x_max_steps_override)
    await AsyncExecutorFactory.get_executor().execute_task(
        background_tasks=background_tasks,
        task_id=created_task.task_id,
        organization_id=current_org.organization_id,
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
    "/tasks/{task_id}/steps/{step_id}/",
    tags=["agent"],
    response_model=Step,
    summary="Executes a specific step",
    include_in_schema=False,
)
@base_router.post(
    "/tasks/{task_id}/steps",
    tags=["agent"],
    response_model=Step,
    summary="Executes the next step",
)
@base_router.post(
    "/tasks/{task_id}/steps/",
    tags=["agent"],
    response_model=Step,
    summary="Executes the next step",
    include_in_schema=False,
)
async def execute_agent_task_step(
    task_id: str,
    step_id: str | None = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    analytics.capture("skyvern-oss-agent-task-step-execute")
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
    step, _, _ = await app.agent.execute_step(current_org, task, step)
    return Response(
        content=step.model_dump_json(exclude_none=True) if step else "",
        status_code=200,
        media_type="application/json",
    )


@base_router.get("/tasks/{task_id}", response_model=TaskResponse)
@base_router.get("/tasks/{task_id}/", response_model=TaskResponse, include_in_schema=False)
async def get_task(
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> TaskResponse:
    analytics.capture("skyvern-oss-agent-task-get")
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

    # get the artifact of the last  screenshot and get the screenshot_url
    latest_action_screenshot_artifacts = await app.DATABASE.get_latest_n_artifacts(
        task_id=task_obj.task_id,
        organization_id=task_obj.organization_id,
        artifact_types=[ArtifactType.SCREENSHOT_ACTION],
        n=SettingsManager.get_settings().TASK_RESPONSE_ACTION_SCREENSHOT_COUNT,
    )
    latest_action_screenshot_urls: list[str] | None = None
    if latest_action_screenshot_artifacts:
        latest_action_screenshot_urls = await app.ARTIFACT_MANAGER.get_share_links(latest_action_screenshot_artifacts)
    elif task_obj.status in [TaskStatus.terminated, TaskStatus.completed]:
        LOG.error(
            "Failed to get latest action screenshots in task response",
            task_id=task_id,
            task_status=task_obj.status,
        )

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
        action_screenshot_urls=latest_action_screenshot_urls,
        screenshot_url=screenshot_url,
        recording_url=recording_url,
        failure_reason=failure_reason,
    )


@base_router.post("/tasks/{task_id}/cancel")
@base_router.post("/tasks/{task_id}/cancel/", include_in_schema=False)
async def cancel_task(
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> None:
    analytics.capture("skyvern-oss-agent-task-get")
    task_obj = await app.DATABASE.get_task(task_id, organization_id=current_org.organization_id)
    if not task_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task not found {task_id}",
        )
    await app.agent.update_task(task_obj, status=TaskStatus.canceled)


@base_router.post(
    "/tasks/{task_id}/retry_webhook",
    tags=["agent"],
    response_model=TaskResponse,
)
@base_router.post(
    "/tasks/{task_id}/retry_webhook/",
    tags=["agent"],
    response_model=TaskResponse,
    include_in_schema=False,
)
async def retry_webhook(
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
) -> TaskResponse:
    analytics.capture("skyvern-oss-agent-task-retry-webhook")
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
    await app.agent.execute_task_webhook(task=task_obj, last_step=latest_step, api_key=x_api_key)

    return task_obj.to_task_response()


@base_router.get("/internal/tasks/{task_id}", response_model=list[Task])
@base_router.get("/internal/tasks/{task_id}/", response_model=list[Task], include_in_schema=False)
async def get_task_internal(
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    """
    Get all tasks.
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
@base_router.get("/tasks/", tags=["agent"], response_model=list[Task], include_in_schema=False)
async def get_agent_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    task_status: Annotated[list[TaskStatus] | None, Query()] = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    """
    Get all tasks.
    :param page: Starting page, defaults to 1
    :param page_size: Page size, defaults to 10
    :return: List of tasks with pagination without steps populated. Steps can be populated by calling the
        get_agent_task endpoint.
    """
    analytics.capture("skyvern-oss-agent-tasks-get")
    tasks = await app.DATABASE.get_tasks(
        page,
        page_size,
        task_status=task_status,
        organization_id=current_org.organization_id,
    )
    return ORJSONResponse([task.to_task_response().model_dump() for task in tasks])


@base_router.get("/internal/tasks", tags=["agent"], response_model=list[Task])
@base_router.get(
    "/internal/tasks/",
    tags=["agent"],
    response_model=list[Task],
    include_in_schema=False,
)
async def get_agent_tasks_internal(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    """
    Get all tasks.
    :param page: Starting page, defaults to 1
    :param page_size: Page size, defaults to 10
    :return: List of tasks with pagination without steps populated. Steps can be populated by calling the
        get_agent_task endpoint.
    """
    analytics.capture("skyvern-oss-agent-tasks-get-internal")
    tasks = await app.DATABASE.get_tasks(page, page_size, organization_id=current_org.organization_id)
    return ORJSONResponse([task.model_dump() for task in tasks])


@base_router.get("/tasks/{task_id}/steps", tags=["agent"], response_model=list[Step])
@base_router.get(
    "/tasks/{task_id}/steps/",
    tags=["agent"],
    response_model=list[Step],
    include_in_schema=False,
)
async def get_agent_task_steps(
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    """
    Get all steps for a task.
    :param task_id:
    :return: List of steps for a task with pagination.
    """
    analytics.capture("skyvern-oss-agent-task-steps-get")
    steps = await app.DATABASE.get_task_steps(task_id, organization_id=current_org.organization_id)
    return ORJSONResponse([step.model_dump(exclude_none=True) for step in steps])


@base_router.get(
    "/tasks/{task_id}/steps/{step_id}/artifacts",
    tags=["agent"],
    response_model=list[Artifact],
)
@base_router.get(
    "/tasks/{task_id}/steps/{step_id}/artifacts/",
    tags=["agent"],
    response_model=list[Artifact],
    include_in_schema=False,
)
async def get_agent_task_step_artifacts(
    task_id: str,
    step_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    """
    Get all artifacts for a list of steps.
    :param task_id:
    :param step_id:
    :return: List of artifacts for a list of steps.
    """
    analytics.capture("skyvern-oss-agent-task-step-artifacts-get")
    artifacts = await app.DATABASE.get_artifacts_for_task_step(
        task_id,
        step_id,
        organization_id=current_org.organization_id,
    )
    if SettingsManager.get_settings().ENV != "local" or SettingsManager.get_settings().GENERATE_PRESIGNED_URLS:
        signed_urls = await app.ARTIFACT_MANAGER.get_share_links(artifacts)
        if signed_urls:
            for i, artifact in enumerate(artifacts):
                artifact.signed_url = signed_urls[i]
        else:
            LOG.warning(
                "Failed to get signed urls for artifacts",
                task_id=task_id,
                step_id=step_id,
            )
    return ORJSONResponse([artifact.model_dump() for artifact in artifacts])


class ActionResultTmp(BaseModel):
    action: dict[str, Any]
    data: dict[str, Any] | list | str | None = None
    exception_message: str | None = None
    success: bool = True


@base_router.get("/tasks/{task_id}/actions", response_model=list[ActionResultTmp])
@base_router.get(
    "/tasks/{task_id}/actions/",
    response_model=list[ActionResultTmp],
    include_in_schema=False,
)
async def get_task_actions(
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[ActionResultTmp]:
    analytics.capture("skyvern-oss-agent-task-actions-get")
    steps = await app.DATABASE.get_task_step_models(task_id, organization_id=current_org.organization_id)
    results: list[ActionResultTmp] = []
    for step_s in steps:
        if not step_s.output or "action_results" not in step_s.output:
            continue
        for action_result in step_s.output["action_results"]:
            results.append(ActionResultTmp.model_validate(action_result))
    return results


@base_router.post("/workflows/{workflow_id}/run", response_model=RunWorkflowResponse)
@base_router.post(
    "/workflows/{workflow_id}/run/",
    response_model=RunWorkflowResponse,
    include_in_schema=False,
)
async def execute_workflow(
    background_tasks: BackgroundTasks,
    workflow_id: str,  # this is the workflow_permanent_id
    workflow_request: WorkflowRequestBody,
    version: int | None = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
    x_max_steps_override: Annotated[int | None, Header()] = None,
) -> RunWorkflowResponse:
    analytics.capture("skyvern-oss-agent-workflow-execute")
    context = skyvern_context.ensure_context()
    request_id = context.request_id
    workflow_run = await app.WORKFLOW_SERVICE.setup_workflow_run(
        request_id=request_id,
        workflow_request=workflow_request,
        workflow_permanent_id=workflow_id,
        organization_id=current_org.organization_id,
        version=version,
        max_steps_override=x_max_steps_override,
    )
    if x_max_steps_override:
        LOG.info("Overriding max steps per run", max_steps_override=x_max_steps_override)
    await AsyncExecutorFactory.get_executor().execute_workflow(
        background_tasks=background_tasks,
        organization_id=current_org.organization_id,
        workflow_id=workflow_run.workflow_id,
        workflow_run_id=workflow_run.workflow_run_id,
        max_steps_override=x_max_steps_override,
        api_key=x_api_key,
    )
    return RunWorkflowResponse(
        workflow_id=workflow_id,
        workflow_run_id=workflow_run.workflow_run_id,
    )


@base_router.get(
    "/workflows/{workflow_id}/runs/{workflow_run_id}",
    response_model=WorkflowRunStatusResponse,
)
@base_router.get(
    "/workflows/{workflow_id}/runs/{workflow_run_id}/",
    response_model=WorkflowRunStatusResponse,
    include_in_schema=False,
)
async def get_workflow_run(
    workflow_id: str,
    workflow_run_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowRunStatusResponse:
    analytics.capture("skyvern-oss-agent-workflow-run-get")
    return await app.WORKFLOW_SERVICE.build_workflow_run_status_response(
        workflow_permanent_id=workflow_id,
        workflow_run_id=workflow_run_id,
        organization_id=current_org.organization_id,
    )


@base_router.post(
    "/workflows",
    openapi_extra={
        "requestBody": {
            "content": {"application/x-yaml": {"schema": WorkflowCreateYAMLRequest.model_json_schema()}},
            "required": True,
        },
    },
    response_model=Workflow,
)
@base_router.post(
    "/workflows/",
    openapi_extra={
        "requestBody": {
            "content": {"application/x-yaml": {"schema": WorkflowCreateYAMLRequest.model_json_schema()}},
            "required": True,
        },
    },
    response_model=Workflow,
    include_in_schema=False,
)
async def create_workflow(
    request: Request,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Workflow:
    analytics.capture("skyvern-oss-agent-workflow-create")
    raw_yaml = await request.body()
    try:
        workflow_yaml = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        raise HTTPException(status_code=422, detail="Invalid YAML")

    workflow_create_request = WorkflowCreateYAMLRequest.model_validate(workflow_yaml)
    return await app.WORKFLOW_SERVICE.create_workflow_from_request(
        organization_id=current_org.organization_id, request=workflow_create_request
    )


@base_router.put(
    "/workflows/{workflow_permanent_id}",
    openapi_extra={
        "requestBody": {
            "content": {"application/x-yaml": {"schema": WorkflowCreateYAMLRequest.model_json_schema()}},
            "required": True,
        },
    },
    response_model=Workflow,
)
@base_router.put(
    "/workflows/{workflow_permanent_id}/",
    openapi_extra={
        "requestBody": {
            "content": {"application/x-yaml": {"schema": WorkflowCreateYAMLRequest.model_json_schema()}},
            "required": True,
        },
    },
    response_model=Workflow,
    include_in_schema=False,
)
async def update_workflow(
    workflow_permanent_id: str,
    request: Request,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Workflow:
    analytics.capture("skyvern-oss-agent-workflow-update")
    # validate the workflow
    raw_yaml = await request.body()
    try:
        workflow_yaml = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        raise HTTPException(status_code=422, detail="Invalid YAML")

    workflow_create_request = WorkflowCreateYAMLRequest.model_validate(workflow_yaml)
    return await app.WORKFLOW_SERVICE.create_workflow_from_request(
        organization_id=current_org.organization_id,
        request=workflow_create_request,
        workflow_permanent_id=workflow_permanent_id,
    )


@base_router.delete("/workflows/{workflow_permanent_id}")
@base_router.delete("/workflows/{workflow_permanent_id}/", include_in_schema=False)
async def delete_workflow(
    workflow_permanent_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> None:
    analytics.capture("skyvern-oss-agent-workflow-delete")
    await app.WORKFLOW_SERVICE.delete_workflow_by_permanent_id(workflow_permanent_id, current_org.organization_id)


@base_router.get("/workflows", response_model=list[Workflow])
@base_router.get("/workflows/", response_model=list[Workflow])
async def get_workflows(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    only_saved_tasks: bool = Query(False),
    only_workflows: bool = Query(False),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[Workflow]:
    """
    Get all workflows with the latest version for the organization.
    """
    analytics.capture("skyvern-oss-agent-workflows-get")

    if only_saved_tasks and only_workflows:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="only_saved_tasks and only_workflows cannot be used together",
        )

    return await app.WORKFLOW_SERVICE.get_workflows_by_organization_id(
        organization_id=current_org.organization_id,
        page=page,
        page_size=page_size,
        only_saved_tasks=only_saved_tasks,
        only_workflows=only_workflows,
    )


@base_router.get("/workflows/{workflow_permanent_id}", response_model=Workflow)
@base_router.get("/workflows/{workflow_permanent_id}/", response_model=Workflow)
async def get_workflow(
    workflow_permanent_id: str,
    version: int | None = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Workflow:
    analytics.capture("skyvern-oss-agent-workflows-get")
    return await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=current_org.organization_id,
        version=version,
    )


@base_router.post("/generate/task", include_in_schema=False)
@base_router.post("/generate/task/")
async def generate_task(
    data: GenerateTaskRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> TaskGeneration:
    llm_prompt = prompt_engine.load_prompt("generate-task", user_prompt=data.prompt)
    try:
        llm_response = await app.LLM_API_HANDLER(prompt=llm_prompt)
        parsed_task_generation_obj = TaskGenerationBase.model_validate(llm_response)

        # generate a TaskGenerationModel
        task_generation = await app.DATABASE.create_task_generation(
            organization_id=current_org.organization_id,
            user_prompt=data.prompt,
            url=parsed_task_generation_obj.url,
            navigation_goal=parsed_task_generation_obj.navigation_goal,
            navigation_payload=parsed_task_generation_obj.navigation_payload,
            data_extraction_goal=parsed_task_generation_obj.data_extraction_goal,
            extracted_information_schema=parsed_task_generation_obj.extracted_information_schema,
            llm=SettingsManager.get_settings().LLM_KEY,
            llm_prompt=llm_prompt,
            llm_response=str(llm_response),
        )
        return task_generation
    except LLMProviderError:
        LOG.error("Failed to generate task", exc_info=True)
        raise HTTPException(status_code=400, detail="Failed to generate task. Please try again later.")


@base_router.put("/organizations/", include_in_schema=False)
@base_router.put("/organizations")
async def update_organization(
    org_update: OrganizationUpdate,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Organization:
    return await app.DATABASE.update_organization(
        current_org.organization_id,
        organization_name=org_update.organization_name,
        webhook_callback_url=org_update.webhook_callback_url,
        max_steps_per_run=org_update.max_steps_per_run,
        max_retries_per_step=org_update.max_retries_per_step,
    )
