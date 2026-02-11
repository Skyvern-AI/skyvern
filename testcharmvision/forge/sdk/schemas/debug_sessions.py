import typing as t
from datetime import datetime

from pydantic import BaseModel, ConfigDict

DebugSessionStatus = t.Literal["created", "completed"]


class BlockRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    block_label: str
    output_parameter_id: str
    workflow_run_id: str
    created_at: datetime


class DebugSession(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    debug_session_id: str
    browser_session_id: str
    vnc_streaming_supported: bool | None = None
    workflow_permanent_id: str | None = None
    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None
    status: DebugSessionStatus


class DebugSessionRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ai_fallback: bool | None = None
    block_label: str
    browser_session_id: str
    code_gen: bool | None = None
    debug_session_id: str
    failure_reason: str | None = None
    output_parameter_id: str
    run_with: str | None = None
    script_run_id: str | None = None
    status: str
    workflow_id: str
    workflow_permanent_id: str
    workflow_run_id: str
    # --
    created_at: datetime
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class DebugSessionRuns(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    debug_session: DebugSession
    runs: list[DebugSessionRun]
