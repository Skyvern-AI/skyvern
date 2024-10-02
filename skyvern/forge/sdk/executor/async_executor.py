import abc

import structlog
from fastapi import BackgroundTasks, Request

from skyvern.exceptions import OrganizationNotFound
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.tasks import TaskStatus

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
        **kwargs: dict,
    ) -> None:
        pass


class BackgroundTaskExecutor(AsyncExecutor):
    async def execute_task(
        self,
        request: Request | None,
        background_tasks: BackgroundTasks,
        task_id: str,
        organization_id: str,
        max_steps_override: int | None,
        api_key: str | None,
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

        context: SkyvernContext = skyvern_context.ensure_context()
        context.task_id = task.task_id
        context.organization_id = organization_id
        context.max_steps_override = max_steps_override

        background_tasks.add_task(
            app.agent.execute_step,
            organization,
            task,
            step,
            api_key,
        )

    async def execute_workflow(
        self,
        request: Request | None,
        background_tasks: BackgroundTasks,
        organization_id: str,
        workflow_id: str,
        workflow_run_id: str,
        max_steps_override: int | None,
        api_key: str | None,
        **kwargs: dict,
    ) -> None:
        LOG.info(
            "Executing workflow using background task executor",
            workflow_run_id=workflow_run_id,
        )

        organization = await app.DATABASE.get_organization(organization_id)
        if organization is None:
            raise OrganizationNotFound(organization_id)

        background_tasks.add_task(
            app.WORKFLOW_SERVICE.execute_workflow,
            workflow_run_id=workflow_run_id,
            api_key=api_key,
            organization=organization,
        )
