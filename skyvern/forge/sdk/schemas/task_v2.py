from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from skyvern.config import settings
from skyvern.schemas.runs import ProxyLocation
from skyvern.utils.url_validators import validate_url

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
    organization_id: str
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
    extracted_information_schema: dict | list | str | None = None
    error_code_mapping: dict | None = None
    model: dict[str, Any] | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    max_screenshot_scrolls: int | None = Field(default=None, alias="max_screenshot_scrolling_times")
    extra_http_headers: dict[str, str] | None = None

    created_at: datetime
    modified_at: datetime

    @property
    def llm_key(self) -> str | None:
        """
        If the `TaskV2` has a `model` defined, then return the mapped llm_key for it.

        Otherwise return `None`.
        """

        if self.model:
            model_name = self.model.get("model_name")
            if model_name:
                mapping = settings.get_model_name_to_llm_key()
                llm_key = mapping.get(model_name, {}).get("llm_key")
                if llm_key:
                    return llm_key

        return None

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
    failure_describe = "failure_describe"


class ThoughtScenario(StrEnum):
    generate_plan = "generate_plan"
    user_goal_check = "user_goal_check"
    failure_describe = "failure_describe"
    summarization = "summarization"
    generate_metadata = "generate_metadata"
    extract_loop_values = "extract_loop_values"
    generate_task_in_loop = "generate_task_in_loop"
    generate_task = "generate_general_task"


class Thought(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    observer_thought_id: str = Field(alias="thought_id")
    observer_cruise_id: str = Field(alias="task_id")
    organization_id: str
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
    reasoning_token_count: int | None = None
    cached_token_count: int | None = None
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
    extracted_information_schema: dict | list | str | None = None
    error_code_mapping: dict[str, str] | None = None
    max_screenshot_scrolls: int | None = None
    extra_http_headers: dict[str, str] | None = None

    @field_validator("url", "webhook_callback_url", "totp_verification_url")
    @classmethod
    def validate_urls(cls, url: str | None) -> str | None:
        if url is None:
            return None

        return validate_url(url)
