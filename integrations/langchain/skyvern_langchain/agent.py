from typing import Any, Dict

from langchain.tools import tool
from skyvern_langchain.schema import GetTaskInput, TaskV1Request, TaskV2Request

from skyvern.agent import Agent
from skyvern.forge.sdk.schemas.observers import ObserverTask
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, TaskResponse


@tool("run-skyvern-agent-task-v1", args_schema=TaskV1Request)
async def run_task_v1(**kwargs: Dict[str, Any]) -> TaskResponse:
    """Use Skyvern agent to run a v1 task. It is usually used for the simple tasks. This function won't return until the task is finished."""
    task_request = TaskV1Request(**kwargs)
    return await Agent().run_task(task_request=task_request, timeout_seconds=task_request.timeout_seconds)


@tool("queue-skyvern-agent-task-v1", args_schema=TaskV1Request)
async def queue_task_v1(**kwargs: Dict[str, Any]) -> CreateTaskResponse:
    """Use Skyvern agent to queue a v1 task. It is usually used for the simple tasks. This function will return immediately and the task will be running in the background."""
    task_request = TaskV1Request(**kwargs)
    return await Agent().create_task(task_request=task_request)


@tool("get-skyvern-agent-task-v1", args_schema=GetTaskInput)
async def get_task_v1(task_id: str) -> TaskResponse | None:
    """Use Skyvern agent to get a v1 task. v1 tasks are usually simple tasks."""
    return await Agent().get_task(task_id=task_id)


@tool("run-skyvern-agent-task-v2", args_schema=TaskV2Request)
async def run_task_v2(**kwargs: Dict[str, Any]) -> ObserverTask:
    """Use Skyvern agent to run a v2 task. It is usually used for the complicated tasks. This function won't return until the task is finished."""
    task_request = TaskV2Request(**kwargs)
    return await Agent().run_observer_task_v_2(task_request=task_request, timeout_seconds=task_request.timeout_seconds)


@tool("queue-skyvern-agent-task-v2", args_schema=TaskV2Request)
async def queue_task_v2(**kwargs: Dict[str, Any]) -> ObserverTask:
    """Use Skyvern agent to queue a v2 task. It is usually used for the complicated tasks. This function will return immediately and the task will be running in the background."""
    task_request = TaskV2Request(**kwargs)
    return await Agent().observer_task_v_2(task_request=task_request)


@tool("get-skyvern-agent-task-v2", args_schema=GetTaskInput)
async def get_task_v2(task_id: str) -> ObserverTask | None:
    """Use Skyvern agent to get a v2 task. v2 tasks are usually complicated tasks."""
    return await Agent().get_observer_task_v_2(task_id=task_id)
