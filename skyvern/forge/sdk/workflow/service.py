import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from skyvern import analytics
from skyvern.config import settings
from skyvern.constants import GET_DOWNLOADED_FILES_TIMEOUT, SAVE_DOWNLOADED_FILES_TIMEOUT
from skyvern.exceptions import (
    FailedToSendWebhook,
    MissingValueForParameter,
    SkyvernException,
    WorkflowNotFound,
    WorkflowRunNotFound,
)
from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_headers
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import ProxyLocation, Task
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock, WorkflowRunTimeline, WorkflowRunTimelineType
from skyvern.forge.sdk.workflow.exceptions import (
    ContextParameterSourceNotDefined,
    InvalidWaitBlockTime,
    InvalidWorkflowDefinition,
    WorkflowDefinitionHasDuplicateParameterKeys,
    WorkflowDefinitionHasReservedParameterKeys,
    WorkflowParameterMissingRequiredValue,
)
from skyvern.forge.sdk.workflow.models.block import (
    ActionBlock,
    BlockStatus,
    BlockType,
    BlockTypeVar,
    CodeBlock,
    DownloadToS3Block,
    ExtractionBlock,
    FileDownloadBlock,
    FileParserBlock,
    ForLoopBlock,
    LoginBlock,
    NavigationBlock,
    PDFParserBlock,
    SendEmailBlock,
    TaskBlock,
    TaskV2Block,
    TextPromptBlock,
    UploadToS3Block,
    UrlBlock,
    ValidationBlock,
    WaitBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    AWSSecretParameter,
    BitwardenCreditCardDataParameter,
    BitwardenLoginCredentialParameter,
    BitwardenSensitiveInformationParameter,
    ContextParameter,
    CredentialParameter,
    OutputParameter,
    Parameter,
    ParameterType,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowRequestBody,
    WorkflowRun,
    WorkflowRunOutputParameter,
    WorkflowRunParameter,
    WorkflowRunResponse,
    WorkflowRunStatus,
    WorkflowStatus,
)
from skyvern.forge.sdk.workflow.models.yaml import (
    BLOCK_YAML_TYPES,
    ForLoopBlockYAML,
    WorkflowCreateYAMLRequest,
    WorkflowDefinitionYAML,
)
from skyvern.webeye.browser_factory import BrowserState

LOG = structlog.get_logger()


class WorkflowService:
    async def setup_workflow_run(
        self,
        request_id: str | None,
        workflow_request: WorkflowRequestBody,
        workflow_permanent_id: str,
        organization_id: str,
        is_template_workflow: bool = False,
        version: int | None = None,
        max_steps_override: int | None = None,
        parent_workflow_run_id: str | None = None,
    ) -> WorkflowRun:
        """
        Create a workflow run and its parameters. Validate the workflow and the organization. If there are missing
        parameters with no default value, mark the workflow run as failed.
        :param request_id: The request id for the workflow run.
        :param workflow_request: The request body for the workflow run, containing the parameters and the config.
        :param workflow_id: The workflow id to run.
        :param organization_id: The organization id for the workflow.
        :param max_steps_override: The max steps override for the workflow run, if any.
        :return: The created workflow run.
        """
        # Validate the workflow and the organization
        workflow = await self.get_workflow_by_permanent_id(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=None if is_template_workflow else organization_id,
            version=version,
        )
        if workflow is None:
            LOG.error(f"Workflow {workflow_permanent_id} not found", workflow_version=version)
            raise WorkflowNotFound(workflow_permanent_id=workflow_permanent_id, version=version)
        workflow_id = workflow.workflow_id
        if workflow_request.proxy_location is None and workflow.proxy_location is not None:
            workflow_request.proxy_location = workflow.proxy_location
        if workflow_request.webhook_callback_url is None and workflow.webhook_callback_url is not None:
            workflow_request.webhook_callback_url = workflow.webhook_callback_url
        # Create the workflow run and set skyvern context
        workflow_run = await self.create_workflow_run(
            workflow_request=workflow_request,
            workflow_permanent_id=workflow_permanent_id,
            workflow_id=workflow_id,
            organization_id=organization_id,
            parent_workflow_run_id=parent_workflow_run_id,
        )
        LOG.info(
            f"Created workflow run {workflow_run.workflow_run_id} for workflow {workflow.workflow_id}",
            request_id=request_id,
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_id=workflow.workflow_id,
            organization_id=workflow.organization_id,
            proxy_location=workflow_request.proxy_location,
            webhook_callback_url=workflow_request.webhook_callback_url,
        )
        skyvern_context.set(
            SkyvernContext(
                organization_id=organization_id,
                request_id=request_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
                max_steps_override=max_steps_override,
            )
        )

        # Create all the workflow run parameters, AWSSecretParameter won't have workflow run parameters created.
        all_workflow_parameters = await self.get_workflow_parameters(workflow_id=workflow.workflow_id)
        try:
            for workflow_parameter in all_workflow_parameters:
                if workflow_request.data and workflow_parameter.key in workflow_request.data:
                    request_body_value = workflow_request.data[workflow_parameter.key]
                    await self.create_workflow_run_parameter(
                        workflow_run_id=workflow_run.workflow_run_id,
                        workflow_parameter=workflow_parameter,
                        value=request_body_value,
                    )
                elif workflow_parameter.default_value is not None:
                    await self.create_workflow_run_parameter(
                        workflow_run_id=workflow_run.workflow_run_id,
                        workflow_parameter=workflow_parameter,
                        value=workflow_parameter.default_value,
                    )
                else:
                    raise MissingValueForParameter(
                        parameter_key=workflow_parameter.key,
                        workflow_id=workflow.workflow_id,
                        workflow_run_id=workflow_run.workflow_run_id,
                    )
        except Exception as e:
            LOG.exception(
                f"Error while setting up workflow run {workflow_run.workflow_run_id}",
                workflow_run_id=workflow_run.workflow_run_id,
            )

            failure_reason = f"Setup workflow failed due to an unexpected exception: {str(e)}"
            if isinstance(e, SkyvernException):
                failure_reason = f"Setup workflow failed due to an SkyvernException({e.__class__.__name__}): {str(e)}"

            await self.mark_workflow_run_as_failed(
                workflow_run_id=workflow_run.workflow_run_id, failure_reason=failure_reason
            )
            raise e

        return workflow_run

    async def execute_workflow(
        self,
        workflow_run_id: str,
        api_key: str,
        organization: Organization,
        browser_session_id: str | None = None,
    ) -> WorkflowRun:
        """Execute a workflow."""
        organization_id = organization.organization_id
        LOG.info(
            "Executing workflow",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            browser_session_id=browser_session_id,
        )
        workflow_run = await self.get_workflow_run(workflow_run_id=workflow_run_id, organization_id=organization_id)
        workflow = await self.get_workflow_by_permanent_id(workflow_permanent_id=workflow_run.workflow_permanent_id)

        # Set workflow run status to running, create workflow run parameters
        await self.mark_workflow_run_as_running(workflow_run_id=workflow_run.workflow_run_id)

        # Get all context parameters from the workflow definition
        context_parameters = [
            parameter
            for parameter in workflow.workflow_definition.parameters
            if isinstance(parameter, ContextParameter)
        ]

        secret_parameters = [
            parameter
            for parameter in workflow.workflow_definition.parameters
            if isinstance(
                parameter,
                (
                    AWSSecretParameter,
                    BitwardenLoginCredentialParameter,
                    BitwardenCreditCardDataParameter,
                    BitwardenSensitiveInformationParameter,
                    CredentialParameter,
                ),
            )
        ]

        # Get all <workflow parameter, workflow run parameter> tuples
        wp_wps_tuples = await self.get_workflow_run_parameter_tuples(workflow_run_id=workflow_run.workflow_run_id)
        workflow_output_parameters = await self.get_workflow_output_parameters(workflow_id=workflow.workflow_id)
        try:
            await app.WORKFLOW_CONTEXT_MANAGER.initialize_workflow_run_context(
                organization,
                workflow_run_id,
                wp_wps_tuples,
                workflow_output_parameters,
                context_parameters,
                secret_parameters,
            )
        except Exception as e:
            LOG.exception(
                f"Error while initializing workflow run context for workflow run {workflow_run.workflow_run_id}",
                workflow_run_id=workflow_run.workflow_run_id,
            )

            exception_message = f"Unexpected error: {str(e)}"
            if isinstance(e, SkyvernException):
                exception_message = f"unexpected SkyvernException({e.__class__.__name__}): {str(e)}"

            failure_reason = f"Failed to initialize workflow run context. failure reason: {exception_message}"
            await self.mark_workflow_run_as_failed(
                workflow_run_id=workflow_run.workflow_run_id, failure_reason=failure_reason
            )
            await self.clean_up_workflow(
                workflow=workflow,
                workflow_run=workflow_run,
                api_key=api_key,
                browser_session_id=browser_session_id,
                close_browser_on_completion=browser_session_id is None,
            )
            return workflow_run

        # Execute workflow blocks
        blocks = workflow.workflow_definition.blocks
        blocks_cnt = len(blocks)
        block_result = None
        for block_idx, block in enumerate(blocks):
            try:
                refreshed_workflow_run = await app.DATABASE.get_workflow_run(
                    workflow_run_id=workflow_run.workflow_run_id,
                    organization_id=organization_id,
                )
                if refreshed_workflow_run and refreshed_workflow_run.status == WorkflowRunStatus.canceled:
                    LOG.info(
                        "Workflow run is canceled, stopping execution inside workflow execution loop",
                        workflow_run_id=workflow_run.workflow_run_id,
                        block_idx=block_idx,
                        block_type=block.block_type,
                        block_label=block.label,
                    )
                    await self.clean_up_workflow(
                        workflow=workflow,
                        workflow_run=workflow_run,
                        api_key=api_key,
                        need_call_webhook=True,
                        close_browser_on_completion=browser_session_id is None,
                        browser_session_id=browser_session_id,
                    )
                    return workflow_run

                if refreshed_workflow_run and refreshed_workflow_run.status == WorkflowRunStatus.timed_out:
                    LOG.info(
                        "Workflow run is timed out, stopping execution inside workflow execution loop",
                        workflow_run_id=workflow_run.workflow_run_id,
                        block_idx=block_idx,
                        block_type=block.block_type,
                        block_label=block.label,
                    )
                    await self.clean_up_workflow(
                        workflow=workflow,
                        workflow_run=workflow_run,
                        api_key=api_key,
                        need_call_webhook=True,
                        close_browser_on_completion=browser_session_id is None,
                        browser_session_id=browser_session_id,
                    )
                    return workflow_run

                parameters = block.get_all_parameters(workflow_run_id)
                await app.WORKFLOW_CONTEXT_MANAGER.register_block_parameters_for_workflow_run(
                    workflow_run_id, parameters, organization
                )
                LOG.info(
                    f"Executing root block {block.block_type} at index {block_idx}/{blocks_cnt - 1} for workflow run {workflow_run_id}",
                    block_type=block.block_type,
                    workflow_run_id=workflow_run.workflow_run_id,
                    block_idx=block_idx,
                    block_type_var=block.block_type,
                    block_label=block.label,
                )
                block_result = await block.execute_safe(
                    workflow_run_id=workflow_run_id,
                    organization_id=organization_id,
                    browser_session_id=browser_session_id,
                )
                if block_result.status == BlockStatus.canceled:
                    LOG.info(
                        f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} was canceled for workflow run {workflow_run_id}, cancelling workflow run",
                        block_type=block.block_type,
                        workflow_run_id=workflow_run.workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_result,
                        block_type_var=block.block_type,
                        block_label=block.label,
                    )
                    await self.mark_workflow_run_as_canceled(workflow_run_id=workflow_run.workflow_run_id)
                    # We're not sending a webhook here because the workflow run is manually marked as canceled.
                    await self.clean_up_workflow(
                        workflow=workflow,
                        workflow_run=workflow_run,
                        api_key=api_key,
                        need_call_webhook=False,
                        close_browser_on_completion=browser_session_id is None,
                        browser_session_id=browser_session_id,
                    )
                    return workflow_run
                elif block_result.status == BlockStatus.failed:
                    LOG.error(
                        f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} failed for workflow run {workflow_run_id}",
                        block_type=block.block_type,
                        workflow_run_id=workflow_run.workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_result,
                        block_type_var=block.block_type,
                        block_label=block.label,
                    )
                    if not block.continue_on_failure:
                        failure_reason = f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} failed. failure reason: {block_result.failure_reason}"
                        await self.mark_workflow_run_as_failed(
                            workflow_run_id=workflow_run.workflow_run_id, failure_reason=failure_reason
                        )
                        await self.clean_up_workflow(
                            workflow=workflow,
                            workflow_run=workflow_run,
                            api_key=api_key,
                            close_browser_on_completion=browser_session_id is None,
                            browser_session_id=browser_session_id,
                        )
                        return workflow_run

                    LOG.warning(
                        f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} failed but will continue executing the workflow run {workflow_run_id}",
                        block_type=block.block_type,
                        workflow_run_id=workflow_run.workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_result,
                        continue_on_failure=block.continue_on_failure,
                        block_type_var=block.block_type,
                        block_label=block.label,
                    )

                elif block_result.status == BlockStatus.terminated:
                    LOG.info(
                        f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} was terminated for workflow run {workflow_run_id}, marking workflow run as terminated",
                        block_type=block.block_type,
                        workflow_run_id=workflow_run.workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_result,
                        block_type_var=block.block_type,
                        block_label=block.label,
                    )

                    if not block.continue_on_failure:
                        failure_reason = f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} terminated. Reason: {block_result.failure_reason}"
                        await self.mark_workflow_run_as_terminated(
                            workflow_run_id=workflow_run.workflow_run_id, failure_reason=failure_reason
                        )
                        await self.clean_up_workflow(
                            workflow=workflow,
                            workflow_run=workflow_run,
                            api_key=api_key,
                            close_browser_on_completion=browser_session_id is None,
                            browser_session_id=browser_session_id,
                        )
                        return workflow_run

                    LOG.warning(
                        f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} was terminated for workflow run {workflow_run_id}, but will continue executing the workflow run",
                        block_type=block.block_type,
                        workflow_run_id=workflow_run.workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_result,
                        continue_on_failure=block.continue_on_failure,
                        block_type_var=block.block_type,
                        block_label=block.label,
                    )

                elif block_result.status == BlockStatus.timed_out:
                    LOG.info(
                        f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} timed out for workflow run {workflow_run_id}, marking workflow run as failed",
                        block_type=block.block_type,
                        workflow_run_id=workflow_run.workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_result,
                        block_type_var=block.block_type,
                        block_label=block.label,
                    )

                    if not block.continue_on_failure:
                        failure_reason = f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} timed out. Reason: {block_result.failure_reason}"
                        await self.mark_workflow_run_as_failed(
                            workflow_run_id=workflow_run.workflow_run_id, failure_reason=failure_reason
                        )
                        await self.clean_up_workflow(
                            workflow=workflow,
                            workflow_run=workflow_run,
                            api_key=api_key,
                            close_browser_on_completion=browser_session_id is None,
                            browser_session_id=browser_session_id,
                        )
                        return workflow_run

                    LOG.warning(
                        f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} timed out for workflow run {workflow_run_id}, but will continue executing the workflow run",
                        block_type=block.block_type,
                        workflow_run_id=workflow_run.workflow_run_id,
                        block_idx=block_idx,
                        block_result=block_result,
                        continue_on_failure=block.continue_on_failure,
                        block_type_var=block.block_type,
                        block_label=block.label,
                    )

            except Exception as e:
                LOG.exception(
                    f"Error while executing workflow run {workflow_run.workflow_run_id}",
                    workflow_run_id=workflow_run.workflow_run_id,
                    block_idx=block_idx,
                    block_type=block.block_type,
                    block_label=block.label,
                )

                exception_message = f"Unexpected error: {str(e)}"
                if isinstance(e, SkyvernException):
                    exception_message = f"unexpected SkyvernException({e.__class__.__name__}): {str(e)}"

                failure_reason = f"Block with type {block.block_type} at index {block_idx}/{blocks_cnt - 1} failed. failure reason: {exception_message}"
                await self.mark_workflow_run_as_failed(
                    workflow_run_id=workflow_run.workflow_run_id, failure_reason=failure_reason
                )
                await self.clean_up_workflow(
                    workflow=workflow,
                    workflow_run=workflow_run,
                    api_key=api_key,
                    browser_session_id=browser_session_id,
                    close_browser_on_completion=browser_session_id is None,
                )
                return workflow_run

        refreshed_workflow_run = await app.DATABASE.get_workflow_run(
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=organization_id,
        )
        if refreshed_workflow_run and refreshed_workflow_run.status not in (
            WorkflowRunStatus.canceled,
            WorkflowRunStatus.failed,
            WorkflowRunStatus.terminated,
            WorkflowRunStatus.timed_out,
        ):
            await self.mark_workflow_run_as_completed(workflow_run_id=workflow_run.workflow_run_id)
        else:
            LOG.info(
                "Workflow run is already timed_out, canceled, failed, or terminated, not marking as completed",
                workflow_run_id=workflow_run.workflow_run_id,
                workflow_run_status=refreshed_workflow_run.status if refreshed_workflow_run else None,
            )
        await self.clean_up_workflow(
            workflow=workflow,
            workflow_run=workflow_run,
            api_key=api_key,
            browser_session_id=browser_session_id,
            close_browser_on_completion=browser_session_id is None,
        )

        # Track workflow run duration when completed
        duration_seconds = (datetime.now(UTC) - workflow_run.created_at.replace(tzinfo=UTC)).total_seconds()
        LOG.info(
            "Workflow run duration metrics",
            workflow_run_id=workflow_run_id,
            workflow_id=workflow_run.workflow_id,
            duration_seconds=duration_seconds,
            workflow_run_status=WorkflowRunStatus.completed,
            organization_id=organization_id,
        )

        return workflow_run

    async def create_workflow(
        self,
        organization_id: str,
        title: str,
        workflow_definition: WorkflowDefinition,
        description: str | None = None,
        proxy_location: ProxyLocation | None = None,
        webhook_callback_url: str | None = None,
        totp_verification_url: str | None = None,
        totp_identifier: str | None = None,
        persist_browser_session: bool = False,
        workflow_permanent_id: str | None = None,
        version: int | None = None,
        is_saved_task: bool = False,
        status: WorkflowStatus = WorkflowStatus.published,
    ) -> Workflow:
        return await app.DATABASE.create_workflow(
            title=title,
            workflow_definition=workflow_definition.model_dump(),
            organization_id=organization_id,
            description=description,
            proxy_location=proxy_location,
            webhook_callback_url=webhook_callback_url,
            totp_verification_url=totp_verification_url,
            totp_identifier=totp_identifier,
            persist_browser_session=persist_browser_session,
            workflow_permanent_id=workflow_permanent_id,
            version=version,
            is_saved_task=is_saved_task,
            status=status,
        )

    async def get_workflow(self, workflow_id: str, organization_id: str | None = None) -> Workflow:
        workflow = await app.DATABASE.get_workflow(workflow_id=workflow_id, organization_id=organization_id)
        if not workflow:
            raise WorkflowNotFound(workflow_id=workflow_id)
        return workflow

    async def get_workflow_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
        version: int | None = None,
        exclude_deleted: bool = True,
    ) -> Workflow:
        workflow = await app.DATABASE.get_workflow_by_permanent_id(
            workflow_permanent_id,
            organization_id=organization_id,
            version=version,
            exclude_deleted=exclude_deleted,
        )
        if not workflow:
            raise WorkflowNotFound(workflow_permanent_id=workflow_permanent_id, version=version)
        return workflow

    async def get_workflows_by_permanent_ids(
        self,
        workflow_permanent_ids: list[str],
        organization_id: str | None = None,
        page: int = 1,
        page_size: int = 10,
        title: str = "",
        statuses: list[WorkflowStatus] | None = None,
    ) -> list[Workflow]:
        return await app.DATABASE.get_workflows_by_permanent_ids(
            workflow_permanent_ids,
            organization_id=organization_id,
            page=page,
            page_size=page_size,
            title=title,
            statuses=statuses,
        )

    async def get_workflows_by_organization_id(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        only_saved_tasks: bool = False,
        only_workflows: bool = False,
        title: str = "",
        statuses: list[WorkflowStatus] | None = None,
    ) -> list[Workflow]:
        """
        Get all workflows with the latest version for the organization.
        """
        return await app.DATABASE.get_workflows_by_organization_id(
            organization_id=organization_id,
            page=page,
            page_size=page_size,
            only_saved_tasks=only_saved_tasks,
            only_workflows=only_workflows,
            title=title,
            statuses=statuses,
        )

    async def update_workflow(
        self,
        workflow_id: str,
        organization_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        workflow_definition: WorkflowDefinition | None = None,
    ) -> Workflow:
        if workflow_definition:
            workflow_definition.validate()

        return await app.DATABASE.update_workflow(
            workflow_id=workflow_id,
            title=title,
            organization_id=organization_id,
            description=description,
            workflow_definition=(workflow_definition.model_dump() if workflow_definition else None),
        )

    async def delete_workflow_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
    ) -> None:
        await app.DATABASE.soft_delete_workflow_by_permanent_id(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
        )

    async def delete_workflow_by_id(
        self,
        workflow_id: str,
        organization_id: str,
    ) -> None:
        await app.DATABASE.soft_delete_workflow_by_id(
            workflow_id=workflow_id,
            organization_id=organization_id,
        )

    async def get_workflow_runs(
        self, organization_id: str, page: int = 1, page_size: int = 10, status: list[WorkflowRunStatus] | None = None
    ) -> list[WorkflowRun]:
        return await app.DATABASE.get_workflow_runs(
            organization_id=organization_id, page=page, page_size=page_size, status=status
        )

    async def get_workflow_runs_for_workflow_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        status: list[WorkflowRunStatus] | None = None,
    ) -> list[WorkflowRun]:
        return await app.DATABASE.get_workflow_runs_for_workflow_permanent_id(
            workflow_permanent_id=workflow_permanent_id,
            organization_id=organization_id,
            page=page,
            page_size=page_size,
            status=status,
        )

    async def create_workflow_run(
        self,
        workflow_request: WorkflowRequestBody,
        workflow_permanent_id: str,
        workflow_id: str,
        organization_id: str,
        parent_workflow_run_id: str | None = None,
    ) -> WorkflowRun:
        return await app.DATABASE.create_workflow_run(
            workflow_permanent_id=workflow_permanent_id,
            workflow_id=workflow_id,
            organization_id=organization_id,
            proxy_location=workflow_request.proxy_location,
            webhook_callback_url=workflow_request.webhook_callback_url,
            totp_verification_url=workflow_request.totp_verification_url,
            totp_identifier=workflow_request.totp_identifier,
            parent_workflow_run_id=parent_workflow_run_id,
        )

    async def mark_workflow_run_as_completed(self, workflow_run_id: str) -> None:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as completed",
            workflow_run_id=workflow_run_id,
            workflow_status="completed",
        )
        await app.DATABASE.update_workflow_run(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.completed,
        )

    async def mark_workflow_run_as_failed(self, workflow_run_id: str, failure_reason: str | None) -> None:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as failed",
            workflow_run_id=workflow_run_id,
            workflow_status="failed",
            failure_reason=failure_reason,
        )
        await app.DATABASE.update_workflow_run(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.failed,
            failure_reason=failure_reason,
        )

    async def mark_workflow_run_as_running(self, workflow_run_id: str) -> None:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as running",
            workflow_run_id=workflow_run_id,
            workflow_status="running",
        )
        await app.DATABASE.update_workflow_run(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.running,
        )

    async def mark_workflow_run_as_terminated(self, workflow_run_id: str, failure_reason: str | None) -> None:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as terminated",
            workflow_run_id=workflow_run_id,
            workflow_status="terminated",
            failure_reason=failure_reason,
        )
        await app.DATABASE.update_workflow_run(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.terminated,
            failure_reason=failure_reason,
        )

    async def mark_workflow_run_as_canceled(self, workflow_run_id: str) -> None:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as canceled",
            workflow_run_id=workflow_run_id,
            workflow_status="canceled",
        )
        await app.DATABASE.update_workflow_run(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.canceled,
        )

    async def get_workflow_run(self, workflow_run_id: str, organization_id: str | None = None) -> WorkflowRun:
        workflow_run = await app.DATABASE.get_workflow_run(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        if not workflow_run:
            raise WorkflowRunNotFound(workflow_run_id)
        return workflow_run

    async def create_workflow_parameter(
        self,
        workflow_id: str,
        workflow_parameter_type: WorkflowParameterType,
        key: str,
        default_value: bool | int | float | str | dict | list | None = None,
        description: str | None = None,
    ) -> WorkflowParameter:
        return await app.DATABASE.create_workflow_parameter(
            workflow_id=workflow_id,
            workflow_parameter_type=workflow_parameter_type,
            key=key,
            description=description,
            default_value=default_value,
        )

    async def create_aws_secret_parameter(
        self, workflow_id: str, aws_key: str, key: str, description: str | None = None
    ) -> AWSSecretParameter:
        return await app.DATABASE.create_aws_secret_parameter(
            workflow_id=workflow_id, aws_key=aws_key, key=key, description=description
        )

    async def create_bitwarden_login_credential_parameter(
        self,
        workflow_id: str,
        bitwarden_client_id_aws_secret_key: str,
        bitwarden_client_secret_aws_secret_key: str,
        bitwarden_master_password_aws_secret_key: str,
        key: str,
        url_parameter_key: str | None = None,
        description: str | None = None,
        bitwarden_collection_id: str | None = None,
        bitwarden_item_id: str | None = None,
    ) -> Parameter:
        return await app.DATABASE.create_bitwarden_login_credential_parameter(
            workflow_id=workflow_id,
            bitwarden_client_id_aws_secret_key=bitwarden_client_id_aws_secret_key,
            bitwarden_client_secret_aws_secret_key=bitwarden_client_secret_aws_secret_key,
            bitwarden_master_password_aws_secret_key=bitwarden_master_password_aws_secret_key,
            key=key,
            url_parameter_key=url_parameter_key,
            description=description,
            bitwarden_collection_id=bitwarden_collection_id,
            bitwarden_item_id=bitwarden_item_id,
        )

    async def create_credential_parameter(
        self,
        workflow_id: str,
        key: str,
        credential_id: str,
        description: str | None = None,
    ) -> CredentialParameter:
        return await app.DATABASE.create_credential_parameter(
            workflow_id=workflow_id,
            key=key,
            credential_id=credential_id,
            description=description,
        )

    async def create_bitwarden_sensitive_information_parameter(
        self,
        workflow_id: str,
        bitwarden_client_id_aws_secret_key: str,
        bitwarden_client_secret_aws_secret_key: str,
        bitwarden_master_password_aws_secret_key: str,
        bitwarden_collection_id: str,
        bitwarden_identity_key: str,
        bitwarden_identity_fields: list[str],
        key: str,
        description: str | None = None,
    ) -> Parameter:
        return await app.DATABASE.create_bitwarden_sensitive_information_parameter(
            workflow_id=workflow_id,
            bitwarden_client_id_aws_secret_key=bitwarden_client_id_aws_secret_key,
            bitwarden_client_secret_aws_secret_key=bitwarden_client_secret_aws_secret_key,
            bitwarden_master_password_aws_secret_key=bitwarden_master_password_aws_secret_key,
            bitwarden_collection_id=bitwarden_collection_id,
            bitwarden_identity_key=bitwarden_identity_key,
            bitwarden_identity_fields=bitwarden_identity_fields,
            key=key,
            description=description,
        )

    async def create_bitwarden_credit_card_data_parameter(
        self,
        workflow_id: str,
        bitwarden_client_id_aws_secret_key: str,
        bitwarden_client_secret_aws_secret_key: str,
        bitwarden_master_password_aws_secret_key: str,
        bitwarden_collection_id: str,
        bitwarden_item_id: str,
        key: str,
        description: str | None = None,
    ) -> Parameter:
        return await app.DATABASE.create_bitwarden_credit_card_data_parameter(
            workflow_id=workflow_id,
            bitwarden_client_id_aws_secret_key=bitwarden_client_id_aws_secret_key,
            bitwarden_client_secret_aws_secret_key=bitwarden_client_secret_aws_secret_key,
            bitwarden_master_password_aws_secret_key=bitwarden_master_password_aws_secret_key,
            bitwarden_collection_id=bitwarden_collection_id,
            bitwarden_item_id=bitwarden_item_id,
            key=key,
            description=description,
        )

    async def create_output_parameter(
        self, workflow_id: str, key: str, description: str | None = None
    ) -> OutputParameter:
        return await app.DATABASE.create_output_parameter(workflow_id=workflow_id, key=key, description=description)

    async def get_workflow_parameters(self, workflow_id: str) -> list[WorkflowParameter]:
        return await app.DATABASE.get_workflow_parameters(workflow_id=workflow_id)

    async def create_workflow_run_parameter(
        self,
        workflow_run_id: str,
        workflow_parameter: WorkflowParameter,
        value: Any,
    ) -> WorkflowRunParameter:
        value = json.dumps(value) if isinstance(value, (dict, list)) else value
        # InvalidWorkflowParameter will be raised if the validation fails
        workflow_parameter.workflow_parameter_type.convert_value(value)

        return await app.DATABASE.create_workflow_run_parameter(
            workflow_run_id=workflow_run_id,
            workflow_parameter=workflow_parameter,
            value=value,
        )

    async def get_workflow_run_parameter_tuples(
        self, workflow_run_id: str
    ) -> list[tuple[WorkflowParameter, WorkflowRunParameter]]:
        return await app.DATABASE.get_workflow_run_parameters(workflow_run_id=workflow_run_id)

    @staticmethod
    async def get_workflow_output_parameters(workflow_id: str) -> list[OutputParameter]:
        return await app.DATABASE.get_workflow_output_parameters(workflow_id=workflow_id)

    @staticmethod
    async def get_workflow_run_output_parameters(
        workflow_run_id: str,
    ) -> list[WorkflowRunOutputParameter]:
        return await app.DATABASE.get_workflow_run_output_parameters(workflow_run_id=workflow_run_id)

    @staticmethod
    async def get_output_parameter_workflow_run_output_parameter_tuples(
        workflow_id: str,
        workflow_run_id: str,
    ) -> list[tuple[OutputParameter, WorkflowRunOutputParameter]]:
        workflow_run_output_parameters = await app.DATABASE.get_workflow_run_output_parameters(
            workflow_run_id=workflow_run_id
        )
        output_parameters = await app.DATABASE.get_workflow_output_parameters(workflow_id=workflow_id)

        return [
            (output_parameter, workflow_run_output_parameter)
            for workflow_run_output_parameter in workflow_run_output_parameters
            for output_parameter in output_parameters
            if output_parameter.output_parameter_id == workflow_run_output_parameter.output_parameter_id
        ]

    async def get_last_task_for_workflow_run(self, workflow_run_id: str) -> Task | None:
        return await app.DATABASE.get_last_task_for_workflow_run(workflow_run_id=workflow_run_id)

    async def get_tasks_by_workflow_run_id(self, workflow_run_id: str) -> list[Task]:
        return await app.DATABASE.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run_id)

    async def build_workflow_run_status_response_by_workflow_id(
        self,
        workflow_run_id: str,
        organization_id: str,
        include_cost: bool = False,
    ) -> WorkflowRunResponse:
        workflow_run = await self.get_workflow_run(workflow_run_id=workflow_run_id, organization_id=organization_id)
        if workflow_run is None:
            LOG.error(f"Workflow run {workflow_run_id} not found")
            raise WorkflowRunNotFound(workflow_run_id=workflow_run_id)
        workflow_permanent_id = workflow_run.workflow_permanent_id
        return await self.build_workflow_run_status_response(
            workflow_permanent_id=workflow_permanent_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            include_cost=include_cost,
        )

    async def build_workflow_run_status_response(
        self,
        workflow_permanent_id: str,
        workflow_run_id: str,
        organization_id: str,
        include_cost: bool = False,
    ) -> WorkflowRunResponse:
        workflow = await self.get_workflow_by_permanent_id(workflow_permanent_id)
        if workflow is None:
            LOG.error(f"Workflow {workflow_permanent_id} not found")
            raise WorkflowNotFound(workflow_permanent_id=workflow_permanent_id)

        workflow_run = await self.get_workflow_run(workflow_run_id=workflow_run_id, organization_id=organization_id)
        workflow_run_tasks = await app.DATABASE.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run_id)
        screenshot_artifacts = []
        screenshot_urls: list[str] | None = None
        # get the last screenshot for the last 3 tasks of the workflow run
        for task in workflow_run_tasks[::-1]:
            screenshot_artifact = await app.DATABASE.get_latest_artifact(
                task_id=task.task_id,
                artifact_types=[
                    ArtifactType.SCREENSHOT_ACTION,
                    ArtifactType.SCREENSHOT_FINAL,
                ],
                organization_id=organization_id,
            )
            if screenshot_artifact:
                screenshot_artifacts.append(screenshot_artifact)
            if len(screenshot_artifacts) >= 3:
                break
        if screenshot_artifacts:
            screenshot_urls = await app.ARTIFACT_MANAGER.get_share_links(screenshot_artifacts)

        recording_url = None
        recording_artifact = await app.DATABASE.get_artifact_for_workflow_run(
            workflow_run_id=workflow_run_id,
            artifact_type=ArtifactType.RECORDING,
            organization_id=organization_id,
        )
        if recording_artifact:
            recording_url = await app.ARTIFACT_MANAGER.get_share_link(recording_artifact)

        downloaded_files: list[FileInfo] | None = None
        downloaded_file_urls: list[str] | None = None
        try:
            async with asyncio.timeout(GET_DOWNLOADED_FILES_TIMEOUT):
                downloaded_files = await app.STORAGE.get_downloaded_files(
                    organization_id=workflow_run.organization_id,
                    task_id=None,
                    workflow_run_id=workflow_run.workflow_run_id,
                )
                if downloaded_files:
                    downloaded_file_urls = [file_info.url for file_info in downloaded_files]
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout to get downloaded files",
                workflow_run_id=workflow_run.workflow_run_id,
            )
        except Exception:
            LOG.warning(
                "Failed to get downloaded files",
                exc_info=True,
                workflow_run_id=workflow_run.workflow_run_id,
            )

        workflow_parameter_tuples = await app.DATABASE.get_workflow_run_parameters(workflow_run_id=workflow_run_id)
        parameters_with_value = {wfp.key: wfrp.value for wfp, wfrp in workflow_parameter_tuples}
        output_parameter_tuples: list[
            tuple[OutputParameter, WorkflowRunOutputParameter]
        ] = await self.get_output_parameter_workflow_run_output_parameter_tuples(
            workflow_id=workflow_run.workflow_id, workflow_run_id=workflow_run_id
        )

        outputs = None
        EXTRACTED_INFORMATION_KEY = "extracted_information"
        if output_parameter_tuples:
            outputs = {output_parameter.key: output.value for output_parameter, output in output_parameter_tuples}
            extracted_information = {
                output_parameter.key: output.value[EXTRACTED_INFORMATION_KEY]
                for output_parameter, output in output_parameter_tuples
                if output.value is not None
                and isinstance(output.value, dict)
                and EXTRACTED_INFORMATION_KEY in output.value
                and output.value[EXTRACTED_INFORMATION_KEY] is not None
            }
            outputs[EXTRACTED_INFORMATION_KEY] = extracted_information

        total_steps = None
        total_cost = None
        if include_cost:
            workflow_run_steps = await app.DATABASE.get_steps_by_task_ids(
                task_ids=[task.task_id for task in workflow_run_tasks], organization_id=organization_id
            )
            workflow_run_blocks = await app.DATABASE.get_workflow_run_blocks(
                workflow_run_id=workflow_run_id, organization_id=organization_id
            )
            text_prompt_blocks = [block for block in workflow_run_blocks if block.block_type == BlockType.TEXT_PROMPT]
            total_steps = len(workflow_run_steps)
            # TODO: This is a temporary cost calculation. We need to implement a more accurate cost calculation.
            # successful steps are the ones that have a status of completed and the total count of unique step.order
            successful_steps = [step for step in workflow_run_steps if step.status == StepStatus.completed]
            total_cost = 0.1 * (len(successful_steps) + len(text_prompt_blocks))
        return WorkflowRunResponse(
            workflow_id=workflow.workflow_permanent_id,
            workflow_run_id=workflow_run_id,
            status=workflow_run.status,
            failure_reason=workflow_run.failure_reason,
            proxy_location=workflow_run.proxy_location,
            webhook_callback_url=workflow_run.webhook_callback_url,
            totp_verification_url=workflow_run.totp_verification_url,
            totp_identifier=workflow_run.totp_identifier,
            created_at=workflow_run.created_at,
            modified_at=workflow_run.modified_at,
            parameters=parameters_with_value,
            screenshot_urls=screenshot_urls,
            recording_url=recording_url,
            downloaded_files=downloaded_files,
            downloaded_file_urls=downloaded_file_urls,
            outputs=outputs,
            total_steps=total_steps,
            total_cost=total_cost,
            workflow_title=workflow.title,
        )

    async def clean_up_workflow(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        api_key: str | None = None,
        close_browser_on_completion: bool = True,
        need_call_webhook: bool = True,
        browser_session_id: str | None = None,
    ) -> None:
        analytics.capture("skyvern-oss-agent-workflow-status", {"status": workflow_run.status})
        tasks = await self.get_tasks_by_workflow_run_id(workflow_run.workflow_run_id)
        all_workflow_task_ids = [task.task_id for task in tasks]
        browser_state = await app.BROWSER_MANAGER.cleanup_for_workflow_run(
            workflow_run.workflow_run_id,
            all_workflow_task_ids,
            close_browser_on_completion=close_browser_on_completion and browser_session_id is None,
            browser_session_id=browser_session_id,
            organization_id=workflow_run.organization_id,
        )
        if browser_state:
            await self.persist_video_data(browser_state, workflow, workflow_run)
            if tasks:
                await self.persist_debug_artifacts(browser_state, tasks[-1], workflow, workflow_run)
            if workflow.persist_browser_session and browser_state.browser_artifacts.browser_session_dir:
                await app.STORAGE.store_browser_session(
                    workflow_run.organization_id,
                    workflow.workflow_permanent_id,
                    browser_state.browser_artifacts.browser_session_dir,
                )
                LOG.info("Persisted browser session for workflow run", workflow_run_id=workflow_run.workflow_run_id)

        await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks(all_workflow_task_ids)

        try:
            async with asyncio.timeout(SAVE_DOWNLOADED_FILES_TIMEOUT):
                await app.STORAGE.save_downloaded_files(
                    workflow_run.organization_id, task_id=None, workflow_run_id=workflow_run.workflow_run_id
                )
        except asyncio.TimeoutError:
            LOG.warning(
                "Timeout to save downloaded files",
                workflow_run_id=workflow_run.workflow_run_id,
            )
        except Exception:
            LOG.warning(
                "Failed to save downloaded files",
                exc_info=True,
                workflow_run_id=workflow_run.workflow_run_id,
            )

        if not need_call_webhook:
            return

        await self.execute_workflow_webhook(workflow_run, api_key)

    async def execute_workflow_webhook(
        self,
        workflow_run: WorkflowRun,
        api_key: str | None = None,
    ) -> None:
        workflow_id = workflow_run.workflow_id
        workflow_run_status_response = await self.build_workflow_run_status_response(
            workflow_permanent_id=workflow_run.workflow_permanent_id,
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=workflow_run.organization_id,
        )
        LOG.info(
            "Built workflow run status response",
            workflow_run_status_response=workflow_run_status_response,
        )

        if not workflow_run.webhook_callback_url:
            LOG.warning(
                "Workflow has no webhook callback url. Not sending workflow response",
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
            )
            return

        if not api_key:
            LOG.warning(
                "Request has no api key. Not sending workflow response",
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
            )
            return

        # send webhook to the webhook callback url
        payload = workflow_run_status_response.model_dump_json()
        headers = generate_skyvern_webhook_headers(
            payload=payload,
            api_key=api_key,
        )
        LOG.info(
            "Sending webhook run status to webhook callback url",
            workflow_id=workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            webhook_callback_url=workflow_run.webhook_callback_url,
            payload=payload,
            headers=headers,
        )
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url=workflow_run.webhook_callback_url, data=payload, headers=headers, timeout=httpx.Timeout(30.0)
                )
            if resp.status_code == 200:
                LOG.info(
                    "Webhook sent successfully",
                    workflow_id=workflow_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    resp_code=resp.status_code,
                    resp_text=resp.text,
                )
            else:
                LOG.info(
                    "Webhook failed",
                    workflow_id=workflow_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    webhook_data=payload,
                    resp=resp,
                    resp_code=resp.status_code,
                    resp_text=resp.text,
                )
        except Exception as e:
            raise FailedToSendWebhook(
                workflow_id=workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
            ) from e

    async def persist_video_data(
        self, browser_state: BrowserState, workflow: Workflow, workflow_run: WorkflowRun
    ) -> None:
        # Create recording artifact after closing the browser, so we can get an accurate recording
        video_artifacts = await app.BROWSER_MANAGER.get_video_artifacts(
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            browser_state=browser_state,
        )
        for video_artifact in video_artifacts:
            await app.ARTIFACT_MANAGER.update_artifact_data(
                artifact_id=video_artifact.video_artifact_id,
                organization_id=workflow_run.organization_id,
                data=video_artifact.video_data,
            )

    async def persist_har_data(
        self,
        browser_state: BrowserState,
        last_step: Step,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> None:
        har_data = await app.BROWSER_MANAGER.get_har_data(
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            browser_state=browser_state,
        )
        if har_data:
            await app.ARTIFACT_MANAGER.create_artifact(
                step=last_step,
                artifact_type=ArtifactType.HAR,
                data=har_data,
            )

    async def persist_browser_console_log(
        self,
        browser_state: BrowserState,
        last_step: Step,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> None:
        browser_log = await app.BROWSER_MANAGER.get_browser_console_log(
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            browser_state=browser_state,
        )
        if browser_log:
            await app.ARTIFACT_MANAGER.create_artifact(
                step=last_step,
                artifact_type=ArtifactType.BROWSER_CONSOLE_LOG,
                data=browser_log,
            )

    async def persist_tracing_data(
        self, browser_state: BrowserState, last_step: Step, workflow_run: WorkflowRun
    ) -> None:
        if browser_state.browser_context is None or browser_state.browser_artifacts.traces_dir is None:
            return

        trace_path = f"{browser_state.browser_artifacts.traces_dir}/{workflow_run.workflow_run_id}.zip"
        await app.ARTIFACT_MANAGER.create_artifact(step=last_step, artifact_type=ArtifactType.TRACE, path=trace_path)

    async def persist_debug_artifacts(
        self,
        browser_state: BrowserState,
        last_task: Task,
        workflow: Workflow,
        workflow_run: WorkflowRun,
    ) -> None:
        last_step = await app.DATABASE.get_latest_step(
            task_id=last_task.task_id, organization_id=last_task.organization_id
        )
        if not last_step:
            return

        await self.persist_browser_console_log(browser_state, last_step, workflow, workflow_run)
        await self.persist_har_data(browser_state, last_step, workflow, workflow_run)
        await self.persist_tracing_data(browser_state, last_step, workflow_run)

    async def create_workflow_from_request(
        self,
        organization: Organization,
        request: WorkflowCreateYAMLRequest,
        workflow_permanent_id: str | None = None,
    ) -> Workflow:
        organization_id = organization.organization_id
        LOG.info(
            "Creating workflow from request",
            organization_id=organization_id,
            title=request.title,
        )
        new_workflow_id: str | None = None
        try:
            if workflow_permanent_id:
                existing_latest_workflow = await self.get_workflow_by_permanent_id(
                    workflow_permanent_id=workflow_permanent_id,
                    organization_id=organization_id,
                    exclude_deleted=False,
                )
                existing_version = existing_latest_workflow.version
                workflow = await self.create_workflow(
                    title=request.title,
                    workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
                    description=request.description,
                    organization_id=organization_id,
                    proxy_location=request.proxy_location,
                    webhook_callback_url=request.webhook_callback_url,
                    totp_verification_url=request.totp_verification_url,
                    totp_identifier=request.totp_identifier,
                    persist_browser_session=request.persist_browser_session,
                    workflow_permanent_id=workflow_permanent_id,
                    version=existing_version + 1,
                    is_saved_task=request.is_saved_task,
                    status=request.status,
                )
            else:
                workflow = await self.create_workflow(
                    title=request.title,
                    workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
                    description=request.description,
                    organization_id=organization_id,
                    proxy_location=request.proxy_location,
                    webhook_callback_url=request.webhook_callback_url,
                    totp_verification_url=request.totp_verification_url,
                    totp_identifier=request.totp_identifier,
                    persist_browser_session=request.persist_browser_session,
                    is_saved_task=request.is_saved_task,
                    status=request.status,
                )
            # Keeping track of the new workflow id to delete it if an error occurs during the creation process
            new_workflow_id = workflow.workflow_id
            # Create parameters from the request
            parameters: dict[str, PARAMETER_TYPE] = {}
            duplicate_parameter_keys = set()

            # Check if user's trying to manually create an output parameter
            if any(
                parameter.parameter_type == ParameterType.OUTPUT for parameter in request.workflow_definition.parameters
            ):
                raise InvalidWorkflowDefinition(message="Cannot manually create output parameters")

            # Check if any parameter keys collide with automatically created output parameter keys
            block_labels = [block.label for block in request.workflow_definition.blocks]
            # TODO (kerem): Check if block labels are unique
            output_parameter_keys = [f"{block_label}_output" for block_label in block_labels]
            parameter_keys = [parameter.key for parameter in request.workflow_definition.parameters]
            if any(key in output_parameter_keys for key in parameter_keys):
                raise WorkflowDefinitionHasReservedParameterKeys(
                    reserved_keys=output_parameter_keys, parameter_keys=parameter_keys
                )

            # Create output parameters for all blocks
            block_output_parameters = await WorkflowService._create_all_output_parameters_for_workflow(
                workflow_id=workflow.workflow_id,
                block_yamls=request.workflow_definition.blocks,
            )
            for block_output_parameter in block_output_parameters.values():
                parameters[block_output_parameter.key] = block_output_parameter

            # We're going to process context parameters after other parameters since they depend on the other parameters
            context_parameter_yamls = []

            for parameter in request.workflow_definition.parameters:
                if parameter.key in parameters:
                    LOG.error(f"Duplicate parameter key {parameter.key}")
                    duplicate_parameter_keys.add(parameter.key)
                    continue
                if parameter.parameter_type == ParameterType.AWS_SECRET:
                    parameters[parameter.key] = await self.create_aws_secret_parameter(
                        workflow_id=workflow.workflow_id,
                        aws_key=parameter.aws_key,
                        key=parameter.key,
                        description=parameter.description,
                    )
                elif parameter.parameter_type == ParameterType.CREDENTIAL:
                    parameters[parameter.key] = await self.create_credential_parameter(
                        workflow_id=workflow.workflow_id,
                        key=parameter.key,
                        description=parameter.description,
                        credential_id=parameter.credential_id,
                    )
                elif parameter.parameter_type == ParameterType.BITWARDEN_LOGIN_CREDENTIAL:
                    if not parameter.bitwarden_collection_id and not parameter.bitwarden_item_id:
                        raise WorkflowParameterMissingRequiredValue(
                            workflow_parameter_type=ParameterType.BITWARDEN_LOGIN_CREDENTIAL,
                            workflow_parameter_key=parameter.key,
                            required_value="bitwarden_collection_id or bitwarden_item_id",
                        )
                    if parameter.bitwarden_collection_id and not parameter.url_parameter_key:
                        raise WorkflowParameterMissingRequiredValue(
                            workflow_parameter_type=ParameterType.BITWARDEN_LOGIN_CREDENTIAL,
                            workflow_parameter_key=parameter.key,
                            required_value="url_parameter_key",
                        )
                    parameters[parameter.key] = await self.create_bitwarden_login_credential_parameter(
                        workflow_id=workflow.workflow_id,
                        bitwarden_client_id_aws_secret_key=parameter.bitwarden_client_id_aws_secret_key,
                        bitwarden_client_secret_aws_secret_key=parameter.bitwarden_client_secret_aws_secret_key,
                        bitwarden_master_password_aws_secret_key=parameter.bitwarden_master_password_aws_secret_key,
                        url_parameter_key=parameter.url_parameter_key,
                        key=parameter.key,
                        description=parameter.description,
                        bitwarden_collection_id=parameter.bitwarden_collection_id,
                        bitwarden_item_id=parameter.bitwarden_item_id,
                    )
                elif parameter.parameter_type == ParameterType.BITWARDEN_SENSITIVE_INFORMATION:
                    parameters[parameter.key] = await self.create_bitwarden_sensitive_information_parameter(
                        workflow_id=workflow.workflow_id,
                        bitwarden_client_id_aws_secret_key=parameter.bitwarden_client_id_aws_secret_key,
                        bitwarden_client_secret_aws_secret_key=parameter.bitwarden_client_secret_aws_secret_key,
                        bitwarden_master_password_aws_secret_key=parameter.bitwarden_master_password_aws_secret_key,
                        # TODO: remove "# type: ignore" after ensuring bitwarden_collection_id is always set
                        bitwarden_collection_id=parameter.bitwarden_collection_id,  # type: ignore
                        bitwarden_identity_key=parameter.bitwarden_identity_key,
                        bitwarden_identity_fields=parameter.bitwarden_identity_fields,
                        key=parameter.key,
                        description=parameter.description,
                    )
                elif parameter.parameter_type == ParameterType.BITWARDEN_CREDIT_CARD_DATA:
                    parameters[parameter.key] = await self.create_bitwarden_credit_card_data_parameter(
                        workflow_id=workflow.workflow_id,
                        bitwarden_client_id_aws_secret_key=parameter.bitwarden_client_id_aws_secret_key,
                        bitwarden_client_secret_aws_secret_key=parameter.bitwarden_client_secret_aws_secret_key,
                        bitwarden_master_password_aws_secret_key=parameter.bitwarden_master_password_aws_secret_key,
                        # TODO: remove "# type: ignore" after ensuring bitwarden_collection_id is always set
                        bitwarden_collection_id=parameter.bitwarden_collection_id,  # type: ignore
                        bitwarden_item_id=parameter.bitwarden_item_id,  # type: ignore
                        key=parameter.key,
                        description=parameter.description,
                    )
                elif parameter.parameter_type == ParameterType.WORKFLOW:
                    parameters[parameter.key] = await self.create_workflow_parameter(
                        workflow_id=workflow.workflow_id,
                        workflow_parameter_type=parameter.workflow_parameter_type,
                        key=parameter.key,
                        default_value=parameter.default_value,
                        description=parameter.description,
                    )
                elif parameter.parameter_type == ParameterType.OUTPUT:
                    parameters[parameter.key] = await self.create_output_parameter(
                        workflow_id=workflow.workflow_id,
                        key=parameter.key,
                        description=parameter.description,
                    )
                elif parameter.parameter_type == ParameterType.CONTEXT:
                    context_parameter_yamls.append(parameter)
                else:
                    LOG.error(f"Invalid parameter type {parameter.parameter_type}")

            # Now we can process the context parameters since all other parameters have been created
            for context_parameter in context_parameter_yamls:
                if context_parameter.source_parameter_key not in parameters:
                    raise ContextParameterSourceNotDefined(
                        context_parameter_key=context_parameter.key,
                        source_key=context_parameter.source_parameter_key,
                    )

                if context_parameter.key in parameters:
                    LOG.error(f"Duplicate parameter key {context_parameter.key}")
                    duplicate_parameter_keys.add(context_parameter.key)
                    continue

                # We're only adding the context parameter to the parameters dict, we're not creating it in the database
                # It'll only be stored in the `workflow.workflow_definition`
                # todo (kerem): should we have a database table for context parameters?
                parameters[context_parameter.key] = ContextParameter(
                    key=context_parameter.key,
                    description=context_parameter.description,
                    source=parameters[context_parameter.source_parameter_key],
                    # Context parameters don't have a default value, the value always depends on the source parameter
                    value=None,
                )

            if duplicate_parameter_keys:
                raise WorkflowDefinitionHasDuplicateParameterKeys(duplicate_keys=duplicate_parameter_keys)
            # Create blocks from the request
            block_label_mapping = {}
            blocks = []
            for block_yaml in request.workflow_definition.blocks:
                block = await self.block_yaml_to_block(workflow, block_yaml, parameters)
                blocks.append(block)
                block_label_mapping[block.label] = block

            # Set the blocks for the workflow definition
            workflow_definition = WorkflowDefinition(parameters=parameters.values(), blocks=blocks)
            workflow = await self.update_workflow(
                workflow_id=workflow.workflow_id,
                organization_id=organization_id,
                workflow_definition=workflow_definition,
            )
            LOG.info(
                f"Created workflow from request, title: {request.title}",
                parameter_keys=[parameter.key for parameter in parameters.values()],
                block_labels=[block.label for block in blocks],
                organization_id=organization_id,
                title=request.title,
                workflow_id=workflow.workflow_id,
            )
            return workflow
        except Exception as e:
            if new_workflow_id:
                LOG.error(
                    f"Failed to create workflow from request, deleting workflow {new_workflow_id}",
                    organization_id=organization_id,
                )
                await self.delete_workflow_by_id(workflow_id=new_workflow_id, organization_id=organization_id)
            else:
                LOG.exception(f"Failed to create workflow from request, title: {request.title}")
            raise e

    @staticmethod
    async def create_output_parameter_for_block(workflow_id: str, block_yaml: BLOCK_YAML_TYPES) -> OutputParameter:
        output_parameter_key = f"{block_yaml.label}_output"
        return await app.DATABASE.create_output_parameter(
            workflow_id=workflow_id,
            key=output_parameter_key,
            description=f"Output parameter for block {block_yaml.label}",
        )

    @staticmethod
    async def _create_all_output_parameters_for_workflow(
        workflow_id: str, block_yamls: list[BLOCK_YAML_TYPES]
    ) -> dict[str, OutputParameter]:
        output_parameters = {}
        for block_yaml in block_yamls:
            output_parameter = await WorkflowService.create_output_parameter_for_block(
                workflow_id=workflow_id, block_yaml=block_yaml
            )
            output_parameters[block_yaml.label] = output_parameter
            # Recursively create output parameters for for loop blocks
            if isinstance(block_yaml, ForLoopBlockYAML):
                output_parameters.update(
                    await WorkflowService._create_all_output_parameters_for_workflow(
                        workflow_id=workflow_id, block_yamls=block_yaml.loop_blocks
                    )
                )
        return output_parameters

    @staticmethod
    async def block_yaml_to_block(
        workflow: Workflow,
        block_yaml: BLOCK_YAML_TYPES,
        parameters: dict[str, Parameter],
    ) -> BlockTypeVar:
        output_parameter = parameters[f"{block_yaml.label}_output"]
        if block_yaml.block_type == BlockType.TASK:
            task_block_parameters = (
                [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                if block_yaml.parameter_keys
                else []
            )
            return TaskBlock(
                label=block_yaml.label,
                url=block_yaml.url,
                title=block_yaml.title,
                parameters=task_block_parameters,
                output_parameter=output_parameter,
                navigation_goal=block_yaml.navigation_goal,
                data_extraction_goal=block_yaml.data_extraction_goal,
                data_schema=block_yaml.data_schema,
                error_code_mapping=block_yaml.error_code_mapping,
                max_steps_per_run=block_yaml.max_steps_per_run,
                max_retries=block_yaml.max_retries,
                complete_on_download=block_yaml.complete_on_download,
                download_suffix=block_yaml.download_suffix,
                continue_on_failure=block_yaml.continue_on_failure,
                totp_verification_url=block_yaml.totp_verification_url,
                totp_identifier=block_yaml.totp_identifier,
                cache_actions=block_yaml.cache_actions,
                complete_criterion=block_yaml.complete_criterion,
                terminate_criterion=block_yaml.terminate_criterion,
            )
        elif block_yaml.block_type == BlockType.FOR_LOOP:
            loop_blocks = [
                await WorkflowService.block_yaml_to_block(workflow, loop_block, parameters)
                for loop_block in block_yaml.loop_blocks
            ]

            loop_over_parameter: Parameter | None = None
            if block_yaml.loop_over_parameter_key:
                loop_over_parameter = parameters[block_yaml.loop_over_parameter_key]

            if block_yaml.loop_variable_reference:
                # it's backaward compatible with jinja style parameter and context paramter
                # we trim the format like {{ loop_key }} into loop_key to initialize the context parater,
                # otherwise it might break the context parameter initialization chain, blow up the worklofw parameters
                # TODO: consider remove this if we totally give up context parameter
                trimmed_key = block_yaml.loop_variable_reference.strip(" {}")
                if trimmed_key in parameters:
                    loop_over_parameter = parameters[trimmed_key]

            if loop_over_parameter is None and not block_yaml.loop_variable_reference:
                raise Exception("Loop value parameter is required for for loop block")

            return ForLoopBlock(
                label=block_yaml.label,
                loop_over=loop_over_parameter,
                loop_variable_reference=block_yaml.loop_variable_reference,
                loop_blocks=loop_blocks,
                output_parameter=output_parameter,
                continue_on_failure=block_yaml.continue_on_failure,
                complete_if_empty=block_yaml.complete_if_empty,
            )
        elif block_yaml.block_type == BlockType.CODE:
            return CodeBlock(
                label=block_yaml.label,
                code=block_yaml.code,
                parameters=(
                    [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                    if block_yaml.parameter_keys
                    else []
                ),
                output_parameter=output_parameter,
                continue_on_failure=block_yaml.continue_on_failure,
            )
        elif block_yaml.block_type == BlockType.TEXT_PROMPT:
            return TextPromptBlock(
                label=block_yaml.label,
                llm_key=block_yaml.llm_key,
                prompt=block_yaml.prompt,
                parameters=(
                    [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                    if block_yaml.parameter_keys
                    else []
                ),
                json_schema=block_yaml.json_schema,
                output_parameter=output_parameter,
                continue_on_failure=block_yaml.continue_on_failure,
            )
        elif block_yaml.block_type == BlockType.DOWNLOAD_TO_S3:
            return DownloadToS3Block(
                label=block_yaml.label,
                output_parameter=output_parameter,
                url=block_yaml.url,
                continue_on_failure=block_yaml.continue_on_failure,
            )
        elif block_yaml.block_type == BlockType.UPLOAD_TO_S3:
            return UploadToS3Block(
                label=block_yaml.label,
                output_parameter=output_parameter,
                path=block_yaml.path,
                continue_on_failure=block_yaml.continue_on_failure,
            )
        elif block_yaml.block_type == BlockType.SEND_EMAIL:
            return SendEmailBlock(
                label=block_yaml.label,
                output_parameter=output_parameter,
                smtp_host=parameters[block_yaml.smtp_host_secret_parameter_key],
                smtp_port=parameters[block_yaml.smtp_port_secret_parameter_key],
                smtp_username=parameters[block_yaml.smtp_username_secret_parameter_key],
                smtp_password=parameters[block_yaml.smtp_password_secret_parameter_key],
                sender=block_yaml.sender,
                recipients=block_yaml.recipients,
                subject=block_yaml.subject,
                body=block_yaml.body,
                file_attachments=block_yaml.file_attachments or [],
                continue_on_failure=block_yaml.continue_on_failure,
            )
        elif block_yaml.block_type == BlockType.FILE_URL_PARSER:
            return FileParserBlock(
                label=block_yaml.label,
                output_parameter=output_parameter,
                file_url=block_yaml.file_url,
                file_type=block_yaml.file_type,
                continue_on_failure=block_yaml.continue_on_failure,
            )
        elif block_yaml.block_type == BlockType.PDF_PARSER:
            return PDFParserBlock(
                label=block_yaml.label,
                output_parameter=output_parameter,
                file_url=block_yaml.file_url,
                json_schema=block_yaml.json_schema,
                continue_on_failure=block_yaml.continue_on_failure,
            )
        elif block_yaml.block_type == BlockType.VALIDATION:
            validation_block_parameters = (
                [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                if block_yaml.parameter_keys
                else []
            )

            if not block_yaml.complete_criterion and not block_yaml.terminate_criterion:
                raise Exception("Both complete criterion and terminate criterion are empty")

            return ValidationBlock(
                label=block_yaml.label,
                task_type=TaskType.validation,
                parameters=validation_block_parameters,
                output_parameter=output_parameter,
                complete_criterion=block_yaml.complete_criterion,
                terminate_criterion=block_yaml.terminate_criterion,
                error_code_mapping=block_yaml.error_code_mapping,
                continue_on_failure=block_yaml.continue_on_failure,
                # only need one step for validation block
                max_steps_per_run=1,
            )

        elif block_yaml.block_type == BlockType.ACTION:
            action_block_parameters = (
                [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                if block_yaml.parameter_keys
                else []
            )

            if not block_yaml.navigation_goal:
                raise Exception("empty action instruction")

            return ActionBlock(
                label=block_yaml.label,
                url=block_yaml.url,
                title=block_yaml.title,
                task_type=TaskType.action,
                parameters=action_block_parameters,
                output_parameter=output_parameter,
                navigation_goal=block_yaml.navigation_goal,
                error_code_mapping=block_yaml.error_code_mapping,
                max_retries=block_yaml.max_retries,
                complete_on_download=block_yaml.complete_on_download,
                download_suffix=block_yaml.download_suffix,
                continue_on_failure=block_yaml.continue_on_failure,
                totp_verification_url=block_yaml.totp_verification_url,
                totp_identifier=block_yaml.totp_identifier,
                cache_actions=block_yaml.cache_actions,
                max_steps_per_run=1,
            )

        elif block_yaml.block_type == BlockType.NAVIGATION:
            navigation_block_parameters = (
                [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                if block_yaml.parameter_keys
                else []
            )
            return NavigationBlock(
                label=block_yaml.label,
                url=block_yaml.url,
                title=block_yaml.title,
                parameters=navigation_block_parameters,
                output_parameter=output_parameter,
                navigation_goal=block_yaml.navigation_goal,
                error_code_mapping=block_yaml.error_code_mapping,
                max_steps_per_run=block_yaml.max_steps_per_run,
                max_retries=block_yaml.max_retries,
                complete_on_download=block_yaml.complete_on_download,
                download_suffix=block_yaml.download_suffix,
                continue_on_failure=block_yaml.continue_on_failure,
                totp_verification_url=block_yaml.totp_verification_url,
                totp_identifier=block_yaml.totp_identifier,
                cache_actions=block_yaml.cache_actions,
                complete_criterion=block_yaml.complete_criterion,
                terminate_criterion=block_yaml.terminate_criterion,
            )

        elif block_yaml.block_type == BlockType.EXTRACTION:
            extraction_block_parameters = (
                [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                if block_yaml.parameter_keys
                else []
            )
            return ExtractionBlock(
                label=block_yaml.label,
                url=block_yaml.url,
                title=block_yaml.title,
                parameters=extraction_block_parameters,
                output_parameter=output_parameter,
                data_extraction_goal=block_yaml.data_extraction_goal,
                data_schema=block_yaml.data_schema,
                max_steps_per_run=block_yaml.max_steps_per_run,
                max_retries=block_yaml.max_retries,
                continue_on_failure=block_yaml.continue_on_failure,
                cache_actions=block_yaml.cache_actions,
            )

        elif block_yaml.block_type == BlockType.LOGIN:
            login_block_parameters = (
                [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                if block_yaml.parameter_keys
                else []
            )
            return LoginBlock(
                label=block_yaml.label,
                url=block_yaml.url,
                title=block_yaml.title,
                parameters=login_block_parameters,
                output_parameter=output_parameter,
                navigation_goal=block_yaml.navigation_goal,
                error_code_mapping=block_yaml.error_code_mapping,
                max_steps_per_run=block_yaml.max_steps_per_run,
                max_retries=block_yaml.max_retries,
                continue_on_failure=block_yaml.continue_on_failure,
                totp_verification_url=block_yaml.totp_verification_url,
                totp_identifier=block_yaml.totp_identifier,
                cache_actions=block_yaml.cache_actions,
                complete_criterion=block_yaml.complete_criterion,
                terminate_criterion=block_yaml.terminate_criterion,
            )

        elif block_yaml.block_type == BlockType.WAIT:
            if block_yaml.wait_sec <= 0 or block_yaml.wait_sec > settings.WORKFLOW_WAIT_BLOCK_MAX_SEC:
                raise InvalidWaitBlockTime(settings.WORKFLOW_WAIT_BLOCK_MAX_SEC)

            return WaitBlock(
                label=block_yaml.label,
                wait_sec=block_yaml.wait_sec,
                continue_on_failure=block_yaml.continue_on_failure,
                output_parameter=output_parameter,
            )

        elif block_yaml.block_type == BlockType.FILE_DOWNLOAD:
            file_download_block_parameters = (
                [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys]
                if block_yaml.parameter_keys
                else []
            )
            return FileDownloadBlock(
                label=block_yaml.label,
                url=block_yaml.url,
                title=block_yaml.title,
                parameters=file_download_block_parameters,
                output_parameter=output_parameter,
                navigation_goal=block_yaml.navigation_goal,
                error_code_mapping=block_yaml.error_code_mapping,
                max_steps_per_run=block_yaml.max_steps_per_run,
                max_retries=block_yaml.max_retries,
                download_suffix=block_yaml.download_suffix,
                continue_on_failure=block_yaml.continue_on_failure,
                totp_verification_url=block_yaml.totp_verification_url,
                totp_identifier=block_yaml.totp_identifier,
                cache_actions=block_yaml.cache_actions,
                complete_on_download=True,
            )
        elif block_yaml.block_type == BlockType.TaskV2:
            return TaskV2Block(
                label=block_yaml.label,
                prompt=block_yaml.prompt,
                url=block_yaml.url,
                totp_verification_url=block_yaml.totp_verification_url,
                totp_identifier=block_yaml.totp_identifier,
                max_iterations=block_yaml.max_iterations,
                max_steps=block_yaml.max_steps,
                output_parameter=output_parameter,
            )
        elif block_yaml.block_type == BlockType.GOTO_URL:
            return UrlBlock(
                label=block_yaml.label,
                url=block_yaml.url,
                output_parameter=output_parameter,
            )

        raise ValueError(f"Invalid block type {block_yaml.block_type}")

    async def create_empty_workflow(
        self,
        organization: Organization,
        title: str,
        proxy_location: ProxyLocation | None = None,
        status: WorkflowStatus = WorkflowStatus.published,
    ) -> Workflow:
        """
        Create a blank workflow with no blocks
        """
        # create a new workflow
        workflow_create_request = WorkflowCreateYAMLRequest(
            title=title,
            workflow_definition=WorkflowDefinitionYAML(
                parameters=[],
                blocks=[],
            ),
            proxy_location=proxy_location,
            status=status,
        )
        return await app.WORKFLOW_SERVICE.create_workflow_from_request(
            organization=organization,
            request=workflow_create_request,
        )

    async def get_workflow_run_timeline(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
    ) -> list[WorkflowRunTimeline]:
        """
        build the tree structure of the workflow run timeline
        """
        workflow_run_blocks = await app.DATABASE.get_workflow_run_blocks(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        # get all the actions for all workflow run blocks
        task_ids = [block.task_id for block in workflow_run_blocks if block.task_id]
        task_id_to_block: dict[str, WorkflowRunBlock] = {
            block.task_id: block for block in workflow_run_blocks if block.task_id
        }
        actions = await app.DATABASE.get_tasks_actions(task_ids=task_ids, organization_id=organization_id)
        for action in actions:
            if not action.task_id:
                continue
            task_block = task_id_to_block[action.task_id]
            task_block.actions.append(action)

        result = []
        block_map: dict[str, WorkflowRunTimeline] = {}
        counter = 0
        while workflow_run_blocks:
            counter += 1
            block = workflow_run_blocks.pop(0)
            workflow_run_timeline = WorkflowRunTimeline(
                type=WorkflowRunTimelineType.block,
                block=block,
                created_at=block.created_at,
                modified_at=block.modified_at,
            )
            if block.parent_workflow_run_block_id:
                if block.parent_workflow_run_block_id in block_map:
                    block_map[block.parent_workflow_run_block_id].children.append(workflow_run_timeline)
                else:
                    # put the block back to the queue
                    workflow_run_blocks.append(block)
            else:
                result.append(workflow_run_timeline)
                block_map[block.workflow_run_block_id] = workflow_run_timeline

            if counter > 1000:
                LOG.error("Too many blocks in the workflow run", workflow_run_id=workflow_run_id)
                break

        return result
