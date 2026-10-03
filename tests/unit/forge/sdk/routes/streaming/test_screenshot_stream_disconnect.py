from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocket
from structlog.testing import capture_logs

from skyvern.forge import app
from skyvern.forge.sdk.routes.streaming import screenshot
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from tests.unit.scoped_asyncio import ScopedAsyncio


class _DisconnectedWebSocket:
    """A client that is already gone but whose sends still succeed.

    That is the production shape: ASGI delivered ``websocket.disconnect``, but a send-only handler
    never reads it, so Starlette keeps accepting sends and asyncio writes them into a dead socket.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._disconnect_delivered = False

    async def accept(self) -> None:
        return None

    async def send_text(self, text: str) -> None:
        return None

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def receive(self) -> dict:
        if not self._disconnect_delivered:
            self._disconnect_delivered = True
            return {"type": "websocket.disconnect", "code": 1006}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_workflow_run_streaming_stops_sending_once_the_client_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SKY-14645: this loop used to keep send_json-ing every 2s until the run finalized, emitting one
    # stdlib "socket.send() raised exception." per write for the whole remaining run.
    websocket = _DisconnectedWebSocket()
    workflow_run = SimpleNamespace(
        status=WorkflowRunStatus.running,
        organization_id="o_1",
        browser_session_id=None,
    )
    database = SimpleNamespace(
        workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=workflow_run)),
    )

    monkeypatch.setattr(screenshot.settings, "BROWSER_STREAMING_MODE", "vnc")
    monkeypatch.setattr(screenshot, "get_current_org", AsyncMock(return_value=SimpleNamespace(organization_id="o_1")))
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "STORAGE", SimpleNamespace(get_streaming_file=AsyncMock(return_value=b"jpeg")))
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(mark_streaming_viewer_active=AsyncMock()))

    with capture_logs() as logs:
        await asyncio.wait_for(
            screenshot.workflow_run_streaming(
                websocket=cast(WebSocket, websocket),
                workflow_run_id="wr_1",
                apikey="key",
            ),
            timeout=10,
        )

    assert len(websocket.sent) <= 1
    [ended] = _connection_ended(logs)
    assert (ended["live_stream_outcome"], ended["live_stream_failure"]) == ("viewer_left", "none")


class _WatchingWebSocket:
    def __init__(self, send_error: Exception | None = None) -> None:
        self.sent: list[dict] = []
        self._send_error = send_error

    async def accept(self) -> None:
        return None

    async def send_text(self, text: str) -> None:
        return None

    async def send_json(self, data: dict) -> None:
        if self._send_error is not None:
            raise self._send_error
        self.sent.append(data)

    async def receive(self) -> dict:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _ScriptedRuns:
    """Each read of a run pops its next scripted step: a status, or an exception to raise."""

    def __init__(self, script: dict[str, list[WorkflowRunStatus | Exception]]) -> None:
        self._script = script

    def _next(self, workflow_run_id: str) -> WorkflowRunStatus:
        step = self._script[workflow_run_id].pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    async def get_workflow_run(self, workflow_run_id: str, organization_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            status=self._next(workflow_run_id), organization_id=organization_id, browser_session_id=None
        )

    async def get_task(self, task_id: str, organization_id: str) -> SimpleNamespace:
        workflow_run_id = f"wr_{task_id.removeprefix('tsk_')}"
        status = TaskStatus(self._next(workflow_run_id).value)
        return SimpleNamespace(
            status=status, workflow_run_id=workflow_run_id, browser_session_id=None, organization_id=organization_id
        )


def _connection_ended(logs: list[dict]) -> list[dict]:
    return [log for log in logs if log.get("live_stream_event") == "connection_ended"]


def _scorecard_tile(logs: list[dict]) -> tuple[set[str], set[str]]:
    """The dashboard tile's distinct-run numerator and denominator, applied to the emitted lines."""
    expected = [log for log in _connection_ended(logs) if log["live_stream_expected"]]
    numerator = {
        log["run_id"]
        for log in expected
        if log["run_phase"] == "active" and log["live_stream_failure"] == "unrecovered"
    }
    return numerator, {log["run_id"] for log in expected}


@pytest.fixture
def run_stream(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Awaitable[_WatchingWebSocket]]:
    monkeypatch.setattr(screenshot.settings, "BROWSER_STREAMING_MODE", "vnc")
    monkeypatch.setattr(screenshot, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    monkeypatch.setattr(screenshot, "get_current_org", AsyncMock(return_value=SimpleNamespace(organization_id="o_1")))
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(mark_streaming_viewer_active=AsyncMock()))

    async def _connect(
        runs: _ScriptedRuns,
        *,
        frame: bytes | None,
        timeout_seconds: int,
        workflow_run_id: str = "",
        task_id: str = "",
        send_error: Exception | None = None,
    ) -> _WatchingWebSocket:
        monkeypatch.setattr(app, "DATABASE", SimpleNamespace(workflow_runs=runs, tasks=runs))
        monkeypatch.setattr(app, "STORAGE", SimpleNamespace(get_streaming_file=AsyncMock(return_value=frame)))
        monkeypatch.setattr(screenshot, "STREAMING_TIMEOUT", timeout_seconds)
        websocket = _WatchingWebSocket(send_error)
        if task_id:
            route = screenshot.task_stream(websocket=cast(WebSocket, websocket), task_id=task_id, apikey="key")
        else:
            route = screenshot.workflow_run_streaming(
                websocket=cast(WebSocket, websocket), workflow_run_id=workflow_run_id, apikey="key"
            )
        await asyncio.wait_for(route, timeout=10)
        return websocket

    return _connect


@pytest.mark.asyncio
async def test_run_stream_tile_counts_a_run_once_and_only_while_its_failure_is_unrecovered(
    run_stream: Callable[..., Awaitable[_WatchingWebSocket]],
) -> None:
    pool_timeout = TimeoutError("QueuePool limit reached")
    runs = _ScriptedRuns(
        {
            "wr_blank": [WorkflowRunStatus.running, pool_timeout, WorkflowRunStatus.running, WorkflowRunStatus.running],
            "wr_recovered": [
                WorkflowRunStatus.running,
                pool_timeout,
                WorkflowRunStatus.running,
                WorkflowRunStatus.completed,
            ],
        }
    )
    never_expire = 10_000
    with capture_logs() as logs:
        # The viewer of a run whose worker never publishes a frame reconnects after a database error, then
        # is timed out on the run page and again on the run's task page.
        await run_stream(runs, frame=None, timeout_seconds=never_expire, workflow_run_id="wr_blank")
        blank_run_page = await run_stream(runs, frame=None, timeout_seconds=-1, workflow_run_id="wr_blank")
        await run_stream(runs, frame=None, timeout_seconds=-1, task_id="tsk_blank")
        # The same database error mid-stream on another run is followed by a reconnect that streams to the end.
        await run_stream(runs, frame=b"png", timeout_seconds=never_expire, workflow_run_id="wr_recovered")
        await run_stream(runs, frame=b"png", timeout_seconds=never_expire, workflow_run_id="wr_recovered")

    assert blank_run_page.sent == [{"workflow_run_id": "wr_blank", "status": "timeout"}]
    assert _scorecard_tile(logs) == ({"wr_blank"}, {"wr_blank", "wr_recovered"})
    timeouts = [log for log in _connection_ended(logs) if log["live_stream_failure"] == "unrecovered"]
    assert {(log["live_stream_stage"], log["run_status"], log["live_stream_lineage"]) for log in timeouts} == {
        ("pre_ready", "running", "local")
    }


@pytest.mark.asyncio
async def test_run_stream_excludes_normal_run_termination_and_viewers_who_arrive_after_it(
    run_stream: Callable[..., Awaitable[_WatchingWebSocket]],
) -> None:
    runs = _ScriptedRuns(
        {
            "wr_1": [WorkflowRunStatus.running, WorkflowRunStatus.running, WorkflowRunStatus.canceled],
            "wr_already_done": [WorkflowRunStatus.completed],
        }
    )
    with capture_logs() as logs:
        websocket = await run_stream(runs, frame=b"png", timeout_seconds=10_000, workflow_run_id="wr_1")
        await run_stream(runs, frame=b"png", timeout_seconds=10_000, workflow_run_id="wr_already_done")

    assert websocket.sent[-1] == {"workflow_run_id": "wr_1", "status": WorkflowRunStatus.canceled}
    ended = _connection_ended(logs)[0]
    assert (ended["live_stream_outcome"], ended["run_phase"], ended["live_stream_stage"]) == (
        "run_finished",
        "finished",
        "post_ready",
    )
    assert _scorecard_tile(logs) == (set(), {"wr_1"})


@pytest.mark.parametrize(
    ("send_error", "outcome"),
    [
        (RuntimeError("Unexpected ASGI message 'websocket.send', after sending 'websocket.close'."), "viewer_left"),
        (RuntimeError('Cannot call "send" once a close message has been sent.'), "viewer_left"),
        (RuntimeError("Event loop is closed"), "stream_error"),
    ],
)
@pytest.mark.asyncio
async def test_run_stream_reads_only_the_close_race_as_the_viewer_leaving(
    run_stream: Callable[..., Awaitable[_WatchingWebSocket]], send_error: Exception, outcome: str
) -> None:
    runs = _ScriptedRuns({"wr_1": [WorkflowRunStatus.running]})
    with capture_logs() as logs:
        await run_stream(runs, frame=b"png", timeout_seconds=10_000, workflow_run_id="wr_1", send_error=send_error)

    [ended] = _connection_ended(logs)
    assert ended["live_stream_outcome"] == outcome


@pytest.mark.asyncio
async def test_task_page_stream_error_counts_as_unrecovered_because_the_task_page_never_reconnects(
    run_stream: Callable[..., Awaitable[_WatchingWebSocket]],
) -> None:
    pool_timeout = TimeoutError("QueuePool limit reached")
    runs = _ScriptedRuns({"wr_task": [WorkflowRunStatus.running, pool_timeout], "wr_run": [pool_timeout]})
    with capture_logs() as logs:
        await run_stream(runs, frame=b"png", timeout_seconds=10_000, task_id="tsk_task")
        await run_stream(runs, frame=b"png", timeout_seconds=10_000, workflow_run_id="wr_run")

    assert {
        (log["run_id"], log["live_stream_outcome"], log["live_stream_failure"]) for log in _connection_ended(logs)
    } == {
        ("wr_task", "stream_error", "unrecovered"),
        ("wr_run", "stream_error", "recoverable"),
    }
    assert _scorecard_tile(logs)[0] == {"wr_task"}
