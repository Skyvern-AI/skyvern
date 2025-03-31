from typing import List, Literal, Optional

from llama_index.core.tools import FunctionTool
from llama_index.core.tools.tool_spec.base import SPEC_FUNCTION_TYPE, BaseToolSpec
from skyvern_llamaindex.settings import settings

from skyvern.agent import SkyvernAgent
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.schemas.task_generations import TaskGenerationBase
from skyvern.forge.sdk.schemas.task_v2 import TaskV2, TaskV2Request
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, TaskRequest, TaskResponse


class SkyvernTool:
    def __init__(self, agent: Optional[SkyvernAgent] = None):
        if agent is None:
            agent = SkyvernAgent()
        self.agent = agent

    def run_task(self) -> FunctionTool:
        task_tool_spec = SkyvernTaskToolSpec(agent=self.agent)
        return task_tool_spec.to_tool_list(["run_task"])[0]

    def dispatch_task(self) -> FunctionTool:
        task_tool_spec = SkyvernTaskToolSpec(agent=self.agent)
        return task_tool_spec.to_tool_list(["dispatch_task"])[0]

    def get_task(self) -> FunctionTool:
        task_tool_spec = SkyvernTaskToolSpec(agent=self.agent)
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
        agent: SkyvernAgent | None = None,
        engine: Literal["TaskV1", "TaskV2"] = settings.engine,
        run_task_timeout_seconds: int = settings.run_task_timeout_seconds,
    ) -> None:
        if agent is None:
            agent = SkyvernAgent()
        self.agent = agent
        self.engine = engine
        self.run_task_timeout_seconds = run_task_timeout_seconds

    # TODO: agent haven't exposed the task v1 generate function, we can migrate to use agent interface when it's available
    async def _generate_v1_task_request(self, user_prompt: str) -> TaskGenerationBase:
        llm_prompt = prompt_engine.load_prompt("generate-task", user_prompt=user_prompt)
        llm_response = await app.LLM_API_HANDLER(prompt=llm_prompt, prompt_name="generate-task")
        return TaskGenerationBase.model_validate(llm_response)

    async def run_task(self, user_prompt: str, url: Optional[str] = None) -> TaskResponse | TaskV2:
        """
        Use Skyvern agent to run a task. This function won't return until the task is finished.

        Args:
            user_prompt[str]: The user's prompt describing the task.
            url (Optional[str]): The URL of the target website for the task.
        """

        if self.engine == "TaskV1":
            return await self.run_task_v1(user_prompt=user_prompt, url=url)
        else:
            return await self.run_task_v2(user_prompt=user_prompt, url=url)

    async def dispatch_task(self, user_prompt: str, url: Optional[str] = None) -> CreateTaskResponse | TaskV2:
        """
        Use Skyvern agent to dispatch a task. This function will return immediately and the task will be running in the background.

        Args:
            user_prompt[str]: The user's prompt describing the task.
            url (Optional[str]): The URL of the target website for the task.
        """

        if self.engine == "TaskV1":
            return await self.dispatch_task_v1(user_prompt=user_prompt, url=url)
        else:
            return await self.dispatch_task_v2(user_prompt=user_prompt, url=url)

    async def get_task(self, task_id: str) -> TaskResponse | TaskV2 | None:
        """
        Use Skyvern agent to get a task.

        Args:
            task_id[str]: The id of the task.
        """

        if self.engine == "TaskV1":
            return await self.get_task_v1(task_id)
        else:
            return await self.get_task_v2(task_id)

    async def run_task_v1(self, user_prompt: str, url: Optional[str] = None) -> TaskResponse:
        task_generation = await self._generate_v1_task_request(user_prompt=user_prompt)
        task_request = TaskRequest.model_validate(task_generation, from_attributes=True)
        if url is not None:
            task_request.url = url

        return await self.agent.run_task_v1(task_request=task_request, timeout_seconds=self.run_task_timeout_seconds)

    async def dispatch_task_v1(self, user_prompt: str, url: Optional[str] = None) -> CreateTaskResponse:
        task_generation = await self._generate_v1_task_request(user_prompt=user_prompt)
        task_request = TaskRequest.model_validate(task_generation, from_attributes=True)
        if url is not None:
            task_request.url = url

        return await self.agent.create_task_v1(task_request=task_request)

    async def get_task_v1(self, task_id: str) -> TaskResponse | None:
        return await self.agent.get_task(task_id=task_id)

    async def run_task_v2(self, user_prompt: str, url: Optional[str] = None) -> TaskV2:
        task_request = TaskV2Request(user_prompt=user_prompt, url=url)
        return await self.agent.run_observer_task_v_2(
            task_request=task_request, timeout_seconds=self.run_task_timeout_seconds
        )

    async def dispatch_task_v2(self, user_prompt: str, url: Optional[str] = None) -> TaskV2:
        task_request = TaskV2Request(user_prompt=user_prompt, url=url)
        return await self.agent.observer_task_v_2(task_request=task_request)

    async def get_task_v2(self, task_id: str) -> TaskV2 | None:
        return await self.agent.get_observer_task_v_2(task_id=task_id)
