from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkflowSchedule(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workflow_schedule_id: str
    organization_id: str
    workflow_permanent_id: str
    cron_expression: str
    timezone: str
    enabled: bool
    parameters: dict[str, Any] | None = None
    temporal_schedule_id: str | None = None
    name: str | None = None
    description: str | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None


class OrganizationScheduleItem(BaseModel):
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
