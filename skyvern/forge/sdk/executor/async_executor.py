import abc

from fastapi import BackgroundTasks

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import Organization
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus


class AsyncExecutor(abc.ABC):
    @abc.abstractmethod
    async def execute_task(
        self,
        background_tasks: BackgroundTasks,
        task: Task,
        organization: Organization,
        max_steps_override: int | None,
        api_key: str | None,
    ) -> None:
        pass

    @abc.abstractmethod
    async def execute_workflow(
        self,
        background_tasks: BackgroundTasks,
        organization: Organization,
        workflow_id: str,
        workflow_run_id: str,
        max_steps_override: int | None,
        api_key: str | None,
    ) -> None:
        pass


class BackgroundTaskExecutor(AsyncExecutor):
    async def execute_task(
        self,
        background_tasks: BackgroundTasks,
        task: Task,
        organization: Organization,
        max_steps_override: int | None,
        api_key: str | None,
    ) -> None:
        step = await app.DATABASE.create_step(
            task.task_id,
            order=0,
            retry_index=0,
            organization_id=organization.organization_id,
        )

        task = await app.DATABASE.update_task(
            task.task_id,
            TaskStatus.running,
            organization_id=organization.organization_id,
        )

        context: SkyvernContext = skyvern_context.ensure_context()
        context.task_id = task.task_id
        context.organization_id = organization.organization_id
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
        background_tasks: BackgroundTasks,
        organization: Organization,
        workflow_id: str,
        workflow_run_id: str,
        max_steps_override: int | None,
        api_key: str | None,
    ) -> None:
        background_tasks.add_task(
            app.WORKFLOW_SERVICE.execute_workflow,
            workflow_run_id=workflow_run_id,
            api_key=api_key,
        )
