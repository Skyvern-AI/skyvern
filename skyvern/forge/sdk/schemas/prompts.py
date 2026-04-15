import json
import typing as t

from pydantic import BaseModel, Field, field_validator, model_validator
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


MAX_SUMMARIZE_OUTPUT_JSON_LENGTH = 100_000
MAX_SUMMARIZE_CONTEXT_STRING_LENGTH = 500


class SummarizeOutputRequest(BaseModel):
    output_json: str = Field(
        ...,
        min_length=1,
        max_length=MAX_SUMMARIZE_OUTPUT_JSON_LENGTH,
        description="The JSON output to summarize",
    )
    workflow_title: str | None = Field(
        None,
        max_length=MAX_SUMMARIZE_CONTEXT_STRING_LENGTH,
        description="Title of the workflow for context",
    )
    block_label: str | None = Field(
        None,
        max_length=MAX_SUMMARIZE_CONTEXT_STRING_LENGTH,
        description="Label of the specific block being summarized",
    )

    @field_validator("output_json")
    @classmethod
    def _validate_output_json(cls, value: str) -> str:
        try:
            json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"output_json must be valid JSON: {exc.msg}") from exc
        except RecursionError as exc:
            raise ValueError("output_json is too deeply nested") from exc
        return value


class SummarizeOutputResponse(BaseModel):
    error: str | None = Field(None, description="Error message if summarization failed")
    summary: str = Field(..., description="The human-readable summary of the output")
