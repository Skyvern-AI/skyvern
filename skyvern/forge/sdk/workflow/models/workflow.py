from datetime import datetime
from enum import StrEnum
from typing import Any, List

from pydantic import BaseModel, field_validator
from typing_extensions import deprecated

from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2
from skyvern.forge.sdk.workflow.exceptions import (
    InvalidFinallyBlockLabel,
    NonTerminalFinallyBlock,
    WorkflowDefinitionHasDuplicateBlockLabels,
)
from skyvern.forge.sdk.workflow.models.block import BlockTypeVar, ForLoopBlock, get_all_blocks
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE, OutputParameter
from skyvern.forge.sdk.workflow.models.validators import normalize_run_with
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


@deprecated("Use WorkflowRunResponse instead")
class RunWorkflowResponse(BaseModel):
    workflow_id: str
    workflow_run_id: str


class WorkflowDefinition(BaseModel):
    version: int = 1
    parameters: list[PARAMETER_TYPE]
    blocks: List[BlockTypeVar]
    finally_block_label: str | None = None
    error_code_mapping: dict[str, str] | None = None

    def validate(self) -> None:
        all_labels: set[str] = set()
        duplicate_labels: set[str] = set()

        def _collect_labels(blocks: list[BlockTypeVar]) -> None:
            for block in blocks:
                if block.label in all_labels:
                    duplicate_labels.add(block.label)
                else:
                    all_labels.add(block.label)
                if isinstance(block, ForLoopBlock) and block.loop_blocks:
                    _collect_labels(block.loop_blocks)

        _collect_labels(self.blocks)

        if duplicate_labels:
            raise WorkflowDefinitionHasDuplicateBlockLabels(duplicate_labels)

        if self.finally_block_label:
            # finally_block_label must reference a top-level block
            top_level_labels = {block.label for block in self.blocks}
            if self.finally_block_label not in top_level_labels:
                raise InvalidFinallyBlockLabel(self.finally_block_label, list(top_level_labels))
            for block in self.blocks:
                if block.label == self.finally_block_label and block.next_block_label is not None:
                    raise NonTerminalFinallyBlock(self.finally_block_label)


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
    run_with: str = "agent"
    ai_fallback: bool = True
    cache_key: str | None = None
    adaptive_caching: bool = False
    code_version: int | None = None
    generate_script_on_terminal: bool = False
    run_sequentially: bool | None = None
    sequential_key: str | None = None
    folder_id: str | None = None
    import_error: str | None = None

    @field_validator("run_with", mode="before")
    @classmethod
    def _normalize_run_with(cls, v: str | None) -> str:
        return normalize_run_with(v)

    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None

    def get_output_parameter(self, label: str) -> OutputParameter | None:
        for block in get_all_blocks(self.workflow_definition.blocks):
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

    def is_final_excluding_canceled(self) -> bool:
        """Like :meth:`is_final` but excludes ``canceled``.

        For callers that can't distinguish a legitimate user/block cancel from
        a synthetic ``canceled`` written as a last-resort fallback — e.g. the
        copilot tool reading the row AFTER ``mark_workflow_run_as_canceled_if_not_final``
        has run. Callers that want to trust a legitimate ``canceled`` must read
        the row BEFORE invoking any cancel helper.
        """
        return self.is_final() and self is not WorkflowRunStatus.canceled


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
    failure_category: list[dict[str, Any]] | None = None
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
    trigger_type: WorkflowRunTriggerType | None = None
    workflow_schedule_id: str | None = None

    @field_validator("run_with", mode="before")
    @classmethod
    def _normalize_run_with(cls, v: str | None) -> str | None:
        """Normalize legacy values but preserve None (means 'inherit from workflow')."""
        if v is None:
            return None
        return normalize_run_with(v)

    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    modified_at: datetime


def is_adaptive_caching(workflow: Workflow, workflow_run: WorkflowRun) -> bool:
    """Compute effective adaptive caching mode from run-level override or workflow setting.

    Uses code_version >= 2 as the primary check. Falls back to the legacy
    adaptive_caching bool for rows that haven't been backfilled yet
    (code_version is None).

    WorkflowRun.run_with is None when not explicitly set (inherits from workflow).
    Workflow.run_with is always "code" or "agent" after normalization.
    """
    run_with = workflow_run.run_with or workflow.run_with
    if run_with == "agent":
        return False
    # run_with == "code": check code_version
    if run_with == "code":
        if workflow.code_version is not None:
            return workflow.code_version >= 2
        return workflow.adaptive_caching
    return False


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
    failure_category: list[dict[str, Any]] | None = None
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
    run_with: str = "agent"
    script_run: ScriptRunResponse | None = None
    errors: list[dict[str, Any]] | None = None

    @field_validator("run_with", mode="before")
    @classmethod
    def _normalize_run_with(cls, v: str | None) -> str:
        return normalize_run_with(v)


class WorkflowRunWithWorkflowResponse(WorkflowRunResponseBase):
    workflow: Workflow
