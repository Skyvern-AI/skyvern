from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

BuildTestConnectFailureState = Literal[
    "already_closed",
    "provisioning_unavailable",
    "cdp_connect_failed",
    "occupied",
    "billing_credit_admission_refusal",
]

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
    diagnostic: str | None = None
    retry_action: Literal["test_end_to_end"] | None = "test_end_to_end"

    @model_validator(mode="after")
    def validate_retry_and_identity(self) -> BuildTestConnectFailure:
        if self.state == "billing_credit_admission_refusal":
            identities = (
                self.workflow_run_id,
                self.workflow_run_block_id,
                self.task_id,
                self.browser_session_id,
                self.occupier_run_id,
                self.diagnostic,
            )
            if self.retry_action is not None or any(value is not None for value in identities):
                raise ValueError("billing credit admission refusal cannot carry a retry or browser/run identity")
        elif self.retry_action != "test_end_to_end":
            raise ValueError("retryable browser acquisition failures require test_end_to_end")
        return self


def build_test_connect_failure_sentence(failure: BuildTestConnectFailure) -> str:
    """A run id means the session was lost after the run started, so the operator must not be
    pointed at provisioning."""
    if failure.state == "billing_credit_admission_refusal":
        return (
            "Build test did not start because credits are exhausted. "
            "No browser or run started. Upgrade your plan in Billing."
        )
    if failure.state == "occupied":
        holder = f" ({failure.occupier_run_id})" if failure.occupier_run_id else ""
        return (
            f"Build-test browser session is already running another test{holder} and cannot take a second one: "
            "retry in a fresh session, or wait for the running test to finish."
        )
    if failure.workflow_run_id:
        return f"Build-test browser session was unavailable before any block ran: {failure.state}."
    return f"Build-test browser acquisition stopped: {failure.state}."
