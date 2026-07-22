from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from skyvern.webeye import cdp_frame_publisher as publisher_module
from skyvern.webeye.cdp_frame_publisher import (
    CDPFramePublisher,
    _write_frame_atomically,
    stream_key_for_task,
    stream_key_for_workflow_run,
)

ORG_ID = "o_test_123"
STREAM_KEY = "wr_test_456.png"


class TargetClosedError(Exception):
    """Stand-in named exactly like the driver's teardown class.

    Both stock Playwright and patchright name this class ``TargetClosedError`` while
    exposing it as distinct types, so the publisher classifies it by type name. A local
    look-alike proves that name-based match without importing either driver.
    """


def _make_cdp_session(frame_bytes_seq: list[bytes]) -> MagicMock:
    """CDP session whose ``send`` returns successive base64-encoded PNGs."""
    encoded_seq = [base64.b64encode(b).decode("ascii") for b in frame_bytes_seq]
    call_state = {"i": 0, "screenshot_params": []}

    async def _send(method: str, params: dict | None = None) -> dict:
        if method != "Page.captureScreenshot":
            return {}
        call_state["screenshot_params"].append(params or {})
        idx = min(call_state["i"], len(encoded_seq) - 1)
        call_state["i"] += 1
        return {"data": encoded_seq[idx]}

    session = MagicMock()
    session.send = AsyncMock(side_effect=_send)
    session.detach = AsyncMock()
    session._screenshot_calls = call_state["screenshot_params"]  # for assertions
    return session


def _make_page_with_session(session: MagicMock) -> MagicMock:
    page = MagicMock()
    page.context = MagicMock()
    page.context.new_cdp_session = AsyncMock(return_value=session)
    return page


def _make_browser_state(page: MagicMock | None, *, connected: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        get_working_page=AsyncMock(return_value=page),
        is_connected=lambda: connected,
    )


@pytest.fixture
def streaming_temp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(publisher_module, "get_skyvern_temp_dir", lambda: str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    save_mock = AsyncMock()
    fake_app = SimpleNamespace(STORAGE=SimpleNamespace(save_streaming_file=save_mock))
    monkeypatch.setattr(publisher_module, "app", fake_app)
    return save_mock


async def _drive_publish_once(pub: CDPFramePublisher) -> None:
    """Call the internal one-frame routine without starting the long-running loop."""
    await pub._publish_one_frame()


def _stream_path(temp_dir: Path) -> Path:
    return temp_dir / ORG_ID / STREAM_KEY


def test_stream_key_helpers() -> None:
    assert stream_key_for_workflow_run("wr_42") == "wr_42.png"
    assert stream_key_for_task("tsk_7") == "tsk_7.png"


@pytest.mark.asyncio
async def test_publishes_initial_frame_to_streaming_storage(streaming_temp_dir: Path, fake_storage: AsyncMock) -> None:
    frame_bytes = b"\xff\xd8jpegbytes1"
    session = _make_cdp_session([frame_bytes])
    page = _make_page_with_session(session)
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(page),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
    )

    await _drive_publish_once(pub)

    target = _stream_path(streaming_temp_dir)
    assert target.exists()
    assert target.read_bytes() == frame_bytes
    fake_storage.assert_awaited_once_with(ORG_ID, STREAM_KEY)
    page.context.new_cdp_session.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_skips_unchanged_frame(streaming_temp_dir: Path, fake_storage: AsyncMock) -> None:
    frame = b"\xff\xd8same"
    session = _make_cdp_session([frame, frame, frame])
    page = _make_page_with_session(session)
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(page),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
    )

    await _drive_publish_once(pub)
    await _drive_publish_once(pub)
    await _drive_publish_once(pub)

    # captureScreenshot is called every tick, but only the first upload happens.
    assert session.send.await_count == 3
    fake_storage.assert_awaited_once()


@pytest.mark.asyncio
async def test_publishes_again_when_frame_changes(streaming_temp_dir: Path, fake_storage: AsyncMock) -> None:
    session = _make_cdp_session([b"frame-a", b"frame-b"])
    page = _make_page_with_session(session)
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(page),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
    )

    await _drive_publish_once(pub)
    await _drive_publish_once(pub)

    assert fake_storage.await_count == 2
    assert _stream_path(streaming_temp_dir).read_bytes() == b"frame-b"


@pytest.mark.asyncio
async def test_reattaches_on_page_switch(streaming_temp_dir: Path, fake_storage: AsyncMock) -> None:
    session_a = _make_cdp_session([b"frame-a"])
    page_a = _make_page_with_session(session_a)
    session_b = _make_cdp_session([b"frame-b"])
    page_b = _make_page_with_session(session_b)

    current = {"page": page_a}

    async def _get_page() -> MagicMock:
        return current["page"]

    state = SimpleNamespace(get_working_page=_get_page)
    pub = CDPFramePublisher(browser_state=state, stream_key=STREAM_KEY, organization_id=ORG_ID)

    await _drive_publish_once(pub)
    current["page"] = page_b
    await _drive_publish_once(pub)

    page_a.context.new_cdp_session.assert_awaited_once_with(page_a)
    page_b.context.new_cdp_session.assert_awaited_once_with(page_b)
    # Old session must be detached when the page changes.
    session_a.detach.assert_awaited()
    assert _stream_path(streaming_temp_dir).read_bytes() == b"frame-b"


@pytest.mark.asyncio
async def test_no_working_page_is_noop(streaming_temp_dir: Path, fake_storage: AsyncMock) -> None:
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(None),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
    )

    await _drive_publish_once(pub)

    assert not _stream_path(streaming_temp_dir).exists()
    fake_storage.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_screenshot_failure_is_non_fatal(streaming_temp_dir: Path, fake_storage: AsyncMock) -> None:
    session = MagicMock()
    session.send = AsyncMock(side_effect=RuntimeError("CDP boom"))
    session.detach = AsyncMock()
    page = _make_page_with_session(session)
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(page),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
    )

    # Must not raise.
    await _drive_publish_once(pub)

    assert not _stream_path(streaming_temp_dir).exists()
    fake_storage.assert_not_awaited()
    # Failed session should be detached so the next tick reattaches.
    session.detach.assert_awaited()


@pytest.mark.parametrize(
    "teardown_exc",
    [
        TargetClosedError("Target page, context or browser has been closed"),
        TargetClosedError("boom"),
        RuntimeError("Connection closed while reading from the driver"),
    ],
    ids=["target_closed_type", "target_closed_type_noncanonical_message", "driver_pipe_message"],
)
@pytest.mark.asyncio
async def test_cdp_session_open_teardown_error_is_debug_and_retried(
    teardown_exc: Exception, fake_storage: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = MagicMock()
    page.context = MagicMock()
    page.context.new_cdp_session = AsyncMock(side_effect=teardown_exc)
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(page),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
    )
    fake_log = SimpleNamespace(debug=MagicMock(), warning=MagicMock())
    monkeypatch.setattr(publisher_module, "LOG", fake_log)

    await _drive_publish_once(pub)
    await _drive_publish_once(pub)

    # A known teardown race is expected and benign: kept at debug, never warned, retried.
    assert page.context.new_cdp_session.await_count == 2
    assert fake_log.debug.call_count == 2
    fake_log.debug.assert_called_with(
        "Could not open CDP session for frame publishing",
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
        exc_info=True,
    )
    fake_log.warning.assert_not_called()
    fake_storage.assert_not_awaited()


@pytest.mark.asyncio
async def test_cdp_session_open_unexpected_error_warns_once_then_dedupes(
    fake_storage: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = MagicMock()
    page.context = MagicMock()
    page.context.new_cdp_session = AsyncMock(side_effect=RuntimeError("CDP proxy handshake incompatible"))
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(page),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
    )
    fake_log = SimpleNamespace(debug=MagicMock(), warning=MagicMock())
    monkeypatch.setattr(publisher_module, "LOG", fake_log)

    await _drive_publish_once(pub)
    await _drive_publish_once(pub)
    await _drive_publish_once(pub)

    # An unexpected, non-teardown attachment failure while the page may still be live must
    # stay visible so persistently blank frames are explainable -- but only the first
    # occurrence warns; subsequent failures in the same streak drop to debug to avoid a flood.
    assert page.context.new_cdp_session.await_count == 3
    fake_log.warning.assert_called_once_with(
        "Could not open CDP session for frame publishing",
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
        exc_info=True,
    )
    assert fake_log.debug.call_count == 2
    fake_storage.assert_not_awaited()


@pytest.mark.asyncio
async def test_cdp_session_open_warn_gate_resets_after_successful_attach(
    streaming_temp_dir: Path, fake_storage: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy attach must re-arm the warning so a *new* unhealthy streak warns again,
    proving the first-occurrence gate silences repeats without hiding fresh failures."""
    bad_page_first = MagicMock()
    bad_page_first.context = MagicMock()
    bad_page_first.context.new_cdp_session = AsyncMock(side_effect=RuntimeError("CDP proxy handshake incompatible"))
    good_page = _make_page_with_session(_make_cdp_session([b"recovered-frame"]))
    bad_page_second = MagicMock()
    bad_page_second.context = MagicMock()
    bad_page_second.context.new_cdp_session = AsyncMock(side_effect=RuntimeError("CDP proxy handshake incompatible"))

    current = {"page": bad_page_first}

    async def _get_page() -> MagicMock:
        return current["page"]

    state = SimpleNamespace(get_working_page=_get_page, is_connected=lambda: True)
    pub = CDPFramePublisher(browser_state=state, stream_key=STREAM_KEY, organization_id=ORG_ID)
    fake_log = SimpleNamespace(debug=MagicMock(), warning=MagicMock(), info=MagicMock())
    monkeypatch.setattr(publisher_module, "LOG", fake_log)

    await _drive_publish_once(pub)  # unexpected failure -> warn (1)
    current["page"] = good_page
    await _drive_publish_once(pub)  # healthy attach -> gate re-arms
    current["page"] = bad_page_second
    await _drive_publish_once(pub)  # new unexpected streak -> warn (2)

    assert fake_log.warning.call_count == 2


@pytest.mark.asyncio
async def test_unexpected_publish_iteration_failure_remains_warning() -> None:
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(None),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
    )

    async def _unexpected_failure() -> None:
        pub._stopped.set()
        raise RuntimeError("unexpected publisher failure")

    pub._publish_one_frame = AsyncMock(side_effect=_unexpected_failure)  # type: ignore[method-assign]

    with capture_logs() as logs:
        await pub._run()

    matching_logs = [log for log in logs if log.get("event") == "CDP frame publish iteration failed"]
    assert len(matching_logs) == 1
    assert matching_logs[0].get("log_level") == "warning"
    assert matching_logs[0].get("stream_key") == STREAM_KEY
    assert matching_logs[0].get("organization_id") == ORG_ID


@pytest.mark.asyncio
async def test_save_streaming_file_failure_is_non_fatal(
    streaming_temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_mock = AsyncMock(side_effect=RuntimeError("remote-storage boom"))
    fake_app = SimpleNamespace(STORAGE=SimpleNamespace(save_streaming_file=save_mock))
    monkeypatch.setattr(publisher_module, "app", fake_app)

    session = _make_cdp_session([b"frame-x"])
    page = _make_page_with_session(session)
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(page),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
    )

    # Must not raise even though the remote upload raises.
    await _drive_publish_once(pub)

    # Local file is still written, so local-disk storage can serve it.
    assert _stream_path(streaming_temp_dir).read_bytes() == b"frame-x"
    save_mock.assert_awaited_once_with(ORG_ID, STREAM_KEY)


@pytest.mark.asyncio
async def test_invalid_base64_is_silently_skipped(streaming_temp_dir: Path, fake_storage: AsyncMock) -> None:
    session = MagicMock()
    # Returning a payload without "data" should not crash; b64 decode error
    # on garbage should also not crash.
    session.send = AsyncMock(return_value={"data": "$$$ not base64 $$$"})
    session.detach = AsyncMock()
    page = _make_page_with_session(session)
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(page),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
    )

    await _drive_publish_once(pub)

    fake_storage.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_is_idempotent(streaming_temp_dir: Path, fake_storage: AsyncMock) -> None:
    session = _make_cdp_session([b"x"])
    page = _make_page_with_session(session)
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(page),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
        capture_interval_seconds=10.0,
    )

    await pub.start()
    task_after_first = pub._task
    await pub.start()
    assert pub._task is task_after_first
    await pub.stop()


@pytest.mark.asyncio
async def test_stop_cancels_task_and_detaches_session(streaming_temp_dir: Path, fake_storage: AsyncMock) -> None:
    session = _make_cdp_session([b"x"])
    page = _make_page_with_session(session)
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(page),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
        capture_interval_seconds=10.0,
    )

    await pub.start()
    # Yield so the loop has a chance to publish at least one frame.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await pub.stop()

    assert pub._task is None
    assert pub._cdp_session is None
    assert pub._attached_page is None
    # Stop is idempotent.
    await pub.stop()


@pytest.mark.asyncio
async def test_run_self_terminates_when_browser_state_disconnects(
    streaming_temp_dir: Path, fake_storage: AsyncMock
) -> None:
    """``close_browser_on_completion=False`` returns the browser to the pool
    without firing on-close. The publisher must still self-terminate when its
    underlying ``BrowserState`` reports disconnected so the loop does not
    spin forever after the run ends."""
    session = _make_cdp_session([b"png-frame"])
    page = _make_page_with_session(session)
    connected = {"flag": True}
    browser_state = SimpleNamespace(
        get_working_page=AsyncMock(return_value=page),
        is_connected=lambda: connected["flag"],
    )
    pub = CDPFramePublisher(
        browser_state=browser_state,
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
        capture_interval_seconds=0.1,
    )

    await pub.start()
    # Let at least one tick run while connected.
    await asyncio.sleep(0.15)
    assert pub.is_running

    connected["flag"] = False
    # Next iteration should observe disconnect and exit the loop.
    await asyncio.sleep(0.25)
    assert not pub.is_running
    assert pub._stopped.is_set()

    # ``stop()`` is still safe to call after self-termination.
    await pub.stop()


@pytest.mark.asyncio
async def test_temp_dir_oserror_is_non_fatal(
    streaming_temp_dir: Path, fake_storage: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a temp dir that cannot be created by patching mkdir.
    real_mkdir = Path.mkdir

    def _boom(self: Path, *args: object, **kwargs: object) -> None:
        if str(self).endswith(ORG_ID):
            raise OSError("disk gone")
        return real_mkdir(self, *args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(Path, "mkdir", _boom)

    session = _make_cdp_session([b"frame"])
    page = _make_page_with_session(session)
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(page),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
    )

    await _drive_publish_once(pub)

    fake_storage.assert_not_awaited()
    # Org directory was never successfully created, so the stream file shouldn't exist.
    assert not (streaming_temp_dir / ORG_ID).exists() or not _stream_path(streaming_temp_dir).exists()


@pytest.mark.asyncio
async def test_retries_after_upload_failure_on_same_bytes(
    streaming_temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upload failure on tick N must not silently dedupe the same frame on tick N+1."""
    call_state = {"n": 0}

    async def _flaky_save(org: str, key: str) -> None:
        call_state["n"] += 1
        if call_state["n"] == 1:
            raise RuntimeError("transient remote-storage failure")
        # Subsequent calls succeed.

    fake_app = SimpleNamespace(STORAGE=SimpleNamespace(save_streaming_file=AsyncMock(side_effect=_flaky_save)))
    monkeypatch.setattr(publisher_module, "app", fake_app)

    same_frame = b"jpeg-same-bytes"
    session = _make_cdp_session([same_frame, same_frame, same_frame])
    page = _make_page_with_session(session)
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(page),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
    )

    await _drive_publish_once(pub)  # save fails; do not mark published
    await _drive_publish_once(pub)  # retry: save succeeds; mark published
    await _drive_publish_once(pub)  # identical bytes already marked => skip

    assert call_state["n"] == 2  # exactly the retry attempted
    assert _stream_path(streaming_temp_dir).read_bytes() == same_frame


@pytest.mark.asyncio
async def test_atomic_write_leaves_no_tmp_files(streaming_temp_dir: Path, fake_storage: AsyncMock) -> None:
    session = _make_cdp_session([b"frame-a", b"frame-b"])
    page = _make_page_with_session(session)
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(page),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
    )

    await _drive_publish_once(pub)
    await _drive_publish_once(pub)

    org_dir = streaming_temp_dir / ORG_ID
    leftovers = [p for p in os.listdir(org_dir) if p != STREAM_KEY]
    assert leftovers == []


@pytest.mark.asyncio
async def test_capture_uses_png_format(streaming_temp_dir: Path, fake_storage: AsyncMock) -> None:
    """The frontend WebSocket consumes screenshots as PNG; capture must request PNG.

    Frame bytes here start with the PNG magic (``\\x89PNG\\r\\n\\x1a\\n``)
    so the round-trip assertion below also confirms PNG bytes hit disk.
    """
    png_bytes = b"\x89PNG\r\n\x1a\nfake-png-payload"
    session = _make_cdp_session([png_bytes])
    page = _make_page_with_session(session)
    pub = CDPFramePublisher(
        browser_state=_make_browser_state(page),
        stream_key=STREAM_KEY,
        organization_id=ORG_ID,
    )

    await _drive_publish_once(pub)

    screenshot_calls = session._screenshot_calls
    assert len(screenshot_calls) == 1
    params = screenshot_calls[0]
    assert params.get("format") == "png"
    # Quality is JPEG-only; PNG capture must omit it.
    assert "quality" not in params

    written = _stream_path(streaming_temp_dir).read_bytes()
    assert written.startswith(b"\x89PNG")


def test_write_frame_atomically_writes_bytes_via_tempfile(tmp_path: Path) -> None:
    """The sync helper used by ``_write_frame`` writes via tempfile+replace and
    leaves no ``.tmp`` siblings behind on success."""
    target_dir = tmp_path / "o_org_x"
    _write_frame_atomically(target_dir, "wr_x.png", b"payload-bytes")

    assert (target_dir / "wr_x.png").read_bytes() == b"payload-bytes"
    # No leftover temp files in the org dir.
    leftovers = [p for p in target_dir.iterdir() if p.name.startswith(".") and p.suffix == ".tmp"]
    assert leftovers == []


def test_write_frame_atomically_cleans_tempfile_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``os.replace`` fails, the tempfile must be unlinked so a failing
    publisher cannot accumulate ``.tmp`` debris in the streaming dir."""
    target_dir = tmp_path / "o_org_x"

    def _boom(src: str, dst: str) -> None:  # type: ignore[unused-argument]
        raise OSError("replace failed")

    monkeypatch.setattr(publisher_module.os, "replace", _boom)

    with pytest.raises(OSError, match="replace failed"):
        _write_frame_atomically(target_dir, "wr_x.png", b"payload-bytes")

    # Target was never written, and the temp file was cleaned up.
    assert not (target_dir / "wr_x.png").exists()
    leftovers = [p for p in target_dir.iterdir() if p.name.startswith(".") and p.suffix == ".tmp"]
    assert leftovers == []
