from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

BuildTestConnectFailureState = Literal["already_closed", "provisioning_unavailable", "cdp_connect_failed"]


class BuildTestConnectFailure(BaseModel):
    """Typed browser-acquisition stop and only the identities created before it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: BuildTestConnectFailureState
    workflow_run_id: str | None = None
    workflow_run_block_id: str | None = None
    task_id: str | None = None
    browser_session_id: str | None = None
    retry_action: Literal["test_end_to_end"] = "test_end_to_end"
