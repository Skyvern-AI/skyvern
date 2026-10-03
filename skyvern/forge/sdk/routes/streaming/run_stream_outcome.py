"""One outcome line per viewer connection to a Run's live stream, so stream health is measurable per Run."""

from __future__ import annotations

import time
from typing import Literal

import structlog

LOG = structlog.get_logger()

RunStreamOutcome = Literal[
    "run_finished",
    "viewer_left",
    "run_not_found",
    "no_frame_timeout",
    "run_unreadable",
    "stream_error",
    "cancelled",
]
RunStreamFailure = Literal["none", "recoverable", "unrecovered"]

# Whether the viewer gives up on this failure is the frontend's reconnect contract: a `timeout` status
# or a non-JSON message ends its stream, and a close without a status is retried only by the run page;
# the task page never reconnects.
_FAILURE_BY_OUTCOME: dict[RunStreamOutcome, RunStreamFailure] = {
    "run_finished": "none",
    "viewer_left": "none",
    "run_not_found": "none",
    "no_frame_timeout": "unrecovered",
    "run_unreadable": "unrecovered",
    "stream_error": "recoverable",
    "cancelled": "recoverable",
}


class RunStreamConnection:
    def __init__(
        self,
        *,
        organization_id: str,
        viewer_reconnects: bool,
        workflow_run_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        self._organization_id = organization_id
        self._viewer_reconnects = viewer_reconnects
        self._workflow_run_id = workflow_run_id
        self._task_id = task_id
        self._opened_at = time.monotonic()
        self._run_status: str | None = None
        self._run_final = False
        self._expected: bool | None = None
        self._has_browser_session = False
        self._frames_sent = 0
        self._outcome: RunStreamOutcome = "cancelled"

    def observe_run(
        self,
        *,
        status: str,
        is_final: bool,
        browser_session_id: str | None,
        workflow_run_id: str | None = None,
    ) -> None:
        if self._expected is None:
            self._expected = not is_final
        self._run_status = str(status)
        self._run_final = is_final
        self._has_browser_session = browser_session_id is not None
        if workflow_run_id is not None:
            self._workflow_run_id = workflow_run_id

    def frame_sent(self) -> None:
        self._frames_sent += 1

    def end(self, outcome: RunStreamOutcome) -> None:
        self._outcome = outcome

    def _failure(self) -> RunStreamFailure:
        failure = _FAILURE_BY_OUTCOME[self._outcome]
        if failure == "recoverable" and not self._viewer_reconnects:
            return "unrecovered"
        return failure

    def log(self) -> None:
        run_phase: Literal["active", "finished"] | None = None
        if self._run_status is not None:
            run_phase = "finished" if self._run_final else "active"
        LOG.info(
            "Run live stream connection ended",
            live_stream_event="connection_ended",
            run_id=self._workflow_run_id or self._task_id,
            workflow_run_id=self._workflow_run_id,
            task_id=self._task_id,
            organization_id=self._organization_id,
            live_stream_transport="run_frames",
            live_stream_lineage="session" if self._has_browser_session else "local",
            live_stream_outcome=self._outcome,
            live_stream_failure=self._failure(),
            live_stream_stage="post_ready" if self._frames_sent else "pre_ready",
            live_stream_expected=bool(self._expected),
            run_phase=run_phase,
            run_status=self._run_status,
            connection_seconds=round(time.monotonic() - self._opened_at, 1),
        )
