from pydantic import BaseModel, Field

from skyvern.forge.sdk.schemas.observers import ObserverTaskRequest
from skyvern.forge.sdk.schemas.tasks import TaskRequest


class TaskV1Request(TaskRequest):
    max_steps: int = 10


class TaskV2Request(ObserverTaskRequest):
    max_iterations: int = 10


class RunTaskV1Schema(BaseModel):
    api_key: str = Field(
        description="The API key of the Skyvern API. You can get the API key from the Skyvern dashboard.",
    )
    endpoint: str = Field(
        description="The endpoint of the Skyvern API. Don't add any path to the endpoint. Default is https://api.skyvern.com",
        default="https://api.skyvern.com",
    )
    task: TaskV1Request


class RunTaskV2Schema(BaseModel):
    api_key: str = Field(
        description="The API key of the Skyvern API. You can get the API key from the Skyvern dashboard."
    )
    endpoint: str = Field(
        description="The endpoint of the Skyvern API. Don't add any path to the endpoint. Default is https://api.skyvern.com",
        default="https://api.skyvern.com",
    )
    task: TaskV2Request


class GetTaskSchema(BaseModel):
    api_key: str = Field(
        description="The API key of the Skyvern API. You can get the API key from the Skyvern dashboard."
    )
    endpoint: str = Field(
        description="The endpoint of the Skyvern API. Don't add any path to the endpoint. Default is https://api.skyvern.com",
        default="https://api.skyvern.com",
    )
    task_id: str
