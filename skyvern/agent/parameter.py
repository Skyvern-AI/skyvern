from skyvern.forge.sdk.schemas.observers import ObserverTaskRequest
from skyvern.forge.sdk.schemas.tasks import TaskRequest


class TaskV1Request(TaskRequest):
    max_steps: int = 10


class TaskV2Request(ObserverTaskRequest):
    max_iterations: int = 10
