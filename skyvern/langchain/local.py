from typing import Any, Dict

from langchain.tools import tool

from skyvern.agent.local import Agent, TaskResponse, TaskV1Request, TaskV2Request
from skyvern.forge.sdk.schemas.observers import ObserverTask


@tool("run-skyvern-simple-task", args_schema=TaskV1Request)
async def run_task_v1(**kwargs: Dict[str, Any]) -> TaskResponse:
    """Run a simple task"""
    return await Agent().run_task_v1(TaskV1Request(**kwargs))


@tool("run-skyvern-task-v2", args_schema=TaskV2Request)
async def run_task_v2(**kwargs: Dict[str, Any]) -> ObserverTask:
    """Run a simple task"""
    return await Agent().run_task_v2(TaskV2Request(**kwargs))
