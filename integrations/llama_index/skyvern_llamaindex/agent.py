from typing import Any, Dict, List, Tuple

from llama_index.core.tools.tool_spec.base import SPEC_FUNCTION_TYPE, BaseToolSpec
from llama_index.core.tools.types import ToolMetadata
from skyvern_llamaindex.schema import GetTaskInput, TaskV1Request, TaskV2Request

from skyvern.agent import Agent
from skyvern.forge.sdk.schemas.observers import ObserverTask
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, TaskResponse


class SkyvernAgentToolSpec(BaseToolSpec):
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
            name="run-skyvern-agent-task-v1",
            description="Use Skyvern agent to run a v1 task. It is usually used for the simple tasks. This function won't return until the task is finished.",
            fn_schema=TaskV1Request,
        ),
        "queue_task_v1": ToolMetadata(
            name="queue-skyvern-agent-task-v1",
            description="Use Skyvern agent to queue a v1 task. It is usually used for the simple tasks. This function will return immediately and the task will be running in the background.",
            fn_schema=TaskV1Request,
        ),
        "get_task_v1": ToolMetadata(
            name="get-skyvern-agent-task-v1",
            description="Use Skyvern agent to get a v1 task. v1 tasks are usually simple tasks.",
            fn_schema=GetTaskInput,
        ),
        "run_task_v2": ToolMetadata(
            name="run-skyvern-agent-task-v2",
            description="Use Skyvern agent to run a v2 task. It is usually used for the complicated tasks. This function won't return until the task is finished.",
            fn_schema=TaskV2Request,
        ),
        "queue_task_v2": ToolMetadata(
            name="queue-skyvern-agent-task-v2",
            description="Use Skyvern agent to queue a v2 task. It is usually used for the complicated tasks. This function will return immediately and the task will be running in the background.",
            fn_schema=TaskV2Request,
        ),
        "get_task_v2": ToolMetadata(
            name="get-skyvern-agent-task-v2",
            description="Use Skyvern agent to get a v2 task. v2 tasks are usually complicated tasks.",
            fn_schema=GetTaskInput,
        ),
    }

    def __init__(self) -> None:
        self.agent = Agent()

    def get_metadata_from_fn_name(
        self, fn_name: str, spec_functions: List[str | Tuple[str, str]] | None = None
    ) -> ToolMetadata | None:
        try:
            getattr(self, fn_name)
        except AttributeError:
            return None

        return self.spec_metadata.get(fn_name)

    async def run_task_v1(self, **kwargs: Dict[str, Any]) -> TaskResponse:
        """Use Skyvern agent to run a v1 task. It is usually used for the simple tasks. This function won't return until the task is finished."""
        task_request = TaskV1Request(**kwargs)
        return await self.agent.run_task(task_request=task_request, timeout_seconds=task_request.timeout_seconds)

    async def queue_task_v1(self, **kwargs: Dict[str, Any]) -> CreateTaskResponse:
        """Use Skyvern agent to queue a v1 task. It is usually used for the simple tasks. This function will return immediately and the task will be running in the background."""
        task_request = TaskV1Request(**kwargs)
        return await self.agent.create_task(task_request=task_request)

    async def get_task_v1(self, task_id: str) -> TaskResponse | None:
        """Use Skyvern agent to get a v1 task. v1 tasks are usually simple tasks."""
        return await self.agent.get_task(task_id=task_id)

    async def run_task_v2(self, **kwargs: Dict[str, Any]) -> ObserverTask:
        """Use Skyvern agent to run a v2 task. It is usually used for the complicated tasks. This function won't return until the task is finished."""
        task_request = TaskV2Request(**kwargs)
        return await self.agent.run_observer_task_v_2(
            task_request=task_request, timeout_seconds=task_request.timeout_seconds
        )

    async def queue_task_v2(self, **kwargs: Dict[str, Any]) -> ObserverTask:
        """Use Skyvern agent to queue a v2 task. It is usually used for the complicated tasks. This function will return immediately and the task will be running in the background."""
        task_request = TaskV2Request(**kwargs)
        return await self.agent.observer_task_v_2(task_request=task_request)

    async def get_task_v2(self, task_id: str) -> ObserverTask | None:
        """Use Skyvern agent to get a v2 task. v2 tasks are usually complicated tasks."""
        return await self.agent.get_observer_task_v_2(task_id=task_id)
