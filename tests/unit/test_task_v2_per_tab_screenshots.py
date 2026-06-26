"""task_v2 completion captures one SCREENSHOT_LLM artifact per open tab for the trajectory judge."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.services import task_v2_service
from skyvern.services.task_v2_service import _persist_completion_tab_screenshots


def _fake_thought() -> SimpleNamespace:
    return SimpleNamespace(observer_thought_id="thgt_test")


def _fake_page(url: str = "https://example.com") -> AsyncMock:
    page = AsyncMock()
    page.url = url
    return page


@pytest.fixture
def artifact_manager(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    manager = SimpleNamespace(create_thought_artifact=AsyncMock(return_value="art_id"))
    monkeypatch.setattr(app, "ARTIFACT_MANAGER", manager)
    return manager.create_thought_artifact


@pytest.fixture(autouse=True)
def stub_screenshot(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    take = AsyncMock(return_value=[b"png-bytes"])
    monkeypatch.setattr(task_v2_service.SkyvernFrame, "take_split_screenshots", take)
    return take


@pytest.mark.asyncio
async def test_captures_one_artifact_per_open_tab(artifact_manager: AsyncMock) -> None:
    pages = [_fake_page() for _ in range(3)]
    browser_state = SimpleNamespace(list_valid_pages=AsyncMock(return_value=pages))
    thought = _fake_thought()

    captured = await _persist_completion_tab_screenshots(browser_state, thought)

    assert captured == 3
    assert artifact_manager.await_count == 3
    for call in artifact_manager.await_args_list:
        # The judge grades artifacts on this exact completion thought — the load-bearing wiring.
        assert call.kwargs["thought"] is thought
        assert call.kwargs["artifact_type"] == ArtifactType.SCREENSHOT_LLM
        assert call.kwargs["data"] == b"png-bytes"


@pytest.mark.asyncio
async def test_enumerates_without_closing_tabs(artifact_manager: AsyncMock) -> None:
    # The whole point of the feature is to prove tabs are still open, so enumeration must use
    # max_pages=0 to avoid list_valid_pages' close-oldest behavior.
    browser_state = SimpleNamespace(list_valid_pages=AsyncMock(return_value=[_fake_page()]))

    await _persist_completion_tab_screenshots(browser_state, _fake_thought())

    browser_state.list_valid_pages.assert_awaited_once_with(max_pages=0)


@pytest.mark.asyncio
async def test_respects_max_cap(artifact_manager: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_v2_service.settings, "MAX_COMPLETION_TAB_SCREENSHOTS_PER_TASK_V2", 2)
    pages = [_fake_page() for _ in range(5)]
    browser_state = SimpleNamespace(list_valid_pages=AsyncMock(return_value=pages))

    captured = await _persist_completion_tab_screenshots(browser_state, _fake_thought())

    assert captured == 2
    assert artifact_manager.await_count == 2


@pytest.mark.asyncio
async def test_enumeration_failure_returns_zero(artifact_manager: AsyncMock) -> None:
    browser_state = SimpleNamespace(list_valid_pages=AsyncMock(side_effect=RuntimeError("boom")))

    captured = await _persist_completion_tab_screenshots(browser_state, _fake_thought())

    assert captured == 0
    artifact_manager.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_tab_failure_does_not_abort_others(
    artifact_manager: AsyncMock, stub_screenshot: AsyncMock
) -> None:
    pages = [_fake_page() for _ in range(3)]
    browser_state = SimpleNamespace(list_valid_pages=AsyncMock(return_value=pages))
    stub_screenshot.side_effect = [[b"a"], RuntimeError("screenshot fail"), [b"c"]]

    captured = await _persist_completion_tab_screenshots(browser_state, _fake_thought())

    assert captured == 2
    assert artifact_manager.await_count == 2


@pytest.mark.asyncio
async def test_bring_to_front_failure_still_captures(artifact_manager: AsyncMock) -> None:
    pages = [_fake_page(), _fake_page()]
    pages[0].bring_to_front = AsyncMock(side_effect=RuntimeError("no front"))
    browser_state = SimpleNamespace(list_valid_pages=AsyncMock(return_value=pages))

    captured = await _persist_completion_tab_screenshots(browser_state, _fake_thought())

    # A tab that cannot be fronted is still screenshottable in Chromium, so nothing is dropped.
    assert captured == 2
    assert artifact_manager.await_count == 2


@pytest.mark.asyncio
async def test_single_tab_is_skipped(artifact_manager: AsyncMock) -> None:
    # The lone active tab is already persisted by the completion check; re-capturing it is waste.
    browser_state = SimpleNamespace(list_valid_pages=AsyncMock(return_value=[_fake_page()]))

    captured = await _persist_completion_tab_screenshots(browser_state, _fake_thought())

    assert captured == 0
    artifact_manager.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_failure_does_not_propagate(artifact_manager: AsyncMock) -> None:
    # Best-effort: a transient artifact/DB write failure must not flip an already-successful run.
    artifact_manager.side_effect = RuntimeError("db down")
    pages = [_fake_page(), _fake_page()]
    browser_state = SimpleNamespace(list_valid_pages=AsyncMock(return_value=pages))

    captured = await _persist_completion_tab_screenshots(browser_state, _fake_thought())

    assert captured == 0
    assert artifact_manager.await_count == 2


@pytest.mark.asyncio
async def test_slow_tab_times_out_and_does_not_block_others(
    artifact_manager: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_v2_service.settings, "BROWSER_SCREENSHOT_TIMEOUT_MS", 50)
    slow_page, fast_page = _fake_page("https://slow"), _fake_page("https://fast")

    async def fake_take(page: object, scroll: bool = False) -> list[bytes]:
        if getattr(page, "url", "") == "https://slow":
            await asyncio.sleep(5)
        return [b"png-bytes"]

    monkeypatch.setattr(task_v2_service.SkyvernFrame, "take_split_screenshots", fake_take)
    browser_state = SimpleNamespace(list_valid_pages=AsyncMock(return_value=[slow_page, fast_page]))

    captured = await _persist_completion_tab_screenshots(browser_state, _fake_thought())

    # The hung tab is abandoned at the per-tab deadline; the healthy tab is still captured.
    assert captured == 1
    assert artifact_manager.await_count == 1
