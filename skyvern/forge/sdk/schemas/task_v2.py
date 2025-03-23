from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from skyvern.forge.sdk.core.validators import validate_url
from skyvern.forge.sdk.schemas.tasks import ProxyLocation

DEFAULT_WORKFLOW_TITLE = "New Workflow"


class TaskV2Status(StrEnum):
    created = "created"
    queued = "queued"
    running = "running"
    failed = "failed"
    terminated = "terminated"
    canceled = "canceled"
    timed_out = "timed_out"
    completed = "completed"

    def is_final(self) -> bool:
        return self in [self.failed, self.terminated, self.canceled, self.timed_out, self.completed]


class TaskV2(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    observer_cruise_id: str = Field(alias="task_id")
    status: TaskV2Status
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


class ThoughtType(StrEnum):
    plan = "plan"
    metadata = "metadata"
    user_goal_check = "user_goal_check"
    internal_plan = "internal_plan"


class ThoughtScenario(StrEnum):
    generate_plan = "generate_plan"
    user_goal_check = "user_goal_check"
    summarization = "summarization"
    generate_metadata = "generate_metadata"
    extract_loop_values = "extract_loop_values"
    generate_task_in_loop = "generate_task_in_loop"
    generate_task = "generate_general_task"


class Thought(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    observer_thought_id: str = Field(alias="thought_id")
    observer_cruise_id: str = Field(alias="task_id")
    organization_id: str | None = None
    workflow_run_id: str | None = None
    workflow_run_block_id: str | None = None
    workflow_id: str | None = None
    workflow_permanent_id: str | None = None
    user_input: str | None = None
    observation: str | None = None
    thought: str | None = None
    answer: str | None = None
    observer_thought_type: ThoughtType | None = Field(alias="thought_type", default=ThoughtType.plan)
    observer_thought_scenario: ThoughtScenario | None = Field(alias="thought_scenario", default=None)
    output: dict[str, Any] | None = None
    input_token_count: int | None = None
    output_token_count: int | None = None
    thought_cost: float | None = None

    created_at: datetime
    modified_at: datetime


class TaskV2Metadata(BaseModel):
    url: str
    workflow_title: str = DEFAULT_WORKFLOW_TITLE

    @field_validator("url")
    @classmethod
    def validate_urls(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return validate_url(v)


class TaskV2Request(BaseModel):
    user_prompt: str
    url: str | None = None
    browser_session_id: str | None = None
    webhook_callback_url: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    proxy_location: ProxyLocation | None = None
    publish_workflow: bool = False

    @field_validator("url", "webhook_callback_url", "totp_verification_url")
    @classmethod
    def validate_urls(cls, url: str | None) -> str | None:
        if url is None:
            return None

        return validate_url(url)
