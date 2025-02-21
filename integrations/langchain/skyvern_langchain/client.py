from typing import Any, Dict, Type

from httpx import AsyncClient
from langchain.tools import BaseTool
from pydantic import BaseModel
from skyvern_langchain.schema import GetTaskInput, TaskV1Request, TaskV2Request

from skyvern.client import AsyncSkyvern
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, TaskResponse


class SkyvernClientBaseTool(BaseTool):
    credential: str = ""
    base_url: str = "https://api.skyvern.com"

    def get_client(self) -> AsyncSkyvern:
        httpx_client = AsyncClient(
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.credential,
            },
        )
        return AsyncSkyvern(base_url=self.base_url, httpx_client=httpx_client)

    def _run(self) -> None:
        raise NotImplementedError("skyvern client tool does not support sync")


class RunSkyvernClientTaskV1Tool(SkyvernClientBaseTool):
    name: str = "run-skyvern-client-task-v1"
    description: str = """Use Skyvern client to run a v1 task. It is usually used for the simple tasks. This function won't return until the task is finished."""
    args_schema: Type[BaseModel] = TaskV1Request

    async def _arun(self, **kwargs: Dict[str, Any]) -> TaskResponse:
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


class QueueSkyvernClientTaskV1Tool(SkyvernClientBaseTool):
    name: str = "queue-skyvern-client-task-v1"
    description: str = """Use Skyvern client to queue a v1 task. It is usually used for the simple tasks. This function will return immediately and the task will be running in the background."""
    args_schema: Type[BaseModel] = TaskV1Request

    async def _arun(self, **kwargs: Dict[str, Any]) -> CreateTaskResponse:
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


class GetSkyvernClientTaskV1Tool(SkyvernClientBaseTool):
    name: str = "get-skyvern-client-task-v1"
    description: str = """Use Skyvern client to get a v1 task. v1 tasks are usually simple tasks."""
    args_schema: Type[BaseModel] = GetTaskInput

    async def _arun(self, task_id: str) -> TaskResponse:
        return await self.get_client().agent.get_task(task_id=task_id)


class RunSkyvernClientTaskV2Tool(SkyvernClientBaseTool):
    name: str = "run-skyvern-client-task-v2"
    description: str = """Use Skyvern client to run a v2 task. It is usually used for the complicated tasks. This function won't return until the task is finished."""
    args_schema: Type[BaseModel] = TaskV2Request

    async def _arun(self, **kwargs: Dict[str, Any]) -> Dict[str, Any | None]:
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


class QueueSkyvernClientTaskV2Tool(SkyvernClientBaseTool):
    name: str = "queue-skyvern-client-task-v2"
    description: str = """Use Skyvern client to queue a v2 task. It is usually used for the complicated tasks. This function will return immediately and the task will be running in the background."""
    args_schema: Type[BaseModel] = TaskV2Request

    async def _arun(self, **kwargs: Dict[str, Any]) -> Dict[str, Any | None]:
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


class GetSkyvernClientTaskV2Tool(SkyvernClientBaseTool):
    name: str = "get-skyvern-client-task-v2"
    description: str = """Use Skyvern client to get a v2 task. It is usually used for the complicated tasks."""
    args_schema: Type[BaseModel] = GetTaskInput

    async def _arun(self, task_id: str) -> Dict[str, Any | None]:
        return await self.get_client().agent.get_observer_task_v_2(task_id=task_id)
