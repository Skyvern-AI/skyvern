from typing import Any, Dict

from langchain.tools import tool

from skyvern.agent import Agent
from skyvern.forge.sdk.schemas.observers import ObserverTask
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, TaskResponse

from .scheme import GetTaskInput, TaskV1Request, TaskV2Request


@tool("run-skyvern-agent-task", args_schema=TaskV1Request)
async def run_task(**kwargs: Dict[str, Any]) -> TaskResponse:
    """Use Skyvern agent to run a task, also named v1 task. It is usually used for the simple tasks."""
    task_request = TaskV1Request(**kwargs)
    return await Agent().run_task(task_request=task_request, timeout_seconds=task_request.timeout_seconds)


@tool("run-skyvern-agent-observer-task", args_schema=TaskV2Request)
async def run_observer_task_v_2(**kwargs: Dict[str, Any]) -> ObserverTask:
    """Use Skyvern agent to run a v2 task, also named observer task. It is usually used for the complicated tasks."""
    task_request = TaskV2Request(**kwargs)
    return await Agent().run_observer_task_v_2(task_request=task_request, timeout_seconds=task_request.timeout_seconds)


@tool("create-skyvern-agent-task", args_schema=TaskV1Request)
async def create_task(**kwargs: Dict[str, Any]) -> CreateTaskResponse:
    """Use Skyvern agent to create a task, also named v1 task. It is usually used for the simple tasks."""
    task_request = TaskV1Request(**kwargs)
    return await Agent().create_task(task_request=task_request)


@tool("get-skyvern-agent-task", args_schema=GetTaskInput)
async def get_task(task_id: str) -> TaskResponse | None:
    """Use Skyvern agent to get a task, also named v1 task. v1 tasks are usually simple tasks."""
    return await Agent().get_task(task_id=task_id)


@tool("create-skyvern-agent-observer-task", args_schema=TaskV2Request)
async def create_observer_task_v_2(**kwargs: Dict[str, Any]) -> ObserverTask:
    """Use Skyvern agent to create a observer task, also named v2 task. It is usually used for the complicated tasks."""
    task_request = TaskV2Request(**kwargs)
    return await Agent().observer_task_v_2(task_request=task_request)


@tool("get-skyvern-agent-observer-task", args_schema=GetTaskInput)
async def get_observer_task_v_2(task_id: str) -> ObserverTask | None:
    """Use Skyvern agent to get a observer task, also named v2 task. v2 tasks are usually complicated tasks."""
    return await Agent().get_observer_task_v_2(task_id=task_id)
