from typing import Any, Dict, List, Literal, Optional

from httpx import AsyncClient
from llama_index.core.tools import FunctionTool
from llama_index.core.tools.tool_spec.base import SPEC_FUNCTION_TYPE, BaseToolSpec
from pydantic import BaseModel
from skyvern_llamaindex.settings import settings

from skyvern.client import AsyncSkyvern
from skyvern.forge.sdk.schemas.task_v2 import TaskV2Request
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, TaskRequest, TaskResponse


class SkyvernTool(BaseModel):
    api_key: str = settings.api_key
    base_url: str = settings.base_url

    def run_task(self) -> FunctionTool:
        task_tool_spec = SkyvernTaskToolSpec(
            api_key=self.api_key,
            base_url=self.base_url,
        )

        return task_tool_spec.to_tool_list(["run_task"])[0]

    def dispatch_task(self) -> FunctionTool:
        task_tool_spec = SkyvernTaskToolSpec(
            api_key=self.api_key,
            base_url=self.base_url,
        )

        return task_tool_spec.to_tool_list(["dispatch_task"])[0]

    def get_task(self) -> FunctionTool:
        task_tool_spec = SkyvernTaskToolSpec(
            api_key=self.api_key,
            base_url=self.base_url,
        )

        return task_tool_spec.to_tool_list(["get_task"])[0]


class SkyvernTaskToolSpec(BaseToolSpec):
    spec_functions: List[SPEC_FUNCTION_TYPE] = [
        "run_task",
        "dispatch_task",
        "get_task",
    ]

    def __init__(
        self,
        *,
        api_key: str = settings.api_key,
        base_url: str = settings.base_url,
        engine: Literal["TaskV1", "TaskV2"] = settings.engine,
        run_task_timeout_seconds: int = settings.run_task_timeout_seconds,
    ):
        httpx_client = AsyncClient(
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
            },
        )
        self.engine = engine
        self.run_task_timeout_seconds = run_task_timeout_seconds
        self.client = AsyncSkyvern(base_url=base_url, httpx_client=httpx_client)

    async def run_task(self, user_prompt: str, url: Optional[str] = None) -> TaskResponse | Dict[str, Any | None]:
        """
        Use Skyvern client to run a task. This function won't return until the task is finished.

        Args:
            user_prompt[str]: The user's prompt describing the task.
            url (Optional[str]): The URL of the target website for the task.
        """

        if self.engine == "TaskV1":
            return await self.run_task_v1(user_prompt=user_prompt, url=url)
        else:
            return await self.run_task_v2(user_prompt=user_prompt, url=url)

    async def dispatch_task(
        self, user_prompt: str, url: Optional[str] = None
    ) -> CreateTaskResponse | Dict[str, Any | None]:
        """
        Use Skyvern client to dispatch a task. This function will return immediately and the task will be running in the background.

        Args:
            user_prompt[str]: The user's prompt describing the task.
            url (Optional[str]): The URL of the target website for the task.
        """

        if self.engine == "TaskV1":
            return await self.dispatch_task_v1(user_prompt=user_prompt, url=url)
        else:
            return await self.dispatch_task_v2(user_prompt=user_prompt, url=url)

    async def get_task(self, task_id: str) -> TaskResponse | Dict[str, Any | None]:
        """
        Use Skyvern client to get a task.

        Args:
            task_id[str]: The id of the task.
        """

        if self.engine == "TaskV1":
            return await self.get_task_v1(task_id)
        else:
            return await self.get_task_v2(task_id)

    async def run_task_v1(self, user_prompt: str, url: Optional[str] = None) -> TaskResponse:
        task_generation = await self.client.agent.generate_task(
            prompt=user_prompt,
        )
        task_request = TaskRequest.model_validate(task_generation, from_attributes=True)
        if url is not None:
            task_request.url = url

        return await self.client.agent.run_task_v1(
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

    async def dispatch_task_v1(self, user_prompt: str, url: Optional[str] = None) -> CreateTaskResponse:
        task_generation = await self.client.agent.generate_task(
            prompt=user_prompt,
        )
        task_request = TaskRequest.model_validate(task_generation, from_attributes=True)
        if url is not None:
            task_request.url = url

        return await self.client.agent.create_task(
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

    async def get_task_v1(self, task_id: str) -> TaskResponse:
        return await self.client.agent.get_task(task_id=task_id)

    async def run_task_v2(self, user_prompt: str, url: Optional[str] = None) -> Dict[str, Any | None]:
        task_request = TaskV2Request(url=url, user_prompt=user_prompt)
        return await self.client.agent.run_observer_task_v_2(
            timeout_seconds=self.run_task_timeout_seconds,
            user_prompt=task_request.user_prompt,
            url=task_request.url,
            browser_session_id=task_request.browser_session_id,
        )

    async def dispatch_task_v2(self, user_prompt: str, url: Optional[str] = None) -> Dict[str, Any | None]:
        task_request = TaskV2Request(url=url, user_prompt=user_prompt)
        return await self.client.agent.observer_task_v_2(
            user_prompt=task_request.user_prompt,
            url=task_request.url,
            browser_session_id=task_request.browser_session_id,
        )

    async def get_task_v2(self, task_id: str) -> Dict[str, Any | None]:
        return await self.client.agent.get_observer_task_v_2(task_id=task_id)
