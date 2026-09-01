"""``RealBrowserState.close`` runs registered on-close callbacks first.

Lets ``_start_frame_publisher`` register ``publisher.stop`` on the browser
state so any caller of ``close()`` stops the publisher implicitly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.browser_artifacts import BrowserArtifacts, VideoArtifact
from skyvern.webeye.display_recorder import DisplayRecorder
from skyvern.webeye.real_browser_state import RealBrowserState


def _bare_state() -> RealBrowserState:
    """RealBrowserState with no live browser context; ``close`` should still
    invoke registered callbacks."""
    # ``pw`` is set to a no-op stub so ``close`` can short-circuit out of the
    # playwright stop branch without raising.
    pw_stub: Any = type("_PW", (), {"stop": AsyncMock()})()
    return RealBrowserState(pw=pw_stub, browser_context=None)


def _recorder(owner_id: str = "owner") -> DisplayRecorder:
    return DisplayRecorder(
        display=":99",
        owner_id=owner_id,
        process=MagicMock(),
        lock_fd=-1,
        video_artifact=VideoArtifact(video_path="recording.webm"),
    )


@pytest.mark.asyncio
async def test_close_runs_registered_callbacks_in_order() -> None:
    state = _bare_state()
    calls: list[str] = []

    async def cb_one() -> None:
        calls.append("one")

    async def cb_two() -> None:
        calls.append("two")

    state.add_on_close(cb_one)
    state.add_on_close(cb_two)

    await state.close()

    assert calls == ["one", "two"]


@pytest.mark.asyncio
async def test_close_callbacks_are_one_shot() -> None:
    """Subsequent ``close()`` calls must not re-fire previous callbacks."""
    state = _bare_state()
    invocations = 0

    async def cb() -> None:
        nonlocal invocations
        invocations += 1

    state.add_on_close(cb)
    await state.close()
    await state.close()
    assert invocations == 1


@pytest.mark.asyncio
async def test_close_swallows_callback_errors() -> None:
    """A misbehaving on-close callback must not block the rest of close."""
    state = _bare_state()
    later_ran = False

    async def cb_explodes() -> None:
        raise RuntimeError("boom")

    async def cb_later() -> None:
        nonlocal later_ran
        later_ran = True

    state.add_on_close(cb_explodes)
    state.add_on_close(cb_later)

    # Must not raise — close() is the universal teardown path.
    await state.close()
    assert later_ran is True


def _state_with_recorder(events: list[str]) -> RealBrowserState:
    artifacts = BrowserArtifacts()
    artifacts._display_recorder = _recorder()
    state = RealBrowserState(pw=MagicMock(), browser_context=MagicMock(), browser_artifacts=artifacts)

    async def cleanup() -> None:
        events.append("provider")

    state._run_browser_cleanup_bounded = cleanup  # type: ignore[method-assign]
    state._stop_driver_bounded = AsyncMock()  # type: ignore[method-assign]
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_result", [True, False])
async def test_display_recorder_outcome_does_not_change_close_result(stop_result: bool) -> None:
    """B4: ``close()``'s return reports only the browser-context teardown (which gates profile
    persistence). A recorder that exits non-zero mid-run — ``release_display_recorder`` returning False
    — must NOT flip that result and suppress an otherwise-clean run's profile write-back."""
    events: list[str] = []
    state = _state_with_recorder(events)

    async def teardown() -> None:
        events.append("context")

    state._teardown_context = teardown  # type: ignore[method-assign]

    async def stop(_: DisplayRecorder) -> bool:
        events.append("recorder")
        return stop_result

    with patch("skyvern.webeye.real_browser_state.release_display_recorder", side_effect=stop):
        assert await state.close() is True

    assert events == ["context", "recorder", "provider"]


@pytest.mark.asyncio
async def test_context_teardown_failure_still_finalizes_display_recorder() -> None:
    """B4: a Playwright context-teardown failure must still stop/finalize the WebM (so upload prep /
    MP4 conversion can run) even though ``close()`` reports the teardown failure for profile gating."""
    events: list[str] = []
    state = _state_with_recorder(events)

    async def teardown() -> None:
        events.append("context")
        raise RuntimeError("context close blew up")

    state._teardown_context = teardown  # type: ignore[method-assign]

    async def stop(_: DisplayRecorder) -> bool:
        events.append("recorder")
        return True

    with patch("skyvern.webeye.real_browser_state.release_display_recorder", side_effect=stop):
        assert await state.close() is False

    assert "recorder" in events, "the recorder must still be finalized after a context-teardown failure"
    assert events == ["context", "recorder", "provider"]


@pytest.mark.asyncio
async def test_close_false_leaves_display_recorder_running() -> None:
    artifacts = BrowserArtifacts()
    artifacts._display_recorder = _recorder()
    state = RealBrowserState(pw=MagicMock(), browser_context=MagicMock(), browser_artifacts=artifacts)
    state._stop_driver_bounded = AsyncMock()  # type: ignore[method-assign]

    with patch("skyvern.webeye.real_browser_state.release_display_recorder", new_callable=AsyncMock) as stop:
        assert await state.close(close_browser_on_completion=False, release_driver=False) is False

    stop.assert_not_awaited()
