from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from skyvern.utils.url_validators import collapse_duplicate_www_prefix


class TaskGenerationBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    organization_id: str | None = None
    user_prompt: str | None = None
    user_prompt_hash: str | None = None
    url: str | None = None
    navigation_goal: str | None = None
    navigation_payload: dict[str, Any] | None = None
    data_extraction_goal: str | None = None
    extracted_information_schema: dict[str, Any] | None = None
    llm: str | None = None
    llm_prompt: str | None = None
    llm_response: str | None = None
    suggested_title: str | None = None

    @field_validator("url")
    @classmethod
    def normalize_url(cls, url: str | None) -> str | None:
        if not url:
            return url
        return collapse_duplicate_www_prefix(url)


class TaskGeneration(TaskGenerationBase):
    task_generation_id: str
    organization_id: str
    user_prompt: str
    user_prompt_hash: str

    created_at: datetime
    modified_at: datetime


class GenerateTaskRequest(BaseModel):
    # prompt needs to be at least 1 character long
    prompt: str = Field(..., min_length=1)
