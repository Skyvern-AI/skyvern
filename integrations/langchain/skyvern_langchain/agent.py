from typing import Any, Type

from langchain.tools import BaseTool
from litellm import BaseModel
from pydantic import Field
from skyvern_langchain.schema import CreateTaskInput, GetTaskInput
from skyvern_langchain.settings import settings

from skyvern import Skyvern
from skyvern.client.types.get_run_response import GetRunResponse
from skyvern.client.types.task_run_response import TaskRunResponse
from skyvern.schemas.runs import RunEngine


class SkyvernTaskBaseTool(BaseTool):
    engine: RunEngine = Field(default=settings.engine)
    run_task_timeout_seconds: int = Field(default=settings.run_task_timeout_seconds)
    agent: Skyvern = Skyvern(base_url=None, api_key=None)

    def _run(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("skyvern task tool does not support sync")


class RunTask(SkyvernTaskBaseTool):
    name: str = "run-skyvern-agent-task"
    description: str = """Use Skyvern agent to run a task. This function won't return until the task is finished."""
    args_schema: Type[BaseModel] = CreateTaskInput

    async def _arun(self, user_prompt: str, url: str | None = None) -> TaskRunResponse:
        return await self.agent.run_task(
            prompt=user_prompt,
            url=url,
            engine=self.engine,
            timeout=self.run_task_timeout_seconds,
            wait_for_completion=True,
        )


class DispatchTask(SkyvernTaskBaseTool):
    name: str = "dispatch-skyvern-agent-task"
    description: str = """Use Skyvern agent to dispatch a task. This function will return immediately and the task will be running in the background."""
    args_schema: Type[BaseModel] = CreateTaskInput

    async def _arun(self, user_prompt: str, url: str | None = None) -> TaskRunResponse:
        return await self.agent.run_task(
            prompt=user_prompt,
            url=url,
            engine=self.engine,
            timeout=self.run_task_timeout_seconds,
            wait_for_completion=False,
        )


class GetTask(SkyvernTaskBaseTool):
    name: str = "get-skyvern-agent-task"
    description: str = """Use Skyvern agent to get a task."""
    args_schema: Type[BaseModel] = GetTaskInput

    async def _arun(self, task_id: str) -> GetRunResponse | None:
        return await self.agent.get_run(run_id=task_id)
