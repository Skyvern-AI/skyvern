from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from structlog.testing import capture_logs
from websockets.exceptions import ConnectionClosedError

from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.routes.streaming import run_stream_outcome, screenshot, verify
from skyvern.forge.sdk.routes.streaming import vnc as vnc_route
from skyvern.forge.sdk.routes.streaming.channels import vnc as vnc_channels
from skyvern.forge.sdk.schemas.persistent_browser_sessions import PersistentBrowserSession
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.services.request_principal import AuthKind, RequestPrincipal
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
        self.headers: dict[str, str] = {}
        self.url = SimpleNamespace(path="/v1/stream/test")

    async def accept(self, subprotocol: str | None = None) -> None:
        return None

    async def send_text(self, text: str) -> None:
        return None

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self, *, code: int = 1000) -> None:
        return None

    async def receive(self) -> dict:
        if not self._disconnect_delivered:
            self._disconnect_delivered = True
            return {"type": "websocket.disconnect", "code": 1006}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def _yield_instead_of_sleeping(_delay: float) -> None:
    await asyncio.sleep(0)


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
    monkeypatch.setattr(screenshot, "asyncio", ScopedAsyncio(sleep=_yield_instead_of_sleeping))
    monkeypatch.setattr(screenshot, "get_current_org", AsyncMock(return_value=SimpleNamespace(organization_id="o_1")))
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "STORAGE", SimpleNamespace(get_streaming_file=AsyncMock(return_value=b"jpeg")))
    monkeypatch.setattr(
        app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            mark_streaming_viewer_active=AsyncMock(),
            enforce_folder_access=AsyncMock(),
            enforce_browser_session_credential_use=AsyncMock(),
            enforce_workflow_run_credential_use=AsyncMock(),
        ),
    )

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
        self.headers: dict[str, str] = {}
        self.url = SimpleNamespace(path="/v1/stream/test")

    async def accept(self, subprotocol: str | None = None) -> None:
        return None

    async def send_text(self, text: str) -> None:
        return None

    async def send_json(self, data: dict) -> None:
        if self._send_error is not None:
            raise self._send_error
        self.sent.append(data)

    async def close(self, *, code: int = 1000) -> None:
        return None

    async def receive(self) -> dict:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _ScriptedRuns:
    """Each read of a run pops its next scripted step: a status, or an exception to raise."""

    def __init__(
        self,
        script: dict[str, list[WorkflowRunStatus | Exception]],
        browser_runtimes: dict[str, str] | None = None,
    ) -> None:
        self._script = script
        self._browser_runtimes = browser_runtimes or {}

    async def get_browser_runtime(self, workflow_run_id: str, organization_id: str) -> str | None:
        return self._browser_runtimes.get(workflow_run_id)

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
    monkeypatch.setattr(
        app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            mark_streaming_viewer_active=AsyncMock(),
            enforce_folder_access=AsyncMock(),
            enforce_browser_session_credential_use=AsyncMock(),
            enforce_workflow_run_credential_use=AsyncMock(),
        ),
    )

    async def _connect(
        runs: _ScriptedRuns,
        *,
        frame: bytes | None,
        timeout_seconds: int,
        workflow_run_id: str = "",
        task_id: str = "",
        send_error: Exception | None = None,
    ) -> _WatchingWebSocket:
        monkeypatch.setattr(
            app,
            "DATABASE",
            SimpleNamespace(
                workflow_runs=runs,
                tasks=runs,
                browser_sessions=SimpleNamespace(
                    get_persistent_browser_session_by_runnable_id=AsyncMock(return_value=None)
                ),
            ),
        )
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
            "wr_blank": [
                WorkflowRunStatus.running,
                pool_timeout,
                WorkflowRunStatus.running,
                WorkflowRunStatus.running,
                WorkflowRunStatus.running,
            ],
            "wr_recovered": [
                WorkflowRunStatus.running,
                pool_timeout,
                WorkflowRunStatus.running,
                WorkflowRunStatus.completed,
            ],
        },
        # The worker publishes a vendor browser's frames on the same route as a local one.
        browser_runtimes={"wr_blank": "vendor", "wr_recovered": "local"},
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
        ("pre_ready", "running", "vendor")
    }
    assert {log["live_stream_lineage"] for log in _connection_ended(logs) if log["run_id"] == "wr_recovered"} == {
        "local"
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
    runs = _ScriptedRuns(
        {"wr_task": [WorkflowRunStatus.running, WorkflowRunStatus.running, pool_timeout], "wr_run": [pool_timeout]}
    )
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


_LOOPBACK_BROWSER = "ws://127.0.0.1:9224/devtools/browser/b-1"


class _Sessions:
    """Persistent sessions by id, held by the named run (or idle), changed by a scenario while watched."""

    def __init__(self, **holders: str | None) -> None:
        now = datetime.now(UTC)
        self.rows = {
            session_id: PersistentBrowserSession(
                persistent_browser_session_id=session_id,
                organization_id="o_1",
                runnable_type="workflow_run" if holder else None,
                runnable_id=holder,
                status="running",
                browser_address=_LOOPBACK_BROWSER,
                upstream_cdp_url=_LOOPBACK_BROWSER,
                created_at=now,
                modified_at=now,
            )
            for session_id, holder in holders.items()
        }
        self.read_error: Exception | None = None
        self._read = asyncio.Event()

    def occupy(self, session_id: str, run_id: str) -> None:
        self.rows[session_id] = self.rows[session_id].model_copy(
            update={"runnable_type": "workflow_run", "runnable_id": run_id}
        )

    def release(self, session_id: str) -> None:
        self.rows[session_id] = self.rows[session_id].model_copy(update={"runnable_type": None, "runnable_id": None})

    def fail(self, session_id: str) -> None:
        self.rows[session_id] = self.rows[session_id].model_copy(update={"status": "failed"})

    async def next_read(self) -> None:
        self._read.clear()
        await self._read.wait()

    async def get_session(self, session_id: str, organization_id: str) -> PersistentBrowserSession | None:
        self._read.set()
        if self.read_error is not None:
            raise self.read_error
        return self.rows.get(session_id)

    async def release_observer_browser_state(self, session_id: str, browser_state: object) -> None:
        return None


def _run_statuses(**statuses: WorkflowRunStatus) -> SimpleNamespace:
    async def get_workflow_run_status(workflow_run_id: str, organization_id: str) -> WorkflowRunStatus | None:
        return statuses.get(workflow_run_id)

    return SimpleNamespace(workflow_runs=SimpleNamespace(get_workflow_run_status=get_workflow_run_status))


@pytest.mark.asyncio
async def test_session_screencast_counts_a_run_only_while_it_holds_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _Sessions(
        pbs_idle=None,
        pbs_released="wr_released",
        pbs_torn_down="wr_done",
        pbs_died="wr_died",
        pbs_blank="wr_blank",
        pbs_died_unsent="wr_died_unsent",
        pbs_dead_on_arrival="wr_dead_on_arrival",
        pbs_viewer_gone="wr_viewer_gone",
    )
    sessions.fail("pbs_dead_on_arrival")
    running, completed = WorkflowRunStatus.running, WorkflowRunStatus.completed
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", sessions)
    monkeypatch.setattr(
        app,
        "DATABASE",
        _run_statuses(
            wr_released=completed,
            wr_done=completed,
            wr_died=running,
            wr_blank=running,
            wr_died_unsent=running,
            wr_dead_on_arrival=running,
            wr_viewer_gone=running,
        ),
    )
    monkeypatch.setattr(screenshot, "get_current_org", AsyncMock(return_value=SimpleNamespace(organization_id="o_1")))

    async def wait_for_browser_state(entity_id: str, entity_type: str, **_: object) -> object | None:
        return None if entity_id == "pbs_blank" else object()

    # A run that owns its session closes it at its end without releasing it first.
    while_watched = {
        "pbs_released": sessions.release,
        "pbs_torn_down": sessions.fail,
        "pbs_died": sessions.fail,
        "pbs_died_unsent": sessions.fail,
    }
    # These viewers are gone by the time the final status is sent to them.
    viewer_gone = {"pbs_died_unsent", "pbs_dead_on_arrival", "pbs_viewer_gone"}

    async def screencast(*, entity_id: str, check_finalized: Callable[[], Awaitable[bool]], **kwargs: object) -> None:
        cast(Callable[[], None], kwargs["on_frame_sent"])()
        if entity_id in while_watched:
            while_watched[entity_id](entity_id)
        # A frame the viewer can no longer receive ends the loop as quietly as the session finishing.
        if await check_finalized() or entity_id in viewer_gone:
            return
        raise WebSocketDisconnect(1001)

    monkeypatch.setattr(screenshot, "wait_for_browser_state", wait_for_browser_state)
    monkeypatch.setattr(screenshot, "start_screencast_loop", screencast)

    with capture_logs() as logs:
        for session_id in list(sessions.rows):
            viewer = _WatchingWebSocket(WebSocketDisconnect(1006) if session_id in viewer_gone else None)
            await asyncio.wait_for(
                screenshot.browser_session_streaming(
                    websocket=cast(WebSocket, viewer),
                    browser_session_id=session_id,
                    apikey="key",
                    force_cdp=True,
                ),
                timeout=10,
            )

    ended = {log["run_id"]: log for log in _connection_ended(logs)}
    assert {
        run_id: (log["live_stream_outcome"], log["live_stream_failure"], log["live_stream_stage"])
        for run_id, log in ended.items()
    } == {
        "wr_released": ("run_finished", "none", "post_ready"),
        "wr_done": ("run_finished", "none", "post_ready"),
        "wr_died": ("session_ended", "unrecovered", "post_ready"),
        "wr_blank": ("no_frame_timeout", "unrecovered", "pre_ready"),
        "wr_died_unsent": ("session_ended", "unrecovered", "post_ready"),
        "wr_dead_on_arrival": ("session_ended", "unrecovered", "pre_ready"),
        "wr_viewer_gone": ("viewer_left", "none", "post_ready"),
    }
    assert {(log["live_stream_transport"], log["live_stream_lineage"]) for log in ended.values()} == {
        ("cdp_screencast", "pbs")
    }
    assert _scorecard_tile(logs) == (
        {"wr_died", "wr_blank", "wr_died_unsent", "wr_dead_on_arrival"},
        set(ended),
    )


class _DisplayServer:
    """A session's VNC upstream: it sends the RFB handshake, then closes if the scenario says so."""

    def __init__(
        self,
        before_close: Callable[[], Awaitable[None]] | None,
        *,
        frame_after_before_close: bool = False,
        frame_count_after_before_close: int = 0,
    ) -> None:
        self._before_close = before_close
        self._handshake_sent = False
        self._before_close_complete = False
        self._frame_after_before_close = frame_after_before_close
        self._followup_frame_sent = False
        self._frames_remaining = frame_count_after_before_close
        self.sent_data: list[bytes] = []

    async def __aenter__(self) -> _DisplayServer:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def send(self, data: bytes) -> None:
        self.sent_data.append(data)

    async def recv(self) -> bytes:
        if not self._handshake_sent:
            self._handshake_sent = True
            return b"RFB 003.008\n"
        if self._before_close is None:
            await asyncio.Event().wait()
        elif not self._before_close_complete:
            self._before_close_complete = True
            await self._before_close()
            if self._frame_after_before_close and not self._followup_frame_sent:
                self._followup_frame_sent = True
                return b"restricted-run-frame"
        if self._frames_remaining:
            self._frames_remaining -= 1
            return b"ordinary-frame"
        raise ConnectionClosedError(None, None)


class _VncViewer:
    def __init__(
        self,
        *,
        leaves: bool,
        client_message: bytes | None = None,
        client_message_ready: asyncio.Event | None = None,
    ) -> None:
        self.client_state = WebSocketState.CONNECTED
        self._leaves = leaves
        self._client_message = client_message
        self._client_message_ready = client_message_ready
        self._client_message_sent = False
        self._framed = asyncio.Event()
        self._closed = asyncio.Event()
        self.close_codes: list[int] = []
        self.sent_data: list[bytes] = []

    async def receive_bytes(self) -> bytes:
        if self._client_message is not None and not self._client_message_sent:
            await self._framed.wait()
            if self._client_message_ready is not None:
                await self._client_message_ready.wait()
            self._client_message_sent = True
            return self._client_message
        await (self._framed if self._leaves else self._closed).wait()
        self.client_state = WebSocketState.DISCONNECTED
        raise WebSocketDisconnect(1001)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_data.append(data)
        self._framed.set()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_codes.append(code)
        self.client_state = WebSocketState.DISCONNECTED
        self._closed.set()


@pytest.fixture
def watch_over_vnc(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Awaitable[_VncViewer]]:
    """Watch a session over the VNC route until the viewer leaves, or `before_close` runs and the display server
    closes (or fails, when it raises)."""
    monkeypatch.setattr(vnc_route, "auth", AsyncMock(return_value="o_1"))
    monkeypatch.setattr(vnc_channels, "get_x_api_key", AsyncMock(return_value="sk-test"))

    async def next_poll(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(verify, "asyncio", ScopedAsyncio(sleep=next_poll))

    async def _watch(
        session_id: str,
        *,
        before_close: Callable[[], Awaitable[None]] | None = None,
        frame_after_before_close: bool = False,
        frame_count_after_before_close: int = 0,
        viewer_leaves: bool = False,
        client_message: bytes | None = None,
        client_message_ready: asyncio.Event | None = None,
    ) -> _VncViewer:
        display_server = _DisplayServer(
            before_close,
            frame_after_before_close=frame_after_before_close,
            frame_count_after_before_close=frame_count_after_before_close,
        )
        monkeypatch.setattr(vnc_channels.websockets, "connect", lambda url, **_: display_server)
        viewer = _VncViewer(
            leaves=viewer_leaves,
            client_message=client_message,
            client_message_ready=client_message_ready,
        )
        await asyncio.wait_for(
            vnc_route.browser_session_stream(
                websocket=cast(WebSocket, viewer),
                browser_session_id=session_id,
                apikey="key",
                client_id=f"client_{session_id}",
            ),
            timeout=10,
        )
        return viewer

    return _watch


@pytest.mark.asyncio
async def test_vnc_counts_a_run_whose_session_died_under_it_but_not_a_viewer_leaving_or_a_relay_hiccup(
    monkeypatch: pytest.MonkeyPatch, watch_over_vnc: Callable[..., Awaitable[_VncViewer]]
) -> None:
    sessions = _Sessions(
        pbs_left="wr_left", pbs_hiccup="wr_hiccup", pbs_broken="wr_broken", pbs_debug=None, pbs_idle=None
    )
    running = WorkflowRunStatus.running
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", sessions)
    monkeypatch.setattr(
        app, "DATABASE", _run_statuses(wr_left=running, wr_hiccup=running, wr_broken=running, wr_debug=running)
    )

    async def relay_hiccup() -> None:
        return None

    async def display_server_read_fails() -> None:
        raise RuntimeError("cannot call recv while another coroutine is already running recv")

    async def debug_run_starts_then_its_session_dies() -> None:
        # The viewer opened an idle debug session; the run it then starts is what the viewer is watching.
        sessions.occupy("pbs_debug", "wr_debug")
        await sessions.next_read()
        sessions.fail("pbs_debug")

    with capture_logs() as logs:
        await watch_over_vnc("pbs_left", viewer_leaves=True)
        await watch_over_vnc("pbs_hiccup", before_close=relay_hiccup)
        await watch_over_vnc("pbs_broken", before_close=display_server_read_fails)
        await watch_over_vnc("pbs_debug", before_close=debug_run_starts_then_its_session_dies)
        await watch_over_vnc("pbs_idle", viewer_leaves=True)

    ended = {log["run_id"]: log for log in _connection_ended(logs)}
    assert {run_id: (log["live_stream_outcome"], log["live_stream_failure"]) for run_id, log in ended.items()} == {
        "wr_left": ("viewer_left", "none"),
        "wr_hiccup": ("stream_error", "recoverable"),
        "wr_broken": ("stream_error", "recoverable"),
        "wr_debug": ("session_ended", "unrecovered"),
    }
    assert {(log["live_stream_transport"], log["live_stream_stage"]) for log in ended.values()} == {
        ("vnc", "post_ready")
    }
    assert _scorecard_tile(logs) == ({"wr_debug"}, {"wr_left", "wr_hiccup", "wr_broken", "wr_debug"})


@pytest.mark.asyncio
async def test_oss_agent_function_allows_browser_session_stream() -> None:
    agent_functions = AgentFunction()
    assert await agent_functions.authorize_browser_session_stream(None, "o_1", "pbs_1") is True
    assert await agent_functions.reauthorize_browser_session_stream(None, "o_1", "pbs_1") is True
    assert await agent_functions.browser_session_stream_reauthorization_required(None, "o_1") is False


@pytest.mark.asyncio
async def test_browser_session_vnc_stream_checks_cloud_authorization_before_opening_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = RequestPrincipal(organization_id="o_1", auth_kind=AuthKind.bearer, user_id="user_1")
    websocket = SimpleNamespace(close=AsyncMock())
    auth = AsyncMock(return_value="o_1")
    authorize = AsyncMock(return_value=False)
    channel = AsyncMock(return_value=None)

    async def deny_resolved_session(**kwargs: object) -> None:
        authorize_stream = kwargs["authorize_stream"]
        assert callable(authorize_stream)
        assert await authorize_stream("pbs_1") is False  # type: ignore[operator]

    channel.side_effect = deny_resolved_session
    monkeypatch.setattr(vnc_route, "auth", auth)
    monkeypatch.setattr(vnc_route, "get_request_principal", lambda: principal)
    monkeypatch.setattr(vnc_route, "get_vnc_channel_for_browser_session", channel)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(authorize_browser_session_stream=authorize))

    await vnc_route.browser_session_stream(
        websocket=cast(WebSocket, websocket), browser_session_id="pbs_1", client_id="client_1"
    )

    auth_call = auth.await_args
    assert auth_call is not None
    assert auth_call.kwargs["folder_action"] == "workflow_run.execute"
    authorize.assert_awaited_once_with(principal, "o_1", "pbs_1")
    websocket.close.assert_awaited_once_with(code=4403)
    channel.assert_awaited_once()


@pytest.mark.parametrize(
    ("resource", "channel_name"),
    [("task_id", "task"), ("workflow_run_id", "workflow_run")],
)
@pytest.mark.asyncio
async def test_task_and_workflow_run_vnc_streams_keep_execute_auth_without_browser_connect_hook(
    monkeypatch: pytest.MonkeyPatch, resource: str, channel_name: str
) -> None:
    websocket = SimpleNamespace(close=AsyncMock())
    auth = AsyncMock(return_value="o_1")
    authorize = AsyncMock(return_value=False)
    channel = AsyncMock(return_value=None)
    resource_id = f"{resource}_1"
    monkeypatch.setattr(vnc_route, "auth", auth)
    monkeypatch.setattr(vnc_route, f"get_vnc_channel_for_{channel_name}", channel)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(authorize_browser_session_stream=authorize))

    await vnc_route.stream(websocket, client_id="client_1", **{resource: resource_id})

    auth_call = auth.await_args
    assert auth_call is not None
    assert auth_call.kwargs["folder_action"] == "workflow_run.execute"
    assert auth_call.kwargs["folder_resource"][resource] == resource_id
    authorize.assert_not_awaited()
    websocket.close.assert_awaited_once_with(code=1013)


@pytest.mark.asyncio
async def test_vnc_attributes_the_stream_ending_to_the_run_holding_the_session_since_the_last_poll(
    monkeypatch: pytest.MonkeyPatch, watch_over_vnc: Callable[..., Awaitable[_VncViewer]]
) -> None:
    sessions = _Sessions(pbs_handoff="wr_first", pbs_idle=None)
    running = WorkflowRunStatus.running
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", sessions)
    monkeypatch.setattr(app, "DATABASE", _run_statuses(wr_second=running, wr_late=running))
    now = [0.0]
    monkeypatch.setattr(run_stream_outcome, "time", SimpleNamespace(monotonic=lambda: now[0]))

    # Neither of these awaits between its changes and the close, so no session poll sees them.
    async def next_run_takes_the_session_then_it_dies() -> None:
        now[0] += 600
        sessions.release("pbs_handoff")
        sessions.occupy("pbs_handoff", "wr_second")
        sessions.fail("pbs_handoff")

    async def run_takes_the_idle_session() -> None:
        sessions.occupy("pbs_idle", "wr_late")

    with capture_logs() as logs:
        await watch_over_vnc("pbs_handoff", before_close=next_run_takes_the_session_then_it_dies)
        await watch_over_vnc("pbs_idle", before_close=run_takes_the_idle_session)

    # The stage belongs to the connection, which had a working stream before either run took the session.
    assert {
        log["run_id"]: (log["live_stream_outcome"], log["live_stream_failure"], log["live_stream_stage"])
        for log in _connection_ended(logs)
    } == {
        "wr_first": ("run_finished", "none", "post_ready"),
        "wr_second": ("session_ended", "unrecovered", "post_ready"),
        "wr_late": ("stream_error", "recoverable", "post_ready"),
    }
    held_for = {log["run_id"]: log["connection_seconds"] for log in _connection_ended(logs)}
    assert (held_for["wr_first"], held_for["wr_second"]) == (600.0, 0.0)
    assert _scorecard_tile(logs) == ({"wr_second"}, {"wr_first", "wr_second", "wr_late"})


@pytest.mark.asyncio
async def test_vnc_reauthorizes_and_closes_when_an_idle_session_is_assigned_to_a_restricted_run(
    monkeypatch: pytest.MonkeyPatch, watch_over_vnc: Callable[..., Awaitable[_VncViewer]]
) -> None:
    sessions = _Sessions(pbs_idle=None)
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", sessions)
    monkeypatch.setattr(app, "DATABASE", _run_statuses(wr_restricted=WorkflowRunStatus.running))

    denied = asyncio.Event()
    authorization_calls = 0

    async def authorize(*_args: object) -> bool:
        nonlocal authorization_calls
        authorization_calls += 1
        if sessions.rows["pbs_idle"].runnable_id == "wr_restricted":
            denied.set()
            return False
        return True

    authorize_mock = AsyncMock(side_effect=authorize)
    reauthorize_mock = AsyncMock(side_effect=authorize)

    monkeypatch.setattr(
        app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            authorize_browser_session_stream=authorize_mock,
            reauthorize_browser_session_stream=reauthorize_mock,
            browser_session_stream_reauthorization_required=AsyncMock(return_value=True),
        ),
    )

    async def run_takes_the_idle_session() -> None:
        sessions.occupy("pbs_idle", "wr_restricted")
        await denied.wait()

    viewer = await watch_over_vnc("pbs_idle", before_close=run_takes_the_idle_session, frame_after_before_close=True)

    assert authorization_calls >= 2
    assert 4403 in viewer.close_codes
    assert b"restricted-run-frame" not in viewer.sent_data


@pytest.mark.asyncio
async def test_vnc_holds_relay_during_state_change_authorization_then_closes_on_denial() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def authorize(_session_id: str) -> bool:
        entered.set()
        await release.wait()
        return False

    channel, close = _vnc_authorization_channel(authorize)
    assert channel.browser_session is not None
    channel.browser_session.runnable_id = "wr_restricted"
    relays = [asyncio.create_task(channel.authorize_stream_relay()) for _ in range(2)]
    await entered.wait()
    assert not any(relay.done() for relay in relays)
    release.set()
    frames = [b"restricted-run-frame" for allowed in await asyncio.gather(*relays) if allowed]

    assert frames == []
    close.assert_awaited_once_with(code=4403, reason="browser-session-access-denied")


@pytest.mark.asyncio
async def test_vnc_close_cancels_in_flight_authorization() -> None:
    channel, _ = _vnc_authorization_channel(None)
    channel.client_id = "client_close_auth"
    channel.websocket = SimpleNamespace(close=AsyncMock())
    channel._stream_authorization_task = asyncio.create_task(asyncio.Event().wait())

    await vnc_channels.VncChannel.close(channel, code=4403, reason="authorization-denied")

    assert channel._stream_authorization_task.cancelled()
    channel.websocket.close.assert_awaited_once_with(code=4403, reason="authorization-denied")


@pytest.mark.asyncio
async def test_vnc_frames_reuse_connect_authorization_without_repeated_database_checks(
    monkeypatch: pytest.MonkeyPatch, watch_over_vnc: Callable[..., Awaitable[_VncViewer]]
) -> None:
    sessions = _Sessions(pbs_idle=None)
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", sessions)
    authorize = AsyncMock(return_value=True)
    reauthorize = AsyncMock(return_value=True)
    monkeypatch.setattr(
        app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            authorize_browser_session_stream=authorize,
            reauthorize_browser_session_stream=reauthorize,
            browser_session_stream_reauthorization_required=AsyncMock(return_value=True),
        ),
    )

    async def keep_open() -> None:
        return None

    viewer = await watch_over_vnc("pbs_idle", before_close=keep_open, frame_count_after_before_close=20)

    # The resolved session is authorized once at connect; streamed frames do no DB-backed checks.
    assert authorize.await_count == 1
    reauthorize.assert_not_awaited()
    assert len(viewer.sent_data) == 21


@pytest.mark.asyncio
async def test_vnc_frames_keep_the_oss_no_reauthorization_behavior(
    monkeypatch: pytest.MonkeyPatch, watch_over_vnc: Callable[..., Awaitable[_VncViewer]]
) -> None:
    sessions = _Sessions(pbs_idle=None)
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", sessions)
    authorize = AsyncMock(return_value=True)
    reauthorize = AsyncMock(return_value=True)
    should_reauthorize = AsyncMock(return_value=False)
    monkeypatch.setattr(
        app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            authorize_browser_session_stream=authorize,
            reauthorize_browser_session_stream=reauthorize,
            browser_session_stream_reauthorization_required=should_reauthorize,
        ),
    )

    async def keep_open() -> None:
        return None

    viewer = await watch_over_vnc("pbs_idle", before_close=keep_open, frame_count_after_before_close=20)

    assert authorize.await_count == 1
    reauthorize.assert_not_awaited()
    assert should_reauthorize.await_count == 1
    assert len(viewer.sent_data) == 21


def _vnc_authorization_channel(
    authorize: Callable[[str], Awaitable[bool]] | None, *, now: float = 0.0
) -> tuple[vnc_channels.VncChannel, AsyncMock]:
    channel = object.__new__(vnc_channels.VncChannel)
    channel.stream_authorizer = authorize
    channel.stream_authorization_probe = None
    channel.stream_authorization_reauthorizer = None
    channel.browser_session = SimpleNamespace(
        persistent_browser_session_id="pbs_auth",
        runnable_type=None,
        runnable_id=None,
        runnable_generation_id=None,
    )
    channel.organization_id = "org_auth"
    channel._stream_authorized_state = channel._authorization_state(channel.browser_session)
    channel._stream_authorized_at = now
    channel._stream_authorization_failed_at = None
    channel._stream_authorization_attempted_state = channel._stream_authorized_state
    channel._stream_authorization_attempted_at = now
    channel._stream_authorization_task = None
    channel._stream_authorization_lock = asyncio.Lock()
    close = AsyncMock()
    object.__setattr__(channel, "close", close)
    return channel, close


@pytest.mark.asyncio
async def test_vnc_rechecks_when_rbac_becomes_enforced_midstream() -> None:
    channel, close = _vnc_authorization_channel(None)
    channel.stream_authorization_probe = AsyncMock(side_effect=[False, True])
    channel.stream_authorization_reauthorizer = AsyncMock(return_value=False)

    assert await channel.check_stream_authorization() is True
    close.assert_not_awaited()
    assert await channel.check_stream_authorization() is False
    channel.stream_authorization_reauthorizer.assert_awaited_once_with("pbs_auth")
    close.assert_awaited_once_with(code=4403, reason="browser-session-access-denied")


@pytest.mark.asyncio
async def test_vnc_authorizes_a_relay_after_grace_was_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vnc_channels, "time", SimpleNamespace(monotonic=lambda: 16.0))
    authorize = AsyncMock(return_value=True)
    channel, close = _vnc_authorization_channel(authorize)

    assert await channel.authorize_stream_relay() is True
    task = channel._stream_authorization_task
    assert task is not None and await task is True
    authorize.assert_awaited_once_with("pbs_auth")
    close.assert_not_awaited()


@pytest.mark.parametrize("kind", ["task", "workflow_run"])
@pytest.mark.asyncio
async def test_idle_run_vnc_verification_reauthorizes_on_poll(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    entity = SimpleNamespace(task_id="tsk_1", workflow_run_id="wr_1")
    session = SimpleNamespace(persistent_browser_session_id="pbs_1")
    observed: list[object] = []
    channel = SimpleNamespace(
        task=entity if kind == "task" else None,
        workflow_run=entity if kind == "workflow_run" else None,
        browser_session=None,
        organization_id="org_1",
        is_open=True,
        live_stream=SimpleNamespace(observe_session=observed.append),
        check_stream_authorization=AsyncMock(),
    )

    async def stop_after_poll(_seconds: float) -> None:
        channel.is_open = False

    monkeypatch.setattr(verify, "asyncio", SimpleNamespace(sleep=stop_after_poll))
    monkeypatch.setattr(verify, f"verify_{kind}", AsyncMock(return_value=(entity, session)))

    await getattr(verify, f"loop_verify_{kind}")(
        channel,
        on_session=lambda current: vnc_channels.VncChannel.on_session_verified(
            cast(vnc_channels.VncChannel, channel), current
        ),
    )

    channel.check_stream_authorization.assert_awaited_once()
    assert observed == [session]


@pytest.mark.asyncio
async def test_vnc_relays_share_one_nonblocking_authorization_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def authorize(_session_id: str) -> bool:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return True

    channel, channel_close = _vnc_authorization_channel(authorize)
    monkeypatch.setattr(vnc_channels, "time", SimpleNamespace(monotonic=lambda: 6.0))

    assert await channel.authorize_stream_relay() is True  # frontend-to-browser keeps the last good decision
    assert await channel.authorize_stream_relay() is True  # browser-to-frontend shares the same pass
    await entered.wait()
    await asyncio.wait_for(channel._stream_authorization_lock.acquire(), timeout=0.1)
    channel._stream_authorization_lock.release()
    poll = asyncio.create_task(channel.authorize_stream_relay(wait_for_pass=True))
    await asyncio.sleep(0)
    assert not poll.done()
    assert calls == 1
    release.set()

    assert await poll is True
    assert calls == 1
    channel_close.assert_not_awaited()


@pytest.mark.parametrize(
    ("sustained", "permission_denied"),
    [(False, False), (True, False), (False, True)],
    ids=["transient", "sustained", "denied"],
)
@pytest.mark.asyncio
async def test_vnc_denies_permission_errors_and_retries_transient_errors_within_grace(
    monkeypatch: pytest.MonkeyPatch, sustained: bool, permission_denied: bool
) -> None:
    now = 100.0
    calls = 0

    async def authorize(_session_id: str) -> bool:
        nonlocal calls
        calls += 1
        if permission_denied:
            raise PermissionError("credential not accessible")
        if sustained or calls == 1:
            raise RuntimeError("temporary lookup failure")
        return True

    channel, channel_close = _vnc_authorization_channel(authorize, now=100.0)
    monkeypatch.setattr(vnc_channels, "time", SimpleNamespace(monotonic=lambda: now))

    async def recheck(at: float) -> bool:
        nonlocal now
        now = at
        assert await channel.authorize_stream_relay() is True
        task = channel._stream_authorization_task
        assert task is not None
        return await task

    assert await recheck(105.0) is not permission_denied
    if permission_denied:
        channel_close.assert_awaited_once_with(code=4403, reason="browser-session-access-denied")
        assert calls == 1
        return
    channel_close.assert_not_awaited()
    if sustained:
        assert await recheck(110.0) is True
        assert await recheck(115.0) is True
        assert await recheck(120.0) is False
        channel_close.assert_awaited_once_with(code=4403, reason="browser-session-access-denied")
        assert calls == 4
    else:
        assert await recheck(110.0) is True
        channel_close.assert_not_awaited()
        assert calls == 2


@pytest.mark.parametrize(
    ("resource", "channel_name"),
    [("task_id", "task"), ("workflow_run_id", "workflow_run")],
)
@pytest.mark.asyncio
async def test_run_based_vnc_stream_authorizes_only_resolved_persistent_sessions(
    monkeypatch: pytest.MonkeyPatch, resource: str, channel_name: str
) -> None:
    principal = RequestPrincipal(organization_id="o_1", auth_kind=AuthKind.bearer, user_id="user_1")
    websocket = SimpleNamespace(close=AsyncMock())
    auth = AsyncMock(return_value="o_1")
    authorize = AsyncMock(return_value=True)
    session_id = "pbs_1"
    client_id = f"{resource}_client"
    resource_id = f"{resource}_1"
    channel = SimpleNamespace(
        browser_session=SimpleNamespace(persistent_browser_session_id=session_id),
        close=AsyncMock(),
        live_stream=SimpleNamespace(finish=AsyncMock()),
    )

    async def resolve_persistent_channel(**kwargs: object) -> tuple[SimpleNamespace, list[object]]:
        authorize_stream = kwargs["authorize_stream"]
        assert callable(authorize_stream)
        assert await authorize_stream(session_id) is True  # type: ignore[operator]
        return channel, []

    monkeypatch.setattr(vnc_route, "auth", auth)
    monkeypatch.setattr(vnc_route, "get_request_principal", lambda: principal)
    monkeypatch.setattr(vnc_route, f"get_vnc_channel_for_{channel_name}", resolve_persistent_channel)
    monkeypatch.setattr(vnc_route, "collect", AsyncMock())
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(authorize_browser_session_stream=authorize))

    await vnc_route.stream(cast(WebSocket, websocket), client_id=client_id, **{resource: resource_id})

    auth_call = auth.await_args
    assert auth_call is not None
    assert auth_call.kwargs["folder_action"] == "workflow_run.execute"
    assert auth_call.kwargs["folder_resource"][resource] == resource_id
    authorize.assert_awaited_once_with(principal, "o_1", session_id)
    channel.close.assert_awaited_once_with(reason="vnc-closed")


@pytest.mark.asyncio
async def test_vnc_connect_authorization_lookup_error_closes_retryably(monkeypatch: pytest.MonkeyPatch) -> None:
    principal = RequestPrincipal(organization_id="o_1", auth_kind=AuthKind.bearer, user_id="user_1")
    websocket = SimpleNamespace(close=AsyncMock())
    authorize = AsyncMock(side_effect=RuntimeError("authorization lookup failed"))

    async def resolve_channel(**kwargs: object) -> None:
        authorize_stream = kwargs["authorize_stream"]
        assert callable(authorize_stream)
        assert await authorize_stream("pbs_1") is False  # type: ignore[operator]
        return None

    monkeypatch.setattr(vnc_route, "auth", AsyncMock(return_value="o_1"))
    monkeypatch.setattr(vnc_route, "get_request_principal", lambda: principal)
    monkeypatch.setattr(vnc_route, "get_vnc_channel_for_browser_session", resolve_channel)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(authorize_browser_session_stream=authorize))

    await vnc_route.stream(cast(WebSocket, websocket), browser_session_id="pbs_1", client_id="client_lookup_failure")

    authorize.assert_awaited_once_with(principal, "o_1", "pbs_1")
    websocket.close.assert_awaited_once_with(code=1013, reason="authorization-temporarily-unavailable")


@pytest.mark.asyncio
async def test_vnc_keeps_a_session_death_it_saw_when_the_closing_session_read_fails(
    monkeypatch: pytest.MonkeyPatch, watch_over_vnc: Callable[..., Awaitable[_VncViewer]]
) -> None:
    sessions = _Sessions(pbs_died="wr_died")
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", sessions)
    monkeypatch.setattr(app, "DATABASE", _run_statuses(wr_died=WorkflowRunStatus.running))

    async def session_dies_then_its_store_goes_down() -> None:
        sessions.fail("pbs_died")
        await sessions.next_read()
        sessions.read_error = TimeoutError("QueuePool limit reached")

    with capture_logs() as logs:
        await watch_over_vnc("pbs_died", before_close=session_dies_then_its_store_goes_down)

    [ended] = _connection_ended(logs)
    assert (ended["live_stream_outcome"], ended["live_stream_failure"], ended["run_phase"]) == (
        "session_ended",
        "unrecovered",
        "active",
    )
    assert _scorecard_tile(logs) == ({"wr_died"}, {"wr_died"})
