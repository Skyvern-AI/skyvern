from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class TaskGenerationBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    organization_id: str | None = None
    user_prompt: str | None = None
    url: str | None = None
    navigation_goal: str | None = None
    navigation_payload: dict[str, Any] | None = None
    data_extraction_goal: str | None = None
    extracted_information_schema: dict[str, Any] | None = None
    llm: str | None = None
    llm_prompt: str | None = None
    llm_response: str | None = None


class TaskGenerationCreate(TaskGenerationBase):
    organization_id: str
    user_prompt: str


class TaskGeneration(TaskGenerationBase):
    task_generation_id: str
    organization_id: str
    user_prompt: str

    created_at: datetime
    modified_at: datetime


class GenerateTaskRequest(BaseModel):
    prompt: str
