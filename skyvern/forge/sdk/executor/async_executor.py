import abc

import structlog
from fastapi import BackgroundTasks, Request

from skyvern.exceptions import OrganizationNotFound
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.observers import ObserverTaskStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.services import observer_service
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

LOG = structlog.get_logger()


class AsyncExecutor(abc.ABC):
    @abc.abstractmethod
    async def execute_task(
        self,
        request: Request | None,
        background_tasks: BackgroundTasks,
        task_id: str,
        organization_id: str,
        max_steps_override: int | None,
        api_key: str | None,
        browser_session_id: str | None,
        **kwargs: dict,
    ) -> None:
        pass

    @abc.abstractmethod
    async def execute_workflow(
        self,
        request: Request | None,
        background_tasks: BackgroundTasks,
        organization_id: str,
        workflow_id: str,
        workflow_run_id: str,
        max_steps_override: int | None,
        api_key: str | None,
        browser_session_id: str | None,
        **kwargs: dict,
    ) -> None:
        pass

    @abc.abstractmethod
    async def execute_cruise(
        self,
        request: Request | None,
        background_tasks: BackgroundTasks | None,
        organization_id: str,
        observer_cruise_id: str,
        max_iterations_override: int | str | None,
        browser_session_id: str | None,
        **kwargs: dict,
    ) -> None:
        pass


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

        close_browser_on_completion = browser_session_id is None

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

        context: SkyvernContext = skyvern_context.ensure_context()
        context.task_id = task.task_id
        context.organization_id = organization_id
        context.max_steps_override = max_steps_override

        if background_tasks:
            background_tasks.add_task(
                app.agent.execute_step,
                organization,
                task,
                step,
                api_key,
                close_browser_on_completion=close_browser_on_completion,
                browser_session_id=browser_session_id,
            )

    async def execute_workflow(
        self,
        request: Request | None,
        background_tasks: BackgroundTasks | None,
        organization_id: str,
        workflow_id: str,
        workflow_run_id: str,
        max_steps_override: int | None,
        api_key: str | None,
        browser_session_id: str | None,
        **kwargs: dict,
    ) -> None:
        LOG.info(
            "Executing workflow using background task executor",
            workflow_run_id=workflow_run_id,
        )

        organization = await app.DATABASE.get_organization(organization_id)
        if organization is None:
            raise OrganizationNotFound(organization_id)

        if background_tasks:
            background_tasks.add_task(
                app.WORKFLOW_SERVICE.execute_workflow,
                workflow_run_id=workflow_run_id,
                api_key=api_key,
                organization=organization,
                browser_session_id=browser_session_id,
            )

    async def execute_cruise(
        self,
        request: Request | None,
        background_tasks: BackgroundTasks | None,
        organization_id: str,
        observer_cruise_id: str,
        max_iterations_override: int | str | None,
        browser_session_id: str | None,
        **kwargs: dict,
    ) -> None:
        LOG.info(
            "Executing cruise using background task executor",
            observer_cruise_id=observer_cruise_id,
        )

        organization = await app.DATABASE.get_organization(organization_id)
        if organization is None:
            raise OrganizationNotFound(organization_id)

        observer_cruise = await app.DATABASE.get_observer_cruise(
            observer_cruise_id=observer_cruise_id, organization_id=organization_id
        )
        if not observer_cruise or not observer_cruise.workflow_run_id:
            raise ValueError("No observer cruise or no workflow run associated with observer cruise")

        # mark observer cruise as queued
        await app.DATABASE.update_observer_cruise(
            observer_cruise_id,
            status=ObserverTaskStatus.queued,
            organization_id=organization_id,
        )
        await app.DATABASE.update_workflow_run(
            workflow_run_id=observer_cruise.workflow_run_id,
            status=WorkflowRunStatus.queued,
        )

        if background_tasks:
            background_tasks.add_task(
                observer_service.run_observer_task,
                organization=organization,
                observer_cruise_id=observer_cruise_id,
                max_iterations_override=max_iterations_override,
                browser_session_id=browser_session_id,
            )
