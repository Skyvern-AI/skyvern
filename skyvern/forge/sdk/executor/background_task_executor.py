from typing import Any

import structlog
from fastapi import BackgroundTasks, Request

from skyvern.exceptions import OrganizationNotFound
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.executor.async_executor import AsyncExecutor
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Status
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.runs import RunEngine, RunType
from skyvern.services import script_service, task_v2_service
from skyvern.utils.files import initialize_skyvern_state_file

LOG = structlog.get_logger()


class BackgroundTaskExecutor(AsyncExecutor):
    async def execute_task(
        self,
        request: Request | None,
        background_tasks: BackgroundTasks | None,
        task_id: str,
        organization_id: str,
        max_steps_override: int | None,
        api_key: str | None,
        browser_session_id: str | None,
        **kwargs: dict,
    ) -> None:
        LOG.info("Executing task using background task executor", task_id=task_id)
        organization = await app.DATABASE.get_organization(organization_id)
        if organization is None:
            raise OrganizationNotFound(organization_id)

        step = await app.DATABASE.create_step(
            task_id,
            order=0,
            retry_index=0,
            organization_id=organization_id,
        )

        task = await app.DATABASE.update_task(
            task_id,
            status=TaskStatus.running,
            organization_id=organization_id,
        )

        close_browser_on_completion = browser_session_id is None and not task.browser_address

        run_obj = await app.DATABASE.get_run(run_id=task_id, organization_id=organization_id)
        engine = RunEngine.skyvern_v1
        if run_obj and run_obj.task_run_type == RunType.openai_cua:
            engine = RunEngine.openai_cua
        elif run_obj and run_obj.task_run_type == RunType.anthropic_cua:
            engine = RunEngine.anthropic_cua
        elif run_obj and run_obj.task_run_type == RunType.ui_tars:
            engine = RunEngine.ui_tars

        context: SkyvernContext = skyvern_context.ensure_context()
        context.task_id = task.task_id
        context.run_id = context.run_id or task.task_id
        context.organization_id = organization_id
        context.max_steps_override = max_steps_override
        context.max_screenshot_scrolls = task.max_screenshot_scrolls

        if background_tasks:
            await initialize_skyvern_state_file(task_id=task_id, organization_id=organization_id)
            background_tasks.add_task(
                app.agent.execute_step,
                organization,
                task,
                step,
                api_key,
                close_browser_on_completion=close_browser_on_completion,
                browser_session_id=browser_session_id,
                engine=engine,
            )

    async def execute_workflow(
        self,
        request: Request | None,
        background_tasks: BackgroundTasks | None,
        organization: Organization,
        workflow_id: str,
        workflow_run_id: str,
        workflow_permanent_id: str,
        max_steps_override: int | None,
        api_key: str | None,
        browser_session_id: str | None,
        block_labels: list[str] | None,
        block_outputs: dict[str, Any] | None,
        **kwargs: dict,
    ) -> None:
        if background_tasks:
            LOG.info(
                "Executing workflow using background task executor",
                workflow_run_id=workflow_run_id,
            )

            await initialize_skyvern_state_file(
                workflow_run_id=workflow_run_id, organization_id=organization.organization_id
            )

            background_tasks.add_task(
                app.WORKFLOW_SERVICE.execute_workflow,
                workflow_run_id=workflow_run_id,
                api_key=api_key,
                organization=organization,
                browser_session_id=browser_session_id,
                block_labels=block_labels,
                block_outputs=block_outputs,
            )
        else:
            LOG.warning("Background tasks not enabled, skipping workflow execution")

    async def execute_task_v2(
        self,
        request: Request | None,
        background_tasks: BackgroundTasks | None,
        organization_id: str,
        task_v2_id: str,
        max_steps_override: int | str | None,
        browser_session_id: str | None,
        **kwargs: dict,
    ) -> None:
        LOG.info(
            "Executing cruise using background task executor",
            task_v2_id=task_v2_id,
        )

        organization = await app.DATABASE.get_organization(organization_id)
        if organization is None:
            raise OrganizationNotFound(organization_id)

        task_v2 = await app.DATABASE.get_task_v2(task_v2_id=task_v2_id, organization_id=organization_id)
        if not task_v2 or not task_v2.workflow_run_id:
            raise ValueError("No task v2 or no workflow run associated with task v2")

        # mark task v2 as queued
        await app.DATABASE.update_task_v2(
            task_v2_id=task_v2_id,
            status=TaskV2Status.queued,
            organization_id=organization_id,
        )
        await app.DATABASE.update_workflow_run(
            workflow_run_id=task_v2.workflow_run_id,
            status=WorkflowRunStatus.queued,
        )

        if background_tasks:
            await initialize_skyvern_state_file(
                workflow_run_id=task_v2.workflow_run_id, organization_id=organization_id
            )
            background_tasks.add_task(
                task_v2_service.run_task_v2,
                organization=organization,
                task_v2_id=task_v2_id,
                max_steps_override=max_steps_override,
                browser_session_id=browser_session_id,
            )

    async def execute_script(
        self,
        request: Request | None,
        script_id: str,
        organization_id: str,
        parameters: dict[str, Any] | None = None,
        workflow_run_id: str | None = None,
        background_tasks: BackgroundTasks | None = None,
        **kwargs: dict,
    ) -> None:
        if background_tasks:
            background_tasks.add_task(
                script_service.execute_script,
                script_id=script_id,
                organization_id=organization_id,
                parameters=parameters,
                workflow_run_id=workflow_run_id,
                background_tasks=background_tasks,
            )
