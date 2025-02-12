from typing import Any, Dict

from langchain.tools import tool

from skyvern.agent.parameter import GetTaskSchema, RunTaskV1Schema, RunTaskV2Schema, TaskV1Request, TaskV2Request
from skyvern.agent.remote import RemoteAgent
from skyvern.forge.sdk.schemas.observers import ObserverTask
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, TaskResponse


@tool("run-remote-skyvern-simple-task", args_schema=RunTaskV1Schema)
async def run_task_v1(
    task: Dict[str, Any], api_key: str, endpoint: str = "https://api.skyvern.com"
) -> CreateTaskResponse:
    """Use remote Skyvern to run a v1 task. v1 task is usually used for the simple tasks."""
    return await RemoteAgent(api_key, endpoint).run_task_v1(TaskV1Request.model_validate(task))


@tool("get-remote-skyvern-simple-task", args_schema=GetTaskSchema)
async def get_task_v1(task_id: str, api_key: str, endpoint: str = "https://api.skyvern.com") -> TaskResponse:
    """Use remote Skyvern to get a v1 task information. v1 task is usually used for the simple tasks."""
    return await RemoteAgent(api_key, endpoint).get_task_v1(task_id)


@tool("run-remote-skyvern-complicated-task", args_schema=RunTaskV2Schema)
async def run_task_v2(task: Dict[str, Any], api_key: str, endpoint: str = "https://api.skyvern.com") -> ObserverTask:
    """Use remote Skyvern to run a v2 task. v2 task is usually used for the complicated tasks."""
    return await RemoteAgent(api_key, endpoint).run_task_v2(TaskV2Request.model_validate(task))


@tool("get-remote-skyvern-complicated-task", args_schema=GetTaskSchema)
async def get_task_v2(task_id: str, api_key: str, endpoint: str = "https://api.skyvern.com") -> ObserverTask:
    """Use remote Skyvern to get a v2 task information. v2 task is usually used for the complicated tasks."""
    return await RemoteAgent(api_key, endpoint).get_task_v2(task_id)
