import typing as t

from pydantic import BaseModel, Field

from testcharmvision.forge.sdk.schemas.task_v2 import TaskV2Request
from testcharmvision.forge.sdk.schemas.tasks import PromptedTaskRequest


class CreateWorkflowFromPromptRequestV1(BaseModel):
    task_version: t.Literal["v1"]
    request: PromptedTaskRequest


class CreateWorkflowFromPromptRequestV2(BaseModel):
    task_version: t.Literal["v2"]
    request: TaskV2Request


CreateFromPromptRequest = t.Annotated[
    t.Union[CreateWorkflowFromPromptRequestV1, CreateWorkflowFromPromptRequestV2], Field(discriminator="task_version")
]


class ImprovePromptRequest(BaseModel):
    context: dict | None = Field(default_factory=dict, description="Additional context about the user's needs")
    prompt: str = Field(..., min_length=1, description="The original prompt to improve")


class ImprovePromptResponse(BaseModel):
    error: str | None = Field(None, description="Error message if prompt improvement failed")
    improved: str = Field(..., description="The improved version of the prompt")
    original: str = Field(..., description="The original prompt provided for improvement")


class BlockInfoForTitle(BaseModel):
    block_type: str = Field(..., description="The type of the workflow block")
    url: str | None = Field(None, description="URL associated with the block")
    goal: str | None = Field(None, description="Goal or prompt text for the block")


class GenerateWorkflowTitleRequest(BaseModel):
    blocks: list[BlockInfoForTitle] = Field(
        ...,
        description="List of block info objects for title generation",
    )


class GenerateWorkflowTitleResponse(BaseModel):
    title: str | None = Field(None, description="The generated workflow title")
