"""A transient page-resolution failure must not drop an already-bound CDP session: the dispatch loop
treats None as "no active page" and silently skips the event while the channel stays open, so the
user keeps interacting with a surface that no longer receives input."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, call

import pytest
from fastapi import WebSocketDisconnect
from playwright._impl._cdp_session import CDPSession as ImplCDPSession
from playwright._impl._connection import Connection
from playwright._impl._errors import TargetClosedError
from playwright._impl._object_factory import create_remote_object
from playwright._impl._transport import Transport
from playwright.async_api import CDPSession
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.forge.sdk.routes.streaming import cdp_input, latency_probe
from skyvern.services.browser_recording.v2.ledger import start_ledger, stop_ledger
from skyvern.services.browser_recording.v2.tap import tap_pipelined


def test_latency_probe_records_the_next_frame_and_clears_pending_input(monkeypatch: pytest.MonkeyPatch) -> None:
    histogram = Mock()
    monkeypatch.setattr(latency_probe, "input_to_frame_seconds", histogram)
    latency_probe.forget("pbs_latency")

    latency_probe.note_frame("pbs_latency", 1.0)
    latency_probe.set_recording("pbs_latency", True)
    latency_probe.note_input("pbs_latency", 2.0)
    latency_probe.note_frame("pbs_latency", 2.125)
    latency_probe.note_frame("pbs_latency", 2.25)
    latency_probe.set_recording("pbs_latency", False)
    latency_probe.note_input("pbs_latency", 3.0)
    latency_probe.note_frame("pbs_latency", 3.5)

    assert histogram.record.call_args_list == [
        call(0.125, {"recording": "on"}),
        call(0.5, {"recording": "off"}),
    ]


def test_forget_clears_the_recording_gauge_so_a_dropped_session_is_not_latched_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An abrupt disconnect never sends END_EXFILTRATION, so teardown's forget() is the only
    thing that can stop the next frame for that id being labelled recording=on."""
    histogram = Mock()
    monkeypatch.setattr(latency_probe, "input_to_frame_seconds", histogram)

    latency_probe.set_recording("pbs_dropped", True)
    latency_probe.forget("pbs_dropped")
    latency_probe.note_input("pbs_dropped", 1.0)
    latency_probe.note_frame("pbs_dropped", 1.25)

    assert histogram.record.call_args_list == [call(0.25, {"recording": "off"})]
    latency_probe.forget("pbs_dropped")


def test_the_recording_cap_evicts_the_oldest_session_not_an_arbitrary_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    histogram = Mock()
    monkeypatch.setattr(latency_probe, "input_to_frame_seconds", histogram)
    ids = [f"pbs_cap_{index}" for index in range(latency_probe.MAX_SESSIONS + 1)]
    for browser_session_id in ids:
        latency_probe.set_recording(browser_session_id, True)

    for browser_session_id in (ids[0], ids[1], ids[-1]):
        latency_probe.note_input(browser_session_id, 0.0)
        latency_probe.note_frame(browser_session_id, 1.0)

    assert histogram.record.call_args_list == [
        call(1.0, {"recording": "off"}),
        call(1.0, {"recording": "on"}),
        call(1.0, {"recording": "on"}),
    ]
    for browser_session_id in ids:
        latency_probe.forget(browser_session_id)


class _RecordingPlaywrightTransport(Transport):
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(loop)
        self.sent: list[dict[str, Any]] = []

    def request_stop(self) -> None:
        pass

    async def wait_until_stopped(self) -> None:
        pass

    async def connect(self) -> None:
        pass

    async def run(self) -> None:
        pass

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_playwright_cdp_send_writes_transport_in_task_submission_order() -> None:
    loop = asyncio.get_running_loop()
    transport = _RecordingPlaywrightTransport(loop)
    connection = Connection(None, create_remote_object, transport, loop)
    session = CDPSession(ImplCDPSession(connection, "CDPSession", "cdp-session", {}))

    first_send = asyncio.create_task(session.send("Input.dispatchMouseEvent", {"sequence": 1}))
    second_send = asyncio.create_task(session.send("Input.dispatchMouseEvent", {"sequence": 2}))
    await asyncio.sleep(0)

    assert [message["params"]["params"]["sequence"] for message in transport.sent] == [1, 2]

    for message in transport.sent:
        transport.on_message({"id": message["id"], "result": {"value": {}}})
    await asyncio.gather(first_send, second_send)


class _FakeSession:
    def __init__(self, name: str, history: dict | None = None) -> None:
        self.name = name
        self.detached = False
        self.sent: list[tuple[str, dict]] = []
        self.history = history

    async def detach(self) -> None:
        self.detached = True

    async def send(self, method: str, params: dict) -> dict | None:
        self.sent.append((method, params))
        if method == "Page.getNavigationHistory":
            return self.history
        return None


class _FakeContext:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def new_cdp_session(self, page: object) -> _FakeSession:
        return self._session


class _FakePage:
    def __init__(self, session: _FakeSession, url: str = "https://example.test/") -> None:
        self.context = _FakeContext(session)
        self.url = url


def _build(monkeypatch: pytest.MonkeyPatch, page: Any) -> tuple[cdp_input.ActivePageCdpInputSession, list[Any]]:
    resolved: list[Any] = [page]

    async def _resolve(*args: Any, **kwargs: Any) -> Any:
        return resolved[0]

    monkeypatch.setattr(cdp_input, "_resolve_working_page", _resolve)
    input_session = cdp_input.ActivePageCdpInputSession(
        browser_state=object(),  # type: ignore[arg-type]
        entity_id="wr_test",
        entity_type="workflow_run",
    )
    return input_session, resolved


@pytest.mark.asyncio
async def test_bound_session_survives_a_transient_resolution_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession("first")
    input_session, resolved = _build(monkeypatch, _FakePage(session))

    assert await input_session.get_session() is session

    resolved[0] = None

    assert await input_session.get_session(force_refresh=True) is session
    assert input_session.page_resolution_failed is False
    assert session.detached is False


@pytest.mark.asyncio
async def test_resolution_failure_before_any_bind_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    input_session, _ = _build(monkeypatch, None)

    assert await input_session.get_session() is None
    assert input_session.page_resolution_failed is True


@pytest.mark.asyncio
async def test_rebinds_when_the_working_page_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _FakeSession("first"), _FakeSession("second")
    input_session, resolved = _build(monkeypatch, _FakePage(first))

    assert await input_session.get_session() is first

    resolved[0] = _FakePage(second)

    assert await input_session.get_session(force_refresh=True) is second
    assert first.detached is True


@pytest.mark.asyncio
async def test_observer_attach_failure_does_not_kill_input_session(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession("first")
    page = _FakePage(session)
    attach_attempted = False

    async def _resolve(*args: Any, **kwargs: Any) -> _FakePage:
        return page

    class _RecordingSession:
        async def attach_page(self, page_key: str, cdp_session: _FakeSession) -> None:
            nonlocal attach_attempted
            attach_attempted = True
            raise RuntimeError("attach failed")

    monkeypatch.setattr(cdp_input, "_resolve_working_page", _resolve)
    monkeypatch.setattr(cdp_input, "get_session_v2", lambda browser_session_id: _RecordingSession())
    input_session = cdp_input.ActivePageCdpInputSession(
        browser_state=object(),  # type: ignore[arg-type]
        entity_id="pbs-test",
        entity_type="browser_session",
    )

    assert await input_session.get_session() is session
    assert attach_attempted is True


@pytest.mark.asyncio
async def test_a_second_recording_attaches_its_own_observer_to_the_same_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The input session outlives a recording, so the next recording must still get attached."""
    session = _FakeSession("first")
    page = _FakePage(session)
    recording_session: Any = None
    attached: list[tuple[str, _FakeSession]] = []

    async def _resolve(*args: Any, **kwargs: Any) -> _FakePage:
        return page

    class _RecordingSession:
        def __init__(self, name: str) -> None:
            self.name = name

        async def attach_page(self, page_key: str, cdp_session: _FakeSession) -> None:
            attached.append((self.name, cdp_session))

    monkeypatch.setattr(cdp_input, "_resolve_working_page", _resolve)
    monkeypatch.setattr(cdp_input, "get_session_v2", lambda browser_session_id: recording_session)
    input_session = cdp_input.ActivePageCdpInputSession(
        browser_state=object(),  # type: ignore[arg-type]
        entity_id="pbs-test",
        entity_type="browser_session",
    )

    assert await input_session.get_session() is session
    recording_session = _RecordingSession("first-recording")
    assert await input_session.get_session(force_refresh=True) is session
    recording_session = None
    assert await input_session.get_session(force_refresh=True) is session
    recording_session = _RecordingSession("second-recording")
    assert await input_session.get_session(force_refresh=True) is session

    assert attached == [("first-recording", session), ("second-recording", session)]


class _FakeNavigablePage:
    """Stands in for the Playwright page `_dispatch_navigate_event` calls `goto()` on."""

    def __init__(
        self,
        response: object = None,
        error: Exception | None = None,
        committed_url_on_error: str | None = None,
        pending_url_on_error: str | None = None,
        reset_error: Exception | None = None,
    ) -> None:
        self.goto_calls: list[str] = []
        self.goto_timeouts: list[float | None] = []
        self._response = response
        self._error = error
        self._committed_url_on_error = committed_url_on_error
        self._pending_url_on_error = pending_url_on_error
        self._pending_url: str | None = None
        self._reset_error = reset_error
        self.url = "about:blank"

    async def goto(self, url: str, *, timeout: float | None = None) -> object:
        self.goto_calls.append(url)
        self.goto_timeouts.append(timeout)
        if url == "about:blank" and self._reset_error is not None:
            error, self._reset_error = self._reset_error, None
            raise error
        if self._error is not None:
            error, self._error = self._error, None
            if self._committed_url_on_error is not None:
                self.url = self._committed_url_on_error
            self._pending_url = self._pending_url_on_error
            raise error
        self._pending_url = None
        self.url = url
        return self._response

    def commit_pending_navigation(self) -> None:
        if self._pending_url is not None:
            self.url, self._pending_url = self._pending_url, None


class _FakeInputSession:
    def __init__(self, cdp_session: _FakeSession, page: object = None) -> None:
        self._cdp_session = cdp_session
        self.page = page if page is not None else _FakeNavigablePage()

    async def get_session(self, *, force_refresh: bool = False) -> _FakeSession:
        return self._cdp_session


class _FakeWebSocket:
    """`_run_input_loop` only touches `receive_text`, `send_json`, and `close`."""

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self.sent_json: list[dict] = []
        self.closed: tuple[int, str] | None = None

    async def receive_text(self) -> str:
        if not self._messages:
            raise WebSocketDisconnect()
        return self._messages.pop(0)

    async def send_json(self, data: dict) -> None:
        self.sent_json.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class _BlockingWebSocket(_FakeWebSocket):
    def __init__(self, messages: list[str]) -> None:
        super().__init__(messages)
        self.all_received = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.close_calls: list[tuple[int, str]] = []

    async def receive_text(self) -> str:
        if self._messages:
            raw = self._messages.pop(0)
            if not self._messages:
                self.all_received.set()
            return raw
        await self.disconnected.wait()
        raise WebSocketDisconnect()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await super().close(code, reason)
        self.close_calls.append((code, reason))
        self.disconnected.set()


class TestNavigateEvent:
    """SKY-13683: a live-view URL input navigates the remote page over the existing
    cdp_input WebSocket, gated by the same take-control check as mouse/keyboard input,
    and validated through the same SSRF guard every real page navigation goes through."""

    @pytest.mark.asyncio
    async def test_dropped_when_not_in_control(self) -> None:
        """The interactor gate lives server-side (cdp_input._run_input_loop), not just in
        the frontend hiding the input box -- anyone dialing the websocket directly without
        having taken control must not be able to redirect the page."""
        page = _FakeNavigablePage()
        input_session = _FakeInputSession(_FakeSession("s"), page=page)
        channel = SimpleNamespace(interactor="agent", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "navigateEvent", "url": "https://example.com"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert page.goto_calls == []
        assert websocket.sent_json == []

    @pytest.mark.asyncio
    async def test_dispatches_page_navigate_after_take_control(self) -> None:
        page = _FakeNavigablePage()
        input_session = _FakeInputSession(_FakeSession("s"), page=page)
        channel = SimpleNamespace(interactor="agent", client_id="c1")
        websocket = _FakeWebSocket(
            [
                json.dumps({"kind": "take-control"}),
                json.dumps({"type": "navigateEvent", "url": "https://example.com/path"}),
            ]
        )

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert page.goto_calls == ["https://example.com/path"]
        assert websocket.sent_json == []

    @pytest.mark.asyncio
    async def test_navigation_protocol_rejection_resets_and_keeps_input_channel_usable(self) -> None:
        session = _FakeSession("s")
        page = _FakeNavigablePage(
            error=PlaywrightError(
                "Page.goto: Protocol error (Page.navigate): 'Page.navigate' destination is not allowed"
            )
        )
        input_session = _FakeInputSession(session, page=page)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket(
            [
                json.dumps({"type": "navigateEvent", "url": "https://unresolvable.invalid"}),
                json.dumps({"type": "mouseEvent", "eventType": "mouseMoved", "x": 10, "y": 20}),
            ]
        )

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert page.goto_calls == ["https://unresolvable.invalid", "about:blank"]
        assert page.goto_timeouts == [None, 5000]
        assert websocket.sent_json == [{"kind": "navigate-error", "reason": "failed"}]
        assert session.sent == [
            (
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": 10, "y": 20, "button": "none", "clickCount": 0, "modifiers": 0},
            )
        ]
        assert websocket.closed is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            TargetClosedError(),
            PlaywrightError("Target page, context or browser has been closed"),
            PlaywrightError("Page.goto: Connection closed while reading from the driver"),
            PlaywrightError(
                "Page.goto: Protocol error (Page.navigate): Session closed. Most likely the page has been closed."
            ),
        ],
        ids=["target_closed", "canonical_base_error", "driver_pipe_closed", "protocol_session_closed"],
    )
    async def test_target_loss_during_navigation_closes_input_channel(self, error: Exception) -> None:
        session = _FakeSession("s")
        page = _FakeNavigablePage(error=error)
        input_session = _FakeInputSession(session, page=page)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket(
            [
                json.dumps({"type": "navigateEvent", "url": "https://example.org"}),
                json.dumps({"type": "mouseEvent", "eventType": "mouseMoved", "x": 10, "y": 20}),
            ]
        )

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert websocket.sent_json == []
        assert websocket.closed == (4411, "dispatch_failed")
        assert session.sent == []

    @pytest.mark.asyncio
    async def test_navigation_timeout_resets_before_error_and_next_input(self) -> None:
        """Pins handler ordering; the fake models, rather than proves, Chromium's contract that a
        later navigation supersedes an earlier pending one."""
        page = _FakeNavigablePage(
            error=PlaywrightTimeoutError("Page.goto: Timeout 30000ms exceeded"),
            pending_url_on_error="http://169.254.169.254/latest/meta-data/",
        )
        input_dispatch_urls: list[str] = []

        class _PageAwareSession(_FakeSession):
            async def send(self, method: str, params: dict) -> dict | None:
                if method.startswith("Input."):
                    input_dispatch_urls.append(page.url)
                return await super().send(method, params)

        class _PendingCommitWebSocket(_FakeWebSocket):
            async def send_json(self, data: dict) -> None:
                await super().send_json(data)
                page.commit_pending_navigation()

        session = _PageAwareSession("s")
        input_session = _FakeInputSession(session, page=page)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _PendingCommitWebSocket(
            [
                json.dumps({"type": "navigateEvent", "url": "https://public.invalid"}),
                json.dumps({"type": "mouseEvent", "eventType": "mouseMoved", "x": 10, "y": 20}),
            ]
        )

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert page.goto_calls == ["https://public.invalid", "about:blank"]
        assert input_dispatch_urls == ["about:blank"]
        assert websocket.sent_json == [{"kind": "navigate-error", "reason": "failed"}]
        assert websocket.closed is None

    @pytest.mark.asyncio
    async def test_navigation_failure_after_internal_redirect_resets_before_next_input(self) -> None:
        page = _FakeNavigablePage(
            error=PlaywrightError("Page.goto: Timeout 30000ms exceeded"),
            committed_url_on_error="http://169.254.169.254/latest/meta-data/",
        )
        input_dispatch_urls: list[str] = []

        class _PageAwareSession(_FakeSession):
            async def send(self, method: str, params: dict) -> dict | None:
                if method.startswith("Input."):
                    input_dispatch_urls.append(page.url)
                return await super().send(method, params)

        session = _PageAwareSession("s")
        input_session = _FakeInputSession(session, page=page)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket(
            [
                json.dumps({"type": "navigateEvent", "url": "https://public.invalid"}),
                json.dumps({"type": "mouseEvent", "eventType": "mouseMoved", "x": 10, "y": 20}),
            ]
        )

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert page.goto_calls == ["https://public.invalid", "about:blank"]
        assert input_dispatch_urls == ["about:blank"]
        assert websocket.sent_json == [{"kind": "navigate-error", "reason": "failed"}]
        assert websocket.closed is None

    @pytest.mark.asyncio
    async def test_rejects_blocked_destination_via_the_real_ssrf_guard(self) -> None:
        """Uses the real validate_navigation_destination (no monkeypatch) against a known
        cloud-metadata IP: if the guard call is ever deleted or bypassed, page.goto WOULD
        get dispatched here and this assertion goes red -- that is the point of the test."""
        page = _FakeNavigablePage()
        input_session = _FakeInputSession(_FakeSession("s"), page=page)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket(
            [json.dumps({"type": "navigateEvent", "url": "http://169.254.169.254/latest/meta-data/"})]
        )

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert page.goto_calls == []
        assert websocket.sent_json == [{"kind": "navigate-error", "reason": "blocked"}]
        assert websocket.closed is None

    @pytest.mark.asyncio
    async def test_rejects_empty_url_without_dispatching(self) -> None:
        page = _FakeNavigablePage()
        input_session = _FakeInputSession(_FakeSession("s"), page=page)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "navigateEvent", "url": "   "})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert page.goto_calls == []
        assert websocket.sent_json == [{"kind": "navigate-error", "reason": "invalid_url"}]

    @pytest.mark.asyncio
    async def test_allows_a_public_destination_through_the_real_guard(self) -> None:
        page = _FakeNavigablePage()
        input_session = _FakeInputSession(_FakeSession("s"), page=page)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "navigateEvent", "url": "https://example.org/"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert page.goto_calls == ["https://example.org/"]
        assert websocket.sent_json == []

    @pytest.mark.asyncio
    async def test_normalizes_a_bare_host_before_dispatch(self) -> None:
        """A schemeless entry like `example.org` passes validation because a scheme is
        prepended for the check; the browser must be sent that same normalized value, not
        the raw user text, or a scheme-less string reaching page.goto behaves differently
        (and any dispatch failure would close the whole channel instead of erroring inline)."""
        page = _FakeNavigablePage()
        input_session = _FakeInputSession(_FakeSession("s"), page=page)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "navigateEvent", "url": "example.org"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert page.goto_calls == ["https://example.org"]
        assert websocket.sent_json == []

    @pytest.mark.asyncio
    async def test_navigate_blocked_via_redirect_chain_resets_the_page(self) -> None:
        """page.goto follows redirects at the network layer, so a destination that itself
        passes validate_navigation_destination can still land on a blocked host after a
        redirect. Uses the real revalidate_redirect_chain/validate_navigation_destination
        (no monkeypatch): remove the revalidation call and this test's second assertion on
        goto_calls goes red, since the page would be left sitting on the blocked content."""
        final_request = SimpleNamespace(
            url="http://169.254.169.254/latest/meta-data/",
            redirected_from=SimpleNamespace(url="https://example.org/redirect", redirected_from=None),
        )
        response = SimpleNamespace(request=final_request)
        page = _FakeNavigablePage(response=response)
        input_session = _FakeInputSession(_FakeSession("s"), page=page)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "navigateEvent", "url": "https://example.org/redirect"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert page.goto_calls == ["https://example.org/redirect", "about:blank"]
        assert websocket.sent_json == [{"kind": "navigate-error", "reason": "blocked"}]

    @pytest.mark.asyncio
    async def test_blocked_redirect_reset_failure_closes_input_channel(self) -> None:
        final_request = SimpleNamespace(
            url="http://169.254.169.254/latest/meta-data/",
            redirected_from=SimpleNamespace(url="https://example.org/redirect", redirected_from=None),
        )
        session = _FakeSession("s")
        page = _FakeNavigablePage(
            response=SimpleNamespace(request=final_request),
            reset_error=PlaywrightTimeoutError("Page.goto: Timeout 5000ms exceeded"),
        )
        input_session = _FakeInputSession(session, page=page)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket(
            [
                json.dumps({"type": "navigateEvent", "url": "https://example.org/redirect"}),
                json.dumps({"type": "mouseEvent", "eventType": "mouseMoved", "x": 10, "y": 20}),
            ]
        )

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert page.goto_calls == ["https://example.org/redirect", "about:blank"]
        assert page.goto_timeouts == [None, 5000]
        assert websocket.sent_json == []
        assert websocket.closed == (4411, "dispatch_failed")
        assert session.sent == []


def _history(current_index: int, *urls: str) -> dict:
    return {
        "currentIndex": current_index,
        "entries": [{"id": i, "url": url} for i, url in enumerate(urls)],
    }


def _dispatched(session: _FakeSession) -> list[tuple[str, dict]]:
    return [call for call in session.sent if call[0] != "Page.getNavigationHistory"]


class TestHistoryNavigation:
    """SKY-13724: the live-view browser chrome drives back/forward/reload over the same
    cdp_input socket, behind the same take-control gate, and re-validates the history entry
    it is about to replay rather than trusting that everything in the back stack was safe."""

    @pytest.mark.asyncio
    async def test_back_navigates_to_the_previous_entry(self) -> None:
        session = _FakeSession("s", history=_history(1, "https://example.org/one", "https://example.org/two"))
        input_session = _FakeInputSession(session)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "goBackEvent"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert _dispatched(session) == [("Page.navigateToHistoryEntry", {"entryId": 0})]
        assert websocket.sent_json == []

    @pytest.mark.asyncio
    async def test_forward_navigates_to_the_next_entry(self) -> None:
        session = _FakeSession("s", history=_history(0, "https://example.org/one", "https://example.org/two"))
        input_session = _FakeInputSession(session)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "goForwardEvent"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert _dispatched(session) == [("Page.navigateToHistoryEntry", {"entryId": 1})]

    @pytest.mark.asyncio
    async def test_reload_reloads_rather_than_replaying_a_history_entry(self) -> None:
        session = _FakeSession("s", history=_history(0, "https://example.org/one"))
        input_session = _FakeInputSession(session)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "reloadEvent"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert _dispatched(session) == [("Page.reload", {})]

    @pytest.mark.asyncio
    async def test_back_at_the_start_of_history_is_a_no_op(self) -> None:
        """The frontend leaves the buttons enabled, so the end-of-stack check has to live
        here; walking off the end must not raise and must not close the input channel."""
        session = _FakeSession("s", history=_history(0, "https://example.org/one"))
        input_session = _FakeInputSession(session)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "goBackEvent"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert _dispatched(session) == []
        assert websocket.sent_json == []
        assert websocket.closed is None

    @pytest.mark.asyncio
    async def test_refuses_to_replay_a_blocked_entry_left_in_the_back_stack(self) -> None:
        """A destination blocked mid-redirect resets the page but stays in the history, so
        going back would re-request it. Uses the real validate_navigation_destination (no
        monkeypatch): drop the guard from _dispatch_history_event and this goes red."""
        session = _FakeSession(
            "s",
            history=_history(1, "http://169.254.169.254/latest/meta-data/", "https://example.org/two"),
        )
        input_session = _FakeInputSession(session)
        channel = SimpleNamespace(interactor="user", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "goBackEvent"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert _dispatched(session) == []
        assert websocket.sent_json == [{"kind": "navigate-error", "reason": "blocked"}]

    @pytest.mark.asyncio
    async def test_dropped_when_not_in_control(self) -> None:
        session = _FakeSession("s", history=_history(1, "https://example.org/one", "https://example.org/two"))
        input_session = _FakeInputSession(session)
        channel = SimpleNamespace(interactor="agent", client_id="c1")
        websocket = _FakeWebSocket([json.dumps({"type": "goBackEvent"})])

        await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

        assert session.sent == []


@pytest.mark.asyncio
async def test_pointer_burst_is_received_before_dispatches_complete() -> None:
    event_count = 10
    release_sends = asyncio.Event()

    class _ControlledSession(_FakeSession):
        def __init__(self) -> None:
            super().__init__("s")
            self.started: list[tuple[str, dict]] = []

        async def send(self, method: str, params: dict) -> None:
            self.started.append((method, params))
            await release_sends.wait()
            self.sent.append((method, params))

    session = _ControlledSession()
    input_session = _FakeInputSession(session)
    channel = SimpleNamespace(interactor="user", client_id="c1")
    websocket = _BlockingWebSocket(
        [
            json.dumps({"type": "wheelEvent", "x": 10, "y": 20, "deltaX": 0, "deltaY": index + 1})
            for index in range(event_count)
        ]
    )
    loop_task = asyncio.create_task(
        cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)
    )

    try:
        await asyncio.wait_for(websocket.all_received.wait(), timeout=0.5)
        async with asyncio.timeout(0.5):
            while len(session.started) < event_count:
                await asyncio.sleep(0)
        assert len(session.started) == event_count
        assert session.sent == []
    finally:
        release_sends.set()
        websocket.disconnected.set()
        await loop_task

    assert len(session.sent) == event_count


@pytest.mark.asyncio
async def test_pipelined_tap_is_ordered_before_semaphore_acquire(monkeypatch: pytest.MonkeyPatch) -> None:
    browser_session_id = "pbs_ledger_order"
    ledger = start_ledger(browser_session_id)
    order: list[str] = []

    class _OrderingSemaphore:
        def __init__(self, value: int) -> None:
            assert value > 0

        async def acquire(self) -> None:
            order.append("acquire")

        def release(self) -> None:
            pass

    def _recording_tap(*args: Any, **kwargs: Any) -> None:
        tap_pipelined(*args, **kwargs)
        order.append("tap")

    monkeypatch.setattr(cdp_input.asyncio, "Semaphore", _OrderingSemaphore)
    monkeypatch.setattr(cdp_input, "tap_pipelined", _recording_tap)
    input_session = _FakeInputSession(_FakeSession("s"))
    channel = SimpleNamespace(interactor="user", client_id="c1")
    websocket = _FakeWebSocket(
        [
            json.dumps(
                {
                    "type": "mouseEvent",
                    "eventType": "mousePressed",
                    "x": 10,
                    "y": 20,
                    "button": "left",
                    "clickCount": 1,
                    "modifiers": 0,
                }
            )
        ]
    )

    try:
        await cdp_input._run_input_loop(
            websocket, channel, input_session, "browser_session_id", browser_session_id, browser_session_id
        )

        assert order[:2] == ["tap", "acquire"]
        assert [(row.seq, row.kind) for row in ledger.rows()] == [(1, "mouse_pressed")]
    finally:
        stop_ledger(browser_session_id)


@pytest.mark.asyncio
async def test_target_closed_background_dispatch_is_dropped_without_closing_input_channel() -> None:
    dispatch_attempted = asyncio.Event()

    class _StaleSession(_FakeSession):
        async def send(self, method: str, params: dict) -> None:
            dispatch_attempted.set()
            raise TargetClosedError("Target page, context or browser has been closed")

    input_session = _FakeInputSession(_StaleSession("s"))
    channel = SimpleNamespace(interactor="user", client_id="c1")
    websocket = _BlockingWebSocket([json.dumps({"type": "wheelEvent", "x": 10, "y": 20, "deltaX": 0, "deltaY": 1})])
    loop_task = asyncio.create_task(
        cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)
    )

    try:
        await asyncio.wait_for(dispatch_attempted.wait(), timeout=0.5)
        await asyncio.sleep(0)
        assert websocket.close_calls == []
    finally:
        websocket.disconnected.set()
        await loop_task


@pytest.mark.asyncio
async def test_background_dispatch_failure_closes_input_channel_once() -> None:
    class _FailingSession(_FakeSession):
        async def send(self, method: str, params: dict) -> None:
            raise RuntimeError("dispatch failed")

    input_session = _FakeInputSession(_FailingSession("s"))
    channel = SimpleNamespace(interactor="user", client_id="c1")
    websocket = _BlockingWebSocket([json.dumps({"type": "wheelEvent", "x": 10, "y": 20, "deltaX": 0, "deltaY": 1})])

    await cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_test", None)

    assert websocket.close_calls == [(4411, "dispatch_failed")]


@pytest.mark.asyncio
async def test_dispatched_input_event_records_wait_and_dispatch_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session = _FakeSession("s1")
    dispatch_hist, wait_hist = Mock(), Mock()
    monkeypatch.setattr(cdp_input, "_input_dispatch_seconds", dispatch_hist)
    monkeypatch.setattr(cdp_input, "_input_wait_seconds", wait_hist)
    incoming = [
        json.dumps(
            {
                "type": "mouseEvent",
                "eventType": "mousePressed",
                "x": 10,
                "y": 20,
                "button": "left",
                "clickCount": 1,
                "modifiers": 0,
            }
        )
    ]

    async def _get_session() -> _FakeSession:
        await asyncio.sleep(0.01)
        return fake_session

    async def _send(method: str, params: dict) -> None:
        await asyncio.sleep(0.01)
        fake_session.sent.append((method, params))

    fake_session.send = _send  # type: ignore[method-assign]
    websocket = _BlockingWebSocket(incoming)
    channel = SimpleNamespace(interactor="user", client_id="c1")
    input_session = SimpleNamespace(get_session=_get_session, page=object())

    loop_task = asyncio.create_task(
        cdp_input._run_input_loop(websocket, channel, input_session, "browser_session_id", "pbs_1", None)
    )
    async with asyncio.timeout(0.5):
        while not fake_session.sent:
            await asyncio.sleep(0)
    websocket.disconnected.set()
    await loop_task

    assert fake_session.sent[0][0] == "Input.dispatchMouseEvent"
    dispatch_value, attributes = dispatch_hist.record.call_args.args
    wait_value, wait_attributes = wait_hist.record.call_args.args
    assert attributes == wait_attributes == {"event_kind": "mouseEvent"}
    # The first delay is active-page resolution; the second is the CDP send. These
    # assertions fail if either timing boundary moves to the wrong side of its await.
    assert wait_value >= 0.005
    assert dispatch_value >= 0.005
