import abc
from typing import Any

from fastapi import BackgroundTasks, Request

from skyvern.forge.sdk.schemas.organizations import Organization


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
        pass

    @abc.abstractmethod
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
        pass

    @abc.abstractmethod
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
        pass
