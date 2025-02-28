from typing import Any, Literal, Type

from langchain.tools import BaseTool
from litellm import BaseModel
from pydantic import Field
from skyvern_langchain.schema import CreateTaskInput, GetTaskInput
from skyvern_langchain.settings import settings

from skyvern.agent import Agent
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.schemas.observers import ObserverTask, ObserverTaskRequest
from skyvern.forge.sdk.schemas.task_generations import TaskGenerationBase
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, TaskRequest, TaskResponse

agent = Agent()


class SkyvernTaskBaseTool(BaseTool):
    engine: Literal["TaskV1", "TaskV2"] = Field(default=settings.engine)
    timeout_seconds: int = Field(default=settings.run_task_timeout)
    agent: Agent = agent

    def _run(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("skyvern task tool does not support sync")

    # TODO: agent haven't exposed the task v1 generate function, we can migrate to use agent interface when it's available
    async def _generate_v1_task_request(self, user_prompt: str) -> TaskGenerationBase:
        llm_prompt = prompt_engine.load_prompt("generate-task", user_prompt=user_prompt)
        llm_response = await app.LLM_API_HANDLER(prompt=llm_prompt, prompt_name="generate-task")
        return TaskGenerationBase.model_validate(llm_response)


class RunTask(SkyvernTaskBaseTool):
    name: str = "run-skyvern-agent-task"
    description: str = """Use Skyvern agent to run a task. This function won't return until the task is finished."""
    args_schema: Type[BaseModel] = CreateTaskInput

    async def _arun(self, user_prompt: str, url: str | None = None) -> TaskResponse | ObserverTask:
        if self.engine == "TaskV1":
            return await self._arun_task_v1(user_prompt=user_prompt, url=url)
        else:
            return await self._arun_task_v2(user_prompt=user_prompt, url=url)

    async def _arun_task_v1(self, user_prompt: str, url: str | None = None) -> TaskResponse:
        task_generation = await self._generate_v1_task_request(user_prompt=user_prompt)
        task_request = TaskRequest.model_validate(task_generation, from_attributes=True)
        if url is not None:
            task_request.url = url

        return await self.agent.run_task(task_request=task_request, timeout_seconds=self.timeout_seconds)

    async def _arun_task_v2(self, user_prompt: str, url: str | None = None) -> ObserverTask:
        task_request = ObserverTaskRequest(user_prompt=user_prompt, url=url)
        return await self.agent.run_observer_task_v_2(task_request=task_request, timeout_seconds=self.timeout_seconds)


class DispatchTask(SkyvernTaskBaseTool):
    name: str = "dispatch-skyvern-agent-task"
    description: str = """Use Skyvern agent to dispatch a task. This function will return immediately and the task will be running in the background."""
    args_schema: Type[BaseModel] = CreateTaskInput

    async def _arun(self, user_prompt: str, url: str | None = None) -> CreateTaskResponse | ObserverTask:
        if self.engine == "TaskV1":
            return await self._arun_task_v1(user_prompt=user_prompt, url=url)
        else:
            return await self._arun_task_v2(user_prompt=user_prompt, url=url)

    async def _arun_task_v1(self, user_prompt: str, url: str | None = None) -> CreateTaskResponse:
        task_generation = await self._generate_v1_task_request(user_prompt=user_prompt)
        task_request = TaskRequest.model_validate(task_generation, from_attributes=True)
        if url is not None:
            task_request.url = url

        return await self.agent.create_task(task_request=task_request)

    async def _arun_task_v2(self, user_prompt: str, url: str | None = None) -> ObserverTask:
        task_request = ObserverTaskRequest(user_prompt=user_prompt, url=url)
        return await self.agent.observer_task_v_2(task_request=task_request)


class GetTask(SkyvernTaskBaseTool):
    name: str = "get-skyvern-agent-task"
    description: str = """Use Skyvern agent to get a task."""
    args_schema: Type[BaseModel] = GetTaskInput

    async def _arun(self, task_id: str) -> TaskResponse | ObserverTask | None:
        if self.engine == "TaskV1":
            return await self._arun_task_v1(task_id=task_id)
        else:
            return await self._arun_task_v2(task_id=task_id)

    async def _arun_task_v1(self, task_id: str) -> TaskResponse | None:
        return await self.agent.get_task(task_id=task_id)

    async def _arun_task_v2(self, task_id: str) -> ObserverTask | None:
        return await self.agent.get_observer_task_v_2(task_id=task_id)
