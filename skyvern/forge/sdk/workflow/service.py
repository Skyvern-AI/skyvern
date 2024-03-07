import asyncio
import json
import time
from datetime import datetime

import requests
import structlog

from skyvern import analytics
from skyvern.exceptions import (
    FailedToSendWebhook,
    MissingValueForParameter,
    WorkflowNotFound,
    WorkflowOrganizationMismatch,
    WorkflowRunNotFound,
)
from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.security import generate_skyvern_signature
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.workflow.context_manager import ContextManager
from skyvern.forge.sdk.workflow.models.parameter import AWSSecretParameter, WorkflowParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowRequestBody,
    WorkflowRun,
    WorkflowRunParameter,
    WorkflowRunStatus,
    WorkflowRunStatusResponse,
)
from skyvern.webeye.browser_factory import BrowserState

LOG = structlog.get_logger()


class WorkflowService:
    async def setup_workflow_run(
        self,
        request_id: str | None,
        workflow_request: WorkflowRequestBody,
        workflow_id: str,
        organization_id: str,
        max_steps_override: int | None = None,
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
        LOG.info(f"Setting up workflow run for workflow {workflow_id}", workflow_id=workflow_id)
        # Validate the workflow and the organization
        workflow = await self.get_workflow(workflow_id=workflow_id)
        if workflow is None:
            LOG.error(f"Workflow {workflow_id} not found")
            raise WorkflowNotFound(workflow_id=workflow_id)
        if workflow.organization_id != organization_id:
            LOG.error(f"Workflow {workflow_id} does not belong to organization {organization_id}")
            raise WorkflowOrganizationMismatch(workflow_id=workflow_id, organization_id=organization_id)
        # Create the workflow run and set skyvern context
        workflow_run = await self.create_workflow_run(workflow_request=workflow_request, workflow_id=workflow_id)
        LOG.info(
            f"Created workflow run {workflow_run.workflow_run_id} for workflow {workflow.workflow_id}",
            request_id=request_id,
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_id=workflow.workflow_id,
            proxy_location=workflow_request.proxy_location,
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

        # Set workflow run status to running, create workflow run parameters
        await self.mark_workflow_run_as_running(workflow_run_id=workflow_run.workflow_run_id)

        # Create all the workflow run parameters, AWSSecretParameter won't have workflow run parameters created.
        all_workflow_parameters = await self.get_workflow_parameters(workflow_id=workflow.workflow_id)
        workflow_run_parameters = []
        for workflow_parameter in all_workflow_parameters:
            if workflow_request.data and workflow_parameter.key in workflow_request.data:
                request_body_value = workflow_request.data[workflow_parameter.key]
                workflow_run_parameter = await self.create_workflow_run_parameter(
                    workflow_run_id=workflow_run.workflow_run_id,
                    workflow_parameter_id=workflow_parameter.workflow_parameter_id,
                    value=request_body_value,
                )
            elif workflow_parameter.default_value is not None:
                workflow_run_parameter = await self.create_workflow_run_parameter(
                    workflow_run_id=workflow_run.workflow_run_id,
                    workflow_parameter_id=workflow_parameter.workflow_parameter_id,
                    value=workflow_parameter.default_value,
                )
            else:
                await self.mark_workflow_run_as_failed(workflow_run_id=workflow_run.workflow_run_id)
                raise MissingValueForParameter(
                    parameter_key=workflow_parameter.key,
                    workflow_id=workflow.workflow_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                )

            workflow_run_parameters.append(workflow_run_parameter)

        LOG.info(
            f"Created workflow run parameters for workflow run {workflow_run.workflow_run_id}",
            workflow_run_id=workflow_run.workflow_run_id,
        )

        return workflow_run

    async def execute_workflow(
        self,
        workflow_run_id: str,
        api_key: str,
    ) -> WorkflowRun:
        """Execute a workflow."""
        workflow_run = await self.get_workflow_run(workflow_run_id=workflow_run_id)
        workflow = await self.get_workflow(workflow_id=workflow_run.workflow_id)

        await app.BROWSER_MANAGER.get_or_create_for_workflow_run(workflow_run=workflow_run)

        # Get all <workflow parameter, workflow run parameter> tuples
        wp_wps_tuples = await self.get_workflow_run_parameter_tuples(workflow_run_id=workflow_run.workflow_run_id)
        # todo(kerem): do this in a better way (a shared context manager? (not really shared because we use batch job))
        context_manager = ContextManager(wp_wps_tuples)
        # Execute workflow blocks
        blocks = workflow.workflow_definition.blocks
        for block_idx, block in enumerate(blocks):
            parameters = block.get_all_parameters()
            await context_manager.register_block_parameters(parameters)
            LOG.info(
                f"Executing root block {block.block_type} at index {block_idx} for workflow run {workflow_run.workflow_run_id}",
                block_type=block.block_type,
                workflow_run_id=workflow_run.workflow_run_id,
                block_idx=block_idx,
            )
            await block.execute(workflow_run_id=workflow_run.workflow_run_id, context_manager=context_manager)

        # Get last task for workflow run
        task = await self.get_last_task_for_workflow_run(workflow_run_id=workflow_run.workflow_run_id)
        if not task:
            LOG.warning(
                f"No tasks found for workflow run {workflow_run.workflow_run_id}, not sending webhook",
                workflow_run_id=workflow_run.workflow_run_id,
            )
            return workflow_run

        # Update workflow status
        if task.status == "completed":
            await self.mark_workflow_run_as_completed(workflow_run_id=workflow_run.workflow_run_id)
        elif task.status == "failed":
            await self.mark_workflow_run_as_failed(workflow_run_id=workflow_run.workflow_run_id)
        elif task.status == "terminated":
            await self.mark_workflow_run_as_terminated(workflow_run_id=workflow_run.workflow_run_id)
        else:
            LOG.warning(
                f"Task {task.task_id} has an incomplete status {task.status}, not updating workflow run status",
                workflow_id=workflow.workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
                task_id=task.task_id,
                status=task.status,
                workflow_run_status=workflow_run.status,
            )

        await self.send_workflow_response(
            workflow=workflow,
            workflow_run=workflow_run,
            api_key=api_key,
            last_task=task,
        )
        return workflow_run

    async def create_workflow(
        self,
        organization_id: str,
        title: str,
        workflow_definition: WorkflowDefinition,
        description: str | None = None,
    ) -> Workflow:
        return await app.DATABASE.create_workflow(
            organization_id=organization_id,
            title=title,
            description=description,
            workflow_definition=workflow_definition.model_dump() if workflow_definition else None,
        )

    async def get_workflow(self, workflow_id: str) -> Workflow:
        workflow = await app.DATABASE.get_workflow(workflow_id=workflow_id)
        if not workflow:
            raise WorkflowNotFound(workflow_id)
        return workflow

    async def update_workflow(
        self,
        workflow_id: str,
        title: str | None = None,
        description: str | None = None,
        workflow_definition: WorkflowDefinition | None = None,
    ) -> Workflow | None:
        return await app.DATABASE.update_workflow(
            workflow_id=workflow_id,
            title=title,
            description=description,
            workflow_definition=workflow_definition.model_dump() if workflow_definition else None,
        )

    async def create_workflow_run(self, workflow_request: WorkflowRequestBody, workflow_id: str) -> WorkflowRun:
        return await app.DATABASE.create_workflow_run(
            workflow_id=workflow_id,
            proxy_location=workflow_request.proxy_location,
            webhook_callback_url=workflow_request.webhook_callback_url,
        )

    async def mark_workflow_run_as_completed(self, workflow_run_id: str) -> None:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as completed", workflow_run_id=workflow_run_id, status="completed"
        )
        await app.DATABASE.update_workflow_run(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.completed,
        )

    async def mark_workflow_run_as_failed(self, workflow_run_id: str) -> None:
        LOG.info(f"Marking workflow run {workflow_run_id} as failed", workflow_run_id=workflow_run_id, status="failed")
        await app.DATABASE.update_workflow_run(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.failed,
        )

    async def mark_workflow_run_as_running(self, workflow_run_id: str) -> None:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as running", workflow_run_id=workflow_run_id, status="running"
        )
        await app.DATABASE.update_workflow_run(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.running,
        )

    async def mark_workflow_run_as_terminated(self, workflow_run_id: str) -> None:
        LOG.info(
            f"Marking workflow run {workflow_run_id} as terminated",
            workflow_run_id=workflow_run_id,
            status="terminated",
        )
        await app.DATABASE.update_workflow_run(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.terminated,
        )

    async def get_workflow_runs(self, workflow_id: str) -> list[WorkflowRun]:
        return await app.DATABASE.get_workflow_runs(workflow_id=workflow_id)

    async def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun:
        workflow_run = await app.DATABASE.get_workflow_run(workflow_run_id=workflow_run_id)
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

    async def get_workflow_parameters(self, workflow_id: str) -> list[WorkflowParameter]:
        return await app.DATABASE.get_workflow_parameters(workflow_id=workflow_id)

    async def create_workflow_run_parameter(
        self,
        workflow_run_id: str,
        workflow_parameter_id: str,
        value: bool | int | float | str | dict | list,
    ) -> WorkflowRunParameter:
        return await app.DATABASE.create_workflow_run_parameter(
            workflow_run_id=workflow_run_id,
            workflow_parameter_id=workflow_parameter_id,
            value=json.dumps(value) if isinstance(value, (dict, list)) else value,
        )

    async def get_workflow_run_parameter_tuples(
        self, workflow_run_id: str
    ) -> list[tuple[WorkflowParameter, WorkflowRunParameter]]:
        return await app.DATABASE.get_workflow_run_parameters(workflow_run_id=workflow_run_id)

    async def get_last_task_for_workflow_run(self, workflow_run_id: str) -> Task | None:
        return await app.DATABASE.get_last_task_for_workflow_run(workflow_run_id=workflow_run_id)

    async def get_tasks_by_workflow_run_id(self, workflow_run_id: str) -> list[Task]:
        return await app.DATABASE.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run_id)

    async def build_workflow_run_status_response(
        self, workflow_id: str, workflow_run_id: str, organization_id: str
    ) -> WorkflowRunStatusResponse:
        workflow = await self.get_workflow(workflow_id=workflow_id)
        if workflow is None:
            LOG.error(f"Workflow {workflow_id} not found")
            raise WorkflowNotFound(workflow_id=workflow_id)
        if workflow.organization_id != organization_id:
            LOG.error(f"Workflow {workflow_id} does not belong to organization {organization_id}")
            raise WorkflowOrganizationMismatch(workflow_id=workflow_id, organization_id=organization_id)

        workflow_run = await self.get_workflow_run(workflow_run_id=workflow_run_id)
        workflow_run_tasks = await app.DATABASE.get_tasks_by_workflow_run_id(workflow_run_id=workflow_run_id)
        screenshot_urls = []
        # get the last screenshot for the last 3 tasks of the workflow run
        for task in workflow_run_tasks[::-1]:
            screenshot_artifact = await app.DATABASE.get_latest_artifact(
                task_id=task.task_id,
                artifact_types=[ArtifactType.SCREENSHOT_ACTION, ArtifactType.SCREENSHOT_FINAL],
                organization_id=organization_id,
            )
            if screenshot_artifact:
                screenshot_url = await app.ARTIFACT_MANAGER.get_share_link(screenshot_artifact)
                if screenshot_url:
                    screenshot_urls.append(screenshot_url)
            if len(screenshot_urls) >= 3:
                break

        recording_url = None
        recording_artifact = await app.DATABASE.get_artifact_for_workflow_run(
            workflow_run_id=workflow_run_id, artifact_type=ArtifactType.RECORDING, organization_id=organization_id
        )
        if recording_artifact:
            recording_url = await app.ARTIFACT_MANAGER.get_share_link(recording_artifact)

        workflow_parameter_tuples = await app.DATABASE.get_workflow_run_parameters(workflow_run_id=workflow_run_id)
        parameters_with_value = {wfp.key: wfrp.value for wfp, wfrp in workflow_parameter_tuples}
        return WorkflowRunStatusResponse(
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            status=workflow_run.status,
            proxy_location=workflow_run.proxy_location,
            webhook_callback_url=workflow_run.webhook_callback_url,
            created_at=workflow_run.created_at,
            modified_at=workflow_run.modified_at,
            parameters=parameters_with_value,
            screenshot_urls=screenshot_urls,
            recording_url=recording_url,
        )

    async def send_workflow_response(
        self,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        last_task: Task,
        api_key: str | None = None,
        close_browser_on_completion: bool = True,
    ) -> None:
        analytics.capture("skyvern-oss-agent-workflow-status", {"status": workflow_run.status})
        browser_state = await app.BROWSER_MANAGER.cleanup_for_workflow_run(
            workflow_run.workflow_run_id, close_browser_on_completion
        )
        if browser_state:
            await self.persist_video_data(browser_state, workflow, workflow_run)
            await self.persist_har_data(browser_state, last_task, workflow, workflow_run)

        # Wait for all tasks to complete before generating the links for the artifacts
        all_workflow_tasks = await app.DATABASE.get_tasks_by_workflow_run_id(
            workflow_run_id=workflow_run.workflow_run_id
        )
        all_workflow_task_ids = [task.task_id for task in all_workflow_tasks]
        await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks_for_tasks(all_workflow_task_ids)

        try:
            # Wait for all tasks to complete. Currently we're using asyncio.create_task() only for uploading artifacts to S3.
            # We're excluding the current task from the list of tasks to wait for to prevent a deadlock.
            st = time.time()
            async with asyncio.timeout(30):
                await asyncio.gather(
                    *[aio_task for aio_task in (asyncio.all_tasks() - {asyncio.current_task()}) if not aio_task.done()]
                )
            LOG.info(
                f"Waiting for all S3 uploads to complete took {time.time() - st} seconds",
                duration=time.time() - st,
            )
        except asyncio.TimeoutError:
            LOG.warning(
                "Timed out waiting for all S3 uploads to complete, not all artifacts may be uploaded. Waited 30 seconds.",
                workflow_id=workflow.workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
            )

        if not workflow_run.webhook_callback_url:
            LOG.warning(
                "Workflow has no webhook callback url. Not sending workflow response",
                workflow_id=workflow.workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
            )
            return

        if not api_key:
            LOG.warning(
                "Request has no api key. Not sending workflow response",
                workflow_id=workflow.workflow_id,
                workflow_run_id=workflow_run.workflow_run_id,
            )
            return

        workflow_run_status_response = await self.build_workflow_run_status_response(
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=workflow.organization_id,
        )
        # send task_response to the webhook callback url
        # TODO: use async requests (httpx)
        timestamp = str(int(datetime.utcnow().timestamp()))
        payload = workflow_run_status_response.model_dump_json()
        signature = generate_skyvern_signature(
            payload=payload,
            api_key=api_key,
        )
        headers = {
            "x-skyvern-timestamp": timestamp,
            "x-skyvern-signature": signature,
            "Content-Type": "application/json",
        }
        LOG.info(
            "Sending webhook run status to webhook callback url",
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            webhook_callback_url=workflow_run.webhook_callback_url,
            payload=payload,
            headers=headers,
        )
        try:
            resp = requests.post(workflow_run.webhook_callback_url, data=payload, headers=headers)
            if resp.ok:
                LOG.info(
                    "Webhook sent successfully",
                    workflow_id=workflow.workflow_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    resp_code=resp.status_code,
                    resp_text=resp.text,
                )
            else:
                LOG.info(
                    "Webhook failed",
                    workflow_id=workflow.workflow_id,
                    workflow_run_id=workflow_run.workflow_run_id,
                    resp=resp,
                    resp_code=resp.status_code,
                    resp_text=resp.text,
                    resp_json=resp.json(),
                )
        except Exception as e:
            raise FailedToSendWebhook(
                workflow_id=workflow.workflow_id, workflow_run_id=workflow_run.workflow_run_id
            ) from e

    async def persist_video_data(
        self, browser_state: BrowserState, workflow: Workflow, workflow_run: WorkflowRun
    ) -> None:
        # Create recording artifact after closing the browser, so we can get an accurate recording
        video_data = await app.BROWSER_MANAGER.get_video_data(
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            browser_state=browser_state,
        )
        if video_data:
            await app.ARTIFACT_MANAGER.update_artifact_data(
                artifact_id=browser_state.browser_artifacts.video_artifact_id,
                organization_id=workflow.organization_id,
                data=video_data,
            )

    async def persist_har_data(
        self, browser_state: BrowserState, last_task: Task, workflow: Workflow, workflow_run: WorkflowRun
    ) -> None:
        har_data = await app.BROWSER_MANAGER.get_har_data(
            workflow_id=workflow.workflow_id, workflow_run_id=workflow_run.workflow_run_id, browser_state=browser_state
        )
        if har_data:
            last_step = await app.DATABASE.get_latest_step(
                task_id=last_task.task_id, organization_id=last_task.organization_id
            )

            if last_step:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=last_step,
                    artifact_type=ArtifactType.HAR,
                    data=har_data,
                )
