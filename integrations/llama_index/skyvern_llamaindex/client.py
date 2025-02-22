from typing import Any, Dict, List, Literal, Tuple

from httpx import AsyncClient
from llama_index.core.tools.tool_spec.base import SPEC_FUNCTION_TYPE, BaseToolSpec
from llama_index.core.tools.types import ToolMetadata
from skyvern_llamaindex.schema import GetTaskInput, TaskV1Request, TaskV2Request

from skyvern.client import AsyncSkyvern
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, TaskResponse


class SkyvernToolSpec(BaseToolSpec):
    spec_functions: List[SPEC_FUNCTION_TYPE] = [
        "run_task",
        "dispatch_task",
        "get_task",
    ]

    spec_metadata: Dict[str, Dict[str, ToolMetadata]] = {
        "TaskV1": {
            "run_task": ToolMetadata(
                name="run-skyvern-client-task",
                description="Use Skyvern client to run a task. This function won't return until the task is finished.",
                fn_schema=TaskV1Request,
            ),
            "dispatch_task": ToolMetadata(
                name="dispatch-skyvern-client-task",
                description="Use Skyvern client to dispatch a task. This function will return immediately and the task will be running in the background.",
                fn_schema=TaskV1Request,
            ),
            "get_task": ToolMetadata(
                name="get-skyvern-client-task",
                description="Use Skyvern client to get a task.",
                fn_schema=GetTaskInput,
            ),
        },
        "TaskV2": {
            "run_task": ToolMetadata(
                name="run-skyvern-client-task",
                description="Use Skyvern client to run a task. This function won't return until the task is finished.",
                fn_schema=TaskV2Request,
            ),
            "dispatch_task": ToolMetadata(
                name="dispatch-skyvern-client-task",
                description="Use Skyvern client to dispatch a task. This function will return immediately and the task will be running in the background.",
                fn_schema=TaskV2Request,
            ),
            "get_task": ToolMetadata(
                name="get-skyvern-client-task",
                description="Use Skyvern client to get a task.",
                fn_schema=GetTaskInput,
            ),
        },
    }

    def __init__(
        self,
        credential: str,
        *,
        base_url: str = "https://api.skyvern.com",
        engine: Literal["TaskV1", "TaskV2"] = "TaskV2",
    ):
        httpx_client = AsyncClient(
            headers={
                "Content-Type": "application/json",
                "x-api-key": credential,
            },
        )
        self.engine = engine
        self.client = AsyncSkyvern(base_url=base_url, httpx_client=httpx_client)

    def get_metadata_from_fn_name(
        self, fn_name: str, spec_functions: List[str | Tuple[str, str]] | None = None
    ) -> ToolMetadata | None:
        try:
            getattr(self, fn_name)
        except AttributeError:
            return None

        return self.spec_metadata.get(self.engine, {}).get(fn_name)

    async def run_task(self, **kwargs: Dict[str, Any]) -> TaskResponse | Dict[str, Any | None]:
        if self.engine == "TaskV1":
            return await self.run_task_v1(**kwargs)
        else:
            return await self.run_task_v2(**kwargs)

    async def dispatch_task(self, **kwargs: Dict[str, Any]) -> CreateTaskResponse | Dict[str, Any | None]:
        if self.engine == "TaskV1":
            return await self.dispatch_task_v1(**kwargs)
        else:
            return await self.dispatch_task_v2(**kwargs)

    async def get_task(self, task_id: str) -> TaskResponse | Dict[str, Any | None]:
        if self.engine == "TaskV1":
            return await self.get_task_v1(task_id)
        else:
            return await self.get_task_v2(task_id)

    async def run_task_v1(self, **kwargs: Dict[str, Any]) -> TaskResponse:
        task_request = TaskV1Request(**kwargs)
        return await self.client.agent.run_task(
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

    async def dispatch_task_v1(self, **kwargs: Dict[str, Any]) -> CreateTaskResponse:
        task_request = TaskV1Request(**kwargs)
        return await self.client.agent.create_task(
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

    async def get_task_v1(self, task_id: str) -> TaskResponse:
        return await self.client.agent.get_task(task_id=task_id)

    async def run_task_v2(self, **kwargs: Dict[str, Any]) -> Dict[str, Any | None]:
        task_request = TaskV2Request(**kwargs)
        return await self.client.agent.run_observer_task_v_2(
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

    async def dispatch_task_v2(self, **kwargs: Dict[str, Any]) -> Dict[str, Any | None]:
        task_request = TaskV2Request(**kwargs)
        return await self.client.agent.observer_task_v_2(
            max_iterations_override=task_request.max_iterations,
            user_prompt=task_request.user_prompt,
            url=task_request.url,
            browser_session_id=task_request.browser_session_id,
            webhook_callback_url=task_request.webhook_callback_url,
            totp_verification_url=task_request.totp_verification_url,
            totp_identifier=task_request.totp_identifier,
            proxy_location=task_request.proxy_location,
        )

    async def get_task_v2(self, task_id: str) -> Dict[str, Any | None]:
        return await self.client.agent.get_observer_task_v_2(task_id=task_id)
