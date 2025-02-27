from typing import Any, Dict, Literal, Type

from httpx import AsyncClient
from langchain.tools import BaseTool
from pydantic import BaseModel, Field
from skyvern_langchain.schema import GetTaskInput, TaskV1Request, TaskV2Request
from skyvern_langchain.settings import settings

from skyvern.client import AsyncSkyvern
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, TaskResponse


class SkyvernTaskBaseTool(BaseTool):
    credential: str = Field(default=settings.credential)
    base_url: str = Field(default=settings.base_url)
    engine: Literal["TaskV1", "TaskV2"] = Field(default=settings.engine)

    def get_client(self) -> AsyncSkyvern:
        httpx_client = AsyncClient(
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.credential,
            },
        )
        return AsyncSkyvern(base_url=self.base_url, httpx_client=httpx_client)

    def _run(self) -> None:
        raise NotImplementedError("skyvern task tool does not support sync")


class RunTask(SkyvernTaskBaseTool):
    name: str = "run-skyvern-client-task"
    description: str = """Use Skyvern client to run a task. This function won't return until the task is finished."""

    def get_input_schema(self) -> Type[BaseModel]:
        if self.engine == "TaskV1":
            return TaskV1Request
        else:
            return TaskV2Request

    async def _arun(self, **kwargs: Dict[str, Any]) -> TaskResponse | Dict[str, Any | None]:
        if self.engine == "TaskV1":
            return await self._arun_task_v1(**kwargs)
        else:
            return await self._arun_task_v2(**kwargs)

    async def _arun_task_v1(self, **kwargs: Dict[str, Any]) -> TaskResponse:
        task_request = TaskV1Request(**kwargs)
        return await self.get_client().agent.run_task(
            max_steps_override=task_request.max_steps,
            timeout_seconds=task_request.timeout_seconds,
            url=task_request.url,
            title=task_request.title,
            webhook_callback_url=task_request.webhook_callback_url,
            totp_verification_url=task_request.totp_verification_url,
            totp_identifier=task_request.totp_identifier,
            navigation_goal=task_request.navigation_goal,
            data_extraction_goal=task_request.data_extraction_goal,
            navigation_payload=task_request.navigation_goal,
            error_code_mapping=task_request.error_code_mapping,
            proxy_location=task_request.proxy_location,
            extracted_information_schema=task_request.extracted_information_schema,
            complete_criterion=task_request.complete_criterion,
            terminate_criterion=task_request.terminate_criterion,
            browser_session_id=task_request.browser_session_id,
        )

    async def _arun_task_v2(self, **kwargs: Dict[str, Any]) -> TaskResponse:
        task_request = TaskV2Request(**kwargs)
        return await self.get_client().agent.run_observer_task_v_2(
            max_iterations_override=task_request.max_iterations,
            timeout_seconds=task_request.timeout_seconds,
            user_prompt=task_request.user_prompt,
            url=task_request.url,
            browser_session_id=task_request.browser_session_id,
            webhook_callback_url=task_request.webhook_callback_url,
            totp_verification_url=task_request.totp_verification_url,
            totp_identifier=task_request.totp_identifier,
            proxy_location=task_request.proxy_location,
        )


class DispatchTask(SkyvernTaskBaseTool):
    name: str = "dispatch-skyvern-client-task"
    description: str = """Use Skyvern client to dispatch a task. This function will return immediately and the task will be running in the background."""

    def get_input_schema(self) -> Type[BaseModel]:
        if self.engine == "TaskV1":
            return TaskV1Request
        else:
            return TaskV2Request

    async def _arun(self, **kwargs: Dict[str, Any]) -> CreateTaskResponse | Dict[str, Any | None]:
        if self.engine == "TaskV1":
            return await self._arun_task_v1(**kwargs)
        else:
            return await self._arun_task_v2(**kwargs)

    async def _arun_task_v1(self, **kwargs: Dict[str, Any]) -> CreateTaskResponse:
        task_request = TaskV1Request(**kwargs)
        return await self.get_client().agent.create_task(
            max_steps_override=task_request.max_steps,
            url=task_request.url,
            title=task_request.title,
            webhook_callback_url=task_request.webhook_callback_url,
            totp_verification_url=task_request.totp_verification_url,
            totp_identifier=task_request.totp_identifier,
            navigation_goal=task_request.navigation_goal,
            data_extraction_goal=task_request.data_extraction_goal,
            navigation_payload=task_request.navigation_goal,
            error_code_mapping=task_request.error_code_mapping,
            proxy_location=task_request.proxy_location,
            extracted_information_schema=task_request.extracted_information_schema,
            complete_criterion=task_request.complete_criterion,
            terminate_criterion=task_request.terminate_criterion,
            browser_session_id=task_request.browser_session_id,
        )

    async def _arun_task_v2(self, **kwargs: Dict[str, Any]) -> Dict[str, Any | None]:
        task_request = TaskV2Request(**kwargs)
        return await self.get_client().agent.observer_task_v_2(
            max_iterations_override=task_request.max_iterations,
            user_prompt=task_request.user_prompt,
            url=task_request.url,
            browser_session_id=task_request.browser_session_id,
            webhook_callback_url=task_request.webhook_callback_url,
            totp_verification_url=task_request.totp_verification_url,
            totp_identifier=task_request.totp_identifier,
            proxy_location=task_request.proxy_location,
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
