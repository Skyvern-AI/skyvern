from datetime import datetime
from typing import Any, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from skyvern.forge.sdk.workflow.schedules import (
    LATEST_FIRST_FIRE_AT,
    MAX_INTERVAL_SECONDS,
    MIN_SCHEDULE_INTERVAL_SECONDS,
    as_utc,
)


def _anchor_as_utc(value: datetime | None) -> datetime | None:
    return as_utc(value) if value is not None else None


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
    cron_expression: str | None = None
    interval_seconds: int | None = None
    first_fire_at: datetime | None = None
    timezone: str
    enabled: bool
    parameters: dict[str, Any] | None = None
    backend_schedule_id: str | None = Field(default=None, alias="temporal_schedule_id")
    name: str | None = None
    description: str | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None

    _first_fire_at_utc = field_validator("first_fire_at")(_anchor_as_utc)


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
    cron_expression: str | None = None
    interval_seconds: int | None = None
    first_fire_at: datetime | None = None
    timezone: str
    enabled: bool
    parameters: dict[str, Any] | None = None
    name: str | None = None
    description: str | None = None
    next_run: datetime | None = None
    created_at: datetime
    modified_at: datetime

    _first_fire_at_utc = field_validator("first_fire_at")(_anchor_as_utc)


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
    cron_expression: str | None = None
    interval_seconds: int | None = Field(
        default=None,
        ge=MIN_SCHEDULE_INTERVAL_SECONDS,
        le=MAX_INTERVAL_SECONDS,
        description="Fixed elapsed interval between runs, in seconds. Mutually exclusive with cron_expression.",
    )
    first_fire_at: AwareDatetime | None = Field(
        default=None,
        description=(
            "First run of an interval schedule; later runs follow every interval_seconds from it. "
            "Must be in the future when changed. Defaults to one interval after creation."
        ),
    )
    timezone: str
    # Default True is the create default (new schedules start enabled). On
    # update the route inspects model_fields_set, so an omitted `enabled`
    # preserves the existing state (never clobbering a concurrent
    # enable/disable) while an explicit true/false is honored.
    enabled: bool | None = True
    parameters: dict[str, Any] | None = None
    name: str | None = None
    description: str | None = None

    @field_validator("first_fire_at")
    @classmethod
    def _first_fire_at_in_range(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        try:
            anchor = as_utc(value).replace(microsecond=0)
        except OverflowError:
            anchor = None
        if anchor is None or anchor > LATEST_FIRST_FIRE_AT:
            raise ValueError(f"first_fire_at must be no later than {LATEST_FIRST_FIRE_AT.isoformat()}")
        return anchor

    @model_validator(mode="after")
    def _exactly_one_cadence(self) -> Self:
        if (self.cron_expression is None) == (self.interval_seconds is None):
            raise ValueError("Set exactly one of cron_expression or interval_seconds")
        if self.first_fire_at is not None and self.interval_seconds is None:
            raise ValueError("first_fire_at requires interval_seconds")
        return self
