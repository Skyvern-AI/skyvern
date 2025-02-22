from typing import Any, Dict, List, Literal, Tuple

from llama_index.core.tools.tool_spec.base import SPEC_FUNCTION_TYPE, BaseToolSpec
from llama_index.core.tools.types import ToolMetadata
from skyvern_llamaindex.schema import GetTaskInput, TaskV1Request, TaskV2Request

from skyvern.agent import Agent
from skyvern.forge.sdk.schemas.observers import ObserverTask
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, TaskResponse


class SkyvernTaskToolSpec(BaseToolSpec):
    spec_functions: List[SPEC_FUNCTION_TYPE] = [
        "run",
        "dispatch",
        "get",
    ]
    spec_metadata: Dict[str, Dict[str, ToolMetadata]] = {
        "TaskV1": {
            "run": ToolMetadata(
                name="run-skyvern-agent-task",
                description="Use Skyvern agent to run a task. This function won't return until the task is finished.",
                fn_schema=TaskV1Request,
            ),
            "dispatch": ToolMetadata(
                name="dispatch-skyvern-agent-task",
                description="Use Skyvern agent to dispatch a task. This function will return immediately and the task will be running in the background.",
                fn_schema=TaskV1Request,
            ),
            "get": ToolMetadata(
                name="get-skyvern-agent-task",
                description="Use Skyvern agent to get a task.",
                fn_schema=GetTaskInput,
            ),
        },
        "TaskV2": {
            "run": ToolMetadata(
                name="run-skyvern-agent-task",
                description="Use Skyvern agent to run a task. This function won't return until the task is finished.",
                fn_schema=TaskV2Request,
            ),
            "dispatch": ToolMetadata(
                name="dispatch-skyvern-agent-task",
                description="Use Skyvern agent to dispatch a task. This function will return immediately and the task will be running in the background.",
                fn_schema=TaskV2Request,
            ),
            "get": ToolMetadata(
                name="get-skyvern-agent-task",
                description="Use Skyvern agent to get a task.",
                fn_schema=GetTaskInput,
            ),
        },
    }

    def __init__(self, *, engine: Literal["TaskV1", "TaskV2"] = "TaskV2") -> None:
        self.agent = Agent()
        self.engine = engine

    def get_metadata_from_fn_name(
        self, fn_name: str, spec_functions: List[str | Tuple[str, str]] | None = None
    ) -> ToolMetadata | None:
        try:
            getattr(self, fn_name)
        except AttributeError:
            return None

        return self.spec_metadata.get(self.engine, {}).get(fn_name)

    async def run(self, **kwargs: Dict[str, Any]) -> TaskResponse | ObserverTask:
        if self.engine == "TaskV1":
            return await self.run_task_v1(**kwargs)
        else:
            return await self.run_task_v2(**kwargs)

    async def dispatch(self, **kwargs: Dict[str, Any]) -> CreateTaskResponse | ObserverTask:
        if self.engine == "TaskV1":
            return await self.dispatch_task_v1(**kwargs)
        else:
            return await self.dispatch_task_v2(**kwargs)

    async def get(self, task_id: str) -> TaskResponse | ObserverTask | None:
        if self.engine == "TaskV1":
            return await self.get_task_v1(task_id)
        else:
            return await self.get_task_v2(task_id)

    async def run_task_v1(self, **kwargs: Dict[str, Any]) -> TaskResponse:
        task_request = TaskV1Request(**kwargs)
        return await self.agent.run_task(task_request=task_request, timeout_seconds=task_request.timeout_seconds)

    async def dispatch_task_v1(self, **kwargs: Dict[str, Any]) -> CreateTaskResponse:
        task_request = TaskV1Request(**kwargs)
        return await self.agent.create_task(task_request=task_request)

    async def get_task_v1(self, task_id: str) -> TaskResponse | None:
        return await self.agent.get_task(task_id=task_id)

    async def run_task_v2(self, **kwargs: Dict[str, Any]) -> ObserverTask:
        task_request = TaskV2Request(**kwargs)
        return await self.agent.run_observer_task_v_2(
            task_request=task_request, timeout_seconds=task_request.timeout_seconds
        )

    async def dispatch_task_v2(self, **kwargs: Dict[str, Any]) -> ObserverTask:
        task_request = TaskV2Request(**kwargs)
        return await self.agent.observer_task_v_2(task_request=task_request)

    async def get_task_v2(self, task_id: str) -> ObserverTask | None:
        return await self.agent.get_observer_task_v_2(task_id=task_id)
