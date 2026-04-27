from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkflowSchedule(BaseModel):
    # populate_by_name + serialize_by_alias keep the wire format stable at
    # `temporal_schedule_id` (the already-deployed name external clients see)
    # while the Python attribute stays backend-agnostic.
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    workflow_schedule_id: str
    organization_id: str
    workflow_permanent_id: str
    cron_expression: str
    timezone: str
    enabled: bool
    parameters: dict[str, Any] | None = None
    backend_schedule_id: str | None = Field(default=None, alias="temporal_schedule_id")
    name: str | None = None
    description: str | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


class OrganizationScheduleItem(BaseModel):
    """Compact schedule projection for the org-wide list endpoint.

    Intentionally omits `backend_schedule_id` — the list view is for browsing
    schedules in the dashboard, not for managing the underlying execution-backend
    binding. Callers that need the backend id should fetch the individual
    schedule via the per-workflow get endpoint.
    """

    workflow_schedule_id: str
    organization_id: str
    workflow_permanent_id: str
    workflow_title: str
    cron_expression: str
    timezone: str
    enabled: bool
    parameters: dict[str, Any] | None = None
    name: str | None = None
    description: str | None = None
    next_run: datetime | None = None
    created_at: datetime
    modified_at: datetime


class WorkflowScheduleCreateRequest(BaseModel):
    cron_expression: str
    timezone: str = "UTC"
    enabled: bool = True
    parameters: dict[str, Any] | None = None
    name: str | None = None
    description: str | None = None


class WorkflowScheduleUpdateRequest(BaseModel):
    cron_expression: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    parameters: dict[str, Any] | None = None
    name: str | None = None
    description: str | None = None


class WorkflowScheduleResponse(BaseModel):
    schedule: WorkflowSchedule
    next_runs: list[datetime] = Field(default_factory=list)


class WorkflowScheduleListResponse(BaseModel):
    schedules: list[WorkflowSchedule]


class OrganizationScheduleListResponse(BaseModel):
    schedules: list[OrganizationScheduleItem]
    total_count: int
    page: int
    page_size: int


class DeleteScheduleResponse(BaseModel):
    ok: bool


class WorkflowScheduleUpsertRequest(BaseModel):
    cron_expression: str
    timezone: str
    enabled: bool = True
    parameters: dict[str, Any] | None = None
    name: str | None = None
    description: str | None = None
