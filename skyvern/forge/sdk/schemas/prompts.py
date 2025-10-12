import typing as t

from pydantic import BaseModel, Field

from skyvern.forge.sdk.schemas.task_v2 import TaskV2Request
from skyvern.forge.sdk.schemas.tasks import PromptedTaskRequest


class CreateWorkflowFromPromptRequestV1(BaseModel):
    task_version: t.Literal["v1"]
    request: PromptedTaskRequest


class CreateWorkflowFromPromptRequestV2(BaseModel):
    task_version: t.Literal["v2"]
    request: TaskV2Request


CreateFromPromptRequest = t.Annotated[
    t.Union[CreateWorkflowFromPromptRequestV1, CreateWorkflowFromPromptRequestV2], Field(discriminator="task_version")
]
