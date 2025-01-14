from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from skyvern.forge.sdk.core.validators import validate_url
from skyvern.forge.sdk.schemas.tasks import ProxyLocation

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
    url: str | None = None
    summary: str | None = None
    output: dict[str, Any] | list | str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    proxy_location: ProxyLocation | None = None
    webhook_callback_url: str | None = None

    created_at: datetime
    modified_at: datetime

    @field_validator("url", "webhook_callback_url", "totp_verification_url")
    @classmethod
    def validate_urls(cls, url: str | None) -> str | None:
        if url is None:
            return None

        return validate_url(url)


class ObserverThoughtType(StrEnum):
    plan = "plan"
    metadata = "metadata"
    user_goal_check = "user_goal_check"
    internal_plan = "internal_plan"


class ObserverThoughtScenario(StrEnum):
    generate_plan = "generate_plan"
    user_goal_check = "user_goal_check"
    summarization = "summarization"
    generate_metadata = "generate_metadata"
    extract_loop_values = "extract_loop_values"
    generate_task_in_loop = "generate_task_in_loop"
    generate_task = "generate_general_task"


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
    observer_thought_type: ObserverThoughtType | None = ObserverThoughtType.plan
    observer_thought_scenario: ObserverThoughtScenario | None = None
    output: dict[str, Any] | None = None

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
    url: str | None = None
    browser_session_id: str | None = None
    webhook_callback_url: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None

    @field_validator("url", "webhook_callback_url", "totp_verification_url")
    @classmethod
    def validate_urls(cls, url: str | None) -> str | None:
        if url is None:
            return None

        return validate_url(url)
