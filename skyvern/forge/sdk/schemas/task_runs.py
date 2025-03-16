from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from skyvern.forge.sdk.schemas.tasks import ProxyLocation


class TaskRunStatus(StrEnum):
    created = "created"
    queued = "queued"
    running = "running"
    timed_out = "timed_out"
    failed = "failed"
    terminated = "terminated"
    completed = "completed"
    canceled = "canceled"


class RunEngine(StrEnum):
    skyvern_v1 = "skyvern-1.0"
    skyvern_v2 = "skyvern-2.0"


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


class TaskRunResponse(BaseModel):
    run_id: str
    engine: RunEngine = RunEngine.skyvern_v1
    status: TaskRunStatus
    goal: str | None = None
    url: str | None = None
    output: dict | list | str | None = None
    failure_reason: str | None = None
    webhook_url: str | None = None
    totp_identifier: str | None = None
    totp_url: str | None = None
    proxy_location: ProxyLocation | None = None
    error_code_mapping: dict[str, str] | None = None
    title: str | None = None
    max_steps: int | None = None
    created_at: datetime
    modified_at: datetime
