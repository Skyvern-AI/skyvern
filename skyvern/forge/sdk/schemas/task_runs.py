from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class TaskRunType(StrEnum):
    task_v1 = "task_v1"
    task_v2 = "task_v2"
    workflow_run = "workflow_run"


class TaskRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_run_id: str
    task_run_type: TaskRunType
    run_id: str
    organization_id: str | None = None
    title: str | None = None
    url: str | None = None
    cached: bool = False
    created_at: datetime
    modified_at: datetime
