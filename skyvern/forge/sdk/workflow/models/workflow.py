from datetime import datetime
from enum import StrEnum
from typing import Any, List

from pydantic import BaseModel, field_validator, model_validator
from typing_extensions import deprecated

from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2
from skyvern.forge.sdk.workflow.exceptions import WorkflowDefinitionHasDuplicateBlockLabels
from skyvern.forge.sdk.workflow.models.block import BlockTypeVar
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE, OutputParameter
from skyvern.schemas.runs import ProxyLocationInput, ScriptRunResponse
from skyvern.schemas.workflows import WorkflowStatus
from skyvern.utils.url_validators import validate_url


@deprecated("Use WorkflowRunRequest instead")
class WorkflowRequestBody(BaseModel):
    data: dict[str, Any] | None = None
    proxy_location: ProxyLocationInput = None
    webhook_callback_url: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    browser_session_id: str | None = None
    browser_profile_id: str | None = None
    max_screenshot_scrolls: int | None = None
    extra_http_headers: dict[str, str] | None = None
    browser_address: str | None = None
    run_with: str | None = None
    ai_fallback: bool | None = None

    @field_validator("webhook_callback_url", "totp_verification_url")
    @classmethod
    def validate_urls(cls, url: str | None) -> str | None:
        if not url:
            return url
        return validate_url(url)

    @model_validator(mode="after")
    def validate_browser_reference(cls, values: "WorkflowRequestBody") -> "WorkflowRequestBody":
        if values.browser_session_id and values.browser_profile_id:
            raise ValueError("Cannot specify both browser_session_id and browser_profile_id")
        return values


@deprecated("Use WorkflowRunResponse instead")
class RunWorkflowResponse(BaseModel):
    workflow_id: str
    workflow_run_id: str


class WorkflowDefinition(BaseModel):
    version: int = 1
    parameters: list[PARAMETER_TYPE]
    blocks: List[BlockTypeVar]

    def validate(self) -> None:
        labels: set[str] = set()
        duplicate_labels: set[str] = set()
        for block in self.blocks:
            if block.label in labels:
                duplicate_labels.add(block.label)
            else:
                labels.add(block.label)

        if duplicate_labels:
            raise WorkflowDefinitionHasDuplicateBlockLabels(duplicate_labels)


class Workflow(BaseModel):
    workflow_id: str
    organization_id: str
    title: str
    workflow_permanent_id: str
    version: int
    is_saved_task: bool
    is_template: bool = False
    description: str | None = None
    workflow_definition: WorkflowDefinition
    proxy_location: ProxyLocationInput = None
    webhook_callback_url: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    persist_browser_session: bool = False
    model: dict[str, Any] | None = None
    status: WorkflowStatus = WorkflowStatus.published
    max_screenshot_scrolls: int | None = None
    extra_http_headers: dict[str, str] | None = None
    run_with: str | None = None
    ai_fallback: bool = False
    cache_key: str | None = None
    run_sequentially: bool | None = None
    sequential_key: str | None = None
    folder_id: str | None = None
    import_error: str | None = None

    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None

    def get_output_parameter(self, label: str) -> OutputParameter | None:
        for block in self.workflow_definition.blocks:
            if block.label == label:
                return block.output_parameter
        return None

    def get_parameter(self, key: str) -> PARAMETER_TYPE | None:
        for parameter in self.workflow_definition.parameters:
            if parameter.key == key:
                return parameter
        return None


class WorkflowRunStatus(StrEnum):
    created = "created"
    queued = "queued"
    running = "running"
    failed = "failed"
    terminated = "terminated"
    canceled = "canceled"
    timed_out = "timed_out"
    completed = "completed"
    paused = "paused"

    def is_final(self) -> bool:
        return self in [
            WorkflowRunStatus.failed,
            WorkflowRunStatus.terminated,
            WorkflowRunStatus.canceled,
            WorkflowRunStatus.timed_out,
            WorkflowRunStatus.completed,
        ]


class WorkflowRun(BaseModel):
    workflow_run_id: str
    workflow_id: str
    workflow_permanent_id: str
    organization_id: str
    browser_session_id: str | None = None
    browser_profile_id: str | None = None
    debug_session_id: str | None = None
    status: WorkflowRunStatus
    extra_http_headers: dict[str, str] | None = None
    proxy_location: ProxyLocationInput = None
    webhook_callback_url: str | None = None
    webhook_failure_reason: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    failure_reason: str | None = None
    parent_workflow_run_id: str | None = None
    workflow_title: str | None = None
    max_screenshot_scrolls: int | None = None
    browser_address: str | None = None
    run_with: str | None = None
    script_run: ScriptRunResponse | None = None
    job_id: str | None = None
    depends_on_workflow_run_id: str | None = None
    sequential_key: str | None = None
    ai_fallback: bool | None = None
    code_gen: bool | None = None

    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    modified_at: datetime


class WorkflowRunParameter(BaseModel):
    workflow_run_id: str
    workflow_parameter_id: str
    value: bool | int | float | str | dict | list
    created_at: datetime


class WorkflowRunOutputParameter(BaseModel):
    workflow_run_id: str
    output_parameter_id: str
    value: dict[str, Any] | list | str | None
    created_at: datetime


class WorkflowRunResponseBase(BaseModel):
    workflow_id: str
    workflow_run_id: str
    status: WorkflowRunStatus
    failure_reason: str | None = None
    proxy_location: ProxyLocationInput = None
    webhook_callback_url: str | None = None
    webhook_failure_reason: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    extra_http_headers: dict[str, str] | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    modified_at: datetime
    parameters: dict[str, Any]
    screenshot_urls: list[str] | None = None
    recording_url: str | None = None
    downloaded_files: list[FileInfo] | None = None
    downloaded_file_urls: list[str] | None = None
    outputs: dict[str, Any] | None = None
    total_steps: int | None = None
    total_cost: float | None = None
    task_v2: TaskV2 | None = None
    workflow_title: str | None = None
    browser_session_id: str | None = None
    browser_profile_id: str | None = None
    max_screenshot_scrolls: int | None = None
    browser_address: str | None = None
    script_run: ScriptRunResponse | None = None
    errors: list[dict[str, Any]] | None = None


class WorkflowRunWithWorkflowResponse(WorkflowRunResponseBase):
    workflow: Workflow
