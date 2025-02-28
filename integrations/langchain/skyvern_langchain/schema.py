from pydantic import BaseModel

from skyvern.forge.sdk.schemas.observers import ObserverTaskRequest
from skyvern.forge.sdk.schemas.tasks import TaskRequest


class TaskV1Request(TaskRequest):
    max_steps: int = 10
    timeout_seconds: int = 60 * 60


class TaskV2Request(ObserverTaskRequest):
    max_iterations: int = 10
    timeout_seconds: int = 60 * 60


class CreateTaskInput(BaseModel):
    user_prompt: str
    url: str | None = None


class GetTaskInput(BaseModel):
    task_id: str
