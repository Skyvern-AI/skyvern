"""Downloads, file choosers, console messages, and the recording kwargs production always passes.

Every test here exists because a listener that never fires, or a context that refuses to be created,
is the failure mode this engine keeps rediscovering: production dereferences a driver capability on
its main path, and the raw-CDP engine's "fails loud rather than degrade" instinct turns that into a
dead run rather than a degraded one.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import pytest_asyncio

from skyvern.webeye import attach_only
from skyvern.webeye.attach_only import AttachOnlyViolation
from skyvern.webeye.skycdp.connection import CdpConnection, CdpSession, TargetInfo
from skyvern.webeye.skycdp.errors import CdpError
from skyvern.webeye.skycdp.facade.artifacts import ConsoleMessage, Download, FileChooser
from skyvern.webeye.skycdp.facade.browser import Browser, CdpSessionFacade
from skyvern.webeye.skycdp.transport import CdpTransport

pytestmark = pytest.mark.asyncio

_OPEN: list[Browser] = []


@pytest_asyncio.fixture(autouse=True)
async def _close_connections() -> Any:
    yield
    while _OPEN:
        await _OPEN.pop().close()


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, payload: str) -> None:
        message = json.loads(payload)
        self.sent.append(message)
        # Answer every command immediately; these tests are about events and arguments, not results.
        # Target.attachToTarget is the exception -- the connection reads sessionId out of it, so a
        # generic result would fail there rather than in the code under test.
        result: dict[str, Any] = {"browserContextId": "ctx-1", "targetInfos": []}
        if message.get("method") == "Target.attachToTarget":
            result["sessionId"] = f"session-for-{(message.get('params') or {}).get('targetId', 'x')}"
        self._inbox.put_nowait(json.dumps({"id": message["id"], "result": result}))

    async def recv(self) -> str:
        message = await self._inbox.get()
        if message is None:
            raise ConnectionError("socket closed")
        return message

    async def close(self) -> None:
        self.closed = True
        await self._inbox.put(None)

    def push(self, message: dict) -> None:
        self._inbox.put_nowait(json.dumps(message))

    def methods(self) -> list[str]:
        return [message.get("method") for message in self.sent]

    def params_for(self, method: str) -> dict:
        for message in self.sent:
            if message.get("method") == method:
                return message.get("params") or {}
        raise AssertionError(f"{method} was never sent; saw {self.methods()}")


async def _connected() -> tuple[Browser, FakeSocket]:
    socket = FakeSocket()
    transport = CdpTransport(socket)  # type: ignore[arg-type]
    await transport.start()
    connection = CdpConnection(transport)
    await connection.start()
    browser = Browser(connection)
    _OPEN.append(browser)
    return browser, socket


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


class TestRecordingKwargs:
    """`new_context(record_video_dir=...)` is on production's main path, so it decides whether the
    engine can create a context at all."""

    async def test_absent_recording_directory_is_not_treated_as_a_request_to_record(self) -> None:
        """Production passes the argument unconditionally and it is None when recording is off.

        Keying on the argument's presence rather than its value rejected every context creation in
        the fleet -- a full offline replay produced 0 of 13 runs, all dying here before touching a
        page.
        """
        browser, _ = await _connected()
        context = await browser.new_context(record_video_dir=None, record_video_size=None, viewport=None)
        assert context is not None

    async def test_recording_request_fails_the_run_in_an_attach_only_worker(self) -> None:
        browser, _ = await _connected()
        attach_only.enforce_attach_only(True)
        try:
            with pytest.raises(AttachOnlyViolation):
                await browser.new_context(record_video_dir="/tmp/video")
        finally:
            attach_only.enforce_attach_only(False)

    async def test_recording_request_elsewhere_drops_the_capability_and_still_builds_a_context(self) -> None:
        """Outside the attach-only worker skycdp runs beside Playwright for comparison.

        Recording cannot exist here -- there is no driver process to do it -- so refusing the context
        would mean the engine never runs at all, which is strictly worse than running without video.
        """
        browser, _ = await _connected()
        assert attach_only.is_enforcing() is False
        context = await browser.new_context(record_video_dir="/tmp/video", record_video_size={"width": 1, "height": 1})
        assert context is not None


class TestListenerRemoval:
    async def test_cdp_session_facade_removes_listeners_by_either_spelling(self) -> None:
        """The download interceptor subscribes with `on` and unsubscribes with `remove_listener`.

        Without the alias that unsubscribe raised AttributeError inside a suppressed block: the
        handler stayed bound, and re-enabling the interceptor made it see every download twice.
        """
        browser, socket = await _connected()
        facade = CdpSessionFacade(browser.connection, session=None)
        seen: list[dict] = []
        facade.on("Browser.downloadWillBegin", seen.append)

        socket.push({"method": "Browser.downloadWillBegin", "params": {"guid": "a"}})
        await _settle()
        assert len(seen) == 1

        facade.remove_listener("Browser.downloadWillBegin", seen.append)
        socket.push({"method": "Browser.downloadWillBegin", "params": {"guid": "b"}})
        await _settle()
        assert len(seen) == 1, "remove_listener did not unhook the handler"


class TestFileChooserInterception:
    async def test_interception_is_enabled_for_every_page_session(self) -> None:
        """Not gated on anyone listening: an un-intercepted file input opens a native dialog that no
        headless run can dismiss, so the renderer blocks until the action times out."""
        browser, socket = await _connected()
        session = CdpSession(browser.connection, "session-1", TargetInfo("t1", "page", "about:blank"))
        await browser.connection.prepare_page_session(session)
        assert "Page.setInterceptFileChooserDialog" in socket.methods()
        assert socket.params_for("Page.setInterceptFileChooserDialog") == {"enabled": True}

    async def test_set_files_refuses_paths_that_do_not_exist(self) -> None:
        """DOM.setFileInputFiles accepts a missing path without complaint and uploads nothing, so the
        check has to happen here or the run reports a successful upload of no file."""
        browser, _ = await _connected()
        session = CdpSession(browser.connection, "session-1", TargetInfo("t1", "page", "about:blank"))
        chooser = FileChooser(session, page=None, backend_node_id=7, multiple=False)
        with pytest.raises(CdpError):
            await chooser.set_files("/nonexistent/definitely-not-here.pdf")


class TestDownload:
    async def test_path_resolves_when_chrome_reports_completion(self) -> None:
        browser, _ = await _connected()
        download = Download(browser.connection, guid="g1", url="http://x/f.csv", suggested_filename="f.csv", page=None)
        download.note_progress({"state": "completed", "filePath": "/tmp/f.csv"})
        assert await download.path() == "/tmp/f.csv"
        assert await download.failure() is None

    async def test_a_cancelled_download_reports_a_failure_rather_than_hanging(self) -> None:
        browser, _ = await _connected()
        download = Download(browser.connection, guid="g1", url="http://x/f.csv", suggested_filename="f.csv", page=None)
        download.note_progress({"state": "canceled"})
        assert await download.path() is None
        assert await download.failure() == "canceled"

    async def test_a_download_with_no_known_frame_still_reaches_a_listener(self) -> None:
        """Losing the file silently is the one outcome worth ruling out."""
        browser, socket = await _connected()
        seen: list[Any] = []
        browser.on("download", seen.append)
        socket.push(
            {
                "method": "Browser.downloadWillBegin",
                "params": {"guid": "g9", "url": "http://x/f.csv", "suggestedFilename": "f.csv", "frameId": "unknown"},
            }
        )
        await _settle()
        assert len(seen) == 1
        assert seen[0].suggested_filename == "f.csv"


class TestPopup:
    async def test_a_new_target_is_announced_to_the_page_that_opened_it(self) -> None:
        """Production listens for popups on the opener, not on the context.

        `openerId` is the only thing distinguishing a popup from any other new page, so without it a
        download arriving via target=_blank is never reached.
        """
        browser, socket = await _connected()
        opener_session = await browser.connection.attach("target-opener")
        opener = await browser._context_for(None)._adopt(opener_session)
        opener_session.target.target_id = "target-opener"

        seen: list[Any] = []
        opener.on("popup", seen.append)

        socket.push(
            {
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": "session-popup",
                    "targetInfo": {
                        "targetId": "target-popup",
                        "type": "page",
                        "url": "about:blank",
                        "openerId": "target-opener",
                    },
                },
            }
        )
        # A real deadline, not a fixed number of event-loop turns. Absorbing the popup goes through
        # attach -> prepare_page_session -> _build_page, each of which awaits the transport, so the
        # number of turns needed depends on scheduling -- it passed locally and failed on a loaded CI
        # runner. Yielding until a deadline is both faster in the common case and not a coin flip.
        deadline = asyncio.get_running_loop().time() + 5.0
        while not seen and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert len(seen) == 1, "the opener was never told it opened a popup"
        assert seen[0] is not opener


class TestConsoleMessage:
    async def test_text_renders_from_the_previews_chrome_already_sent(self) -> None:
        """Resolving each argument would cost a round trip per console line, on the highest-volume
        event there is."""
        message = ConsoleMessage(
            {
                "type": "error",
                "args": [{"type": "string", "value": "boom"}, {"type": "number", "value": 42}],
                "stackTrace": {"callFrames": [{"url": "http://x/a.js", "lineNumber": 3, "columnNumber": 9}]},
            }
        )
        assert message.type == "error"
        assert message.text == "boom 42"
        assert message.location["url"] == "http://x/a.js"
        assert message.location["lineNumber"] == 3

    async def test_an_object_argument_falls_back_to_its_description(self) -> None:
        message = ConsoleMessage({"type": "log", "args": [{"type": "object", "description": "Error: nope"}]})
        assert message.text == "Error: nope"


class TestNetworkStateIsBounded:
    """Per-request state is retained so `response.body()` can be lazy, which makes it a leak risk.

    `body()` is fetched after the transfer ends, so releasing on `loadingFinished` -- the obvious
    place -- would break the one consumer this exists for. The state is bounded instead.
    """

    async def test_tracking_is_bounded_rather_than_growing_with_the_page(self) -> None:
        from skyvern.webeye.skycdp.facade import network_events

        browser, socket = await _connected()
        session = CdpSession(browser.connection, "session-1", TargetInfo("t1", "page", "about:blank"))
        page = await browser._context_for(None)._adopt(session)

        for index in range(network_events._MAX_TRACKED_REQUESTS + 250):
            page._network._on_request_will_be_sent(
                {"requestId": f"r{index}", "request": {"url": f"http://x/{index}"}, "type": "XHR"}
            )

        tracked = len(page._network._requests)
        assert tracked <= network_events._MAX_TRACKED_REQUESTS, f"tracking grew unbounded to {tracked}"
        # Oldest evicted, newest kept -- a body is only ever asked about a recent request.
        assert "r0" not in page._network._requests
        assert f"r{network_events._MAX_TRACKED_REQUESTS + 249}" in page._network._requests

    async def test_asking_for_an_evicted_body_says_so_instead_of_returning_nothing(self) -> None:
        browser, _ = await _connected()
        session = CdpSession(browser.connection, "session-1", TargetInfo("t1", "page", "about:blank"))
        page = await browser._context_for(None)._adopt(session)
        with pytest.raises(CdpError, match="no longer tracked"):
            await page._network.response_body("never-seen")

    async def test_a_closed_page_holds_no_request_state(self) -> None:
        browser, _ = await _connected()
        session = CdpSession(browser.connection, "session-1", TargetInfo("t1", "page", "about:blank"))
        page = await browser._context_for(None)._adopt(session)
        page._network._on_request_will_be_sent({"requestId": "r1", "request": {"url": "http://x/"}, "type": "XHR"})
        assert page._network._requests

        await page.close()
        assert page._network._requests == {}
        assert page._network._finished == {}
