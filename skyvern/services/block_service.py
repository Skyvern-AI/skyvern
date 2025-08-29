import typing as t

import structlog
from fastapi import BackgroundTasks, Request

from skyvern.exceptions import OutputParameterNotFound, WorkflowNotFound
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.executor.factory import AsyncExecutorFactory
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
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
    request: Request,
    background_tasks: BackgroundTasks,
    api_key: str,
    block_labels: list[str],
    workflow_id: str,
    workflow_run_id: str,
    workflow_permanent_id: str,
    organization: Organization,
    user_id: str,
    browser_session_id: str | None = None,
    block_outputs: dict[str, t.Any] | None = None,
) -> None:
    """
    Runs one or more blocks of a workflow.
    """

    workflow = await app.DATABASE.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_id,
        organization_id=organization.organization_id,
    )

    if not workflow:
        raise WorkflowNotFound(workflow_permanent_id=workflow_id)

    block_output_parameters: dict[str, OutputParameter] = {}

    for block_label in block_labels:
        output_parameter = workflow.get_output_parameter(block_label)

        if not output_parameter:
            raise OutputParameterNotFound(block_label=block_label, workflow_permanent_id=workflow_id)

        block_output_parameters[block_label] = output_parameter

    for block_label, output_parameter in block_output_parameters.items():
        await app.DATABASE.create_block_run(
            organization_id=organization.organization_id,
            user_id=user_id,
            block_label=block_label,
            output_parameter_id=output_parameter.output_parameter_id,
            workflow_run_id=workflow_run_id,
        )

    LOG.info(
        "Executing block(s)",
        organization_id=organization.organization_id,
        workflow_run_id=workflow_run_id,
        block_labels=block_labels,
        block_outputs=block_outputs,
    )

    await AsyncExecutorFactory.get_executor().execute_workflow(
        request=request,
        background_tasks=background_tasks,
        organization=organization,
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        workflow_permanent_id=workflow_permanent_id,
        max_steps_override=None,
        browser_session_id=browser_session_id,
        api_key=api_key,
        block_labels=block_labels,
        block_outputs=block_outputs,
    )
