from typing import Any, Dict, Literal, Type

from httpx import AsyncClient
from langchain.tools import BaseTool
from pydantic import BaseModel, Field
from skyvern_langchain.schema import CreateTaskInput, GetTaskInput
from skyvern_langchain.settings import settings

from skyvern.client import AsyncSkyvern
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Request
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, TaskRequest, TaskResponse


class SkyvernTaskBaseTool(BaseTool):
    api_key: str = Field(default=settings.api_key)
    base_url: str = Field(default=settings.base_url)
    engine: Literal["TaskV1", "TaskV2"] = Field(default=settings.engine)
    run_task_timeout_seconds: int = Field(default=settings.run_task_timeout_seconds)

    def get_client(self) -> AsyncSkyvern:
        httpx_client = AsyncClient(
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
            },
        )
        return AsyncSkyvern(base_url=self.base_url, httpx_client=httpx_client)

    def _run(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("skyvern task tool does not support sync")


class RunTask(SkyvernTaskBaseTool):
    name: str = "run-skyvern-client-task"
    description: str = """Use Skyvern client to run a task. This function won't return until the task is finished."""
    args_schema: Type[BaseModel] = CreateTaskInput

    async def _arun(self, user_prompt: str, url: str | None = None) -> TaskResponse | Dict[str, Any | None]:
        if self.engine == "TaskV1":
            return await self._arun_task_v1(user_prompt=user_prompt, url=url)
        else:
            return await self._arun_task_v2(user_prompt=user_prompt, url=url)

    async def _arun_task_v1(self, user_prompt: str, url: str | None = None) -> TaskResponse:
        task_generation = await self.get_client().agent.generate_task(
            prompt=user_prompt,
        )

        task_request = TaskRequest.model_validate(task_generation, from_attributes=True)
        if url is not None:
            task_request.url = url

        return await self.get_client().agent.run_task_v1(
            timeout_seconds=self.run_task_timeout_seconds,
            url=task_request.url,
            title=task_request.title,
            navigation_goal=task_request.navigation_goal,
            data_extraction_goal=task_request.data_extraction_goal,
            navigation_payload=task_request.navigation_goal,
            error_code_mapping=task_request.error_code_mapping,
            extracted_information_schema=task_request.extracted_information_schema,
            complete_criterion=task_request.complete_criterion,
            terminate_criterion=task_request.terminate_criterion,
        )

    async def _arun_task_v2(self, user_prompt: str, url: str | None = None) -> TaskResponse:
        task_request = TaskV2Request(url=url, user_prompt=user_prompt)
        return await self.get_client().agent.run_observer_task_v_2(
            timeout_seconds=self.run_task_timeout_seconds,
            user_prompt=task_request.user_prompt,
            url=task_request.url,
            browser_session_id=task_request.browser_session_id,
        )


class DispatchTask(SkyvernTaskBaseTool):
    name: str = "dispatch-skyvern-client-task"
    description: str = """Use Skyvern client to dispatch a task. This function will return immediately and the task will be running in the background."""
    args_schema: Type[BaseModel] = CreateTaskInput

    async def _arun(self, user_prompt: str, url: str | None = None) -> CreateTaskResponse | Dict[str, Any | None]:
        if self.engine == "TaskV1":
            return await self._arun_task_v1(user_prompt=user_prompt, url=url)
        else:
            return await self._arun_task_v2(user_prompt=user_prompt, url=url)

    async def _arun_task_v1(self, user_prompt: str, url: str | None = None) -> CreateTaskResponse:
        task_generation = await self.get_client().agent.generate_task(
            prompt=user_prompt,
        )

        task_request = TaskRequest.model_validate(task_generation, from_attributes=True)
        if url is not None:
            task_request.url = url

        return await self.get_client().agent.create_task(
            url=task_request.url,
            title=task_request.title,
            navigation_goal=task_request.navigation_goal,
            data_extraction_goal=task_request.data_extraction_goal,
            navigation_payload=task_request.navigation_goal,
            error_code_mapping=task_request.error_code_mapping,
            extracted_information_schema=task_request.extracted_information_schema,
            complete_criterion=task_request.complete_criterion,
            terminate_criterion=task_request.terminate_criterion,
        )

    async def _arun_task_v2(self, user_prompt: str, url: str | None = None) -> Dict[str, Any | None]:
        task_request = TaskV2Request(url=url, user_prompt=user_prompt)
        return await self.get_client().agent.observer_task_v_2(
            user_prompt=task_request.user_prompt,
            url=task_request.url,
            browser_session_id=task_request.browser_session_id,
        )


class GetTask(SkyvernTaskBaseTool):
    name: str = "get-skyvern-client-task"
    description: str = """Use Skyvern client to get a task."""
    args_schema: Type[BaseModel] = GetTaskInput

    async def _arun(self, task_id: str) -> Dict[str, Any | None]:
        if self.engine == "TaskV1":
            return await self._arun_task_v1(task_id=task_id)
        else:
            return await self._arun_task_v2(task_id=task_id)

    async def _arun_task_v1(self, task_id: str) -> TaskResponse:
        return await self.get_client().agent.get_task(task_id=task_id)

    async def _arun_task_v2(self, task_id: str) -> Dict[str, Any | None]:
        return await self.get_client().agent.get_observer_task_v_2(task_id=task_id)
