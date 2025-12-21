import asyncio
from enum import Enum
from typing import Annotated, Any

import structlog
import yaml
from fastapi import (
    BackgroundTasks,
    Body,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi import status as http_status
from fastapi.responses import ORJSONResponse
from pydantic import ValidationError

from skyvern import analytics
from skyvern._version import __version__
from skyvern.config import settings
from skyvern.exceptions import (
    CannotUpdateWorkflowDueToCodeCache,
    MissingBrowserAddressError,
    SkyvernHTTPException,
)
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.curl_converter import curl_to_http_request_block_params
from skyvern.forge.sdk.core.permissions.permission_checker_factory import PermissionCheckerFactory
from skyvern.forge.sdk.core.security import generate_skyvern_signature
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.routes.code_samples import (
    CANCEL_RUN_CODE_SAMPLE_PYTHON,
    CANCEL_RUN_CODE_SAMPLE_TS,
    CREATE_WORKFLOW_CODE_SAMPLE_CURL,
    CREATE_WORKFLOW_CODE_SAMPLE_PYTHON,
    CREATE_WORKFLOW_CODE_SAMPLE_TS,
    DELETE_WORKFLOW_CODE_SAMPLE_PYTHON,
    DELETE_WORKFLOW_CODE_SAMPLE_TS,
    GET_RUN_CODE_SAMPLE_PYTHON,
    GET_RUN_CODE_SAMPLE_TS,
    GET_RUN_TIMELINE_CODE_SAMPLE_PYTHON,
    GET_RUN_TIMELINE_CODE_SAMPLE_TS,
    GET_WORKFLOWS_CODE_SAMPLE_PYTHON,
    GET_WORKFLOWS_CODE_SAMPLE_TS,
    RETRY_RUN_WEBHOOK_CODE_SAMPLE_PYTHON,
    RETRY_RUN_WEBHOOK_CODE_SAMPLE_TS,
    RUN_TASK_CODE_SAMPLE_PYTHON,
    RUN_TASK_CODE_SAMPLE_TS,
    RUN_WORKFLOW_CODE_SAMPLE_PYTHON,
    RUN_WORKFLOW_CODE_SAMPLE_TS,
    UPDATE_WORKFLOW_CODE_SAMPLE_CURL,
    UPDATE_WORKFLOW_CODE_SAMPLE_PYTHON,
    UPDATE_WORKFLOW_CODE_SAMPLE_TS,
)
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router, legacy_v2_router
from skyvern.forge.sdk.schemas.ai_suggestions import AISuggestionBase, AISuggestionRequest
from skyvern.forge.sdk.schemas.organizations import (
    GetOrganizationAPIKeysResponse,
    GetOrganizationsResponse,
    Organization,
    OrganizationUpdate,
)
from skyvern.forge.sdk.schemas.prompts import CreateFromPromptRequest
from skyvern.forge.sdk.schemas.task_generations import GenerateTaskRequest, TaskGeneration
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Request
from skyvern.forge.sdk.schemas.tasks import (
    CreateTaskResponse,
    ModelsResponse,
    OrderBy,
    SortDirection,
    Task,
    TaskRequest,
    TaskResponse,
    TaskStatus,
)
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunTimeline
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.exceptions import (
    FailedToCreateWorkflow,
    FailedToUpdateWorkflow,
    InvalidTemplateWorkflowPermanentId,
    WorkflowParameterMissingRequiredValue,
)
from skyvern.forge.sdk.workflow.models.workflow import (
    RunWorkflowResponse,
    Workflow,
    WorkflowRequestBody,
    WorkflowRun,
    WorkflowRunResponseBase,
    WorkflowRunStatus,
    WorkflowRunWithWorkflowResponse,
)
from skyvern.schemas.artifacts import EntityType, entity_type_to_param
from skyvern.schemas.folders import Folder, FolderCreate, FolderUpdate, UpdateWorkflowFolderRequest
from skyvern.schemas.runs import (
    CUA_ENGINES,
    BlockRunRequest,
    BlockRunResponse,
    RunEngine,
    RunResponse,
    RunType,
    TaskRunRequest,
    TaskRunResponse,
    UploadFileResponse,
    WorkflowRunRequest,
    WorkflowRunResponse,
)
from skyvern.schemas.webhooks import RetryRunWebhookRequest
from skyvern.schemas.workflows import BlockType, WorkflowCreateYAMLRequest, WorkflowRequest, WorkflowStatus
from skyvern.services import block_service, run_service, task_v1_service, task_v2_service, workflow_service
from skyvern.services.pdf_import_service import pdf_import_service
from skyvern.webeye.actions.actions import Action

LOG = structlog.get_logger()


class AISuggestionType(str, Enum):
    DATA_SCHEMA = "data_schema"


################# /v1 Endpoints #################
@base_router.post(
    "/run/tasks",
    tags=["Agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "run_task",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": RUN_TASK_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": RUN_TASK_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    description="Run a task",
    summary="Run a task",
    responses={
        200: {"description": "Successfully run task"},
        400: {"description": "Invalid agent engine"},
    },
)
@base_router.post("/run/tasks/", include_in_schema=False)
async def run_task(
    request: Request,
    background_tasks: BackgroundTasks,
    run_request: TaskRunRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
    x_user_agent: Annotated[str | None, Header()] = None,
) -> TaskRunResponse:
    analytics.capture("skyvern-oss-run-task", data={"url": run_request.url})
    await PermissionCheckerFactory.get_instance().check(current_org, browser_session_id=run_request.browser_session_id)
    await app.RATE_LIMITER.rate_limit_submit_run(current_org.organization_id)

    if run_request.engine in CUA_ENGINES or run_request.engine == RunEngine.skyvern_v1:
        # create task v1
        # if there's no url, call task generation first to generate the url, data schema if any
        url = run_request.url
        data_extraction_goal = None
        data_extraction_schema = run_request.data_extraction_schema
        navigation_goal = run_request.prompt
        navigation_payload = None
        if not url:
            task_generation = await task_v1_service.generate_task(
                user_prompt=run_request.prompt,
                organization=current_org,
            )
            # What if it's a SDK request with browser_session_id?
            url = task_generation.url
            navigation_goal = task_generation.navigation_goal or run_request.prompt
            if run_request.engine in CUA_ENGINES:
                navigation_goal = run_request.prompt
            navigation_payload = task_generation.navigation_payload
            data_extraction_goal = task_generation.data_extraction_goal
            data_extraction_schema = data_extraction_schema or task_generation.extracted_information_schema

        task_v1_request = TaskRequest(
            title=run_request.title,
            url=url,
            navigation_goal=navigation_goal,
            navigation_payload=navigation_payload,
            data_extraction_goal=data_extraction_goal,
            extracted_information_schema=data_extraction_schema,
            error_code_mapping=run_request.error_code_mapping,
            proxy_location=run_request.proxy_location,
            browser_session_id=run_request.browser_session_id,
            webhook_callback_url=run_request.webhook_url,
            totp_verification_url=run_request.totp_url,
            totp_identifier=run_request.totp_identifier,
            include_action_history_in_verification=run_request.include_action_history_in_verification,
            model=run_request.model,
            max_screenshot_scrolls=run_request.max_screenshot_scrolls,
            extra_http_headers=run_request.extra_http_headers,
            browser_address=run_request.browser_address,
        )
        task_v1_response = await task_v1_service.run_task(
            task=task_v1_request,
            organization=current_org,
            engine=run_request.engine,
            x_max_steps_override=run_request.max_steps,
            x_api_key=x_api_key,
            request=request,
            background_tasks=background_tasks,
        )
        run_type = RunType.task_v1
        if run_request.engine == RunEngine.openai_cua:
            run_type = RunType.openai_cua
        elif run_request.engine == RunEngine.anthropic_cua:
            run_type = RunType.anthropic_cua
        # build the task run response
        return TaskRunResponse(
            run_id=task_v1_response.task_id,
            run_type=run_type,
            status=str(task_v1_response.status),
            output=task_v1_response.extracted_information,
            failure_reason=task_v1_response.failure_reason,
            created_at=task_v1_response.created_at,
            modified_at=task_v1_response.modified_at,
            app_url=f"{settings.SKYVERN_APP_URL.rstrip('/')}/runs/{task_v1_response.task_id}",
            run_request=TaskRunRequest(
                engine=run_request.engine,
                prompt=task_v1_response.navigation_goal,
                url=task_v1_response.url,
                webhook_url=task_v1_response.webhook_callback_url,
                totp_identifier=task_v1_response.totp_identifier,
                totp_url=task_v1_response.totp_verification_url,
                proxy_location=task_v1_response.proxy_location,
                max_steps=task_v1_response.max_steps_per_run,
                data_extraction_schema=task_v1_response.extracted_information_schema,
                error_code_mapping=task_v1_response.error_code_mapping,
                browser_session_id=run_request.browser_session_id,
                max_screenshot_scrolls=run_request.max_screenshot_scrolls,
            ),
        )
    if run_request.engine == RunEngine.skyvern_v2:
        # create task v2
        try:
            task_v2 = await task_v2_service.initialize_task_v2(
                organization=current_org,
                user_prompt=run_request.prompt,
                user_url=run_request.url,
                totp_identifier=run_request.totp_identifier,
                totp_verification_url=run_request.totp_url,
                webhook_callback_url=run_request.webhook_url,
                proxy_location=run_request.proxy_location,
                publish_workflow=run_request.publish_workflow,
                extracted_information_schema=run_request.data_extraction_schema,
                error_code_mapping=run_request.error_code_mapping,
                create_task_run=True,
                model=run_request.model,
                max_screenshot_scrolling_times=run_request.max_screenshot_scrolls,
                extra_http_headers=run_request.extra_http_headers,
                browser_address=run_request.browser_address,
            )
        except MissingBrowserAddressError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except LLMProviderError:
            LOG.error("LLM failure to initialize task v2", exc_info=True)
            raise HTTPException(
                status_code=500, detail="Skyvern LLM failure to initialize task v2. Please try again later."
            )
        await AsyncExecutorFactory.get_executor().execute_task_v2(
            request=request,
            background_tasks=background_tasks,
            organization_id=current_org.organization_id,
            task_v2_id=task_v2.observer_cruise_id,
            max_steps_override=run_request.max_steps,
            browser_session_id=run_request.browser_session_id,
        )
        refreshed_task_v2 = await app.DATABASE.get_task_v2(
            task_v2_id=task_v2.observer_cruise_id, organization_id=current_org.organization_id
        )
        task_v2 = refreshed_task_v2 if refreshed_task_v2 else task_v2
        return TaskRunResponse(
            run_id=task_v2.observer_cruise_id,
            run_type=RunType.task_v2,
            status=str(task_v2.status),
            output=task_v2.output,
            failure_reason=None,
            created_at=task_v2.created_at,
            modified_at=task_v2.modified_at,
            app_url=f"{settings.SKYVERN_APP_URL.rstrip('/')}/runs/{task_v2.workflow_run_id}",
            run_request=TaskRunRequest(
                engine=RunEngine.skyvern_v2,
                prompt=task_v2.prompt,
                url=task_v2.url,
                webhook_url=task_v2.webhook_callback_url,
                totp_identifier=task_v2.totp_identifier,
                totp_url=task_v2.totp_verification_url,
                proxy_location=task_v2.proxy_location,
                max_steps=run_request.max_steps,
                browser_session_id=run_request.browser_session_id,
                error_code_mapping=task_v2.error_code_mapping,
                data_extraction_schema=task_v2.extracted_information_schema,
                publish_workflow=run_request.publish_workflow,
                max_screenshot_scrolls=run_request.max_screenshot_scrolls,
            ),
        )
    LOG.error("Invalid agent engine", engine=run_request.engine, organization_id=current_org.organization_id)
    raise HTTPException(status_code=400, detail=f"Invalid agent engine: {run_request.engine}")


@base_router.post(
    "/run/workflows",
    tags=["Workflows"],
    openapi_extra={
        "x-fern-sdk-method-name": "run_workflow",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": RUN_WORKFLOW_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": RUN_WORKFLOW_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    description="Run a workflow",
    summary="Run a workflow",
    responses={
        200: {"description": "Successfully run workflow"},
        400: {"description": "Invalid workflow run request"},
    },
)
@base_router.post("/run/workflows/", include_in_schema=False)
async def run_workflow(
    request: Request,
    background_tasks: BackgroundTasks,
    workflow_run_request: WorkflowRunRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    template: bool = Query(False),
    x_api_key: Annotated[str | None, Header()] = None,
    x_max_steps_override: Annotated[int | None, Header()] = None,
    x_user_agent: Annotated[str | None, Header()] = None,
) -> WorkflowRunResponse:
    analytics.capture("skyvern-oss-run-workflow")
    await PermissionCheckerFactory.get_instance().check(
        current_org, browser_session_id=workflow_run_request.browser_session_id
    )
    await app.RATE_LIMITER.rate_limit_submit_run(current_org.organization_id)
    workflow_id = workflow_run_request.workflow_id
    context = skyvern_context.ensure_context()
    request_id = context.request_id
    legacy_workflow_request = WorkflowRequestBody(
        data=workflow_run_request.parameters,
        proxy_location=workflow_run_request.proxy_location,
        webhook_callback_url=workflow_run_request.webhook_url,
        totp_identifier=workflow_run_request.totp_identifier,
        totp_verification_url=workflow_run_request.totp_url,
        browser_session_id=workflow_run_request.browser_session_id,
        browser_profile_id=workflow_run_request.browser_profile_id,
        max_screenshot_scrolls=workflow_run_request.max_screenshot_scrolls,
        extra_http_headers=workflow_run_request.extra_http_headers,
        browser_address=workflow_run_request.browser_address,
        run_with=workflow_run_request.run_with,
        ai_fallback=workflow_run_request.ai_fallback,
    )

    try:
        workflow_run = await workflow_service.run_workflow(
            workflow_id=workflow_id,
            organization=current_org,
            workflow_request=legacy_workflow_request,
            template=template,
            version=None,
            max_steps=x_max_steps_override,
            api_key=x_api_key,
            request_id=request_id,
            request=request,
            background_tasks=background_tasks,
        )
    except MissingBrowserAddressError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return WorkflowRunResponse(
        run_id=workflow_run.workflow_run_id,
        run_type=RunType.workflow_run,
        status=str(workflow_run.status),
        output=None,
        failure_reason=workflow_run.failure_reason,
        created_at=workflow_run.created_at,
        modified_at=workflow_run.modified_at,
        run_request=workflow_run_request,
        downloaded_files=None,
        recording_url=None,
        app_url=f"{settings.SKYVERN_APP_URL.rstrip('/')}/runs/{workflow_run.workflow_run_id}",
        run_with=workflow_run.run_with,
        ai_fallback=workflow_run.ai_fallback,
    )


@base_router.get(
    "/runs/{run_id}",
    tags=["Agent", "Workflows"],
    response_model=RunResponse,
    description="Get run information (task run, workflow run)",
    summary="Get a run by id",
    openapi_extra={
        "x-fern-sdk-method-name": "get_run",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": GET_RUN_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": GET_RUN_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    responses={
        200: {"description": "Successfully got run"},
        404: {"description": "Run not found"},
    },
)
@base_router.get(
    "/runs/{run_id}/",
    response_model=RunResponse,
    include_in_schema=False,
)
async def get_run(
    run_id: str = Path(
        ..., description="The id of the task run or the workflow run.", examples=["tsk_123", "tsk_v2_123", "wr_123"]
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> RunResponse:
    run_response = await run_service.get_run_response(run_id, organization_id=current_org.organization_id)
    if not run_response:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Task run not found {run_id}",
        )
    return run_response


@base_router.post(
    "/runs/{run_id}/cancel",
    tags=["Agent", "Workflows"],
    openapi_extra={
        "x-fern-sdk-method-name": "cancel_run",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": CANCEL_RUN_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": CANCEL_RUN_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    description="Cancel a run (task or workflow)",
    summary="Cancel a run by id",
)
@base_router.post("/runs/{run_id}/cancel/", include_in_schema=False)
async def cancel_run(
    run_id: str = Path(..., description="The id of the task run or the workflow run to cancel."),
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    analytics.capture("skyvern-oss-agent-cancel-run")

    await run_service.cancel_run(run_id, organization_id=current_org.organization_id, api_key=x_api_key)


@legacy_base_router.post(
    "/workflows",
    openapi_extra={
        "requestBody": {
            "content": {"application/x-yaml": {"schema": WorkflowCreateYAMLRequest.model_json_schema()}},
            "required": True,
        },
        "x-fern-sdk-method-name": "create_workflow",
    },
    response_model=Workflow,
    tags=["agent"],
)
@legacy_base_router.post(
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
async def create_workflow_legacy(
    request: Request,
    folder_id: str | None = Query(None, description="Optional folder ID to assign the workflow to"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Workflow:
    analytics.capture("skyvern-oss-agent-workflow-create-legacy")
    raw_yaml = await request.body()
    try:
        workflow_yaml = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        raise HTTPException(status_code=422, detail="Invalid YAML")

    try:
        workflow_create_request = WorkflowCreateYAMLRequest.model_validate(workflow_yaml)
        # Override folder_id if provided as query parameter
        if folder_id is not None:
            workflow_create_request.folder_id = folder_id
        return await app.WORKFLOW_SERVICE.create_workflow_from_request(
            organization=current_org, request=workflow_create_request
        )
    except WorkflowParameterMissingRequiredValue as e:
        raise e
    except Exception as e:
        LOG.error("Failed to create workflow", exc_info=True, organization_id=current_org.organization_id)
        raise FailedToCreateWorkflow(str(e))


@base_router.post(
    "/workflows",
    response_model=Workflow,
    tags=["Workflows"],
    openapi_extra={
        "x-fern-sdk-method-name": "create_workflow",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "curl", "code": CREATE_WORKFLOW_CODE_SAMPLE_CURL},
                    {"sdk": "python", "code": CREATE_WORKFLOW_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": CREATE_WORKFLOW_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    description="Create a new workflow",
    summary="Create a new workflow",
    responses={
        200: {"description": "Successfully created workflow"},
        422: {"description": "Invalid workflow definition"},
    },
)
@base_router.post(
    "/workflows/",
    response_model=Workflow,
    include_in_schema=False,
)
async def create_workflow(
    data: WorkflowRequest,
    folder_id: str | None = Query(None, description="Optional folder ID to assign the workflow to"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Workflow:
    analytics.capture("skyvern-oss-agent-workflow-create")
    try:
        if data.yaml_definition:
            workflow_json_from_yaml = yaml.safe_load(data.yaml_definition)
            workflow_definition = WorkflowCreateYAMLRequest.model_validate(workflow_json_from_yaml)
        elif data.json_definition:
            workflow_definition = data.json_definition
        else:
            raise HTTPException(
                status_code=422,
                detail="Invalid workflow definition. Workflow should be provided in either yaml or json format.",
            )
        # Override folder_id if provided as query parameter
        if folder_id is not None:
            workflow_definition.folder_id = folder_id
        return await app.WORKFLOW_SERVICE.create_workflow_from_request(
            organization=current_org,
            request=workflow_definition,
        )
    except yaml.YAMLError:
        raise HTTPException(status_code=422, detail="Invalid YAML")
    except WorkflowParameterMissingRequiredValue as e:
        raise e
    except Exception as e:
        LOG.error("Failed to create workflow", exc_info=True, organization_id=current_org.organization_id)
        raise FailedToCreateWorkflow(str(e))


@base_router.post(
    "/workflows/create-from-prompt",
    include_in_schema=False,
)
async def create_workflow_from_prompt(
    data: CreateFromPromptRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
    x_max_iterations_override: Annotated[int | str | None, Header()] = None,
    x_max_steps_override: Annotated[int | str | None, Header()] = None,
) -> dict[str, Any]:
    task_version = data.task_version or "v2"
    request = data.request

    if x_max_iterations_override or x_max_steps_override:
        LOG.info(
            "Overriding max steps for workflow-from-prompt",
            max_iterations_override=x_max_iterations_override,
            max_steps_override=x_max_steps_override,
        )
    await PermissionCheckerFactory.get_instance().check(organization, browser_session_id=request.browser_session_id)

    if isinstance(x_max_iterations_override, str):
        try:
            x_max_iterations_override = int(x_max_iterations_override)
        except ValueError:
            x_max_iterations_override = None

    if isinstance(x_max_steps_override, str):
        try:
            x_max_steps_override = int(x_max_steps_override)
        except ValueError:
            x_max_steps_override = None

    try:
        workflow = await app.WORKFLOW_SERVICE.create_workflow_from_prompt(
            organization=organization,
            user_prompt=request.user_prompt,
            totp_identifier=request.totp_identifier,
            totp_verification_url=request.totp_verification_url,
            webhook_callback_url=request.webhook_callback_url,
            proxy_location=request.proxy_location,
            max_screenshot_scrolling_times=request.max_screenshot_scrolls,
            extra_http_headers=request.extra_http_headers,
            max_iterations=x_max_iterations_override,
            max_steps=x_max_steps_override,
            status=WorkflowStatus.published if request.publish_workflow else WorkflowStatus.auto_generated,
            run_with=request.run_with,
            ai_fallback=request.ai_fallback if request.ai_fallback is not None else True,
            task_version=task_version,
        )
    except Exception as e:
        LOG.error("Failed to create workflow from prompt", exc_info=True, organization_id=organization.organization_id)
        raise FailedToCreateWorkflow(str(e))

    return workflow.model_dump(by_alias=True)


async def _validate_file_size(file: UploadFile) -> UploadFile:
    try:
        file.file.seek(0, 2)  # Move the pointer to the end of the file
        size = file.file.tell()  # Get the current position of the pointer, which represents the file size
        file.file.seek(0)  # Reset the pointer back to the beginning
    except Exception as e:
        raise HTTPException(status_code=500, detail="Could not determine file size.") from e

    if size > app.SETTINGS_MANAGER.MAX_UPLOAD_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File size exceeds the maximum allowed size ({app.SETTINGS_MANAGER.MAX_UPLOAD_FILE_SIZE / 1024 / 1024} MB)",
        )
    return file


@legacy_base_router.post(
    "/workflows/import-pdf",
    response_model=dict[str, Any],
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "import_workflow_from_pdf",
        "x-fern-examples": [
            {
                "code-samples": [
                    {
                        "sdk": "curl",
                        "code": 'curl -X POST "https://api.skyvern.com/workflows/import-pdf" \\\n  -H "Authorization: Bearer YOUR_API_KEY" \\\n  -F "file=@sop_document.pdf"',
                    }
                ]
            }
        ],
    },
    description="Import a workflow from a PDF containing Standard Operating Procedures",
    summary="Import workflow from PDF",
    responses={
        200: {"description": "Successfully imported workflow from PDF"},
        400: {"description": "Invalid PDF file or no content found"},
        422: {"description": "Failed to convert SOP to workflow"},
        500: {"description": "Internal server error during processing"},
    },
)
@legacy_base_router.post(
    "/workflows/import-pdf/",
    response_model=dict[str, Any],
    include_in_schema=False,
)
async def import_workflow_from_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = Depends(_validate_file_size),
    folder_id: str | None = Query(None, description="Optional folder ID to assign the imported workflow to"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> dict[str, Any]:
    """Import a workflow from a PDF file containing Standard Operating Procedures."""
    analytics.capture("skyvern-oss-workflow-import-pdf")

    # Read file and validate early (before creating import record)
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    try:
        file_contents = await file.read()
        file_name = file.filename
    finally:
        # Release underlying SpooledTemporaryFile ASAP
        await file.close()

    # Extract text in executor to avoid blocking event loop (1-2 seconds)
    try:
        sop_text = await asyncio.to_thread(
            pdf_import_service.extract_text_from_pdf,
            file_contents,
            file_name,
        )
    except HTTPException:
        # Re-raise validation errors immediately
        raise

    # Validation passed! Create empty workflow v1 with status='importing'
    empty_workflow = await app.DATABASE.create_workflow(
        title=f"Importing {file_name}",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=current_org.organization_id,
        status=WorkflowStatus.importing,
        folder_id=folder_id,
    )

    # Process PDF import in background (LLM call is the slow part)
    async def process_pdf_import() -> None:
        try:
            # Create workflow from extracted text (LLM processing)
            result = await pdf_import_service.create_workflow_from_sop_text(sop_text, current_org)

            # Create v2 with real content
            await app.WORKFLOW_SERVICE.create_workflow_from_request(
                organization=current_org,
                request=WorkflowCreateYAMLRequest.model_validate(result),
                workflow_permanent_id=empty_workflow.workflow_permanent_id,
            )

            # Update v1 status to published (v1 won't show in list since v2 is latest version)
            await app.DATABASE.update_workflow(
                workflow_id=empty_workflow.workflow_id,
                organization_id=current_org.organization_id,
                status=WorkflowStatus.published,
            )

            LOG.info(
                "Workflow import completed",
                workflow_permanent_id=empty_workflow.workflow_permanent_id,
                organization_id=current_org.organization_id,
            )
        except Exception as e:
            # Log full error server-side for debugging
            LOG.exception(
                "Workflow import failed",
                workflow_permanent_id=empty_workflow.workflow_permanent_id,
                error=str(e),
                organization_id=current_org.organization_id,
            )

            # Provide sanitized user-facing error message (don't expose internal details/PII)
            sanitized_error = "Import failed. Please verify the PDF content and try again."

            # Mark v1 as import_failed with sanitized error
            await app.DATABASE.update_workflow(
                workflow_id=empty_workflow.workflow_id,
                organization_id=current_org.organization_id,
                status=WorkflowStatus.import_failed,
                import_error=sanitized_error,
            )

    background_tasks.add_task(process_pdf_import)

    return {
        "workflow_permanent_id": empty_workflow.workflow_permanent_id,
        "status": "importing",
        "file_name": file.filename,
        "organization_id": current_org.organization_id,
        "created_at": empty_workflow.created_at.isoformat(),
    }


@legacy_base_router.put(
    "/workflows/{workflow_id}",
    openapi_extra={
        "requestBody": {
            "content": {"application/x-yaml": {"schema": WorkflowCreateYAMLRequest.model_json_schema()}},
            "required": True,
        },
        "x-fern-sdk-method-name": "update_workflow",
    },
    response_model=Workflow,
    tags=["agent"],
)
@legacy_base_router.put(
    "/workflows/{workflow_id}/",
    openapi_extra={
        "requestBody": {
            "content": {"application/x-yaml": {"schema": WorkflowCreateYAMLRequest.model_json_schema()}},
            "required": True,
        },
    },
    response_model=Workflow,
    include_in_schema=False,
)
async def update_workflow_legacy(
    request: Request,
    workflow_id: str = Path(
        ..., description="The ID of the workflow to update. Workflow ID starts with `wpid_`.", examples=["wpid_123"]
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
    delete_code_cache_is_ok: bool = Query(False),
) -> Workflow:
    analytics.capture("skyvern-oss-agent-workflow-update")
    # validate the workflow
    raw_yaml = await request.body()
    try:
        workflow_yaml = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        raise HTTPException(status_code=422, detail="Invalid YAML")

    try:
        workflow_create_request = WorkflowCreateYAMLRequest.model_validate(workflow_yaml)
        return await app.WORKFLOW_SERVICE.create_workflow_from_request(
            organization=current_org,
            request=workflow_create_request,
            workflow_permanent_id=workflow_id,
            delete_code_cache_is_ok=delete_code_cache_is_ok,
        )
    except CannotUpdateWorkflowDueToCodeCache as e:
        raise HTTPException(
            status_code=422,
            detail=str(e),
        ) from e
    except WorkflowParameterMissingRequiredValue as e:
        raise e
    except (SkyvernHTTPException, ValidationError) as e:
        # Bubble up well-formed client errors so they are not converted to 500s
        raise e
    except Exception as e:
        LOG.exception(
            "Failed to update workflow",
            workflow_permanent_id=workflow_id,
            organization_id=current_org.organization_id,
        )
        raise FailedToUpdateWorkflow(workflow_id, f"<{type(e).__name__}: {str(e)}>")


@base_router.post(
    "/workflows/{workflow_id}",
    response_model=Workflow,
    tags=["Workflows"],
    openapi_extra={
        "x-fern-sdk-method-name": "update_workflow",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "curl", "code": UPDATE_WORKFLOW_CODE_SAMPLE_CURL},
                    {"sdk": "python", "code": UPDATE_WORKFLOW_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": UPDATE_WORKFLOW_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    description="Update a workflow",
    summary="Update a workflow",
    responses={
        200: {"description": "Successfully updated workflow"},
        422: {"description": "Invalid workflow definition"},
    },
)
@base_router.post(
    "/workflows/{workflow_id}/",
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
    data: WorkflowRequest,
    workflow_id: str = Path(
        ..., description="The ID of the workflow to update. Workflow ID starts with `wpid_`.", examples=["wpid_123"]
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Workflow:
    analytics.capture("skyvern-oss-agent-workflow-update")
    try:
        if data.yaml_definition:
            workflow_json_from_yaml = yaml.safe_load(data.yaml_definition)
            workflow_definition = WorkflowCreateYAMLRequest.model_validate(workflow_json_from_yaml)
        elif data.json_definition:
            workflow_definition = data.json_definition
        else:
            raise HTTPException(
                status_code=422,
                detail="Invalid workflow definition. Workflow should be provided in either yaml or json format.",
            )
        return await app.WORKFLOW_SERVICE.create_workflow_from_request(
            organization=current_org,
            request=workflow_definition,
            workflow_permanent_id=workflow_id,
        )
    except yaml.YAMLError:
        raise HTTPException(status_code=422, detail="Invalid YAML")
    except WorkflowParameterMissingRequiredValue as e:
        raise e
    except (SkyvernHTTPException, ValidationError) as e:
        # Bubble up well-formed client errors so they are not converted to 500s
        raise e
    except Exception as e:
        LOG.exception(
            "Failed to update workflow",
            exc_info=True,
            organization_id=current_org.organization_id,
            workflow_permanent_id=workflow_id,
        )
        raise FailedToUpdateWorkflow(workflow_id, f"<{type(e).__name__}: {str(e)}>")


@legacy_base_router.delete(
    "/workflows/{workflow_id}",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "delete_workflow",
    },
)
@legacy_base_router.delete("/workflows/{workflow_id}/", include_in_schema=False)
@base_router.post(
    "/workflows/{workflow_id}/delete",
    tags=["Workflows"],
    openapi_extra={
        "x-fern-sdk-method-name": "delete_workflow",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": DELETE_WORKFLOW_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": DELETE_WORKFLOW_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    description="Delete a workflow",
    summary="Delete a workflow",
    responses={200: {"description": "Successfully deleted workflow"}},
)
@base_router.post("/workflows/{workflow_id}/delete/", include_in_schema=False)
async def delete_workflow(
    workflow_id: str = Path(
        ..., description="The ID of the workflow to delete. Workflow ID starts with `wpid_`.", examples=["wpid_123"]
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> None:
    analytics.capture("skyvern-oss-agent-workflow-delete")
    await app.WORKFLOW_SERVICE.delete_workflow_by_permanent_id(workflow_id, current_org.organization_id)


################# Folder Endpoints #################
@legacy_base_router.post("/folders", response_model=Folder, tags=["agent"], include_in_schema=False)
@legacy_base_router.post("/folders/", response_model=Folder, include_in_schema=False)
@base_router.post(
    "/folders",
    response_model=Folder,
    tags=["Workflows"],
    include_in_schema=False,
    description="Create a new folder to organize workflows",
    summary="Create folder",
    responses={
        200: {"description": "Successfully created folder"},
        400: {"description": "Invalid request"},
    },
)
@base_router.post("/folders/", response_model=Folder, include_in_schema=False)
async def create_folder(
    data: FolderCreate,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Folder:
    analytics.capture("skyvern-oss-folder-create")
    folder_model = await app.DATABASE.create_folder(
        organization_id=current_org.organization_id,
        title=data.title,
        description=data.description,
    )
    workflow_count = await app.DATABASE.get_folder_workflow_count(
        folder_id=folder_model.folder_id,
        organization_id=current_org.organization_id,
    )
    return Folder(
        folder_id=folder_model.folder_id,
        organization_id=folder_model.organization_id,
        title=folder_model.title,
        description=folder_model.description,
        workflow_count=workflow_count,
        created_at=folder_model.created_at,
        modified_at=folder_model.modified_at,
    )


@legacy_base_router.get("/folders/{folder_id}", response_model=Folder, tags=["agent"], include_in_schema=False)
@legacy_base_router.get("/folders/{folder_id}/", response_model=Folder, include_in_schema=False)
@base_router.get(
    "/folders/{folder_id}",
    response_model=Folder,
    tags=["Workflows"],
    include_in_schema=False,
    description="Get a specific folder by ID",
    summary="Get folder",
    responses={
        200: {"description": "Successfully retrieved folder"},
        404: {"description": "Folder not found"},
    },
)
@base_router.get("/folders/{folder_id}/", response_model=Folder, include_in_schema=False)
async def get_folder(
    folder_id: str = Path(..., description="Folder ID", examples=["fld_123"]),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Folder:
    folder = await app.DATABASE.get_folder(
        folder_id=folder_id,
        organization_id=current_org.organization_id,
    )
    if not folder:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=f"Folder {folder_id} not found")

    workflow_count = await app.DATABASE.get_folder_workflow_count(
        folder_id=folder.folder_id,
        organization_id=current_org.organization_id,
    )

    return Folder(
        folder_id=folder.folder_id,
        organization_id=folder.organization_id,
        title=folder.title,
        description=folder.description,
        workflow_count=workflow_count,
        created_at=folder.created_at,
        modified_at=folder.modified_at,
    )


@legacy_base_router.get("/folders", response_model=list[Folder], tags=["agent"], include_in_schema=False)
@legacy_base_router.get("/folders/", response_model=list[Folder], include_in_schema=False)
@base_router.get(
    "/folders",
    response_model=list[Folder],
    tags=["Workflows"],
    include_in_schema=False,
    description="Get all folders for the organization",
    summary="Get folders",
    responses={
        200: {"description": "Successfully retrieved folders"},
    },
)
@base_router.get("/folders/", response_model=list[Folder], include_in_schema=False)
async def get_folders(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(100, ge=1, le=500, description="Number of folders per page"),
    search: str | None = Query(None, description="Search folders by title or description"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[Folder]:
    folders = await app.DATABASE.get_folders(
        organization_id=current_org.organization_id,
        page=page,
        page_size=page_size,
        search_query=search,
    )

    # Get workflow counts for all folders in a single query
    if folders:
        folder_ids = [folder.folder_id for folder in folders]
        workflow_counts = await app.DATABASE.get_folder_workflow_counts_batch(
            folder_ids=folder_ids,
            organization_id=current_org.organization_id,
        )
    else:
        workflow_counts = {}

    # Build result with workflow counts
    result = []
    for folder in folders:
        result.append(
            Folder(
                folder_id=folder.folder_id,
                organization_id=folder.organization_id,
                title=folder.title,
                description=folder.description,
                workflow_count=workflow_counts.get(folder.folder_id, 0),
                created_at=folder.created_at,
                modified_at=folder.modified_at,
            )
        )

    return result


@legacy_base_router.put("/folders/{folder_id}", response_model=Folder, tags=["agent"], include_in_schema=False)
@legacy_base_router.put("/folders/{folder_id}/", response_model=Folder, include_in_schema=False)
@base_router.put(
    "/folders/{folder_id}",
    response_model=Folder,
    tags=["Workflows"],
    include_in_schema=False,
    description="Update a folder's title or description",
    summary="Update folder",
    responses={
        200: {"description": "Successfully updated folder"},
        404: {"description": "Folder not found"},
    },
)
@base_router.put("/folders/{folder_id}/", response_model=Folder, include_in_schema=False)
async def update_folder(
    folder_id: str = Path(..., description="Folder ID", examples=["fld_123"]),
    data: FolderUpdate = Body(...),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Folder:
    folder = await app.DATABASE.update_folder(
        folder_id=folder_id,
        organization_id=current_org.organization_id,
        title=data.title,
        description=data.description,
    )
    if not folder:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=f"Folder {folder_id} not found")

    workflow_count = await app.DATABASE.get_folder_workflow_count(
        folder_id=folder.folder_id,
        organization_id=current_org.organization_id,
    )

    return Folder(
        folder_id=folder.folder_id,
        organization_id=folder.organization_id,
        title=folder.title,
        description=folder.description,
        workflow_count=workflow_count,
        created_at=folder.created_at,
        modified_at=folder.modified_at,
    )


@legacy_base_router.delete("/folders/{folder_id}", tags=["agent"], include_in_schema=False)
@legacy_base_router.delete("/folders/{folder_id}/", include_in_schema=False)
@base_router.delete(
    "/folders/{folder_id}",
    tags=["Workflows"],
    include_in_schema=False,
    description="Delete a folder. Optionally delete all workflows in the folder.",
    summary="Delete folder",
    responses={
        200: {"description": "Successfully deleted folder"},
        404: {"description": "Folder not found"},
    },
)
@base_router.delete("/folders/{folder_id}/", include_in_schema=False)
async def delete_folder(
    folder_id: str = Path(..., description="Folder ID", examples=["fld_123"]),
    delete_workflows: bool = Query(False, description="If true, also delete all workflows in this folder"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> dict:
    analytics.capture("skyvern-oss-folder-delete")
    success = await app.DATABASE.soft_delete_folder(
        folder_id=folder_id,
        organization_id=current_org.organization_id,
        delete_workflows=delete_workflows,
    )
    if not success:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=f"Folder {folder_id} not found")

    return {"status": "deleted", "folder_id": folder_id, "workflows_deleted": delete_workflows}


@legacy_base_router.put(
    "/workflows/{workflow_permanent_id}/folder", response_model=Workflow, tags=["agent"], include_in_schema=False
)
@legacy_base_router.put("/workflows/{workflow_permanent_id}/folder/", response_model=Workflow, include_in_schema=False)
@base_router.put(
    "/workflows/{workflow_permanent_id}/folder",
    response_model=Workflow,
    tags=["Workflows"],
    include_in_schema=False,
    description="Update a workflow's folder assignment for the latest version",
    summary="Update workflow folder",
    responses={
        200: {"description": "Successfully updated workflow folder"},
        404: {"description": "Workflow not found"},
        400: {"description": "Folder not found"},
    },
)
@base_router.put("/workflows/{workflow_permanent_id}/folder/", response_model=Workflow, include_in_schema=False)
async def update_workflow_folder(
    workflow_permanent_id: str = Path(..., description="Workflow permanent ID", examples=["wpid_123"]),
    data: UpdateWorkflowFolderRequest = Body(...),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Workflow:
    try:
        workflow = await app.DATABASE.update_workflow_folder(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=current_org.organization_id,
            folder_id=data.folder_id,
        )
        if not workflow:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND, detail=f"Workflow {workflow_permanent_id} not found"
            )

        return workflow
    except ValueError as e:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


@legacy_base_router.post(
    "/utilities/curl-to-http",
    tags=["Utilities"],
    openapi_extra={
        "x-fern-sdk-method-name": "convert_curl_to_http",
    },
    description="Convert a curl command to HTTP request parameters",
    summary="Convert curl to HTTP parameters",
    responses={
        200: {"description": "Successfully converted curl command"},
        400: {"description": "Invalid curl command"},
    },
)
@legacy_base_router.post("/utilities/curl-to-http/", include_in_schema=False)
async def convert_curl_to_http(
    request: dict[str, str],
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> dict[str, Any]:
    """
    Convert a curl command to HTTP request parameters.

    This endpoint is useful for converting curl commands to the format
    needed by the HTTP Request workflow block.

    Request body should contain:
    - curl_command: The curl command string to convert

    Returns:
    - method: HTTP method
    - url: The URL
    - headers: Dict of headers
    - body: Request body as dict
    - timeout: Default timeout
    - follow_redirects: Default follow redirects setting
    """
    curl_command = request.get("curl_command")
    if not curl_command:
        raise HTTPException(status_code=400, detail="curl_command is required in the request body")

    try:
        result = curl_to_http_request_block_params(curl_command)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        LOG.error(
            "Failed to convert curl command",
            error=str(e),
            organization_id=current_org.organization_id,
        )
        raise HTTPException(status_code=400, detail=f"Failed to convert curl command: {str(e)}")


@base_router.get(
    "/artifacts/{artifact_id}",
    tags=["Artifacts"],
    response_model=Artifact,
    openapi_extra={
        "x-fern-sdk-method-name": "get_artifact",
    },
    description="Get an artifact",
    summary="Get an artifact",
    responses={
        200: {"description": "Successfully retrieved artifact"},
        404: {"description": "Artifact not found"},
    },
)
@base_router.get("/artifacts/{artifact_id}/", response_model=Artifact, include_in_schema=False)
async def get_artifact(
    artifact_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Artifact:
    analytics.capture("skyvern-oss-artifact-get")
    artifact = await app.DATABASE.get_artifact_by_id(
        artifact_id=artifact_id,
        organization_id=current_org.organization_id,
    )
    if not artifact:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Artifact not found {artifact_id}",
        )
    signed_urls = await app.ARTIFACT_MANAGER.get_share_links([artifact])
    if signed_urls and len(signed_urls) == 1:
        artifact.signed_url = signed_urls[0]
    return artifact


@base_router.get(
    "/runs/{run_id}/artifacts",
    tags=["Artifacts"],
    response_model=list[Artifact],
    openapi_extra={
        "x-fern-sdk-method-name": "get_run_artifacts",
    },
    description="Get artifacts for a run",
    summary="Get artifacts for a run",
)
@base_router.get("/runs/{run_id}/artifacts/", response_model=list[Artifact], include_in_schema=False)
async def get_run_artifacts(
    run_id: str = Path(..., description="The id of the task run or the workflow run."),
    artifact_type: Annotated[list[ArtifactType] | None, Query()] = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    analytics.capture("skyvern-oss-run-artifacts-get")
    # Get artifacts as a list (not grouped by type)
    artifacts = await app.DATABASE.get_artifacts_for_run(
        run_id=run_id,
        organization_id=current_org.organization_id,
        artifact_types=artifact_type,
        group_by_type=False,  # This ensures we get a list, not a dict
    )

    # Ensure we have a list of artifacts (since group_by_type=False, this will always be a list)
    artifacts_list = artifacts if isinstance(artifacts, list) else []

    signed_urls = await app.ARTIFACT_MANAGER.get_share_links(artifacts_list)
    if signed_urls and len(signed_urls) == len(artifacts_list):
        for i, artifact in enumerate(artifacts_list):
            artifact.signed_url = signed_urls[i]

    return ORJSONResponse([artifact.model_dump() for artifact in artifacts_list])


@base_router.post(
    "/runs/{run_id}/retry_webhook",
    tags=["Agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "retry_run_webhook",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": RETRY_RUN_WEBHOOK_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": RETRY_RUN_WEBHOOK_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    description="Retry sending the webhook for a run",
    summary="Retry run webhook",
)
@base_router.post("/runs/{run_id}/retry_webhook/", include_in_schema=False)
async def retry_run_webhook(
    run_id: str = Path(..., description="The id of the task run or the workflow run.", examples=["tsk_123", "wr_123"]),
    request: RetryRunWebhookRequest | None = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    analytics.capture("skyvern-oss-agent-run-retry-webhook")
    await run_service.retry_run_webhook(
        run_id,
        organization_id=current_org.organization_id,
        api_key=x_api_key,
        webhook_url=request.webhook_url if request else None,
    )


@base_router.get(
    "/runs/{run_id}/timeline",
    tags=["Agent", "Workflows"],
    response_model=list[WorkflowRunTimeline],
    openapi_extra={
        "x-fern-sdk-method-name": "get_run_timeline",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": GET_RUN_TIMELINE_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": GET_RUN_TIMELINE_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    description="Get timeline for a run (workflow run or task_v2 run)",
    summary="Get run timeline",
    responses={
        200: {"description": "Successfully retrieved run timeline"},
        404: {"description": "Run not found"},
        400: {"description": "Timeline not available for this run type"},
    },
)
@base_router.get(
    "/runs/{run_id}/timeline/",
    response_model=list[WorkflowRunTimeline],
    include_in_schema=False,
)
async def get_run_timeline(
    run_id: str = Path(
        ..., description="The id of the workflow run or task_v2 run.", examples=["wr_123", "tsk_v2_123"]
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[WorkflowRunTimeline]:
    analytics.capture("skyvern-oss-run-timeline-get")

    # Check if the run exists
    run_response = await run_service.get_run_response(run_id, organization_id=current_org.organization_id)
    if not run_response:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Run not found {run_id}",
        )

    # Handle workflow runs directly
    if run_response.run_type == RunType.workflow_run:
        return await _flatten_workflow_run_timeline(current_org.organization_id, run_id)

    # Handle task_v2 runs by getting their associated workflow_run_id
    if run_response.run_type == RunType.task_v2:
        task_v2 = await app.DATABASE.get_task_v2(task_v2_id=run_id, organization_id=current_org.organization_id)
        if not task_v2:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail=f"Task v2 not found {run_id}",
            )

        if not task_v2.workflow_run_id:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"Task v2 {run_id} has no associated workflow run",
            )

        return await _flatten_workflow_run_timeline(current_org.organization_id, task_v2.workflow_run_id)

    # Timeline not available for other run types
    raise HTTPException(
        status_code=http_status.HTTP_400_BAD_REQUEST,
        detail=f"Timeline not available for run type {run_response.run_type}",
    )


@base_router.post(
    "/run/workflows/blocks",
    include_in_schema=False,
    response_model=BlockRunResponse,
)
async def run_block(
    request: Request,
    background_tasks: BackgroundTasks,
    block_run_request: BlockRunRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
    user_id: str = Depends(org_auth_service.get_current_user_id),
    template: bool = Query(False),
    x_api_key: Annotated[str | None, Header()] = None,
) -> BlockRunResponse:
    """
    Kick off the execution of one or more blocks in a workflow. Returns the
    workflow_run_id.
    """

    # NOTE(jdo): if you're running debugger locally, and you want to see the
    # block runs happening (no temporal; no pbs), then uncomment these two
    # lines; that'll make the block run happen in a new local browser instance.
    # LOG.critical("REMOVING BROWSER SESSION ID")
    # block_run_request.browser_session_id = None

    workflow_run = await block_service.ensure_workflow_run(
        organization=organization,
        template=template,
        workflow_permanent_id=block_run_request.workflow_id,
        block_run_request=block_run_request,
    )

    browser_session_id = block_run_request.browser_session_id

    await block_service.execute_blocks(
        request=request,
        background_tasks=background_tasks,
        api_key=x_api_key or "",
        block_labels=block_run_request.block_labels,
        workflow_id=block_run_request.workflow_id,
        workflow_run_id=workflow_run.workflow_run_id,
        workflow_permanent_id=workflow_run.workflow_permanent_id,
        organization=organization,
        user_id=user_id,
        browser_session_id=browser_session_id,
        block_outputs=block_run_request.block_outputs,
    )

    return BlockRunResponse(
        block_labels=block_run_request.block_labels,
        run_id=workflow_run.workflow_run_id,
        run_type=RunType.workflow_run,
        status=str(workflow_run.status),
        output=None,
        failure_reason=workflow_run.failure_reason,
        created_at=workflow_run.created_at,
        modified_at=workflow_run.modified_at,
        run_request=block_run_request,
        downloaded_files=None,
        recording_url=None,
        app_url=f"{settings.SKYVERN_APP_URL.rstrip('/')}/runs/{workflow_run.workflow_run_id}",
    )


################# Legacy Endpoints #################
@legacy_base_router.post(
    "/webhook",
    tags=["server"],
    openapi_extra={
        "x-fern-sdk-method-name": "webhook",
    },
    include_in_schema=False,
)
@legacy_base_router.post("/webhook/", include_in_schema=False)
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
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Missing webhook signature or timestamp",
        )

    generated_signature = generate_skyvern_signature(
        payload.decode("utf-8"),
        settings.SKYVERN_API_KEY,
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


@legacy_base_router.get(
    "/heartbeat",
    tags=["server"],
    openapi_extra={
        "x-fern-sdk-method-name": "heartbeat",
    },
)
@legacy_base_router.get("/heartbeat/", include_in_schema=False)
async def heartbeat() -> Response:
    """
    Check if the server is running.
    """
    return Response(content="Server is running.", status_code=200, headers={"X-Skyvern-API-Version": __version__})


@legacy_base_router.get(
    "/models",
    tags=["agent"],
    openapi_extra={},
)
@legacy_base_router.get("/models/", include_in_schema=False)
async def models() -> ModelsResponse:
    """
    Get a list of available models.
    """
    mapping = settings.get_model_name_to_llm_key()
    just_labels = {k: v["label"] for k, v in mapping.items() if "anthropic" not in k.lower()}

    return ModelsResponse(models=just_labels)


@legacy_base_router.post(
    "/tasks",
    tags=["agent"],
    response_model=CreateTaskResponse,
    openapi_extra={
        "x-fern-sdk-method-name": "run_task_v1",
    },
)
@legacy_base_router.post(
    "/tasks/",
    response_model=CreateTaskResponse,
    include_in_schema=False,
)
async def run_task_v1(
    request: Request,
    background_tasks: BackgroundTasks,
    task: TaskRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
    x_max_steps_override: Annotated[int | None, Header()] = None,
    x_user_agent: Annotated[str | None, Header()] = None,
) -> CreateTaskResponse:
    analytics.capture("skyvern-oss-agent-task-create", data={"url": task.url})
    await PermissionCheckerFactory.get_instance().check(current_org, browser_session_id=task.browser_session_id)
    await app.RATE_LIMITER.rate_limit_submit_run(current_org.organization_id)

    created_task = await task_v1_service.run_task(
        task=task,
        organization=current_org,
        x_max_steps_override=x_max_steps_override,
        x_api_key=x_api_key,
        request=request,
        background_tasks=background_tasks,
    )
    return CreateTaskResponse(task_id=created_task.task_id)


@legacy_base_router.get(
    "/tasks/{task_id}",
    tags=["agent"],
    response_model=TaskResponse,
    openapi_extra={
        "x-fern-sdk-method-name": "get_task_v1",
    },
)
@legacy_base_router.get("/tasks/{task_id}/", response_model=TaskResponse, include_in_schema=False)
async def get_task_v1(
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> TaskResponse:
    analytics.capture("skyvern-oss-agent-task-get")
    return await task_v1_service.get_task_v1_response(task_id=task_id, organization_id=current_org.organization_id)


@legacy_base_router.post(
    "/tasks/{task_id}/cancel",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "cancel_task",
    },
)
@legacy_base_router.post("/tasks/{task_id}/cancel/", include_in_schema=False)
async def cancel_task(
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    analytics.capture("skyvern-oss-agent-task-get")
    task_obj = await app.DATABASE.get_task(task_id, organization_id=current_org.organization_id)
    if not task_obj:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Task not found {task_id}",
        )
    task = await app.agent.update_task(task_obj, status=TaskStatus.canceled)
    # retry the webhook
    await app.agent.execute_task_webhook(task=task, api_key=x_api_key)


async def _cancel_workflow_run(workflow_run_id: str, organization_id: str, x_api_key: str | None = None) -> None:
    workflow_run = await app.DATABASE.get_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )

    if not workflow_run:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Workflow run not found {workflow_run_id}",
        )

    if workflow_run.browser_session_id:
        await app.PERSISTENT_SESSIONS_MANAGER.release_browser_session(workflow_run.browser_session_id, organization_id)

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
    await app.WORKFLOW_SERVICE.execute_workflow_webhook(workflow_run, api_key=x_api_key)


async def _continue_workflow_run(workflow_run_id: str, organization_id: str) -> None:
    workflow_run = await app.DATABASE.get_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
        status=WorkflowRunStatus.paused,
    )

    if not workflow_run:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Workflow run not found {workflow_run_id}",
        )

    await app.WORKFLOW_SERVICE.mark_workflow_run_as_running(workflow_run_id)


@legacy_base_router.post(
    "/workflows/runs/{workflow_run_id}/cancel",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "cancel_workflow_run",
    },
)
@legacy_base_router.post("/workflows/runs/{workflow_run_id}/cancel/", include_in_schema=False)
async def cancel_workflow_run(
    workflow_run_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    await _cancel_workflow_run(workflow_run_id, current_org.organization_id, x_api_key)


@base_router.post(
    "/workflows/runs/{workflow_run_id}/continue",
    include_in_schema=False,
)
@base_router.post("/workflows/runs/{workflow_run_id}/continue/", include_in_schema=False)
async def continue_workflow_run(
    workflow_run_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> None:
    await _continue_workflow_run(workflow_run_id, current_org.organization_id)


@legacy_base_router.post(
    "/runs/{browser_session_id}/workflow_run/{workflow_run_id}/cancel/",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "cancel_workflow_run",
    },
)
@legacy_base_router.post("/runs/{browser_session_id}/workflow_run/{workflow_run_id}/cancel/", include_in_schema=False)
async def cancel_persistent_browser_session_workflow_run(
    workflow_run_id: str,
    browser_session_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    await _cancel_workflow_run(workflow_run_id, current_org.organization_id, x_api_key)


@legacy_base_router.post(
    "/tasks/{task_id}/retry_webhook",
    tags=["agent"],
    response_model=TaskResponse,
    openapi_extra={
        "x-fern-sdk-method-name": "retry_webhook",
    },
)
@legacy_base_router.post(
    "/tasks/{task_id}/retry_webhook/",
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
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Task not found {task_id}",
        )

    # get latest step
    latest_step = await app.DATABASE.get_latest_step(task_id, organization_id=current_org.organization_id)
    if not latest_step:
        return await app.agent.build_task_response(task=task_obj)

    # retry the webhook
    await app.agent.execute_task_webhook(task=task_obj, api_key=x_api_key)

    return await app.agent.build_task_response(task=task_obj, last_step=latest_step)


@legacy_base_router.get(
    "/tasks",
    tags=["agent"],
    response_model=list[Task],
    openapi_extra={
        "x-fern-sdk-method-name": "get_tasks",
    },
)
@legacy_base_router.get(
    "/tasks/",
    response_model=list[Task],
    include_in_schema=False,
)
async def get_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    task_status: Annotated[list[TaskStatus] | None, Query()] = None,
    workflow_run_id: Annotated[str | None, Query()] = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    only_standalone_tasks: bool = Query(False),
    application: Annotated[str | None, Query()] = None,
    sort: OrderBy = Query(OrderBy.created_at),
    order: SortDirection = Query(SortDirection.desc),
) -> Response:
    """
    Get all tasks.
    :param page: Starting page, defaults to 1
    :param page_size: Page size, defaults to 10
    :param task_status: Task status filter
    :param workflow_run_id: Workflow run id filter
    :param only_standalone_tasks: Only standalone tasks, tasks which are part of a workflow run will be filtered out
    :param order: Direction to sort by, ascending or descending
    :param sort: Column to sort by, created_at or modified_at
    :return: List of tasks with pagination without steps populated. Steps can be populated by calling the
        get_agent_task endpoint.
    """
    analytics.capture("skyvern-oss-agent-tasks-get")
    if only_standalone_tasks and workflow_run_id:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="only_standalone_tasks and workflow_run_id cannot be used together",
        )
    tasks = await app.DATABASE.get_tasks(
        page,
        page_size,
        task_status=task_status,
        workflow_run_id=workflow_run_id,
        organization_id=current_org.organization_id,
        only_standalone_tasks=only_standalone_tasks,
        order=order,
        order_by_column=sort,
        application=application,
    )
    return ORJSONResponse([(await app.agent.build_task_response(task=task)).model_dump() for task in tasks])


@legacy_base_router.get(
    "/runs",
    tags=["agent"],
    response_model=list[WorkflowRun | Task],
    openapi_extra={
        "x-fern-sdk-method-name": "get_runs",
    },
)
@legacy_base_router.get(
    "/runs/",
    response_model=list[WorkflowRun | Task],
    include_in_schema=False,
)
async def get_runs(
    current_org: Organization = Depends(org_auth_service.get_current_org),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    status: Annotated[list[WorkflowRunStatus] | None, Query()] = None,
    search_key: str | None = Query(
        None,
        description="Search runs by parameter key, parameter description, or run parameter value.",
    ),
) -> Response:
    analytics.capture("skyvern-oss-agent-runs-get")

    # temporary limit to 100 runs
    if page > 10:
        return []

    runs = await app.DATABASE.get_all_runs(
        current_org.organization_id, page=page, page_size=page_size, status=status, search_key=search_key
    )
    return ORJSONResponse([run.model_dump() for run in runs])


@legacy_base_router.get(
    "/tasks/{task_id}/steps",
    tags=["agent"],
    response_model=list[Step],
    openapi_extra={
        "x-fern-sdk-method-name": "get_steps",
    },
)
@legacy_base_router.get(
    "/tasks/{task_id}/steps/",
    response_model=list[Step],
    include_in_schema=False,
)
async def get_steps(
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


@legacy_base_router.get(
    "/{entity_type}/{entity_id}/artifacts",
    tags=["agent"],
    response_model=list[Artifact],
    openapi_extra={
        "x-fern-sdk-method-name": "get_artifacts",
    },
)
@legacy_base_router.get(
    "/{entity_type}/{entity_id}/artifacts/",
    response_model=list[Artifact],
    include_in_schema=False,
)
async def get_artifacts(
    entity_type: EntityType,
    entity_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Response:
    """
    Get all artifacts for an entity (step, task, workflow_run).

    Args:
        entity_type: Type of entity to fetch artifacts for
        entity_id: ID of the entity
        current_org: Current organization from auth

    Returns:
        List of artifacts for the entity

    Raises:
        HTTPException: If entity is not supported
    """

    if entity_type not in entity_type_to_param:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid entity_type: {entity_type}",
        )

    analytics.capture("skyvern-oss-agent-entity-artifacts-get")
    params = {
        entity_type_to_param[entity_type]: entity_id,
    }
    artifacts = await app.DATABASE.get_artifacts_by_entity_id(organization_id=current_org.organization_id, **params)  # type: ignore

    signed_urls = await app.ARTIFACT_MANAGER.get_share_links(artifacts)
    if signed_urls and len(signed_urls) == len(artifacts):
        for i, artifact in enumerate(artifacts):
            artifact.signed_url = signed_urls[i]

    return ORJSONResponse([artifact.model_dump() for artifact in artifacts])


@legacy_base_router.get(
    "/tasks/{task_id}/steps/{step_id}/artifacts",
    tags=["agent"],
    response_model=list[Artifact],
    openapi_extra={
        "x-fern-sdk-method-name": "get_step_artifacts",
    },
)
@legacy_base_router.get(
    "/tasks/{task_id}/steps/{step_id}/artifacts/",
    response_model=list[Artifact],
    include_in_schema=False,
)
async def get_step_artifacts(
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
    signed_urls = await app.ARTIFACT_MANAGER.get_share_links(artifacts)
    if signed_urls and len(signed_urls) == len(artifacts):
        for i, artifact in enumerate(artifacts):
            artifact.signed_url = signed_urls[i]
    return ORJSONResponse([artifact.model_dump() for artifact in artifacts])


@legacy_base_router.get(
    "/tasks/{task_id}/actions",
    response_model=list[Action],
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_actions",
    },
)
@legacy_base_router.get(
    "/tasks/{task_id}/actions/",
    response_model=list[Action],
    include_in_schema=False,
)
async def get_actions(
    task_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[Action]:
    analytics.capture("skyvern-oss-agent-task-actions-get")
    actions = await app.DATABASE.get_task_actions(task_id, organization_id=current_org.organization_id)
    return actions


@legacy_base_router.post(
    "/workflows/{workflow_id}/run",
    response_model=RunWorkflowResponse,
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "run_workflow_legacy",
    },
)
@legacy_base_router.post(
    "/workflows/{workflow_id}/run/",
    response_model=RunWorkflowResponse,
    include_in_schema=False,
)
async def run_workflow_legacy(
    request: Request,
    background_tasks: BackgroundTasks,
    workflow_id: str,  # this is the workflow_permanent_id internally
    workflow_request: WorkflowRequestBody,
    version: int | None = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    template: bool = Query(False),
    x_api_key: Annotated[str | None, Header()] = None,
    x_max_steps_override: Annotated[int | None, Header()] = None,
    x_user_agent: Annotated[str | None, Header()] = None,
) -> RunWorkflowResponse:
    analytics.capture("skyvern-oss-agent-workflow-execute")
    context = skyvern_context.ensure_context()
    request_id = context.request_id
    await PermissionCheckerFactory.get_instance().check(
        current_org,
        browser_session_id=workflow_request.browser_session_id,
    )
    await app.RATE_LIMITER.rate_limit_submit_run(current_org.organization_id)

    try:
        workflow_run = await workflow_service.run_workflow(
            workflow_id=workflow_id,
            organization=current_org,
            workflow_request=workflow_request,
            template=template,
            version=version,
            max_steps=x_max_steps_override,
            api_key=x_api_key,
            request_id=request_id,
            request=request,
            background_tasks=background_tasks,
        )
    except MissingBrowserAddressError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return RunWorkflowResponse(
        workflow_id=workflow_id,
        workflow_run_id=workflow_run.workflow_run_id,
    )


@legacy_base_router.get(
    "/workflows/runs",
    response_model=list[WorkflowRun],
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_workflow_runs",
    },
)
@legacy_base_router.get(
    "/workflows/runs/",
    response_model=list[WorkflowRun],
    include_in_schema=False,
)
async def get_workflow_runs(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    status: Annotated[list[WorkflowRunStatus] | None, Query()] = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[WorkflowRun]:
    analytics.capture("skyvern-oss-agent-workflow-runs-get")
    return await app.WORKFLOW_SERVICE.get_workflow_runs(
        organization_id=current_org.organization_id,
        page=page,
        page_size=page_size,
        status=status,
    )


@legacy_base_router.get(
    "/workflows/{workflow_id}/runs",
    response_model=list[WorkflowRun],
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_workflow_runs_by_id",
    },
)
@legacy_base_router.get(
    "/workflows/{workflow_id}/runs/",
    response_model=list[WorkflowRun],
    include_in_schema=False,
)
async def get_workflow_runs_by_id(
    workflow_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    status: Annotated[list[WorkflowRunStatus] | None, Query()] = None,
    search_key: str | None = Query(
        None,
        description="Search runs by parameter key, parameter description, or run parameter value.",
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[WorkflowRun]:
    """
    Get workflow runs for a specific workflow permanent id.
    """
    analytics.capture("skyvern-oss-agent-workflow-runs-get")
    return await app.WORKFLOW_SERVICE.get_workflow_runs_for_workflow_permanent_id(
        workflow_permanent_id=workflow_id,
        organization_id=current_org.organization_id,
        page=page,
        page_size=page_size,
        status=status,
        search_key=search_key,
    )


@legacy_base_router.get(
    "/workflows/{workflow_id}/runs/{workflow_run_id}",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_workflow_run_with_workflow_id",
    },
)
@legacy_base_router.get(
    "/workflows/{workflow_id}/runs/{workflow_run_id}/",
    include_in_schema=False,
)
async def get_workflow_run_with_workflow_id(
    workflow_id: str,
    workflow_run_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> dict[str, Any]:
    analytics.capture("skyvern-oss-agent-workflow-run-get")
    workflow_run_status_response = await app.WORKFLOW_SERVICE.build_workflow_run_status_response(
        workflow_permanent_id=workflow_id,
        workflow_run_id=workflow_run_id,
        organization_id=current_org.organization_id,
        include_cost=True,
    )
    return_dict = workflow_run_status_response.model_dump(by_alias=True)

    browser_session = await app.DATABASE.get_persistent_browser_session_by_runnable_id(
        runnable_id=workflow_run_id,
        organization_id=current_org.organization_id,
    )

    browser_session_id = browser_session.persistent_browser_session_id if browser_session else None

    return_dict["browser_session_id"] = browser_session_id or return_dict.get("browser_session_id")

    return return_dict


@base_router.get(
    "/workflows/runs/{workflow_run_id}",
    include_in_schema=False,
)
@base_router.get(
    "/workflows/runs/{workflow_run_id}/",
    include_in_schema=False,
)
async def get_workflow_and_run_from_workflow_run_id(
    workflow_run_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowRunWithWorkflowResponse:
    workflow = await app.WORKFLOW_SERVICE.get_workflow_by_workflow_run_id(
        workflow_run_id=workflow_run_id,
        organization_id=current_org.organization_id,
    )

    if not workflow:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Workflow run not found {workflow_run_id}",
        )

    workflow_run_status_api_response = await get_workflow_run_with_workflow_id(
        workflow_id=workflow.workflow_permanent_id,
        workflow_run_id=workflow_run_id,
        current_org=current_org,
    )

    workflow_run_status_api_response["workflow"] = workflow

    response = WorkflowRunWithWorkflowResponse.model_validate(workflow_run_status_api_response)

    return response


@legacy_base_router.get(
    "/workflows/{workflow_id}/runs/{workflow_run_id}/timeline",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_workflow_run_timeline",
    },
)
@legacy_base_router.get(
    "/workflows/{workflow_id}/runs/{workflow_run_id}/timeline/",
    include_in_schema=False,
)
async def get_workflow_run_timeline(
    workflow_run_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[WorkflowRunTimeline]:
    return await _flatten_workflow_run_timeline(current_org.organization_id, workflow_run_id)


@legacy_base_router.get(
    "/workflows/runs/{workflow_run_id}",
    response_model=WorkflowRunResponseBase,
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_workflow_run",
    },
)
@legacy_base_router.get(
    "/workflows/runs/{workflow_run_id}/",
    response_model=WorkflowRunResponseBase,
    include_in_schema=False,
)
async def get_workflow_run(
    workflow_run_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> WorkflowRunResponseBase:
    analytics.capture("skyvern-oss-agent-workflow-run-get")
    return await app.WORKFLOW_SERVICE.build_workflow_run_status_response_by_workflow_id(
        workflow_run_id=workflow_run_id,
        organization_id=current_org.organization_id,
    )


@legacy_base_router.get(
    "/workflows",
    response_model=list[Workflow],
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_workflows",
    },
)
@legacy_base_router.get(
    "/workflows/",
    response_model=list[Workflow],
    include_in_schema=False,
)
@base_router.get(
    "/workflows",
    response_model=list[Workflow],
    tags=["Workflows"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_workflows",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": GET_WORKFLOWS_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": GET_WORKFLOWS_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
)
@base_router.get("/workflows/", response_model=list[Workflow], include_in_schema=False)
async def get_workflows(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    only_saved_tasks: bool = Query(False),
    only_workflows: bool = Query(False),
    only_templates: bool = Query(False),
    search_key: str | None = Query(
        None,
        description="Unified search across workflow title, folder name, and parameter metadata (key, description, default_value).",
    ),
    title: str = Query("", deprecated=True, description="Deprecated: use search_key instead."),
    folder_id: str | None = Query(None, description="Filter workflows by folder ID"),
    status: Annotated[list[WorkflowStatus] | None, Query()] = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    template: bool = Query(False),
) -> list[Workflow]:
    """
    Get all workflows with the latest version for the organization.

    Search semantics:
    - If `search_key` is provided, its value is used as a unified search term for
      `workflows.title`, `folders.title`, and workflow parameter metadata (key, description, and default_value for
      `WorkflowParameterModel`).
    - Falls back to deprecated `title` (title-only search) if `search_key` is not provided.
    - Parameter metadata search excludes soft-deleted parameter rows across all parameter tables.
    """
    analytics.capture("skyvern-oss-agent-workflows-get")

    # Determine the effective search term: prioritize search_key, fallback to title
    effective_search = search_key or (title if title else None)

    # Default to published and draft if no status filter provided
    effective_statuses = status if status else [WorkflowStatus.published, WorkflowStatus.draft]

    if template:
        global_workflows_permanent_ids = await app.STORAGE.retrieve_global_workflows()
        if not global_workflows_permanent_ids:
            return []
        workflows = await app.WORKFLOW_SERVICE.get_workflows_by_permanent_ids(
            workflow_permanent_ids=global_workflows_permanent_ids,
            page=page,
            page_size=page_size,
            search_key=effective_search or "",
            statuses=effective_statuses,
        )
        return workflows

    if only_saved_tasks and only_workflows:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="only_saved_tasks and only_workflows cannot be used together",
        )

    return await app.WORKFLOW_SERVICE.get_workflows_by_organization_id(
        organization_id=current_org.organization_id,
        page=page,
        page_size=page_size,
        only_saved_tasks=only_saved_tasks,
        only_workflows=only_workflows,
        only_templates=only_templates,
        search_key=effective_search,
        folder_id=folder_id,
        statuses=effective_statuses,
    )


@base_router.put(
    "/workflows/{workflow_permanent_id}/template",
    tags=["Workflows"],
    include_in_schema=False,
)
async def set_workflow_template_status(
    workflow_permanent_id: str,
    is_template: bool = Query(...),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> dict:
    """
    Set or unset a workflow as a template.

    Template status is stored at the workflow_permanent_id level (not per-version),
    meaning all versions of a workflow share the same template status.
    """
    return await app.WORKFLOW_SERVICE.set_template_status(
        organization_id=current_org.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        is_template=is_template,
    )


@legacy_base_router.get(
    "/workflows/templates",
    response_model=list[Workflow],
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_workflow_templates",
    },
)
@legacy_base_router.get(
    "/workflows/templates/",
    response_model=list[Workflow],
    include_in_schema=False,
)
async def get_workflow_templates() -> list[Workflow]:
    global_workflows_permanent_ids = await app.STORAGE.retrieve_global_workflows()

    if not global_workflows_permanent_ids:
        return []

    workflows = await app.WORKFLOW_SERVICE.get_workflows_by_permanent_ids(
        workflow_permanent_ids=global_workflows_permanent_ids,
        statuses=[WorkflowStatus.published, WorkflowStatus.draft],
    )

    return workflows


@legacy_base_router.get(
    "/workflows/{workflow_permanent_id}",
    response_model=Workflow,
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_workflow",
    },
)
@legacy_base_router.get("/workflows/{workflow_permanent_id}/", response_model=Workflow, include_in_schema=False)
async def get_workflow(
    workflow_permanent_id: str,
    version: int | None = None,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    template: bool = Query(False),
) -> Workflow:
    analytics.capture("skyvern-oss-agent-workflows-get")
    if template:
        if workflow_permanent_id not in await app.STORAGE.retrieve_global_workflows():
            raise InvalidTemplateWorkflowPermanentId(workflow_permanent_id=workflow_permanent_id)

    return await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=None if template else current_org.organization_id,
        version=version,
    )


@legacy_base_router.get(
    "/workflows/{workflow_permanent_id}/versions",
    response_model=list[Workflow],
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_workflow_versions",
    },
)
@legacy_base_router.get(
    "/workflows/{workflow_permanent_id}/versions/", response_model=list[Workflow], include_in_schema=False
)
async def get_workflow_versions(
    workflow_permanent_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
    template: bool = Query(False),
) -> list[Workflow]:
    """
    Get all versions of a workflow by its permanent ID.
    """
    analytics.capture("skyvern-oss-agent-workflow-versions-get")
    if template:
        if workflow_permanent_id not in await app.STORAGE.retrieve_global_workflows():
            raise InvalidTemplateWorkflowPermanentId(workflow_permanent_id=workflow_permanent_id)

    return await app.WORKFLOW_SERVICE.get_workflow_versions_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=None if template else current_org.organization_id,
    )


@legacy_base_router.post(
    "/suggest/{ai_suggestion_type}",
    include_in_schema=False,
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "suggest",
    },
)
@legacy_base_router.post("/suggest/{ai_suggestion_type}/", include_in_schema=False)
async def suggest(
    ai_suggestion_type: AISuggestionType,
    data: AISuggestionRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> AISuggestionBase:
    llm_prompt = ""

    if ai_suggestion_type == AISuggestionType.DATA_SCHEMA:
        llm_prompt = prompt_engine.load_prompt("suggest-data-schema", input=data.input, additional_context=data.context)

    try:
        new_ai_suggestion = await app.DATABASE.create_ai_suggestion(
            organization_id=current_org.organization_id,
            ai_suggestion_type=ai_suggestion_type,
        )

        llm_response = await app.LLM_API_HANDLER(
            prompt=llm_prompt,
            ai_suggestion=new_ai_suggestion,
            prompt_name="suggest-data-schema",
            organization_id=current_org.organization_id,
        )
        parsed_ai_suggestion = AISuggestionBase.model_validate(llm_response)

        return parsed_ai_suggestion

    except LLMProviderError:
        LOG.error("Failed to suggest data schema", exc_info=True)
        raise HTTPException(status_code=400, detail="Failed to suggest data schema. Please try again later.")


@legacy_base_router.post(
    "/generate/task",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "generate_task",
    },
)
@legacy_base_router.post("/generate/task/", include_in_schema=False)
async def generate_task(
    data: GenerateTaskRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> TaskGeneration:
    analytics.capture("skyvern-oss-agent-generate-task")
    return await task_v1_service.generate_task(
        user_prompt=data.prompt,
        organization=current_org,
    )


@legacy_base_router.put(
    "/organizations",
    tags=["server"],
    openapi_extra={
        "x-fern-sdk-method-name": "update_organization",
    },
)
@legacy_base_router.put(
    "/organizations",
    include_in_schema=False,
)
async def update_organization(
    org_update: OrganizationUpdate,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> Organization:
    return await app.DATABASE.update_organization(
        current_org.organization_id,
        max_steps_per_run=org_update.max_steps_per_run,
    )


@legacy_base_router.get(
    "/organizations",
    tags=["server"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_organizations",
    },
)
@legacy_base_router.get(
    "/organizations/",
    include_in_schema=False,
)
async def get_organizations(
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> GetOrganizationsResponse:
    return GetOrganizationsResponse(organizations=[current_org])


@legacy_base_router.get(
    "/organizations/{organization_id}/apikeys/",
    tags=["server"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_api_keys",
    },
)
@legacy_base_router.get(
    "/organizations/{organization_id}/apikeys",
    include_in_schema=False,
)
async def get_api_keys(
    organization_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> GetOrganizationAPIKeysResponse:
    if organization_id != current_org.organization_id:
        raise HTTPException(status_code=403, detail="You do not have permission to access this organization")
    api_keys = []
    org_auth_token = await app.DATABASE.get_valid_org_auth_token(organization_id, OrganizationAuthTokenType.api.value)
    if org_auth_token:
        api_keys.append(org_auth_token)
    return GetOrganizationAPIKeysResponse(api_keys=api_keys)


@base_router.post(
    "/upload_file",
    tags=["Files"],
    openapi_extra={
        "x-fern-sdk-method-name": "upload_file",
    },
    include_in_schema=True,
    response_model=UploadFileResponse,
)
@base_router.post("/upload_file/", include_in_schema=False)
@legacy_base_router.post("/upload_file", include_in_schema=False)
@legacy_base_router.post("/upload_file/", include_in_schema=False)
async def upload_file(
    file: UploadFile = Depends(_validate_file_size),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> UploadFileResponse:
    uris = await app.STORAGE.save_legacy_file(
        organization_id=current_org.organization_id, filename=file.filename, fileObj=file.file
    )
    if not uris:
        raise HTTPException(status_code=500, detail="Failed to upload file to S3.")
    presigned_url, uploaded_s3_uri = uris
    return UploadFileResponse(s3_uri=uploaded_s3_uri, presigned_url=presigned_url)


@legacy_v2_router.post(
    "/tasks",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "run_task_v2",
    },
)
@legacy_v2_router.post(
    "/tasks/",
    include_in_schema=False,
)
async def run_task_v2(
    request: Request,
    background_tasks: BackgroundTasks,
    data: TaskV2Request,
    organization: Organization = Depends(org_auth_service.get_current_org),
    x_max_iterations_override: Annotated[int | str | None, Header()] = None,
    x_max_steps_override: Annotated[int | str | None, Header()] = None,
) -> dict[str, Any]:
    if x_max_iterations_override or x_max_steps_override:
        LOG.info(
            "Overriding max steps for task v2",
            max_iterations_override=x_max_iterations_override,
            max_steps_override=x_max_steps_override,
        )
    await PermissionCheckerFactory.get_instance().check(organization, browser_session_id=data.browser_session_id)
    await app.RATE_LIMITER.rate_limit_submit_run(organization.organization_id)

    try:
        task_v2 = await task_v2_service.initialize_task_v2(
            organization=organization,
            user_prompt=data.user_prompt,
            user_url=str(data.url) if data.url else None,
            totp_identifier=data.totp_identifier,
            totp_verification_url=data.totp_verification_url,
            webhook_callback_url=data.webhook_callback_url,
            proxy_location=data.proxy_location,
            publish_workflow=data.publish_workflow,
            create_task_run=True,
            extracted_information_schema=data.extracted_information_schema,
            error_code_mapping=data.error_code_mapping,
            max_screenshot_scrolling_times=data.max_screenshot_scrolls,
            browser_session_id=data.browser_session_id,
            extra_http_headers=data.extra_http_headers,
            browser_address=data.browser_address,
        )
    except MissingBrowserAddressError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except LLMProviderError:
        LOG.error("LLM failure to initialize task v2", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Skyvern LLM failure to initialize task v2. Please try again later."
        )
    analytics.capture("skyvern-oss-agent-task-v2", data={"url": task_v2.url})
    await AsyncExecutorFactory.get_executor().execute_task_v2(
        request=request,
        background_tasks=background_tasks,
        organization_id=organization.organization_id,
        task_v2_id=task_v2.observer_cruise_id,
        max_steps_override=x_max_steps_override or x_max_iterations_override,
        browser_session_id=data.browser_session_id,
    )
    return task_v2.model_dump(by_alias=True)


@legacy_v2_router.get(
    "/tasks/{task_id}",
    tags=["agent"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_task_v2",
    },
)
@legacy_v2_router.get(
    "/tasks/{task_id}/",
    include_in_schema=False,
)
async def get_task_v2(
    task_id: str,
    organization: Organization = Depends(org_auth_service.get_current_org),
) -> dict[str, Any]:
    task_v2 = await task_v2_service.get_task_v2(task_id, organization.organization_id)
    if not task_v2:
        raise HTTPException(status_code=404, detail=f"Task v2 {task_id} not found")
    return task_v2.model_dump(by_alias=True)


async def _flatten_workflow_run_timeline_recursive(
    timeline: WorkflowRunTimeline,
    organization_id: str,
) -> list[WorkflowRunTimeline]:
    """
    Recursively flatten a timeline item and its children, handling TaskV2 blocks.

    TaskV2 blocks are replaced with their internal workflow run blocks.
    Other blocks (like ForLoop) are kept with their children recursively processed.
    """
    result = []

    # Check if this is a TaskV2 block that needs to be flattened
    if timeline.block and timeline.block.block_type == BlockType.TaskV2:
        if timeline.block.block_workflow_run_id:
            # Recursively flatten the TaskV2's internal workflow run
            nested_timeline = await _flatten_workflow_run_timeline(
                organization_id=organization_id,
                workflow_run_id=timeline.block.block_workflow_run_id,
            )
            result.extend(nested_timeline)
        else:
            LOG.warning(
                "Block workflow run id is not set for task_v2 block",
                workflow_run_block_id=timeline.block.workflow_run_block_id if timeline.block else None,
                organization_id=organization_id,
            )
            result.append(timeline)
    else:
        # For non-TaskV2 blocks, process children recursively to handle nested TaskV2 blocks
        new_children = []
        if timeline.children:
            for child in timeline.children:
                child_results = await _flatten_workflow_run_timeline_recursive(
                    timeline=child,
                    organization_id=organization_id,
                )
                new_children.extend(child_results)

        # Create a new timeline with processed children
        processed_timeline = WorkflowRunTimeline(
            type=timeline.type,
            block=timeline.block,
            thought=timeline.thought,
            children=new_children,
            created_at=timeline.created_at,
            modified_at=timeline.modified_at,
        )
        result.append(processed_timeline)

    return result


async def _flatten_workflow_run_timeline(organization_id: str, workflow_run_id: str) -> list[WorkflowRunTimeline]:
    """
    Get the timeline workflow runs including the nested workflow runs in a flattened list
    """

    # get task v2 by workflow run id
    task_v2_obj = await app.DATABASE.get_task_v2_by_workflow_run_id(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )
    # get all the workflow run blocks
    workflow_run_block_timeline = await app.WORKFLOW_SERVICE.get_workflow_run_timeline(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )

    # Recursively flatten the timeline, handling TaskV2 blocks at any nesting level
    final_workflow_run_block_timeline = []
    for timeline in workflow_run_block_timeline:
        if not timeline.block:
            continue

        flattened = await _flatten_workflow_run_timeline_recursive(
            timeline=timeline,
            organization_id=organization_id,
        )
        final_workflow_run_block_timeline.extend(flattened)

    if task_v2_obj and task_v2_obj.observer_cruise_id:
        thought_timeline = await task_v2_service.get_thought_timelines(
            task_v2_id=task_v2_obj.observer_cruise_id,
            organization_id=organization_id,
        )
        final_workflow_run_block_timeline.extend(thought_timeline)
    final_workflow_run_block_timeline.sort(key=lambda x: x.created_at, reverse=True)
    return final_workflow_run_block_timeline
