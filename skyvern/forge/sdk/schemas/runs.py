from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from skyvern.schemas.runs import TERMINAL_STATUSES, RunType  # noqa: F401


class Run(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_run_id: str
    task_run_type: RunType
    run_id: str
    organization_id: str | None = None
    title: str | None = None
    url: str | None = None
    cached: bool = False
    # Run history fields
    status: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    workflow_permanent_id: str | None = None
    # dict when script execution metadata is present (e.g. {"ai_fallback_triggered": True}),
    # bool (True/False) when only the ran-as-script flag is needed, None when not a script run.
    script_run: dict | bool | None = None
    parent_workflow_run_id: str | None = None
    debug_session_id: str | None = None
    # Internal denormalized column for trigram search — excluded from serialization.
    searchable_text: str | None = Field(default=None, exclude=True)
    # Compute cost tracking fields
    instance_type: str | None = None
    vcpu_millicores: int | None = None
    duration_ms: int | None = None
    compute_cost: float | None = None
    created_at: datetime
    modified_at: datetime
