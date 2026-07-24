"""``scrape_website`` timeout classification must follow the per-run selected browser engine's
timeout family, not a hard-coded stock-Playwright ``isinstance`` — so a run pinned to a different
engine still routes its native page-analysis timeouts to PAGE_LOAD_TIMEOUT, while a foreign
exception (including another engine's timeout) is not misclassified as a timeout.

These stay driver-agnostic: they pin fake engine selections and mock ``scrape_web_unsafe`` so they
hold on an image shipping only stock Playwright.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.exceptions import ScrapingFailed, SkyvernPageAnalysisTimeout
from skyvern.webeye.browser_engine import BrowserEngineMetadata, BrowserEngineSelection
from skyvern.webeye.scraper import scraper

_TIMEOUT_REASON_MARKER = "page-analysis timeout"


class _EngineAError(Exception):
    pass


class _EngineATimeout(_EngineAError):
    pass


async def _never_start():  # pragma: no cover - never awaited in these tests
    raise AssertionError("start_driver must not be called")


def _selection(name: str, error_type: type[BaseException], timeout_type: type[BaseException]) -> BrowserEngineSelection:
    return BrowserEngineSelection(
        name=name,
        start_driver=_never_start,
        error_type=error_type,
        timeout_error_type=timeout_type,
        metadata=BrowserEngineMetadata(name=name, version="0.0.0"),
        selection_reason="test",
    )


def _browser_state(selection: BrowserEngineSelection | None) -> SimpleNamespace:
    return SimpleNamespace(engine_selection=selection, get_working_page=AsyncMock(return_value=None))


async def _run_scrape_and_capture(browser_state: SimpleNamespace, error: BaseException) -> ScrapingFailed:
    with patch.object(scraper, "scrape_web_unsafe", AsyncMock(side_effect=error)):
        with pytest.raises(ScrapingFailed) as exc_info:
            await scraper.scrape_website(
                browser_state,  # type: ignore[arg-type]
                "https://example.com/path?token=secret",
                cleanup_element_tree=AsyncMock(),
                max_retries=0,
            )
    return exc_info.value


@pytest.mark.asyncio
async def test_selected_engine_native_timeout_is_classified_as_timeout() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    failure = await _run_scrape_and_capture(_browser_state(selection), _EngineATimeout("deadline exceeded"))
    assert _TIMEOUT_REASON_MARKER in (failure.reason or "")
    assert isinstance(failure.__cause__, _EngineATimeout)


@pytest.mark.asyncio
async def test_skyvern_page_analysis_timeout_is_engine_neutral() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    failure = await _run_scrape_and_capture(
        _browser_state(selection), SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page")
    )
    assert _TIMEOUT_REASON_MARKER in (failure.reason or "")
    assert isinstance(failure.__cause__, SkyvernPageAnalysisTimeout)


@pytest.mark.asyncio
async def test_incremental_element_tree_retries_without_wait_after_skyvern_page_analysis_timeout() -> None:
    frame = SimpleNamespace(url="https://example.com")
    get_incremental_element_tree = AsyncMock(
        side_effect=[
            SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page"),
            ([], []),
        ]
    )
    skyvern_frame = SimpleNamespace(
        get_frame=lambda: frame,
        get_incremental_element_tree=get_incremental_element_tree,
    )
    cleanup_element_tree = AsyncMock(return_value=[])

    incremental_page = scraper.IncrementalScrapePage(skyvern_frame=skyvern_frame)  # type: ignore[arg-type]
    result = await incremental_page.get_incremental_element_tree(cleanup_element_tree)

    assert result == []
    assert get_incremental_element_tree.await_args_list == [
        call(wait_until_finished=True),
        call(wait_until_finished=False),
    ]
    cleanup_element_tree.assert_awaited_once_with(frame, frame.url, [])


def _incremental_page(
    selection: BrowserEngineSelection | None, side_effect: list
) -> tuple[scraper.IncrementalScrapePage, AsyncMock]:
    frame = SimpleNamespace(url="https://example.com")
    get_incremental_element_tree = AsyncMock(side_effect=side_effect)
    skyvern_frame = SimpleNamespace(
        get_frame=lambda: frame,
        get_incremental_element_tree=get_incremental_element_tree,
    )
    page = scraper.IncrementalScrapePage(skyvern_frame=skyvern_frame, engine_selection=selection)  # type: ignore[arg-type]
    return page, get_incremental_element_tree


@pytest.mark.asyncio
async def test_incremental_tree_retries_once_on_selected_engine_native_timeout() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    page, get_tree = _incremental_page(selection, [_EngineATimeout("deadline exceeded"), ([], [])])
    result = await page.get_incremental_element_tree(AsyncMock(return_value=[]))
    assert result == []
    assert get_tree.await_args_list == [call(wait_until_finished=True), call(wait_until_finished=False)]


@pytest.mark.asyncio
async def test_incremental_tree_does_not_retry_on_foreign_timeout_under_nonplaywright_selection() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    page, get_tree = _incremental_page(selection, [PlaywrightTimeoutError("pw timeout"), ([], [])])
    with pytest.raises(PlaywrightTimeoutError):
        await page.get_incremental_element_tree(AsyncMock(return_value=[]))
    assert get_tree.await_args_list == [call(wait_until_finished=True)]


@pytest.mark.asyncio
async def test_incremental_tree_retries_on_stock_timeout_when_selection_missing() -> None:
    page, get_tree = _incremental_page(None, [PlaywrightTimeoutError("pw timeout"), ([], [])])
    result = await page.get_incremental_element_tree(AsyncMock(return_value=[]))
    assert result == []
    assert get_tree.await_args_list == [call(wait_until_finished=True), call(wait_until_finished=False)]


@pytest.mark.asyncio
async def test_incremental_tree_retries_on_skyvern_timeout_under_nonplaywright_selection() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    page, get_tree = _incremental_page(
        selection, [SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page"), ([], [])]
    )
    result = await page.get_incremental_element_tree(AsyncMock(return_value=[]))
    assert result == []
    assert get_tree.await_args_list == [call(wait_until_finished=True), call(wait_until_finished=False)]


@pytest.mark.asyncio
async def test_incremental_tree_propagates_non_timeout_without_retry() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    page, get_tree = _incremental_page(selection, [ValueError("boom"), ([], [])])
    with pytest.raises(ValueError):
        await page.get_incremental_element_tree(AsyncMock(return_value=[]))
    assert get_tree.await_args_list == [call(wait_until_finished=True)]


def test_resolve_engine_selection_for_task_reads_live_browser_state() -> None:
    from skyvern.forge import app
    from skyvern.webeye.actions.handler import resolve_engine_selection_for_task

    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    task = SimpleNamespace(task_id="tsk_1", workflow_run_id="wr_1")
    get_for_task = MagicMock(return_value=SimpleNamespace(engine_selection=selection))
    with patch.object(app.BROWSER_MANAGER, "get_for_task", get_for_task):
        assert resolve_engine_selection_for_task(task) is selection  # type: ignore[arg-type]
    get_for_task.assert_called_once_with("tsk_1", workflow_run_id="wr_1")


def test_resolve_engine_selection_for_task_returns_none_when_no_browser_state() -> None:
    from skyvern.forge import app
    from skyvern.webeye.actions.handler import resolve_engine_selection_for_task

    task = SimpleNamespace(task_id="tsk_1", workflow_run_id="wr_1")
    with patch.object(app.BROWSER_MANAGER, "get_for_task", MagicMock(return_value=None)):
        assert resolve_engine_selection_for_task(task) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_foreign_engine_timeout_is_not_classified_as_timeout() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    failure = await _run_scrape_and_capture(_browser_state(selection), PlaywrightTimeoutError("pw timeout"))
    assert _TIMEOUT_REASON_MARKER not in (failure.reason or "")
    assert isinstance(failure.__cause__, PlaywrightTimeoutError)


@pytest.mark.asyncio
async def test_unrelated_exception_is_not_classified_as_timeout_and_is_not_swallowed() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    failure = await _run_scrape_and_capture(_browser_state(selection), ValueError("boom"))
    assert _TIMEOUT_REASON_MARKER not in (failure.reason or "")
    assert isinstance(failure.__cause__, ValueError)


@pytest.mark.asyncio
async def test_playwright_selection_preserves_stock_timeout_classification() -> None:
    selection = _selection("playwright", PlaywrightError, PlaywrightTimeoutError)
    failure = await _run_scrape_and_capture(_browser_state(selection), PlaywrightTimeoutError("pw timeout"))
    assert _TIMEOUT_REASON_MARKER in (failure.reason or "")


@pytest.mark.asyncio
async def test_no_selection_falls_back_to_stock_playwright_timeout_identity() -> None:
    failure = await _run_scrape_and_capture(_browser_state(None), PlaywrightTimeoutError("pw timeout"))
    assert _TIMEOUT_REASON_MARKER in (failure.reason or "")


@pytest.mark.asyncio
async def test_no_selection_non_timeout_is_not_classified_as_timeout() -> None:
    failure = await _run_scrape_and_capture(_browser_state(None), PlaywrightError("navigated away"))
    assert _TIMEOUT_REASON_MARKER not in (failure.reason or "")
