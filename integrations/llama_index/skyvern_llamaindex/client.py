from typing import Any, Dict, List, Tuple

from httpx import AsyncClient
from llama_index.core.tools.tool_spec.base import SPEC_FUNCTION_TYPE, BaseToolSpec
from llama_index.core.tools.types import ToolMetadata
from skyvern_llamaindex.schema import GetTaskInput, TaskV1Request, TaskV2Request

from skyvern.client import AsyncSkyvern
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, TaskResponse


class SkyvernClientToolSpec(BaseToolSpec):
    spec_functions: List[SPEC_FUNCTION_TYPE] = [
        "run_task_v1",
        "queue_task_v1",
        "get_task_v1",
        "run_task_v2",
        "queue_task_v2",
        "get_task_v2",
    ]

    spec_metadata: Dict[str, ToolMetadata] = {
        "run_task_v1": ToolMetadata(
            name="run-skyvern-client-task-v1",
            description="Use Skyvern client to run a v1 task. It is usually used for the simple tasks. This function won't return until the task is finished.",
            fn_schema=TaskV1Request,
        ),
        "queue_task_v1": ToolMetadata(
            name="queue-skyvern-client-task-v1",
            description="Use Skyvern client to queue a v1 task. It is usually used for the simple tasks. This function will return immediately and the task will be running in the background.",
            fn_schema=TaskV1Request,
        ),
        "get_task_v1": ToolMetadata(
            name="get-skyvern-client-task-v1",
            description="Use Skyvern client to get a v1 task. v1 tasks are usually simple tasks.",
            fn_schema=GetTaskInput,
        ),
        "run_task_v2": ToolMetadata(
            name="run-skyvern-client-task-v2",
            description="Use Skyvern client to run a v2 task. It is usually used for the complicated tasks. This function won't return until the task is finished.",
            fn_schema=TaskV2Request,
        ),
        "queue_task_v2": ToolMetadata(
            name="queue-skyvern-client-task-v2",
            description="Use Skyvern client to queue a v2 task. It is usually used for the complicated tasks. This function will return immediately and the task will be running in the background.",
            fn_schema=TaskV2Request,
        ),
        "get_task_v2": ToolMetadata(
            name="get-skyvern-client-task-v2",
            description="Use Skyvern client to get a v2 task. It is usually used for the complicated tasks.",
            fn_schema=GetTaskInput,
        ),
    }

    def __init__(self, credential: str, base_url: str = "https://api.skyvern.com"):
        httpx_client = AsyncClient(
            headers={
                "Content-Type": "application/json",
                "x-api-key": credential,
            },
        )
        self.client = AsyncSkyvern(base_url=base_url, httpx_client=httpx_client)

    def get_metadata_from_fn_name(
        self, fn_name: str, spec_functions: List[str | Tuple[str, str]] | None = None
    ) -> ToolMetadata | None:
        try:
            getattr(self, fn_name)
        except AttributeError:
            return None

        return self.spec_metadata.get(fn_name)

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

    async def queue_task_v1(self, **kwargs: Dict[str, Any]) -> CreateTaskResponse:
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

    async def queue_task_v2(self, **kwargs: Dict[str, Any]) -> Dict[str, Any | None]:
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
