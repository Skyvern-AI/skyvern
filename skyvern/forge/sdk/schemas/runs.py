from datetime import datetime

from pydantic import BaseModel, ConfigDict

from skyvern.schemas.runs import RunType


class Run(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_run_id: str
    task_run_type: RunType
    run_id: str
    organization_id: str | None = None
    title: str | None = None
    url: str | None = None
    cached: bool = False
    # Compute cost tracking fields
    instance_type: str | None = None
    vcpu_millicores: int | None = None
    duration_ms: int | None = None
    compute_cost: float | None = None
    created_at: datetime
    modified_at: datetime
