from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

BuildTestConnectFailureState = Literal["already_closed", "provisioning_unavailable", "cdp_connect_failed", "occupied"]

SUPERSEDED_BY_NEWER_TEST_REASON = "Stopped because a newer test in this chat took over the browser."


class BuildTestConnectFailure(BaseModel):
    """Typed browser-acquisition stop and only the identities created before it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: BuildTestConnectFailureState
    workflow_run_id: str | None = None
    workflow_run_block_id: str | None = None
    task_id: str | None = None
    browser_session_id: str | None = None
    occupier_run_id: str | None = None
    retry_action: Literal["test_end_to_end"] = "test_end_to_end"


def build_test_connect_failure_sentence(failure: BuildTestConnectFailure) -> str:
    """A run id means the session was lost after the run started, so the operator must not be
    pointed at provisioning."""
    if failure.state == "occupied":
        holder = f" ({failure.occupier_run_id})" if failure.occupier_run_id else ""
        return (
            f"Build-test browser session is already running another test{holder} and cannot take a second one: "
            "retry in a fresh session, or wait for the running test to finish."
        )
    if failure.workflow_run_id:
        return f"Build-test browser session was unavailable before any block ran: {failure.state}."
    return f"Build-test browser acquisition stopped: {failure.state}."
