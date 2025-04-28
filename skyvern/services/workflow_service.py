import structlog
from fastapi import BackgroundTasks, Request

from skyvern.forge import app
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.exceptions import InvalidTemplateWorkflowPermanentId
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody, WorkflowRun
from skyvern.schemas.runs import RunType

LOG = structlog.get_logger(__name__)


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
) -> WorkflowRun:
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
    await AsyncExecutorFactory.get_executor().execute_workflow(
        request=request,
        background_tasks=background_tasks,
        organization=organization,
        workflow_id=workflow_run.workflow_id,
        workflow_run_id=workflow_run.workflow_run_id,
        max_steps_override=max_steps,
        browser_session_id=workflow_request.browser_session_id,
        api_key=api_key,
    )
    return workflow_run
