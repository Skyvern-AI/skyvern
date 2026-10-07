from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from skyvern.constants import SCRUBBED_VALUE
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus

MAX_WORKFLOW_RUN_GROUP_ITEMS = 25
SCRUBBED_SUBMISSION_KEY_PREFIX = "scrubbed:"


class WorkflowRunGroupStatus(StrEnum):
    active = "active"
    cancel_requested = "cancel_requested"
    finished = "finished"


class WorkflowRunGroupItemState(StrEnum):
    pending = "pending"
    dispatching = "dispatching"
    dispatched = "dispatched"
    done = "done"
    canceled = "canceled"
    failed_to_start = "failed_to_start"

    def is_final(self) -> bool:
        return self in (
            WorkflowRunGroupItemState.done,
            WorkflowRunGroupItemState.canceled,
            WorkflowRunGroupItemState.failed_to_start,
        )


IN_FLIGHT_ITEM_STATES = (WorkflowRunGroupItemState.dispatching, WorkflowRunGroupItemState.dispatched)


class WorkflowRunGroupItemOutcome(StrEnum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"
    unknown = "unknown"
    canceled = "canceled"


class WorkflowRunGroup(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workflow_run_group_id: str
    organization_id: str
    workflow_permanent_id: str
    requested_version: int | None
    workflow_id: str
    submission_key: str
    input_fingerprint: str
    status: WorkflowRunGroupStatus
    created_at: datetime
    modified_at: datetime
    finished_at: datetime | None


class WorkflowRunGroupItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workflow_run_group_id: str
    position: int
    item_key: str
    parameters: dict[str, Any]
    workflow_run_id: str
    state: WorkflowRunGroupItemState
    dispatch_token: str | None
    claimed_at: datetime | None
    dispatch_attempts: int
    failure_reason: str | None


class WorkflowRunGroupItemRequest(BaseModel):
    # Forbidding extra fields rejects per-item browser session, address or profile inputs.
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=255)
    parameters: dict[str, Any] = Field(default_factory=dict)


class WorkflowRunGroupCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(description="The workflow permanent id (wpid_...) every item runs against.")
    version: int | None = Field(default=None, ge=1)
    submission_key: str = Field(min_length=1, max_length=255)
    items: list[WorkflowRunGroupItemRequest] = Field(min_length=1, max_length=MAX_WORKFLOW_RUN_GROUP_ITEMS)

    @model_validator(mode="after")
    def _validate_keys(self) -> "WorkflowRunGroupCreateRequest":
        keys = [item.key for item in self.items]
        if len(set(keys)) != len(keys):
            raise ValueError("item keys must be unique within a group")
        if self.submission_key.startswith(SCRUBBED_SUBMISSION_KEY_PREFIX):
            raise ValueError(f"submission_key cannot start with {SCRUBBED_SUBMISSION_KEY_PREFIX!r}")
        # The retention scrub renames item keys into this space, and a caller key already there would collide.
        if any(key.startswith(SCRUBBED_VALUE) for key in keys):
            raise ValueError(f"item keys cannot start with {SCRUBBED_VALUE!r}")
        return self


class WorkflowRunGroupItemResponse(BaseModel):
    key: str
    position: int
    workflow_run_id: str
    state: WorkflowRunGroupItemState
    run_status: WorkflowRunStatus | None
    outcome: WorkflowRunGroupItemOutcome
    failure_reason: str | None


class WorkflowRunGroupResponse(BaseModel):
    workflow_run_group_id: str
    workflow_permanent_id: str
    workflow_id: str
    requested_version: int | None
    submission_key: str
    status: WorkflowRunGroupStatus
    created_at: datetime
    finished_at: datetime | None
    items: list[WorkflowRunGroupItemResponse]
