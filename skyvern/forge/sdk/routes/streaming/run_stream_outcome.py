"""One outcome line per viewer connection to a Run's live stream, so stream health is measurable per Run."""

from __future__ import annotations

import time
from typing import Literal, cast

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE,
    PersistentBrowserSession,
    is_final_status,
)
from skyvern.webeye.browser_acquisition_sample import BROWSER_RUNTIMES, BrowserRuntime
from skyvern.webeye.persistent_sessions_manager import PBS_TASK_RUNNABLE_TYPE

LOG = structlog.get_logger()

RunStreamOutcome = Literal[
    "run_finished",
    "viewer_left",
    "run_not_found",
    "no_frame_timeout",
    "run_unreadable",
    "stream_error",
    "cancelled",
    "session_ended",
]
RunStreamFailure = Literal["none", "recoverable", "unrecovered"]
# run_frames is the frames a run's worker publishes; vnc and cdp_screencast stream a persistent session's browser.
LiveStreamTransport = Literal["run_frames", "vnc", "cdp_screencast"]

# Whether the viewer gives up on this failure is the frontend's reconnect contract: a `timeout` status
# or a non-JSON message ends its stream, and a close without a status is retried only by the run page;
# the task page never reconnects. A session that has ended leaves nothing to reconnect to.
_FAILURE_BY_OUTCOME: dict[RunStreamOutcome, RunStreamFailure] = {
    "run_finished": "none",
    "viewer_left": "none",
    "run_not_found": "none",
    "no_frame_timeout": "unrecovered",
    "run_unreadable": "unrecovered",
    "stream_error": "recoverable",
    "cancelled": "recoverable",
    "session_ended": "unrecovered",
}

# A session held under one of these is running that run's browser, so its viewer is watching that run.
_RUN_RUNNABLE_TYPES = frozenset({"workflow_run", FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE, PBS_TASK_RUNNABLE_TYPE})


def _run_holding(session: PersistentBrowserSession) -> tuple[str | None, str | None] | None:
    if session.runnable_type not in _RUN_RUNNABLE_TYPES or not session.runnable_id:
        return None
    if session.runnable_type == PBS_TASK_RUNNABLE_TYPE:
        return None, session.runnable_id
    return session.runnable_id, None


class RunStreamConnection:
    def __init__(
        self,
        *,
        organization_id: str,
        viewer_reconnects: bool,
        transport: LiveStreamTransport = "run_frames",
        workflow_run_id: str | None = None,
        task_id: str | None = None,
        browser_session_id: str | None = None,
    ) -> None:
        self._organization_id = organization_id
        self._viewer_reconnects = viewer_reconnects
        self._transport = transport
        self._workflow_run_id = workflow_run_id
        self._task_id = task_id
        self._browser_session_id = browser_session_id
        self._opened_at = time.monotonic()
        self._run_status: str | None = None
        self._run_final = False
        self._expected: bool | None = None
        self._frames_sent = 0
        self._outcome: RunStreamOutcome = "cancelled"
        self._ended = False

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
        self._browser_session_id = browser_session_id
        if workflow_run_id is not None:
            self._workflow_run_id = workflow_run_id

    def observe_session(self, session: PersistentBrowserSession | None) -> None:
        """A session-keyed stream is attributed to whichever run holds the session while it is watched.

        The holder changes only when a run releases the session, which is that run's normal end, so the
        released run's line is emitted then and the connection moves on to the next holder, if any.
        """
        if session is None or is_final_status(session.status):
            self.end_first("session_ended")
            if session is None:
                return
        holder = _run_holding(session)
        attributed = self._attributed_run()
        if holder == attributed:
            return
        if attributed is not None:
            self._log(
                outcome="run_finished",
                lineage="pbs",
                run_phase="finished",
                run_status=None,
            )
            # Only the timing restarts: whether the stream ever worked belongs to the connection.
            self._opened_at = time.monotonic()
        self._workflow_run_id, self._task_id = holder or (None, None)
        self._expected = True if holder else None

    def frame_sent(self) -> None:
        self._frames_sent += 1

    def end(self, outcome: RunStreamOutcome) -> None:
        self._outcome = outcome
        self._ended = True

    def end_first(self, outcome: RunStreamOutcome) -> None:
        """Keep the first ending: in a relay, the side that closes first is the cause and the other side's
        close is its echo."""
        if not self._ended:
            self.end(outcome)

    def _attributed_run(self) -> tuple[str | None, str | None] | None:
        if self._workflow_run_id is None and self._task_id is None:
            return None
        return self._workflow_run_id, self._task_id

    def _failure(self, outcome: RunStreamOutcome) -> RunStreamFailure:
        failure = _FAILURE_BY_OUTCOME[outcome]
        if failure == "recoverable" and not self._viewer_reconnects:
            return "unrecovered"
        return failure

    async def finish(self) -> None:
        """Emit the connection's line. Never raises: it runs in the handler's `finally`."""
        if self._transport == "run_frames":
            run_phase: Literal["active", "finished"] | None = None
            if self._run_status is not None:
                run_phase = "finished" if self._run_final else "active"
            self._log(
                outcome=self._outcome,
                lineage=await self._run_frames_lineage(),
                run_phase=run_phase,
                run_status=self._run_status,
            )
            return
        session_gone = await self._reread_session()
        # A session watched while no run held it is not a run's live stream.
        if self._attributed_run() is None:
            return
        read = await self._read_run_status()
        outcome = self._outcome
        if outcome in ("stream_error", "cancelled", "session_ended") and session_gone is not None:
            # The relay and the session poll race to see a dying browser; only the session row says it ended.
            if session_gone:
                outcome = "session_ended"
            elif outcome == "session_ended":
                outcome = "stream_error"
        if read is not None and read[1] and outcome != "viewer_left":
            # The run was done with the browser, so its session or stream ending is the normal teardown.
            outcome = "run_finished"
        self._log(
            outcome=outcome,
            lineage="pbs",
            run_phase=None if read is None else ("finished" if read[1] else "active"),
            run_status=None if read is None else read[0],
        )

    async def _run_frames_lineage(self) -> BrowserRuntime:
        # Frames reach this route from whatever browser the run's worker holds, so only the run's own
        # recorded runtime separates a local browser from a vendor's.
        if self._browser_session_id is not None:
            return "pbs"
        runtime: str | None = None
        if self._workflow_run_id is not None:
            try:
                runtime = await app.DATABASE.workflow_runs.get_browser_runtime(
                    workflow_run_id=self._workflow_run_id, organization_id=self._organization_id
                )
            except Exception:
                LOG.debug("Could not read the run's browser runtime", workflow_run_id=self._workflow_run_id)
        return cast(BrowserRuntime, runtime) if runtime in BROWSER_RUNTIMES else "local"

    async def _read_run_status(self) -> tuple[str, bool] | None:
        try:
            if self._workflow_run_id is not None:
                workflow_run_status = await app.DATABASE.workflow_runs.get_workflow_run_status(
                    workflow_run_id=self._workflow_run_id, organization_id=self._organization_id
                )
                if workflow_run_status is None:
                    return None
                return str(workflow_run_status), workflow_run_status.is_final()
            if self._task_id is not None:
                task = await app.DATABASE.tasks.get_task(task_id=self._task_id, organization_id=self._organization_id)
                return None if task is None else (str(task.status), task.status.is_final())
        except Exception:
            LOG.debug("Could not read the watched run's status", run_id=self._workflow_run_id or self._task_id)
        return None

    async def _reread_session(self) -> bool | None:
        """Whether the session has ended, or None when it could not be read.

        The last poll can be a holder behind, so the connection first moves to whichever run holds the session now.
        """
        if self._browser_session_id is None:
            return None
        try:
            session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
                session_id=self._browser_session_id, organization_id=self._organization_id
            )
        except Exception:
            LOG.debug("Could not re-read the watched session", browser_session_id=self._browser_session_id)
            return None
        self.observe_session(session)
        return session is None or is_final_status(session.status)

    def _log(
        self,
        *,
        outcome: RunStreamOutcome,
        lineage: BrowserRuntime,
        run_phase: Literal["active", "finished"] | None,
        run_status: str | None,
    ) -> None:
        LOG.info(
            "Run live stream connection ended",
            live_stream_event="connection_ended",
            run_id=self._workflow_run_id or self._task_id,
            workflow_run_id=self._workflow_run_id,
            task_id=self._task_id,
            browser_session_id=self._browser_session_id,
            organization_id=self._organization_id,
            live_stream_transport=self._transport,
            live_stream_lineage=lineage,
            live_stream_outcome=outcome,
            live_stream_failure=self._failure(outcome),
            live_stream_stage="post_ready" if self._frames_sent else "pre_ready",
            live_stream_expected=bool(self._expected),
            run_phase=run_phase,
            run_status=run_status,
            connection_seconds=round(time.monotonic() - self._opened_at, 1),
        )
