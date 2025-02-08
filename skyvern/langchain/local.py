from typing import Any, Dict

from langchain.tools import tool

from skyvern.agent.local import Agent
from skyvern.agent.parameter import TaskV1Request, TaskV2Request
from skyvern.forge.sdk.schemas.observers import ObserverTask
from skyvern.forge.sdk.schemas.tasks import TaskResponse


@tool("run-local-skyvern-simple-task", args_schema=TaskV1Request)
async def run_task_v1(**kwargs: Dict[str, Any]) -> TaskResponse:
    """Use local Skyvern to run a v1 task. v1 task is usually used for the simple tasks."""
    return await Agent().run_task_v1(TaskV1Request(**kwargs))


@tool("run-local-skyvern-complicated-task", args_schema=TaskV2Request)
async def run_task_v2(**kwargs: Dict[str, Any]) -> ObserverTask:
    """Use local Skyvern to run a v2 task. v2 task is usually used for the complicated tasks."""
    return await Agent().run_task_v2(TaskV2Request(**kwargs))
