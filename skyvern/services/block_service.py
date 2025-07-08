import structlog

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody, WorkflowRun
from skyvern.schemas.runs import WorkflowRunRequest
from skyvern.services import workflow_service

LOG = structlog.get_logger()


async def ensure_workflow_run(
    organization: Organization,
    template: bool,
    workflow_permanent_id: str,
    workflow_run_request: WorkflowRunRequest,
    x_max_steps_override: int | None = None,
) -> WorkflowRun:
    context = skyvern_context.ensure_context()

    legacy_workflow_request = WorkflowRequestBody(
        data=workflow_run_request.parameters,
        proxy_location=workflow_run_request.proxy_location,
        webhook_callback_url=workflow_run_request.webhook_url,
        totp_identifier=workflow_run_request.totp_identifier,
        totp_verification_url=workflow_run_request.totp_url,
        browser_session_id=workflow_run_request.browser_session_id,
        max_screenshot_scrolls=workflow_run_request.max_screenshot_scrolls,
        extra_http_headers=workflow_run_request.extra_http_headers,
    )

    workflow_run = await workflow_service.prepare_workflow(
        workflow_id=workflow_permanent_id,
        organization=organization,
        workflow_request=legacy_workflow_request,
        template=template,
        version=None,
        max_steps=x_max_steps_override,
        request_id=context.request_id,
    )

    return workflow_run


async def execute_blocks(
    api_key: str,
    block_labels: list[str],
    workflow_run_id: str,
    organization: Organization,
    browser_session_id: str | None = None,
) -> WorkflowRun:
    """
    Runs one or more blocks of a workflow.
    """

    LOG.info(
        "Executing block(s)",
        organization_id=organization.organization_id,
        workflow_run_id=workflow_run_id,
        block_labels=block_labels,
    )

    workflow_run = await app.WORKFLOW_SERVICE.execute_workflow(
        workflow_run_id=workflow_run_id,
        api_key=api_key,
        organization=organization,
        block_labels=block_labels,
        browser_session_id=browser_session_id,
    )

    return workflow_run
