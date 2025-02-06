import httpx

from skyvern.agent.parameter import TaskV1Request, TaskV2Request
from skyvern.forge.sdk.schemas.observers import ObserverTask
from skyvern.forge.sdk.schemas.tasks import CreateTaskResponse, TaskResponse


class RemoteAgent:
    def __init__(self, api_key: str, endpoint: str = "https://api.skyvern.com"):
        self.endpoint = endpoint
        self.api_key = api_key
        self.client = httpx.AsyncClient(
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
            }
        )

    async def run_task_v1(self, task: TaskV1Request) -> CreateTaskResponse:
        url = f"{self.endpoint}/api/v1/tasks"
        payload = task.model_dump_json()
        headers = {"x_max_steps_override": str(task.max_steps)}
        response = await self.client.post(url, headers=headers, data=payload)
        return CreateTaskResponse.model_validate(response.json())

    async def run_task_v2(self, task: TaskV2Request) -> ObserverTask:
        url = f"{self.endpoint}/api/v2/tasks"
        payload = task.model_dump_json()
        headers = {"x_max_iterations_override": str(task.max_iterations)}
        response = await self.client.post(url, headers=headers, data=payload)
        return ObserverTask.model_validate(response.json())

    async def get_task_v1(self, task_id: str) -> TaskResponse:
        """Get a task by id."""
        url = f"{self.endpoint}/api/v1/tasks/{task_id}"
        response = await self.client.get(url)
        return TaskResponse.model_validate(response.json())

    async def get_task_v2(self, task_id: str) -> ObserverTask:
        """Get a task by id."""
        url = f"{self.endpoint}/api/v2/tasks/{task_id}"
        response = await self.client.get(url)
        return ObserverTask.model_validate(response.json())
