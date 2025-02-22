from typing import Any, Dict, List, Literal, Type

from langchain.tools import BaseTool
from litellm import BaseModel
from skyvern_langchain.schema import GetTaskInput, TaskV1Request, TaskV2Request

from skyvern.agent import Agent
from skyvern.forge.sdk.schemas.observers import ObserverTask
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, TaskResponse


class SkyvernTaskTools:
    def __init__(self, engine: Literal["TaskV1", "TaskV2"] = "TaskV2") -> None:
        self.agent = Agent()
        self.engine = engine

    @property
    def get(self) -> BaseTool:
        return GetSkyvernTaskTool(agent=self.agent, engine=self.engine)

    @property
    def dispatch(self) -> BaseTool:
        return DispatchSkyvernTaskTool(agent=self.agent, engine=self.engine)

    @property
    def run(self) -> BaseTool:
        return RunSkyvernTaskTool(agent=self.agent, engine=self.engine)

    def get_tools(self) -> List[BaseTool]:
        return [
            self.run,
            self.dispatch,
            self.get,
        ]


class SkyvernTaskBaseTool(BaseTool):
    engine: Literal["TaskV1", "TaskV2"] = "TaskV2"
    agent: Agent | None = None

    def _run(self) -> None:
        raise NotImplementedError("skyvern task tool does not support sync")

    def get_agent(self) -> Agent:
        if self.agent is None:
            self.agent = Agent()
        return self.agent


class RunSkyvernTaskTool(SkyvernTaskBaseTool):
    name: str = "run-skyvern-agent-task"
    description: str = """Use Skyvern agent to run a task. This function won't return until the task is finished."""

    def get_input_schema(self) -> Type[BaseModel]:
        if self.engine == "TaskV1":
            return TaskV1Request
        else:
            return TaskV2Request

    async def _arun(self, **kwargs: Dict[str, Any]) -> Dict[str, Any | None]:
        if self.engine == "TaskV1":
            return await self._arun_task_v1(**kwargs)
        else:
            return await self._arun_task_v2(**kwargs)

    async def _arun_task_v1(self, **kwargs: Dict[str, Any]) -> Dict[str, Any | None]:
        task_request = TaskV1Request(**kwargs)
        return await self.get_agent().run_task(task_request=task_request, timeout_seconds=task_request.timeout_seconds)

    async def _arun_task_v2(self, **kwargs: Dict[str, Any]) -> Dict[str, Any | None]:
        task_request = TaskV2Request(**kwargs)
        return await self.get_agent().run_observer_task_v_2(
            task_request=task_request, timeout_seconds=task_request.timeout_seconds
        )


class DispatchSkyvernTaskTool(SkyvernTaskBaseTool):
    name: str = "dispatch-skyvern-agent-task"
    description: str = """Use Skyvern agent to dispatch a task. This function will return immediately and the task will be running in the background."""

    def get_input_schema(self) -> Type[BaseModel]:
        if self.engine == "TaskV1":
            return TaskV1Request
        else:
            return TaskV2Request

    async def _arun(self, **kwargs: Dict[str, Any]) -> CreateTaskResponse | ObserverTask:
        if self.engine == "TaskV1":
            return await self._arun_task_v1(**kwargs)
        else:
            return await self._arun_task_v2(**kwargs)

    async def _arun_task_v1(self, **kwargs: Dict[str, Any]) -> CreateTaskResponse:
        task_request = TaskV1Request(**kwargs)
        return await self.get_agent().create_task(task_request=task_request)

    async def _arun_task_v2(self, **kwargs: Dict[str, Any]) -> ObserverTask:
        task_request = TaskV2Request(**kwargs)
        return await self.get_agent().observer_task_v_2(task_request=task_request)


class GetSkyvernTaskTool(SkyvernTaskBaseTool):
    name: str = "get-skyvern-agent-task"
    description: str = """Use Skyvern agent to get a task."""
    args_schema: Type[BaseModel] = GetTaskInput

    async def _arun(self, task_id: str) -> TaskResponse | ObserverTask | None:
        if self.engine == "TaskV1":
            return await self._arun_task_v1(task_id=task_id)
        else:
            return await self._arun_task_v2(task_id=task_id)

    async def _arun_task_v1(self, task_id: str) -> TaskResponse | None:
        return await self.get_agent().get_task(task_id=task_id)

    async def _arun_task_v2(self, task_id: str) -> ObserverTask | None:
        return await self.get_agent().get_observer_task_v_2(task_id=task_id)
