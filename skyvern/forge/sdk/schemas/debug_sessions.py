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
    # Mirrors the visible PBS's saved profile so the UI can warn before a debug
    # run with a credential profile diverges from the user's stream.
    pbs_browser_profile_id: str | None = None
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


# Backend-authoritative verdict for a single LoginBlock in a debug session.
# The FE pre-check works off a bounded credentials window; this endpoint
# resolves the credential through the org-scoped lookup the run path uses
# so a Play retry can recover even when pagination would miss the credential.
DebugLoginBlockCompatibilityReason = t.Literal["pbs_no_profile", "pbs_different_profile"]


class DebugLoginBlockCompatibility(BaseModel):
    compatible: bool
    reason: DebugLoginBlockCompatibilityReason | None = None
