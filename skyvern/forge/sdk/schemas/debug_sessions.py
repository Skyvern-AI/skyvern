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
