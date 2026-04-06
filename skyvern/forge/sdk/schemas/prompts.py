import typing as t

from pydantic import BaseModel, Field, model_validator
from typing_extensions import Self

from skyvern.forge.sdk.schemas.task_v2 import TaskV2Request
from skyvern.forge.sdk.schemas.tasks import PromptedTaskRequest


class PromptedTaskRequestOptionalUrl(PromptedTaskRequest):
    url: str | None = None  # type: ignore[assignment]  # Pydantic allows narrowing required→optional in subclasses

    @model_validator(mode="after")
    def validate_url(self) -> Self:
        if self.url is None:
            return self
        return super().validate_url()


class CreateWorkflowFromPromptRequestV1(BaseModel):
    task_version: t.Literal["v1"]
    request: PromptedTaskRequestOptionalUrl


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
