"""Arm-seam tests (SKY-15466 two-phase capture). ``check_and_fix_state`` arms the recorder exactly once, only
after a working page exists and the optional initial navigate completes — wrapping ONLY the navigate in
try/finally so a permanent nav failure still records the error page and propagates. A failed arm is logged.
"""

import asyncio
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from skyvern.webeye import real_browser_state
from skyvern.webeye.display_recorder import DisplayRecorder
from skyvern.webeye.real_browser_state import RealBrowserState


def _rec(arm_result: bool = True) -> MagicMock:
    r = MagicMock(spec=DisplayRecorder)  # spec makes isinstance(r, DisplayRecorder) True
    r.arm_capture.return_value = arm_result
    r.owner_id = "wr_x"
    return r


def _state(recorder: object, working_page: object) -> RealBrowserState:
    st = RealBrowserState.__new__(RealBrowserState)  # bypass __init__
    st._disconnect_listener_contexts = weakref.WeakSet()  # constructor invariants the browser_context setter reads
    st._run_released = False
    st.browser_context = MagicMock()  # non-None -> creation branch skipped
    st.browser_context.pages = []
    st.external_browser = False
    st._connection_loss = None
    st.browser_artifacts = SimpleNamespace(_display_recorder=recorder, remote_browser_session_id=None)
    st.get_working_page = AsyncMock(return_value=working_page)
    st.set_working_page = AsyncMock()
    st._close_all_other_pages = AsyncMock()
    st.list_valid_pages = AsyncMock(return_value=[])
    return st


@pytest.mark.asyncio
async def test_arm_after_working_page_when_no_navigation() -> None:
    rec = _rec()
    page = MagicMock()
    page.url = "http://127.0.0.1/dynamic.html"
    st = _state(rec, working_page=page)  # page already exists -> creation block skipped
    await st.check_and_fix_state(url=None)
    rec.arm_capture.assert_called_once()


@pytest.mark.parametrize("nav_raises", [False, True], ids=["nav_ok", "nav_fails"])
@pytest.mark.asyncio
async def test_arm_strictly_after_navigation(nav_raises: bool) -> None:
    # arm runs in the navigate finally, so the order is ["nav", "arm"] whether the first navigation succeeds or
    # raises permanently; the failing run still records the error page and the original exception propagates.
    events: list[str] = []
    rec = _rec()
    rec.arm_capture.side_effect = lambda: (events.append("arm"), True)[1]
    newpage = MagicMock()
    newpage.url = "about:blank"
    st = _state(rec, working_page=None)  # triggers page creation + navigate
    st.browser_context.new_page = AsyncMock(return_value=newpage)

    def _nav(**kwargs: object) -> None:
        events.append("nav")
        if nav_raises:
            raise RuntimeError("dead url")

    st.navigate_to_url = AsyncMock(side_effect=_nav)
    if nav_raises:
        with pytest.raises(RuntimeError, match="dead url"):  # original exception preserved
            await st.check_and_fix_state(url="http://127.0.0.1/dynamic.html")
    else:
        await st.check_and_fix_state(url="http://127.0.0.1/dynamic.html")
    assert events == ["nav", "arm"]  # arm strictly AFTER navigation — fails if arm moves before navigate


@pytest.mark.asyncio
async def test_arm_failure_is_logged_and_does_not_raise() -> None:
    rec = _rec(arm_result=False)
    page = MagicMock()
    page.url = "http://127.0.0.1/dynamic.html"
    st = _state(rec, working_page=page)
    with capture_logs() as logs:
        await st.check_and_fix_state(url=None)
    assert any(log["log_level"] == "warning" and log.get("display_recording_owner_id") == rec.owner_id for log in logs)


@pytest.mark.asyncio
async def test_no_recorder_means_no_arm_and_no_error() -> None:
    page = MagicMock()
    page.url = "http://127.0.0.1/dynamic.html"
    st = _state(recorder=None, working_page=page)
    await st.check_and_fix_state(url=None)  # must not raise


@pytest.mark.asyncio
async def test_page_creation_failure_does_not_arm() -> None:
    rec = _rec()
    st = _state(rec, working_page=None)
    st.browser_context.new_page = AsyncMock(side_effect=RuntimeError("no page"))
    with pytest.raises(RuntimeError, match="no page"):
        await st.check_and_fix_state(url="http://127.0.0.1/dynamic.html")
    rec.arm_capture.assert_not_called()  # no meaningful page yet -> no arm


def _ready_state(monkeypatch: pytest.MonkeyPatch, working_page: object) -> tuple[RealBrowserState, list[str]]:
    events: list[str] = []
    agent = MagicMock()
    agent.note_working_page_ready.side_effect = lambda context, page: events.append("ready")
    monkeypatch.setattr(real_browser_state.app, "AGENT_FUNCTION", agent)
    st = _state(_rec(), working_page=working_page)
    st._close_all_other_pages = AsyncMock(side_effect=lambda **_: events.append("cleanup"))
    return st, events


@pytest.mark.parametrize("nav_raises", [False, True], ids=["nav_ok", "nav_fails"])
@pytest.mark.asyncio
async def test_the_working_page_is_reported_ready_after_cleanup_and_the_first_navigation(
    monkeypatch: pytest.MonkeyPatch, nav_raises: bool
) -> None:
    newpage = MagicMock()
    newpage.url = "about:blank"
    st, events = _ready_state(monkeypatch, working_page=None)
    st.browser_context.new_page = AsyncMock(return_value=newpage)

    def _nav(**kwargs: object) -> None:
        events.append("nav")
        if nav_raises:
            raise RuntimeError("dead url")

    st.navigate_to_url = AsyncMock(side_effect=_nav)
    if nav_raises:
        with pytest.raises(RuntimeError, match="dead url"):
            await st.check_and_fix_state(url="http://127.0.0.1/dynamic.html")
    else:
        await st.check_and_fix_state(url="http://127.0.0.1/dynamic.html")
    assert events == ["cleanup", "nav", "ready"]
    real_browser_state.app.AGENT_FUNCTION.note_working_page_ready.assert_called_once_with(st.browser_context, newpage)


@pytest.mark.parametrize("existing", [False, True], ids=["new_page_without_navigation", "existing_working_page"])
@pytest.mark.asyncio
async def test_a_working_page_without_a_first_navigation_is_reported_ready(
    monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    page = MagicMock()
    page.url = "http://127.0.0.1/dynamic.html"
    st, events = _ready_state(monkeypatch, working_page=page if existing else None)
    st.browser_context.new_page = AsyncMock(return_value=page)
    await st.check_and_fix_state(url=None)
    assert events == (["ready"] if existing else ["cleanup", "ready"])
    real_browser_state.app.AGENT_FUNCTION.note_working_page_ready.assert_called_once_with(st.browser_context, page)


@pytest.mark.asyncio
async def test_a_cancelled_first_navigation_reports_no_ready_page(monkeypatch: pytest.MonkeyPatch) -> None:
    newpage = MagicMock()
    newpage.url = "about:blank"
    st, _events = _ready_state(monkeypatch, working_page=None)
    st.browser_context.new_page = AsyncMock(return_value=newpage)
    st.navigate_to_url = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await st.check_and_fix_state(url="http://127.0.0.1/dynamic.html")
    real_browser_state.app.AGENT_FUNCTION.note_working_page_ready.assert_not_called()


@pytest.mark.parametrize("nav_raises", [False, True], ids=["nav_ok", "nav_fails"])
@pytest.mark.asyncio
async def test_a_failing_ready_hook_never_changes_the_run(monkeypatch: pytest.MonkeyPatch, nav_raises: bool) -> None:
    newpage = MagicMock()
    newpage.url = "about:blank"
    st, _events = _ready_state(monkeypatch, working_page=None)
    real_browser_state.app.AGENT_FUNCTION.note_working_page_ready.side_effect = ValueError("hook bug")
    st.browser_context.new_page = AsyncMock(return_value=newpage)
    st.navigate_to_url = AsyncMock(side_effect=RuntimeError("dead url") if nav_raises else None)
    if nav_raises:
        with pytest.raises(RuntimeError, match="dead url"):
            await st.check_and_fix_state(url="http://127.0.0.1/dynamic.html")
    else:
        await st.check_and_fix_state(url="http://127.0.0.1/dynamic.html")
    real_browser_state.app.AGENT_FUNCTION.note_working_page_ready.assert_called_once()
