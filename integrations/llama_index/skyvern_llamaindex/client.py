from typing import Any, List

from llama_index.core.tools import FunctionTool
from llama_index.core.tools.tool_spec.base import SPEC_FUNCTION_TYPE, BaseToolSpec
from pydantic import BaseModel
from skyvern_llamaindex.settings import settings

from skyvern import Skyvern
from skyvern.client.types.get_run_response import GetRunResponse
from skyvern.client.types.task_run_response import TaskRunResponse
from skyvern.schemas.runs import RunEngine


class SkyvernTool(BaseModel):
    api_key: str = settings.api_key
    base_url: str = settings.base_url

    def run_task(self) -> FunctionTool:
        task_tool_spec = SkyvernTaskToolSpec(
            api_key=self.api_key,
            base_url=self.base_url,
        )

        return task_tool_spec.to_tool_list(["run_task"])[0]

    def dispatch_task(self) -> FunctionTool:
        task_tool_spec = SkyvernTaskToolSpec(
            api_key=self.api_key,
            base_url=self.base_url,
        )

        return task_tool_spec.to_tool_list(["dispatch_task"])[0]

    def get_task(self) -> FunctionTool:
        task_tool_spec = SkyvernTaskToolSpec(
            api_key=self.api_key,
            base_url=self.base_url,
        )

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
        api_key: str = settings.api_key,
        base_url: str = settings.base_url,
        engine: RunEngine = settings.engine,
        run_task_timeout_seconds: int = settings.run_task_timeout_seconds,
    ):
        self.engine = engine
        self.run_task_timeout_seconds = run_task_timeout_seconds
        self.client = Skyvern(base_url=base_url, api_key=api_key)

    async def run_task(
        self,
        user_prompt: str | None = None,
        url: str | None = None,
        *_: Any,
        **kw: Any,
    ) -> TaskRunResponse:
        """
        Use Skyvern client to run a task. This function won't return until the task is finished.

        Args:
            user_prompt[str]: The user's prompt describing the task.
            url (Optional[str]): The URL of the target website for the task.
        """
        if user_prompt is None and kw.get("args"):
            user_prompt = kw["args"][0]

        if url is None:
            if kw.get("args") and len(kw["args"]) > 1:
                url = kw["args"][1]
            elif kw.get("kwargs"):
                url = kw["kwargs"].get("url")

        assert user_prompt is not None, "user_prompt is required"

        return await self.client.run_task(
            prompt=user_prompt,
            url=url,
            engine=self.engine,
            timeout=self.run_task_timeout_seconds,
            wait_for_completion=True,
        )

    async def dispatch_task(
        self,
        user_prompt: str | None = None,
        url: str | None = None,
        *_: Any,
        **kw: Any,
    ) -> TaskRunResponse:
        """
        Use Skyvern client to dispatch a task. This function will return immediately and the task will be running in the background.

        Args:
            user_prompt[str]: The user's prompt describing the task.
            url (Optional[str]): The URL of the target website for the task.
        """
        if user_prompt is None and kw.get("args"):
            user_prompt = kw["args"][0]

        if url is None:
            if kw.get("args") and len(kw["args"]) > 1:
                url = kw["args"][1]
            elif kw.get("kwargs"):
                url = kw["kwargs"].get("url")

        assert user_prompt is not None, "user_prompt is required"
        return await self.client.run_task(
            prompt=user_prompt,
            url=url,
            engine=self.engine,
            timeout=self.run_task_timeout_seconds,
            wait_for_completion=False,
        )

    async def get_task(self, task_id: str | None = None, *_: Any, **kwargs: Any) -> GetRunResponse | None:
        """
        Use Skyvern client to get a task.

        Args:
            task_id[str]: The id of the task.
        """
        if task_id is None and "args" in kwargs:
            task_id = kwargs["args"][0]

        assert task_id is not None, "task_id is required"
        return await self.client.get_run(run_id=task_id)
