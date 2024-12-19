from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, HttpUrl, field_validator

from skyvern.forge.sdk.core.validators import validate_url

DEFAULT_WORKFLOW_TITLE = "New Workflow"


class ObserverCruiseStatus(StrEnum):
    created = "created"
    queued = "queued"
    running = "running"
    failed = "failed"
    terminated = "terminated"
    canceled = "canceled"
    timed_out = "timed_out"
    completed = "completed"


class ObserverCruise(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    observer_cruise_id: str
    status: ObserverCruiseStatus
    organization_id: str | None = None
    workflow_run_id: str | None = None
    workflow_id: str | None = None
    workflow_permanent_id: str | None = None
    prompt: str | None = None
    url: HttpUrl | None = None

    created_at: datetime
    modified_at: datetime


class ObserverThought(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    observer_thought_id: str
    observer_cruise_id: str
    organization_id: str | None = None
    workflow_run_id: str | None = None
    workflow_run_block_id: str | None = None
    workflow_id: str | None = None
    workflow_permanent_id: str | None = None
    user_input: str | None = None
    observation: str | None = None
    thought: str | None = None
    answer: str | None = None

    created_at: datetime
    modified_at: datetime


class ObserverMetadata(BaseModel):
    url: str
    workflow_title: str = DEFAULT_WORKFLOW_TITLE

    @field_validator("url")
    @classmethod
    def validate_urls(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return validate_url(v)


class CruiseRequest(BaseModel):
    user_prompt: str
    url: HttpUrl | None = None
