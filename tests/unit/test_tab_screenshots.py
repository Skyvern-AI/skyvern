"""End-state per-tab screenshot capture via webeye.utils.page.capture_open_tab_screenshots.

The helper persists one screenshot per open tab, so trajectory judges can see every page the run
reached, not just the working one. The caller's persist callback owns the artifact sink.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.webeye.utils.page import SkyvernFrame, capture_open_tab_screenshots


def _fake_tab() -> AsyncMock:
    tab = AsyncMock()
    tab.bring_to_front = AsyncMock()
    return tab


def _fake_browser_state(pages: list[AsyncMock]) -> SimpleNamespace:
    return SimpleNamespace(list_valid_pages=AsyncMock(return_value=pages), engine_selection=None)


@pytest.fixture(autouse=True)
def stub_screenshot(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    take = AsyncMock(return_value=[b"png-bytes"])
    monkeypatch.setattr(SkyvernFrame, "take_split_screenshots", take)
    return take


@pytest.mark.asyncio
async def test_persists_one_frame_per_open_tab() -> None:
    persist = AsyncMock()
    browser_state = _fake_browser_state([_fake_tab() for _ in range(3)])

    captured = await capture_open_tab_screenshots(browser_state, persist=persist)

    assert captured == 3
    assert persist.await_count == 3
    assert all(call.args[0] == b"png-bytes" for call in persist.await_args_list)
    # max_pages=0 avoids list_valid_pages' close-oldest behavior — never close the tabs we capture.
    browser_state.list_valid_pages.assert_awaited_once_with(max_pages=0)


@pytest.mark.asyncio
async def test_captures_single_tab_by_default() -> None:
    persist = AsyncMock()
    browser_state = _fake_browser_state([_fake_tab()])

    assert await capture_open_tab_screenshots(browser_state, persist=persist) == 1
    assert persist.await_count == 1


@pytest.mark.asyncio
async def test_skip_single_tab_returns_zero() -> None:
    # task_v2 passes skip_single_tab=True: the lone active tab is already captured at completion.
    persist = AsyncMock()
    browser_state = _fake_browser_state([_fake_tab()])

    assert await capture_open_tab_screenshots(browser_state, persist=persist, skip_single_tab=True) == 0
    assert persist.await_count == 0


@pytest.mark.asyncio
async def test_no_open_tabs_returns_zero() -> None:
    persist = AsyncMock()
    browser_state = _fake_browser_state([])

    assert await capture_open_tab_screenshots(browser_state, persist=persist) == 0
    assert persist.await_count == 0


@pytest.mark.asyncio
async def test_best_effort_skips_failing_tab(stub_screenshot: AsyncMock) -> None:
    stub_screenshot.side_effect = [[b"a"], RuntimeError("screenshot fail"), [b"c"]]
    persist = AsyncMock()
    browser_state = _fake_browser_state([_fake_tab() for _ in range(3)])

    assert await capture_open_tab_screenshots(browser_state, persist=persist) == 2
    assert persist.await_count == 2
